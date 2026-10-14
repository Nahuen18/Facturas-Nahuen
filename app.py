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
import time
import base64
import math
import queue
import threading
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

# timeout y reintentos del cliente de Claude: si una llamada se cuelga, corta
# a los 60s en vez de quedarse esperando para siempre y bloquear el worker.
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0, max_retries=2)

# Timeouts de red (segundos) para las llamadas a Meta. (conexion, lectura)
HTTP_TIMEOUT = (15, 60)
# Reintentos del worker ante cortes transitorios de red
MAX_REINTENTOS = 3

# ---------- Idempotencia, cola y control de fila ----------
mensajes_procesados = set()   # guarda los wamid ya procesados
MAX_IDS_MEMORIA = 1000        # tope para que el set no crezca sin limite
cola_facturas = queue.Queue() # facturas pendientes de procesar en segundo plano
proxima_fila = None           # siguiente fila a escribir; se calcula una sola vez

# ---------- Palabras clave de combustible ----------
PALABRAS_COMBUSTIBLE = [
    "bencina", "gasolina", "diesel", "diésel", "combustible",
    "gas oil", "kerosene", "petróleo", "petroleo", "gnc",
]

def es_combustible(detalle):
    detalle_lower = (detalle or "").lower()
    return any(palabra in detalle_lower for palabra in PALABRAS_COMBUSTIBLE)


# ---------- Proveedores que NO se desglosan por item ----------
# Para estos se registra UNA sola fila usando los valores IMPRESOS de la
# factura (sin calcular ni aplicar descuentos):
#   - neto  = MONTO NETO + MONTO EXENTO
#   - iva   = I.V.A. impreso (textual)
#   - total = TOTAL impreso (textual)
# En la columna Detalle se pone el item de mayor valor neto.
# La comparacion normaliza el nombre (minusculas y sin espacios), asi que
# "Luzparral S.A.", "LUZ PARRAL", etc. calzan igual. Agrega mas claves si
# quieres el mismo trato para otro proveedor.
PROVEEDORES_SIN_DESGLOSE = ["luzparral", "chilexpress", "entel"]

def sin_desglose(proveedor):
    prov = "".join((proveedor or "").lower().split())
    return any(clave in prov for clave in PROVEEDORES_SIN_DESGLOSE)


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
  "monto_exento": numero,
  "iva_factura": numero,
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
- "monto_exento" es el MONTO EXENTO impreso en el resumen de la factura (si no aparece, usa 0).
- "iva_factura" es el valor del I.V.A. impreso en el resumen de la factura (copialo tal cual, no lo calcules).
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
    Detecta cuando los valores del detalle vienen con IVA incluido y los corrige
    dividiendo por 1.19.

    Un caso real de IVA incluido deja la suma del detalle ~19% por encima del
    MONTO NETO impreso. Por eso se exige un MARGEN (5%) antes de corregir: asi
    una diferencia de pocos pesos por redondeo de la propia factura (p.ej. items
    que suman 55.799 contra un neto impreso de 55.798) NO gatilla la correccion
    y los netos se respetan tal cual.
    """
    suma_detalle = sum(p.get("neto") or 0 for p in productos)
    if neto_factura and suma_detalle > neto_factura * 1.05:
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
    """
    Lectura 'fria' que se usa UNA sola vez (al arrancar o tras reinicio) para
    saber en que fila retomar. Despues el worker lleva la cuenta en memoria.
    """
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
    global proxima_fila
    sheet = get_sheet()
    total_factura = round(datos.get("total_factura") or 0)
    neto_factura  = datos.get("neto_factura") or 0
    descuento     = datos.get("descuento") or 0
    productos     = datos.get("productos", []) or []
    numero_factura = datos.get("numero_factura") or ""
    proveedor_nombre = datos.get("proveedor", "") or ""
    es_duplicada  = factura_duplicada(sheet, numero_factura)

    # ----- Fila inicial en memoria (una sola vez) -----
    if proxima_fila is None:
        proxima_fila = primera_fila_vacia(sheet)

    # ===== Caso especial: proveedores sin desglose (ej. Luz Parral) =====
    # Se usa UNA sola fila con los valores IMPRESOS de la factura:
    #   neto  = MONTO NETO + MONTO EXENTO
    #   iva   = I.V.A. impreso (textual)
    #   total = TOTAL impreso (textual)
    # No se calcula IVA ni se aplica descuento (ya viene reflejado en el neto).
    if sin_desglose(proveedor_nombre):
        monto_exento = datos.get("monto_exento") or 0
        neto  = round((neto_factura or 0) + (monto_exento or 0))
        iva   = round(datos.get("iva_factura") or 0)
        total = round(total_factura or 0)
        detalle = ""
        if productos:
            detalle_top = max(productos, key=lambda p: p.get("neto") or 0)
            detalle = (detalle_top.get("detalle") or "").capitalize()

        matriz = [[
            datos.get("fecha_emision") or "",
            numero_factura,
            proveedor_nombre.title(),
            neto,
            iva,
            "",        # impuesto especifico: no aplica
            total,
            "",        # item (vacio)
            detalle,
        ]]
        tiene_multiples = False

    # ===== Logica normal: una fila por producto =====
    else:
        productos = corregir_iva_incluido(productos, neto_factura)
        productos = prorratear_descuento(productos, descuento)

        if not productos:
            return 0, (proveedor_nombre or "proveedor desconocido").title(), es_duplicada

        tiene_multiples = len(productos) > 1
        matriz = []
        for producto in productos:
            neto = round(producto.get("neto") or 0)
            iva  = round(neto * 0.19)
            detalle = (producto.get("detalle") or "").capitalize()
            impuesto_esp = max(total_factura - neto - iva, 0) if es_combustible(detalle) else 0
            total = neto + iva + impuesto_esp
            matriz.append([
                datos.get("fecha_emision") or "",
                numero_factura,
                proveedor_nombre.title(),
                neto,
                iva,
                impuesto_esp if impuesto_esp > 0 else "",
                total,
                "",
                detalle,
            ])

    # ----- Escribir todas las filas de la factura en UNA sola llamada -----
    fila_inicio = proxima_fila
    fila_fin    = proxima_fila + len(matriz) - 1
    sheet.update(f"B{fila_inicio}:J{fila_fin}", matriz, value_input_option="USER_ENTERED")
    filas_escritas = list(range(fila_inicio, fila_fin + 1))
    proxima_fila = fila_fin + 1   # avanzar en memoria, sin releer la planilla

    # ----- Colores -----
    if es_duplicada:
        rojo = {"red": 0.918, "green": 0.298, "blue": 0.235}
        aplicar_color(sheet, filas_escritas, rojo, col_inicio=1, col_fin=10)
        print(f"DUPLICADA: Factura {numero_factura} ya existia en la planilla.")
    elif tiene_multiples:
        verde_claro = {"red": 0.714, "green": 0.843, "blue": 0.659}
        aplicar_color(sheet, filas_escritas, verde_claro, col_inicio=1, col_fin=4)

    return len(matriz), (proveedor_nombre or "proveedor desconocido").title(), es_duplicada


# ---------- Descargar archivo desde Meta ----------
def descargar_archivo_meta(media_id):
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    # 1) Pedir la URL del media (con timeout para que no se cuelgue)
    r_info = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    r_info.raise_for_status()
    url_info = r_info.json()
    download_url = url_info.get("url")
    mime_type = url_info.get("mime_type", "image/jpeg")
    # 2) Descargar el archivo (con timeout)
    resp = requests.get(download_url, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content, mime_type


# ---------- Worker de fondo: procesa la cola una factura a la vez ----------
def worker_facturas():
    """
    Corre en un hilo aparte. Toma facturas de la cola y las procesa de a una,
    en orden. Al ser secuencial y unico, evita colisiones al escribir en la
    planilla y no satura la API de Google Sheets.
    """
    while True:
        media_id, message_id = cola_facturas.get()
        try:
            # Reintentar ante cortes de red transitorios (connection reset,
            # timeouts). Como la escritura a la planilla es la ultima llamada,
            # un fallo antes de ella no deja nada a medias y es seguro reintentar.
            ultimo_error = None
            for intento in range(1, MAX_REINTENTOS + 1):
                try:
                    file_bytes, mime_type = descargar_archivo_meta(media_id)
                    datos = extraer_datos_factura(file_bytes, mime_type)
                    n_productos, proveedor, es_duplicada = agregar_filas(datos)
                    if es_duplicada:
                        print(f"Factura DUPLICADA de {proveedor} agregada y marcada en rojo.")
                    else:
                        print(f"Factura de {proveedor} registrada: {n_productos} linea(s).")
                    ultimo_error = None
                    break
                except (requests.exceptions.RequestException, ConnectionError) as e:
                    ultimo_error = e
                    print(f"Intento {intento}/{MAX_REINTENTOS} fallo (red) en {message_id}: {e}")
                    time.sleep(2 * intento)   # espera creciente antes de reintentar

            if ultimo_error is not None:
                # Agotados los reintentos: liberar el id para que Meta pueda reenviar
                if message_id:
                    mensajes_procesados.discard(message_id)
                print(f"Factura {message_id} NO procesada tras {MAX_REINTENTOS} intentos: {ultimo_error}")

        except Exception as e:
            # Cualquier otro error (ej. JSON invalido de Claude): liberar el id
            if message_id:
                mensajes_procesados.discard(message_id)
            print(f"Error procesando factura {message_id}: {e}")
        finally:
            cola_facturas.task_done()


# Iniciar el worker de fondo al arrancar la app (funciona bajo gunicorn tambien)
threading.Thread(target=worker_facturas, daemon=True).start()


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
    """
    Responde 200 de inmediato y encola cada factura para procesarla en segundo
    plano. Asi Meta nunca hace timeout ni reintenta, y no se pierde ninguna.
    """
    data = request.get_json(silent=True) or {}

    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]["value"]

        # Iterar TODOS los mensajes del payload (Meta puede enviar varios juntos)
        for mensaje in changes.get("messages", []):
            message_id = mensaje.get("id")   # wamid unico de cada mensaje
            tipo       = mensaje.get("type")

            # Idempotencia: ignorar reintentos del mismo mensaje
            if message_id in mensajes_procesados:
                print(f"Mensaje {message_id} ya encolado; se ignora el reintento.")
                continue

            if tipo == "image":
                media_id = mensaje["image"]["id"]
            elif tipo == "document":
                media_id = mensaje["document"]["id"]
            else:
                continue

            mensajes_procesados.add(message_id)
            if len(mensajes_procesados) > MAX_IDS_MEMORIA:
                mensajes_procesados.pop()

            cola_facturas.put((media_id, message_id))
            print(f"Factura encolada ({message_id}). Pendientes: {cola_facturas.qsize()}")

    except Exception as e:
        print(f"Error encolando mensaje: {e}")

    # Responder de inmediato, sin esperar el procesamiento
    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def health():
    return "Bot de facturas activo.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
