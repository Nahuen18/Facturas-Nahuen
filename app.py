"""
Bot de Facturas por WhatsApp - Meta API directo (sin Twilio)
Recibe fotos o PDFs de facturas por WhatsApp, extrae datos con Claude,
y agrega filas en Google Sheets.

Columnas (B-J):
B: Fecha Emision | C: N Factura | D: Proveedor | E: Neto | F: Iva |
G: Impuesto Espec. | H: Total | I: Item (vacio) | J: Detalle

Variables de entorno necesarias:
- ANTHROPIC_API_KEY
- WHATSAPP_VERIFY_TOKEN
- WHATSAPP_ACCESS_TOKEN
- GOOGLE_CREDENTIALS_JSON
- SPREADSHEET_ID
"""

import os
import json
import base64
import math
import requests
from flask import Flask, request, jsonify
import anthropic
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ---------- Configuracion ----------
ANTHROPIC_API_KEY       = os.environ["ANTHROPIC_API_KEY"]
VERIFY_TOKEN            = os.environ["WHATSAPP_VERIFY_TOKEN"]
WHATSAPP_ACCESS_TOKEN   = os.environ["WHATSAPP_ACCESS_TOKEN"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
SPREADSHEET_ID          = os.environ["SPREADSHEET_ID"]

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

Analiza esta factura y devuelve SOLO un JSON valido (sin texto adicional, sin
markdown, sin backticks) con esta estructura exacta:

{
  "fecha_emision": "DD-MM-AAAA",
  "numero_factura": "string",
  "proveedor": "string",
  "neto_factura": numero,
  "total_factura": numero,
  "descuento": numero,
  "productos": [
    {
      "detalle": "nombre del producto o servicio",
      "neto": numero
    }
  ]
}

Reglas CRITICAS:
- La columna "Valor" de cada item SIEMPRE es el valor que aparece junto al producto en el detalle.
- "neto_factura" es el MONTO NETO total impreso en el resumen final de la factura
  (puede llamarse "Neto", "Monto Neto", "Base imponible" o similar).
- "total_factura" es el TOTAL FINAL impreso en la factura (incluyendo IVA e impuestos adicionales).
- "descuento" es el monto total de descuento que aparece en la factura (si no hay, usa 0).
- NO calcules IVA ni impuesto especifico, eso lo hace el sistema.
- Una entrada en "productos" por cada item distinto.
- Numeros sin puntos de miles ni simbolos (ej. 78990).
- Si un dato no aparece, usa null.
"""


def extraer_datos_factura(file_bytes, media_type="image/jpeg"):
    """Extrae datos de una factura, ya sea imagen o PDF."""
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    if media_type == "application/pdf":
        contenido = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            },
            {"type": "text", "text": PROMPT_EXTRACCION},
        ]
    else:
        contenido = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            },
            {"type": "text", "text": PROMPT_EXTRACCION},
        ]

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": contenido}],
    )
    texto = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(texto)


# ---------- Logica de precios ----------
def redondear(valor):
    """Redondea al entero mas cercano: >=0.5 sube, <0.5 baja."""
    return math.floor(valor + 0.5)


def corregir_iva_incluido(productos, neto_factura):
    """
    Si la suma de valores del detalle > neto_factura, los precios incluyen IVA.
    En ese caso divide cada valor por 1.19 para obtener el neto real.
    """
    suma_detalle = sum(p.get("neto") or 0 for p in productos)
    if neto_factura and suma_detalle > neto_factura:
        for p in productos:
            p["neto"] = redondear((p.get("neto") or 0) / 1.19)
    return productos


def prorratear_descuento(productos, descuento):
    """
    Proratea el descuento en partes iguales entre los productos.
    Si un producto queda negativo, se excluye del prorrateo y se redistribuye
    entre los restantes. Si solo queda uno, se aplica al de mayor valor.
    """
    if not descuento or descuento <= 0:
        return productos

    netos = [p.get("neto") or 0 for p in productos]
    indices_activos = list(range(len(netos)))

    while True:
        if not indices_activos:
            break
        parte = descuento / len(indices_activos)
        nuevos_netos = netos[:]
        negativos = []

        for i in indices_activos:
            nuevos_netos[i] = redondear(netos[i] - parte)
            if nuevos_netos[i] < 0:
                negativos.append(i)

        if not negativos:
            # Todos quedaron positivos, aplicar
            netos = nuevos_netos
            break
        elif len(negativos) == len(indices_activos):
            # Todos quedan negativos: aplicar descuento solo al de mayor valor
            i_max = max(indices_activos, key=lambda i: netos[i])
            netos[i_max] = max(redondear(netos[i_max] - descuento), 0)
            break
        else:
            # Excluir negativos y redistribuir
            indices_activos = [i for i in indices_activos if i not in negativos]

    for i, p in enumerate(productos):
        p["neto"] = netos[i]

    return productos


# ---------- Escribir en Google Sheets ----------
def primera_fila_vacia(sheet):
    columna_b = sheet.col_values(2)
    for i in range(2, len(columna_b)):
        if str(columna_b[i]).strip() == "":
            return i + 1
    return max(len(columna_b) + 1, 3)


def factura_duplicada(sheet, numero_factura):
    """Verifica si el numero de factura ya existe en la columna C."""
    columna_c = sheet.col_values(3)
    return str(numero_factura) in [str(v).strip() for v in columna_c]


def aplicar_color(sheet, filas, color, col_inicio=1, col_fin=10):
    """Aplica un color a un rango de columnas de las filas indicadas."""
    requests_body = []
    for fila_num in filas:
        requests_body.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet.id,
                    "startRowIndex": fila_num - 1,
                    "endRowIndex": fila_num,
                    "startColumnIndex": col_inicio,
                    "endColumnIndex": col_fin,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor"
            }
        })
    if requests_body:
        sheet.spreadsheet.batch_update({"requests": requests_body})


def agregar_filas(datos):
    sheet = get_sheet()
    total_factura = round(datos.get("total_factura") or 0)
    neto_factura  = datos.get("neto_factura") or 0
    descuento     = datos.get("descuento") or 0
    productos     = datos.get("productos", [])
    numero_factura = datos.get("numero_factura") or ""
    es_duplicada  = factura_duplicada(sheet, numero_factura)

    # 1. Corregir si los precios del detalle incluyen IVA
    productos = corregir_iva_incluido(productos, neto_factura)

    # 2. Prorratear descuento si corresponde
    productos = prorratear_descuento(productos, descuento)

    tiene_multiples = len(productos) > 1
    filas_escritas = []

    for producto in productos:
        fila_num = primera_fila_vacia(sheet)
        neto = round(producto.get("neto") or 0)
        iva  = round(neto * 0.19)
        detalle = (producto.get("detalle") or "").capitalize()
        impuesto_esp = max(total_factura - neto - iva, 0) if es_combustible(detalle) else 0
        total = neto + iva + impuesto_esp

        valores = [
            datos.get("fecha_emision") or "",
            numero_factura,
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

    # Colores
    if es_duplicada:
        rojo = {"red": 0.918, "green": 0.298, "blue": 0.235}
        aplicar_color(sheet, filas_escritas, rojo, col_inicio=1, col_fin=10)
        print(f"DUPLICADA: Factura {numero_factura} ya existia en la planilla.")
    elif tiene_multiples:
        verde_claro = {"red": 0.714, "green": 0.843, "blue": 0.659}
        aplicar_color(sheet, filas_escritas, verde_claro, col_inicio=1, col_fin=4)

    return len(productos), datos.get("proveedor", "proveedor desconocido").title(), es_duplicada


# ---------- Descargar archivo desde Meta ----------
def descargar_archivo_meta(media_id):
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    url_info = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        headers=headers
    ).json()
    download_url = url_info.get("url")
    mime_type = url_info.get("mime_type", "image/jpeg")
    resp = requests.get(download_url, headers=headers)
    resp.raise_for_status()
    return resp.content, mime_type


# ---------- Webhook ----------
@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Token invalido", 403


@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    data = request.get_json(silent=True) or {}

    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]["value"]
        mensaje = changes["messages"][0]
        tipo    = mensaje.get("type")

        if tipo == "image":
            media_id = mensaje["image"]["id"]
        elif tipo == "document":
            media_id = mensaje["document"]["id"]
        else:
            return jsonify({"status": "ignored"}), 200

        file_bytes, mime_type = descargar_archivo_meta(media_id)
        datos = extraer_datos_factura(file_bytes, mime_type)
        n_productos, proveedor, es_duplicada = agregar_filas(datos)

        if es_duplicada:
            print(f"Factura DUPLICADA de {proveedor} agregada y marcada en rojo.")
        else:
            print(f"Factura de {proveedor} registrada: {n_productos} linea(s).")

    except Exception as e:
        print(f"Error procesando mensaje: {e}")

    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def health():
    return "Bot de facturas activo.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
