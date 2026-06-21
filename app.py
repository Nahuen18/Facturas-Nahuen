"""
Bot de Facturas por WhatsApp
Recibe una foto de factura por WhatsApp (vía Twilio), extrae los datos con
Claude, y agrega una fila por producto en la primera hoja de un Google Sheet.

Columnas que se llenan automáticamente (en este orden):
Fecha Emision | N° Factura | Proveedor | Neto | Iva | Impuesto Espec. | Total | Ítem | Detalle

- "Impuesto Espec." se deja siempre vacío (lo llena el usuario manualmente si aplica).
- "Ítem" se deja siempre vacío (el usuario lo clasifica manualmente).
- Si la factura tiene varios productos, se agrega una fila por producto.
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

# ---------- Configuración (se lee desde variables de entorno en Railway) ----------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]  # contenido completo del .json, como texto
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------- Conexión a Google Sheets ----------
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
    return sh.get_worksheet(0)  # siempre la primera hoja (la "activa" del mes)


# ---------- Extracción de datos con Claude ----------
PROMPT_EXTRACCION = """Eres un asistente que extrae datos de facturas de compras agrícolas chilenas.

Analiza la imagen de esta factura y devuelve SOLO un JSON válido (sin texto adicional, sin
markdown, sin backticks) con esta estructura exacta:

{
  "fecha_emision": "DD-MM-AAAA",
  "numero_factura": "string",
  "proveedor": "string",
  "productos": [
    {
      "detalle": "nombre del producto o servicio",
      "neto": numero,
      "iva": numero,
      "total": numero
    }
  ]
}

Reglas:
- Si la factura tiene varios productos/ítems distintos, crea una entrada en "productos" por cada uno,
  con su neto, iva y total individual (neto + iva = total de esa línea).
- Si la factura solo tiene un total general sin desglose por producto, crea un solo producto con
  detalle = descripción general de la factura, y neto/iva/total correspondientes al total de la factura.
- Los números van sin puntos de miles ni símbolos, solo el valor (ej. 150000, no "150.000" ni "$150.000").
- Si algún dato no aparece en la imagen, usa null.
- No incluyas la columna de Impuesto Específico, no la necesito.
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
    # por si acaso Claude agrega ```json ... ```
    texto = texto.replace("```json", "").replace("```", "").strip()
    return json.loads(texto)


# ---------- Escribir en Google Sheets ----------
def agregar_filas(datos):
    sheet = get_sheet()
    for producto in datos.get("productos", []):
        fila = [
            datos.get("fecha_emision") or "",
            datos.get("numero_factura") or "",
            datos.get("proveedor") or "",
            producto.get("neto") or 0,
            producto.get("iva") or 0,
            "",  # Impuesto Espec. - siempre vacío
            producto.get("total") or 0,
            "",  # Ítem - lo llena el usuario manualmente
            producto.get("detalle") or "",
        ]
        sheet.append_row(fila, value_input_option="USER_ENTERED")


# ---------- Webhook de Twilio ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    num_media = int(request.form.get("NumMedia", 0))
    sender = request.form.get("From", "")

    if num_media == 0:
        return responder_twiml("Envíame una foto de la factura para procesarla.")

    try:
        media_url = request.form.get("MediaUrl0")
        media_type = request.form.get("MediaContentType0", "image/jpeg")

        img_resp = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        img_resp.raise_for_status()

        datos = extraer_datos_factura(img_resp.content, media_type)
        agregar_filas(datos)

        n_productos = len(datos.get("productos", []))
        proveedor = datos.get("proveedor", "proveedor desconocido")
        mensaje = f"✅ Factura de {proveedor} registrada: {n_productos} línea(s) agregada(s) a la planilla."
        return responder_twiml(mensaje)

    except Exception as e:
        return responder_twiml(f"⚠️ No pude procesar la factura: {str(e)}")


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
