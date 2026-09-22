from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

from config import BOT_TOKEN, ADMIN_ID
from database import (
    init_database,
    register_user,
    get_user,
    get_users_count
)


def main_menu():
    keyboard = [
        [
            InlineKeyboardButton("💰 Inversiones", callback_data="investments"),
            InlineKeyboardButton("💳 Depósitos", callback_data="deposits")
        ],
        [
            InlineKeyboardButton("💸 Retiros", callback_data="withdrawals"),
            InlineKeyboardButton("📊 Mi cuenta", callback_data="account")
        ],
        [
            InlineKeyboardButton("📜 Historial", callback_data="history"),
            InlineKeyboardButton("👥 Referidos", callback_data="referrals")
        ],
        [
            InlineKeyboardButton("🆘 Soporte", callback_data="support")
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


def admin_menu():
    keyboard = [
        [
            InlineKeyboardButton("👥 Usuarios", callback_data="admin_users"),
            InlineKeyboardButton("💰 Depósitos", callback_data="admin_deposits")
        ],
        [
            InlineKeyboardButton("💸 Retiros", callback_data="admin_withdrawals"),
            InlineKeyboardButton("📈 Inversiones", callback_data="admin_investments")
        ],
        [
            InlineKeyboardButton("📊 Estadísticas", callback_data="admin_stats"),
            InlineKeyboardButton("⚙️ Configuración", callback_data="admin_config")
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    register_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )

    text = (
        f"👋 Hola, {user.first_name}!\n\n"
        "Bienvenido a nuestra plataforma.\n\n"
        "Selecciona una opción:"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu()
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "⛔ No tienes autorización para acceder al panel administrativo."
        )
        return

    await update.message.reply_text(
        "🔐 PANEL DE ADMINISTRACIÓN\n\n"
        "Selecciona una opción:",
        reply_markup=admin_menu()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "account":

        user = get_user(user_id)

        if not user:
            await query.message.reply_text(
                "❌ Usuario no encontrado."
            )
            return

        username = (
            f"@{user['username']}"
            if user["username"]
            else "Sin username"
        )

        text = (
            "👤 MI CUENTA\n\n"
            f"🆔 ID: {user['telegram_id']}\n"
            f"👤 Usuario: {username}\n"
            f"💰 Saldo: ${user['balance']:.2f}\n"
        )

        await query.message.reply_text(text)

    elif data == "investments":

        await query.message.reply_text(
            "💰 INVERSIONES\n\n"
            "Esta sección será configurada próximamente."
        )

    elif data == "deposits":

        await query.message.reply_text(
            "💳 DEPÓSITOS\n\n"
            "La sección de depósitos será configurada próximamente."
        )

    elif data == "withdrawals":

        await query.message.reply_text(
            "💸 RETIROS\n\n"
            "La sección de retiros será configurada próximamente."
        )

    elif data == "history":

        await query.message.reply_text(
            "📜 HISTORIAL\n\n"
            "Tu historial aparecerá aquí."
        )

    elif data == "referrals":

        await query.message.reply_text(
            "👥 REFERIDOS\n\n"
            "El sistema de referidos será configurado próximamente."
        )

    elif data == "support":

        await query.message.reply_text(
            "🆘 SOPORTE\n\n"
            "Contacta con el administrador para recibir asistencia."
        )

    elif data.startswith("admin_"):

        if user_id != ADMIN_ID:
            await query.message.reply_text(
                "⛔ Acceso denegado."
            )
            return

        if data == "admin_users":

            total = get_users_count()

            await query.message.reply_text(
                f"👥 USUARIOS\n\n"
                f"Usuarios registrados: {total}"
            )

        elif data == "admin_stats":

            total = get_users_count()

            await query.message.reply_text(
                "📊 ESTADÍSTICAS\n\n"
                f"👥 Usuarios: {total}\n"
                "💰 Depósitos: próximamente\n"
                "💸 Retiros: próximamente\n"
                "📈 Inversiones: próximamente"
            )

        else:

            await query.message.reply_text(
                "⚙️ Esta sección será configurada próximamente."
            )


def run():

    init_database()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    print("🤖 Bot iniciado correctamente.")

    application.run_polling()


if __name__ == "__main__":
    run()
