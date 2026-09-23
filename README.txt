BOT DE INVERSION - VERSION COMPLETA

Archivos:
- bot.py
- requirements.txt

VARIABLES DE RENDER:
TELEGRAM_TOKEN = token del bot
ADMIN_TELEGRAM_ID = ID numerico del administrador
USDT_TRC20_ADDRESS = wallet USDT TRC20

Opcionales:
PROFIT_TIME=16:15
PROFIT_TIMEZONE=America/Sao_Paulo
BACKUP_TIME=06:00
BACKUP_TIMEZONE=America/Sao_Paulo
DAILY_RATE=0.005
TARGET_MULTIPLIER=2.0

FUNCIONES INCLUIDAS:
- Panel administrativo privado
- 🔒 Bloquear bot / 🔓 Desbloquear bot
- ♻️ Restaurar respaldo .db
- 💾 Respaldo completo .db + .xlsx
- 💰 Pago diario 0,5%
- Ganancias automáticas lunes-viernes a las 16:15
- Retiro únicamente de ganancias disponibles
- Retiro mínimo 15 USDT y una solicitud cada 7 días
- Revisión de depósitos
- Revisión de retiros
- Aprobación de retiro con mensaje + comprobante/foto
- Rechazo de retiro con motivo y devolución de fondos
- Planes de inversión
- Referidos
- Historial
- SQLite
- Servidor aiohttp para Render

IMPORTANTE:
No reemplazar database.db al desplegar una nueva versión si se quiere conservar la base existente.
Antes de cambios importantes, usar Crear respaldo.
La restauración recibe un archivo .db por Telegram y crea un respaldo de seguridad antes de reemplazar la base.
