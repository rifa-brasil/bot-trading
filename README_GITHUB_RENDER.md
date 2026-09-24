# Bot de Inversión Telegram — versión actualizada

## Archivos que deben estar en GitHub

- `bot.py`
- `requirements.txt`
- carpeta `images/` con las 20 imágenes

No subir `database.db`, la carpeta `backups/`, tokens ni secretos.

## Variables de entorno en Render

- `TELEGRAM_TOKEN` = token del bot
- `ADMIN_TELEGRAM_ID` = ID numérico del administrador
- `USDT_TRC20_ADDRESS` = wallet USDT TRC20
- `PLAN_NAME` = opcional
- `TARGET_MULTIPLIER` = opcional, por defecto `2.0`
- `MAX_INVESTMENT` = opcional
- `BACKUP_TIME` = opcional, por defecto `06:00`
- `BACKUP_TIMEZONE` = opcional, por defecto `America/Sao_Paulo`

## Render

Tipo: **Web Service**.

Build Command:

`pip install -r requirements.txt`

Start Command:

`python bot.py`

## Importante sobre la base de datos

Antes de sustituir el código en producción, crear un respaldo de la base de datos actual.

Esta versión modifica la estructura SQLite automáticamente al arrancar para añadir:

- planes/depositos independientes;
- control de depósitos ya invertidos;
- cuotas diarias;
- estado de mantenimiento;
- relación entre inversión y depósito.

No borrar `database.db` al desplegar.

## Nuevas funciones

### Pago Diario

Es informativo. El administrador pulsa `💰 Pago Diario`, introduce por ejemplo `0,35` o `0,35%`, y el bot calcula cuánto correspondería acreditar sobre el capital activo, respetando el límite de ganancia de cada inversión. No acredita nada.

### Cuotas Diarias

El administrador elige una de estas cuotas:

0,25% · 0,30% · 0,35% · 0,40% · 0,45% · 0,50% · 0,55% · 0,60% · 0,65% · 0,70% · 0,75% · 0,80% · 0,85% · 0,90% · 0,95% · 1,00%

La cuota elegida acredita automáticamente a cada inversión activa `capital × cuota`, con tope en la ganancia restante hasta el 200% del capital inicial. El usuario recibe la imagen correspondiente y el importe acreditado.

Solo se permite una cuota acreditada por fecha local configurada.

### Planes independientes

Seleccionar Plan 300 significa depositar exactamente 300 USDT. No se descuenta saldo, ganancias ni depósitos anteriores para completar el nuevo plan.

Los depósitos aprobados que todavía no fueron invertidos aparecen en `📈 Inversiones` como planes disponibles, con su botón `Invertir Plan X USDT`.

### Mantenimiento

`🔒 Bloquear bot` activa el mantenimiento y envía solamente `bot bloqueado.jpg` a los usuarios.

`🔓 Desbloquear bot` desactiva el mantenimiento y envía solamente `bot operativo.jpg` a los usuarios.

El administrador continúa teniendo acceso al panel.

### Bienvenida

El primer `/start` de un usuario nuevo envía `bienvenida.jpg`.

### Comprobante de retiro

Después de aprobar un retiro, el administrador pulsa `📸 Enviar comprobante` y envía la foto/documento. El comprobante se envía exclusivamente al usuario de ese retiro y después se envía `retiro enviado.jpg`.

### Respaldos

El respaldo manual y el automático envían `.db` y `.xlsx`.

El Excel coloca como primeras columnas:

`Nombre | Nombre de usuario | ID Telegram`
