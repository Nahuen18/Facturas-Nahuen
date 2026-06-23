"""
Bot de Facturas por WhatsApp
Recibe una foto de factura por WhatsApp (via Twilio), extrae los datos con
Claude, y agrega una fila por producto en la primera hoja de un Google Sheet.

Columnas que se llenan automaticamente (B-J):
B: Fecha Emision | C: N Factura | D: Proveedor | E: Neto | F: Iva |
G: Impuesto Espec. (vacio) | H: Total | I: Item (vacio) | J: Detalle

Logica:
- La columna "Valor" de cada item en la factura chilena SIEMPRE es el monto NETO.
- IVA de cada item = neto x 19%
- Total de cada item = neto + IVA
- Se agrega una fila por cada item.
- Datos empiezan en fila 4.
"""

import os
import json
import base64
import requests
from flask import Flask, request
import anthropic
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ---------- Configuracion ----------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------- Conexion a Google Sheets ----------
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.get_worksheet(0)


# ---------- Extraccion de datos con Claude ----------
PROMPT_EXTRACCION = """Eres un asistente que extrae datos de facturas de compras agricolas chilenas.

Analiza la imagen de esta factura y devuelve SOLO un JSON valido (sin texto adicional, sin
markdown, sin backticks) con esta estructura exacta:

{
  "fecha_emision": "DD-MM-AAAA",
  "numero_factura": "string",
  "proveedor": "string",
  "productos": [
    {
      "detalle": "nombre del producto o servicio",
      "neto": numero
    }
  ]
}

Reglas CRITICAS:
- La columna "Valor" que aparece en el detalle de cada item de una factura chilena
  SIEMPRE corresponde al valor NETO (sin IVA). Extrae ese valor en el campo "neto".
- NO calcules ni extraigas el IVA ni el total por item — eso lo calcula el sistema.
- Si la factura tiene varios items, crea una entrada en "productos" por cada uno.
- Si la factura tiene un solo concepto general, crea una sola entrada.
- Los numeros van sin puntos de miles ni simbolos (ej. 78990, no "78.990" ni "$78.990").
- Si algun dato no aparece en la imagen, usa null.
"""


def extraer_datos_factura(image_bytes, media_type):
    img_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": img_b64},
                    },
                    {"type": "text", "text": PROMPT_EXTRACCION},
                ],
            }
        ],
    )
    texto = response.content[0].text.strip()
    texto = texto.replace("```json", "").replace("```", "").strip()
    return json.loads(texto)


# ---------- Escribir en Google Sheets ----------
def primera_fila_vacia(sheet):
    """Busca la primera fila vacia en columna B a partir de fila 4."""
    columna_b = sheet.col_values(2)
    for i in range(3, len(columna_b)):
        if str(columna_b[i]).strip() == "":
            return i + 1
    return max(len(columna_b) + 1, 4)


def agregar_filas(datos):
    sheet = get_sheet()
    for producto in datos.get("productos", []):
        fila_num = primera_fila_vacia(sheet)
        neto = round(producto.get("neto") or 0)
        iva = round(neto * 0.19)
        total = neto + iva
        valores = [
            datos.get("fecha_emision") or "",
            datos.get("numero_factura") or "",
            datos.get("proveedor") or "",
            neto,
            iva,
            "",      # Impuesto Espec. - siempre vacio
            total,
            "",      # Item - lo llena el usuario manualmente
            producto.get("detalle") or "",
        ]
        sheet.update(f"B{fila_num}:J{fila_num}", [valores], value_input_option="USER_ENTERED")


# ---------- Webhook de Twilio ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    num_media = int(request.form.get("NumMedia", 0))

    if num_media == 0:
        return responder_twiml("Enviame una foto de la factura para procesarla.")

    try:
        media_url = request.form.get("MediaUrl0")
        media_type = request.form.get("MediaContentType0", "image/jpeg")

        img_resp = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        img_resp.raise_for_status()

        datos = extraer_datos_factura(img_resp.content, media_type)
        agregar_filas(datos)

        n_productos = len(datos.get("productos", []))
        proveedor = datos.get("proveedor", "proveedor desconocido")
        mensaje = f"Factura de {proveedor} registrada: {n_productos} linea(s) agregada(s) a la planilla."
        return responder_twiml(mensaje)

    except Exception as e:
        return responder_twiml(f"No pude procesar la factura: {str(e)}")


def responder_twiml(mensaje):
    from xml.sax.saxutils import escape
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{escape(mensaje)}</Message></Response>"""
    return twiml, 200, {"Content-Type": "text/xml"}


@app.route("/", methods=["GET"])
def health():
    return "Bot de facturas activo."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
