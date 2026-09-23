import os
import asyncio
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)


# =========================================================
# SERVIDOR WEB
# MISMA ESTRUCTURA DEL BOT DE RIFA
# =========================================================

async def handle_web(request):
    return web.Response(text="Bot de Inversión Activo y en Línea 24/7!")


async def start_web_server():
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

    print(f"🌐 Servidor web corriendo en el puerto {port}")


# =========================================================
# CONFIGURACIÓN
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

ADMIN_TELEGRAM_ID = int(
    os.environ.get("ADMIN_TELEGRAM_ID", "0")
)


# =========================================================
# COMPROBACIÓN DEL TOKEN
# =========================================================

if not TELEGRAM_TOKEN:
    raise ValueError(
        "❌ No existe la variable TELEGRAM_TOKEN"
    )


# =========================================================
# MENÚ PRINCIPAL
# =========================================================

def menu_principal():

    keyboard = [

        [
            InlineKeyboardButton(
                "👤 Mi cuenta",
                callback_data="cuenta"
            ),
            InlineKeyboardButton(
                "📈 Inversiones",
                callback_data="inversiones"
            )
        ],

        [
            InlineKeyboardButton(
                "💰 Depositar",
                callback_data="depositar"
            ),
            InlineKeyboardButton(
                "💸 Retirar",
                callback_data="retirar"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 Referidos",
                callback_data="referidos"
            ),
            InlineKeyboardButton(
                "📜 Historial",
                callback_data="historial"
            )
        ],

        [
            InlineKeyboardButton(
                "ℹ️ Información",
                callback_data="informacion"
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

    nombre = user.first_name or "Usuario"

    texto = (
        "💰 *PLATAFORMA DE INVERSIÓN*\n"
        "\n"
        f"👋 Bienvenido, *{nombre}*\n"
        "\n"
        "💵 *Saldo:* $0.00\n"
        "📈 *Invertido:* $0.00\n"
        "💎 *Ganancias:* $0.00\n"
        "\n"
        "Selecciona una opción:"
    )

    await update.message.reply_text(
        texto,
        parse_mode="Markdown",
        reply_markup=menu_principal()
    )


# =========================================================
# BOTONES
# =========================================================

async def boton_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    accion = query.data

    # -----------------------------------------------------
    # MI CUENTA
    # -----------------------------------------------------

    if accion == "cuenta":

        user = query.from_user

        texto = (
            "👤 *MI CUENTA*\n"
            "\n"
            f"👤 Nombre: {user.first_name or 'Sin nombre'}\n"
            f"🆔 ID: `{user.id}`\n"
            f"🔗 Usuario: @{user.username or 'Sin usuario'}\n"
            "\n"
            "💵 Saldo: $0.00\n"
            "📈 Invertido: $0.00\n"
            "💎 Ganancias: $0.00"
        )

        teclado = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="inicio"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado)
        )


    # -----------------------------------------------------
    # INVERSIONES
    # -----------------------------------------------------

    elif accion == "inversiones":

        texto = (
            "📈 *INVERSIONES*\n"
            "\n"
            "Actualmente no tienes inversiones activas.\n"
            "\n"
            "Los planes de inversión se configurarán "
            "en la siguiente etapa."
        )

        teclado = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="inicio"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado)
        )


    # -----------------------------------------------------
    # DEPOSITAR
    # -----------------------------------------------------

    elif accion == "depositar":

        texto = (
            "💰 *DEPOSITAR*\n"
            "\n"
            "La función de depósitos será configurada "
            "en la siguiente etapa.\n"
            "\n"
            "Aquí posteriormente mostraremos:\n"
            "• Dirección de wallet\n"
            "• Red disponible\n"
            "• Cantidad a depositar\n"
            "• Hash de transacción\n"
            "• Confirmación del administrador"
        )

        teclado = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="inicio"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado)
        )


    # -----------------------------------------------------
    # RETIRAR
    # -----------------------------------------------------

    elif accion == "retirar":

        texto = (
            "💸 *RETIRAR*\n"
            "\n"
            "La función de retiros será configurada "
            "en la siguiente etapa.\n"
            "\n"
            "Aquí posteriormente el usuario podrá "
            "solicitar un retiro."
        )

        teclado = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="inicio"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado)
        )


    # -----------------------------------------------------
    # REFERIDOS
    # -----------------------------------------------------

    elif accion == "referidos":

        texto = (
            "👥 *REFERIDOS*\n"
            "\n"
            "Sistema de referidos próximamente.\n"
            "\n"
            "Aquí posteriormente aparecerán:\n"
            "• Tu enlace de referido\n"
            "• Cantidad de referidos\n"
            "• Bonificaciones\n"
            "• Historial"
        )

        teclado = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="inicio"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado)
        )


    # -----------------------------------------------------
    # HISTORIAL
    # -----------------------------------------------------

    elif accion == "historial":

        texto = (
            "📜 *HISTORIAL*\n"
            "\n"
            "Todavía no existen movimientos."
        )

        teclado = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="inicio"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado)
        )


    # -----------------------------------------------------
    # INFORMACIÓN
    # -----------------------------------------------------

    elif accion == "informacion":

        texto = (
            "ℹ️ *INFORMACIÓN*\n"
            "\n"
            "Bienvenido a nuestra plataforma.\n"
            "\n"
            "Desde este bot podrás gestionar tu "
            "cuenta, depósitos, inversiones, retiros "
            "y referidos.\n"
            "\n"
            "⚠️ Esta versión es únicamente una prueba "
            "del funcionamiento del bot."
        )

        teclado = [
            [
                InlineKeyboardButton(
                    "⬅️ Volver",
                    callback_data="inicio"
                )
            ]
        ]

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado)
        )


    # -----------------------------------------------------
    # VOLVER AL INICIO
    # -----------------------------------------------------

    elif accion == "inicio":

        user = query.from_user

        nombre = user.first_name or "Usuario"

        texto = (
            "💰 *PLATAFORMA DE INVERSIÓN*\n"
            "\n"
            f"👋 Bienvenido, *{nombre}*\n"
            "\n"
            "💵 *Saldo:* $0.00\n"
            "📈 *Invertido:* $0.00\n"
            "💎 *Ganancias:* $0.00\n"
            "\n"
            "Selecciona una opción:"
        )

        await query.edit_message_text(
            texto,
            parse_mode="Markdown",
            reply_markup=menu_principal()
        )


# =========================================================
# MAIN
# MISMO SISTEMA DEL BOT DE RIFA
# =========================================================

async def main():

    # 1. Levanta el servidor web
    await start_web_server()

    # 2. Crea el bot
    app = ApplicationBuilder().token(
        TELEGRAM_TOKEN
    ).build()

    # 3. Comando /start
    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    # 4. Botones
    app.add_handler(
        CallbackQueryHandler(
            boton_callback
        )
    )

    print(
        "🤖 Bot de Inversión iniciado correctamente..."
    )

    # 5. Inicia Telegram
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # 6. Mantiene el proceso activo
    await asyncio.Event().wait()


# =========================================================
# EJECUCIÓN
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
