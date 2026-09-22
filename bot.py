import os
import asyncio
import sqlite3

from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)


# =========================================================
# CONFIGURACIÓN
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_ID = os.environ.get("ADMIN_ID", "")

DATABASE_FILE = "bot.db"


# =========================================================
# VALIDACIÓN
# =========================================================

if not TELEGRAM_TOKEN:
    raise ValueError(
        "❌ No se encontró la variable TELEGRAM_TOKEN."
    )


# =========================================================
# BASE DE DATOS
# =========================================================

def init_database():
    """Crea la base de datos y las tablas necesarias."""

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

    print("🗄️ Base de datos inicializada correctamente.")


def register_user(user):
    """Registra al usuario si todavía no existe."""

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO users (
            telegram_id,
            username,
            first_name,
            last_name
        )
        VALUES (?, ?, ?, ?)
    """, (
        user.id,
        user.username,
        user.first_name,
        user.last_name
    ))

    conn.commit()
    conn.close()


def get_users_count():
    """Devuelve la cantidad de usuarios registrados."""

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users")
    result = cursor.fetchone()

    conn.close()

    return result[0] if result else 0


# =========================================================
# SERVIDOR WEB
# =========================================================

async def handle_web(request):
    """
    Página principal del servidor.
    Mantiene la misma estructura del bot de rifas.
    """

    return web.Response(
        text="Bot de Trading Activo y en Línea 24/7!"
    )


async def start_web_server():
    """
    Inicia el servidor web usando el puerto PORT,
    igual que el bot de rifas.
    """

    app = web.Application()

    app.router.add_get("/", handle_web)

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(os.environ.get("PORT", 10000))

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"🌐 Servidor web corriendo en el puerto {port}"
    )


# =========================================================
# MENÚ PRINCIPAL
# =========================================================

def main_menu():

    keyboard = [
        [
            InlineKeyboardButton(
                "💰 Mi cuenta",
                callback_data="account"
            ),
            InlineKeyboardButton(
                "📊 Inversiones",
                callback_data="investments"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Depositar",
                callback_data="deposit"
            ),
            InlineKeyboardButton(
                "💸 Retirar",
                callback_data="withdraw"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Referidos",
                callback_data="referrals"
            ),
            InlineKeyboardButton(
                "📜 Historial",
                callback_data="history"
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Información",
                callback_data="info"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# MENÚ ADMINISTRADOR
# =========================================================

def admin_menu():

    keyboard = [
        [
            InlineKeyboardButton(
                "👥 Usuarios",
                callback_data="admin_users"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Depósitos",
                callback_data="admin_deposits"
            ),
            InlineKeyboardButton(
                "💸 Retiros",
                callback_data="admin_withdrawals"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Estadísticas",
                callback_data="admin_stats"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# /START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    # Registrar usuario
    register_user(user)

    nombre = user.first_name or "Usuario"

    texto = (
        f"👋 Hola, *{nombre}*.\n\n"
        "🤖 *Bienvenido a nuestra plataforma.*\n\n"
        "Desde este bot podrás gestionar tu cuenta, "
        "consultar inversiones, depósitos, retiros, "
        "referidos e historial.\n\n"
        "👇 Selecciona una opción:"
    )

    await update.message.reply_text(
        texto,
        reply_markup=main_menu(),
        parse_mode="Markdown"
    )


# =========================================================
# CALLBACKS
# =========================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    # Registrar por seguridad
    register_user(user)

    accion = query.data

    # -----------------------------------------------------
    # MENÚ PRINCIPAL
    # -----------------------------------------------------

    if accion == "account":

        texto = (
            "💰 *MI CUENTA*\n\n"
            "Saldo disponible: `0.00`\n"
            "Capital invertido: `0.00`\n"
            "Ganancias: `0.00`\n\n"
            "Esta sección será conectada con el "
            "sistema financiero en la siguiente etapa."
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------

    if accion == "investments":

        texto = (
            "📊 *INVERSIONES*\n\n"
            "Actualmente no tienes inversiones activas.\n\n"
            "Los planes de inversión serán agregados "
            "en la siguiente etapa del proyecto."
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------

    if accion == "deposit":

        texto = (
            "💳 *DEPÓSITAR*\n\n"
            "El sistema de depósitos todavía no está "
            "habilitado.\n\n"
            "En la siguiente etapa agregaremos el proceso "
            "completo de depósito y verificación."
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------

    if accion == "withdraw":

        texto = (
            "💸 *RETIRAR*\n\n"
            "El sistema de retiros todavía no está "
            "habilitado.\n\n"
            "Esta función será conectada posteriormente."
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------

    if accion == "referrals":

        texto = (
            "👥 *REFERIDOS*\n\n"
            "Tu sistema de referidos todavía no está "
            "configurado.\n\n"
            "Aquí posteriormente aparecerá tu enlace "
            "personal y las estadísticas de referidos."
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------

    if accion == "history":

        texto = (
            "📜 *HISTORIAL*\n\n"
            "Todavía no existen movimientos registrados."
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------

    if accion == "info":

        texto = (
            "ℹ️ *INFORMACIÓN*\n\n"
            "Esta plataforma está siendo desarrollada "
            "por etapas.\n\n"
            "Próximamente estarán disponibles las "
            "funciones de depósitos, inversiones, "
            "retiros, referidos y administración."
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="home"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # -----------------------------------------------------
    # VOLVER AL INICIO
    # -----------------------------------------------------

    if accion == "home":

        nombre = user.first_name or "Usuario"

        texto = (
            f"👋 Hola, *{nombre}*.\n\n"
            "🤖 *Panel principal*\n\n"
            "Selecciona una opción:"
        )

        await query.edit_message_text(
            texto,
            reply_markup=main_menu(),
            parse_mode="Markdown"
        )

        return

    # =====================================================
    # ADMINISTRADOR
    # =====================================================

    if accion.startswith("admin_"):

        if not ADMIN_ID:
            await query.answer(
                "Administrador no configurado.",
                show_alert=True
            )
            return

        if str(user.id) != str(ADMIN_ID):

            await query.answer(
                "⛔ No tienes autorización.",
                show_alert=True
            )

            return

        # -------------------------------------------------
        # USUARIOS
        # -------------------------------------------------

        if accion == "admin_users":

            total = get_users_count()

            texto = (
                "👥 *USUARIOS*\n\n"
                f"Usuarios registrados: *{total}*"
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "⬅️ Panel admin",
                        callback_data="admin_home"
                    )
                ]
            ]

            await query.edit_message_text(
                texto,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

            return

        # -------------------------------------------------
        # DEPÓSITOS
        # -------------------------------------------------

        if accion == "admin_deposits":

            texto = (
                "💰 *DEPÓSITOS*\n\n"
                "No hay un sistema de depósitos "
                "implementado todavía."
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "⬅️ Panel admin",
                        callback_data="admin_home"
                    )
                ]
            ]

            await query.edit_message_text(
                texto,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

            return

        # -------------------------------------------------
        # RETIROS
        # -------------------------------------------------

        if accion == "admin_withdrawals":

            texto = (
                "💸 *RETIROS*\n\n"
                "No hay solicitudes de retiro "
                "registradas todavía."
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "⬅️ Panel admin",
                        callback_data="admin_home"
                    )
                ]
            ]

            await query.edit_message_text(
                texto,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

            return

        # -------------------------------------------------
        # ESTADÍSTICAS
        # -------------------------------------------------

        if accion == "admin_stats":

            total = get_users_count()

            texto = (
                "📊 *ESTADÍSTICAS*\n\n"
                f"👥 Usuarios: *{total}*\n"
                "💰 Depósitos: `0.00`\n"
                "📈 Inversiones: `0.00`\n"
                "💸 Retiros: `0.00`"
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "⬅️ Panel admin",
                        callback_data="admin_home"
                    )
                ]
            ]

            await query.edit_message_text(
                texto,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

            return

        # -------------------------------------------------
        # PANEL ADMIN
        # -------------------------------------------------

        if accion == "admin_home":

            texto = (
                "🔐 *PANEL DE ADMINISTRACIÓN*\n\n"
                "Selecciona una opción:"
            )

            await query.edit_message_text(
                texto,
                reply_markup=admin_menu(),
                parse_mode="Markdown"
            )

            return


# =========================================================
# COMANDO ADMIN
# =========================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    if not ADMIN_ID:

        await update.message.reply_text(
            "⛔ El administrador no está configurado."
        )

        return

    if str(user.id) != str(ADMIN_ID):

        await update.message.reply_text(
            "⛔ No tienes autorización para acceder."
        )

        return

    await update.message.reply_text(
        "🔐 *PANEL DE ADMINISTRACIÓN*\n\n"
        "Selecciona una opción:",
        reply_markup=admin_menu(),
        parse_mode="Markdown"
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    # -----------------------------------------------------
    # 1. BASE DE DATOS
    # -----------------------------------------------------

    init_database()

    # -----------------------------------------------------
    # 2. SERVIDOR WEB
    # -----------------------------------------------------

    await start_web_server()

    # -----------------------------------------------------
    # 3. BOT DE TELEGRAM
    # -----------------------------------------------------

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # -----------------------------------------------------
    # COMANDOS
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    # -----------------------------------------------------
    # BOTONES
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )

    # -----------------------------------------------------
    # INICIAR BOT
    # -----------------------------------------------------

    print(
        "🤖 Bot de Trading iniciado correctamente..."
    )

    await app.initialize()

    await app.start()

    await app.updater.start_polling()

    print(
        "📡 Telegram polling iniciado correctamente..."
    )

    # -----------------------------------------------------
    # MANTENER PROCESO ACTIVO
    # -----------------------------------------------------

    await asyncio.Event().wait()


# =========================================================
# EJECUCIÓN
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())
