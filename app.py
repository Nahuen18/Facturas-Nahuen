"""
Bot de Facturas por WhatsApp - Meta API directo (sin Twilio)
Recibe fotos de facturas por WhatsApp, extrae datos con Claude,
y agrega filas en Google Sheets.

Columnas (B-J):
B: Fecha Emision | C: N Factura | D: Proveedor | E: Neto | F: Iva |
G: Impuesto Espec. | H: Total | I: Item (vacio) | J: Detalle

Variables de entorno necesarias:
- ANTHROPIC_API_KEY
- WHATSAPP_VERIFY_TOKEN   <- nuevo (antes era TWILIO_*)
- WHATSAPP_ACCESS_TOKEN  <- nuevo (token de acceso de Meta)
- GOOGLE_CREDENTIALS_JSON
- SPREADSHEET_ID
"""

import os
import json
import base64
import requests
from flask import Flask, request, jsonify
import anthropic
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ---------- Configuracion ----------
ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]
VERIFY_TOKEN          = os.environ["WHATSAPP_VERIFY_TOKEN"]   # nahuen2026
WHATSAPP_ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]   # token de Meta
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
SPREADSHEET_ID        = os.environ["SPREADSHEET_ID"]

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------- Palabras clave de combustible ----------
PALABRAS_COMBUSTIBLE = [
    "bencina", "gasolina", "diesel", "diésel", "combustible",
    "gas oil", "kerosene", "petróleo", "petroleo", "gnc",
]

def es_combustible(detalle):
    detalle_lower = (detalle or "").lower()
    return any(palabra in detalle_lower for palabra in PALABRAS_COMBUSTIBLE)


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
    return gc.open_by_key(SPREADSHEET_ID).get_worksheet(0)


# ---------- Extraccion de datos con Claude ----------
PROMPT_EXTRACCION = """Eres un asistente que extrae datos de facturas de compras agricolas chilenas.

Analiza la imagen de esta factura y devuelve SOLO un JSON valido (sin texto adicional, sin
markdown, sin backticks) con esta estructura exacta:

{
  "fecha_emision": "DD-MM-AAAA",
  "numero_factura": "string",
  "proveedor": "string",
  "total_factura": numero,
  "productos": [
    {
      "detalle": "nombre del producto o servicio",
      "neto": numero
    }
  ]
}

Reglas CRITICAS:
- La columna "Valor" de cada item SIEMPRE es el valor NETO (sin IVA).
- "total_factura" es el TOTAL FINAL impreso en la factura.
- NO calcules IVA ni impuesto especifico, eso lo hace el sistema.
- Una entrada en "productos" por cada item distinto.
- Numeros sin puntos de miles ni simbolos (ej. 78990).
- Si un dato no aparece, usa null.
"""


def extraer_datos_factura(image_bytes, media_type="image/jpeg"):
    img_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": img_b64},
                },
                {"type": "text", "text": PROMPT_EXTRACCION},
            ],
        }],
    )
    texto = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(texto)


# ---------- Escribir en Google Sheets ----------
def primera_fila_vacia(sheet):
    columna_b = sheet.col_values(2)
    for i in range(2, len(columna_b)):
        if str(columna_b[i]).strip() == "":
            return i + 1
    return max(len(columna_b) + 1, 3)


def aplicar_color_verde(sheet, filas):
    verde_claro = {"red": 0.714, "green": 0.843, "blue": 0.659}
    requests_body = []
    for fila_num in filas:
        requests_body.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet.id,
                    "startRowIndex": fila_num - 1,
                    "endRowIndex": fila_num,
                    "startColumnIndex": 1,
                    "endColumnIndex": 4,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": verde_claro}},
                "fields": "userEnteredFormat.backgroundColor"
            }
        })
    if requests_body:
        sheet.spreadsheet.batch_update({"requests": requests_body})


def agregar_filas(datos):
    sheet = get_sheet()
    total_factura = round(datos.get("total_factura") or 0)
    productos = datos.get("productos", [])
    tiene_multiples = len(productos) > 1
    filas_escritas = []

    for producto in productos:
        fila_num = primera_fila_vacia(sheet)
        neto = round(producto.get("neto") or 0)
        iva = round(neto * 0.19)
        detalle = producto.get("detalle") or ""
        impuesto_esp = max(total_factura - neto - iva, 0) if es_combustible(detalle) else 0
        total = neto + iva + impuesto_esp

        valores = [
            datos.get("fecha_emision") or "",
            datos.get("numero_factura") or "",
            datos.get("proveedor", "").title(),
            neto,
            iva,
            impuesto_esp if impuesto_esp > 0 else "",
            total,
            "",
            detalle,
        ]
        sheet.update(f"B{fila_num}:J{fila_num}", [valores], value_input_option="USER_ENTERED")
        filas_escritas.append(fila_num)

    if tiene_multiples:
        aplicar_color_verde(sheet, filas_escritas)

    return len(productos), datos.get("proveedor", "proveedor desconocido").title()


# ---------- Descargar imagen desde Meta ----------
def descargar_imagen_meta(media_id):
    """Descarga una imagen desde los servidores de Meta usando el media_id."""
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}

    # 1. Obtener la URL de descarga
    url_info = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        headers=headers
    ).json()
    download_url = url_info.get("url")
    mime_type = url_info.get("mime_type", "image/jpeg")

    # 2. Descargar la imagen
    img_resp = requests.get(download_url, headers=headers)
    img_resp.raise_for_status()
    return img_resp.content, mime_type


# ---------- Webhook ----------
@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    """Meta llama a este endpoint con GET para verificar el webhook."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Token invalido", 403


@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    """Meta envía los mensajes entrantes aquí."""
    data = request.get_json(silent=True) or {}

    try:
        entry    = data["entry"][0]
        changes  = entry["changes"][0]["value"]
        mensaje  = changes["messages"][0]
        tipo     = mensaje.get("type")

        if tipo != "image":
            return jsonify({"status": "ignored"}), 200

        media_id = mensaje["image"]["id"]
        image_bytes, mime_type = descargar_imagen_meta(media_id)
        datos = extraer_datos_factura(image_bytes, mime_type)
        n_productos, proveedor = agregar_filas(datos)

        print(f"Factura de {proveedor} registrada: {n_productos} linea(s).")

    except Exception as e:
        print(f"Error procesando mensaje: {e}")

    # Meta requiere siempre respuesta 200
    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def health():
    return "Bot de facturas activo.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
