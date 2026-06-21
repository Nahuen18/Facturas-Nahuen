# Bot de Facturas WhatsApp → Google Sheets

## Qué hace
Recibe una foto de factura por WhatsApp, extrae los datos con Claude
(proveedor, fecha, N° factura, y cada producto con su neto/iva/total),
y agrega una fila por producto en la primera hoja de tu Google Sheet.

Columnas que llena: Fecha Emision | N° Factura | Proveedor | Neto | Iva |
Impuesto Espec. (vacío) | Total | Ítem (vacío, lo llenas tú) | Detalle

## Variables de entorno necesarias (configurar en Railway)

| Variable | Qué va ahí |
|---|---|
| ANTHROPIC_API_KEY | Tu API key de https://console.anthropic.com |
| TWILIO_ACCOUNT_SID | Desde el dashboard de Twilio |
| TWILIO_AUTH_TOKEN | Desde el dashboard de Twilio |
| GOOGLE_CREDENTIALS_JSON | El CONTENIDO completo del archivo .json de la cuenta de servicio, pegado como texto plano |
| SPREADSHEET_ID | El código largo en la URL de tu Google Sheet |

## Pasos para desplegar en Railway

1. Crea cuenta en https://railway.app (puedes entrar con GitHub).
2. Click "New Project" → "Deploy from GitHub repo" (sube esta carpeta a un repo de GitHub primero)
   o usa "Empty Project" y luego la opción de subir archivos / Railway CLI.
3. En el proyecto, ve a la pestaña "Variables" y agrega las 5 variables de la tabla de arriba.
   - Para GOOGLE_CREDENTIALS_JSON: abre el archivo .json descargado con un editor de texto,
     copia TODO el contenido (desde { hasta }) y pégalo como valor de la variable.
4. Railway detectará el Procfile y desplegará automáticamente.
5. Una vez desplegado, Railway te da una URL pública, ej: https://bot-facturas-production.up.railway.app
6. Esa URL + "/webhook" es lo que vas a pegar en Twilio:
   https://bot-facturas-production.up.railway.app/webhook

## Configurar el webhook en Twilio

1. Ve a la consola de Twilio → Messaging → Try it out → Send a WhatsApp message
2. Busca el campo "WHEN A MESSAGE COMES IN" (Sandbox settings)
3. Pega tu URL + /webhook, método POST
4. Guarda

## Probar

Envía una foto de una factura al número de WhatsApp Sandbox de Twilio.
Deberías recibir una respuesta confirmando cuántas líneas se agregaron,
y ver las filas nuevas en tu Google Sheet.

## Notas importantes

- El sistema SIEMPRE escribe en la PRIMERA hoja del archivo. Cuando cambies de mes,
  mueve la hoja nueva (ej. "Ago") para que quede primera (arrastrarla al inicio de las pestañas).
- La columna "Ítem" y "Impuesto Espec." se dejan siempre vacías para que tú las completes.
- Si Claude no logra leer bien algún dato, ese campo queda vacío — revisa la fila igual.
