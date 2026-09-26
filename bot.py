import os
import sqlite3
import shutil
import uuid
import asyncio
import re
from io import BytesIO
from html import escape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from datetime import datetime, timedelta, timezone

from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    InputFile,
)
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

# =========================================================
# CONFIGURACIÓN
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")

# Wallet TRC20/USDT que recibirán los depósitos.
# CONFIGÚRALA EN RENDER COMO VARIABLE DE ENTORNO.
USDT_TRC20_ADDRESS = os.getenv("USDT_TRC20_ADDRESS", "").strip()

DB_FILE = "database.db"
BACKUP_DIR = "backups"

# Plan de prueba/configurable.
# No representa una promesa de rentabilidad: son parámetros del sistema.
PLAN_NAME = os.getenv("PLAN_NAME", "Plan Inicial")
DAILY_RATE = 0.005  # Tasa histórica/base; los pagos diarios reales se seleccionan manualmente.
DAILY_QUOTA_OPTIONS = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00]
DAILY_QUOTA_LABELS = {
    0.25: "0,25%", 0.30: "0,30%", 0.35: "0,35%", 0.40: "0,40%",
    0.45: "0,45%", 0.50: "0,50%", 0.55: "0,55%", 0.60: "0,60%",
    0.65: "0,65%", 0.70: "0,70%", 0.75: "0,75%", 0.80: "0,80%",
    0.85: "0,85%", 0.90: "0,90%", 0.95: "0,95%", 1.00: "1,00%",
}
BACKUP_TIME = os.getenv("BACKUP_TIME", "06:00").strip()
BACKUP_TIMEZONE = os.getenv("BACKUP_TIMEZONE", "America/Sao_Paulo").strip()
TARGET_MULTIPLIER = float(os.getenv("TARGET_MULTIPLIER", "2.0"))
MIN_INVESTMENT = 50.0  # inversión mínima: 50 USDT
MIN_WITHDRAWAL = 15.0  # retiro mínimo: 15 USDT
WITHDRAWAL_INTERVAL_DAYS = 7
WITHDRAWAL_FEE_RATE = 0.03
REFERRAL_BONUS_FIRST_RATE = float(os.getenv("REFERRAL_BONUS_FIRST_RATE", "0.03") or "0.03")
REFERRAL_BONUS_SECOND_RATE = float(os.getenv("REFERRAL_BONUS_SECOND_RATE", "0.015") or "0.015")
# Compatibilidad: el porcentaje histórico principal queda apuntando al primero.
REFERRAL_BONUS_RATE = REFERRAL_BONUS_FIRST_RATE
ADMIN_WALLET_URL = os.getenv("ADMIN_WALLET_URL", "").strip()
TRONSCAN_TX_URL = "https://tronscan.org/#/transaction/"
PROFIT_TIMEZONE = os.getenv("PROFIT_TIMEZONE", "America/Sao_Paulo").strip()
IMAGES_DIR = Path(__file__).resolve().parent / "images"
WELCOME_IMAGE = IMAGES_DIR / "bienvenida.jpg"
LOCK_IMAGE = IMAGES_DIR / "bot bloqueado.jpg"
UNLOCK_IMAGE = IMAGES_DIR / "bot operativo.jpg"
WITHDRAW_SENT_IMAGE = IMAGES_DIR / "retiro enviado.jpg"
USDT_ICON_IMAGE = IMAGES_DIR / "usdt_trc20_icon.png"

# Usuarios y ganancias acreditadas en la última ejecución manual.
# Se utiliza para enviar una sola notificación por usuario, aunque tenga varias inversiones.
LAST_DAILY_PROFITS = {}
MAX_INVESTMENT = float(os.getenv("MAX_INVESTMENT", "1000000"))

# Planes de inversión disponibles. El monto del plan queda definido por el botón.
INVESTMENT_PLANS = [50, 100, 120, 150, 200, 250, 300, 350, 500, 650, 1000, 1500, 2000]

# Estados de conversación
DEP_AMOUNT, DEP_TX, DEP_PHOTO = range(3)
WITHDRAW_AMOUNT, WITHDRAW_ADDRESS = range(3, 5)

# =========================================================
# WEB SERVER PARA RENDER
# =========================================================

async def handle_web(request):
    return web.Response(text="Bot de Inversión activo y en línea.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_web)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Servidor web corriendo en el puerto {port}")


# =========================================================
# BASE DE DATOS
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            nombre TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            saldo REAL NOT NULL DEFAULT 0,
            invertido REAL NOT NULL DEFAULT 0,
            ganancias REAL NOT NULL DEFAULT 0,
            total_depositado REAL NOT NULL DEFAULT 0,
            total_retirado REAL NOT NULL DEFAULT 0,
            codigo_referido TEXT UNIQUE,
            referido_por TEXT,
            fecha_registro TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            telefono TEXT NOT NULL DEFAULT '',
            pais TEXT NOT NULL DEFAULT '',
            wallet_retiro TEXT NOT NULL DEFAULT '',
            registro_completo INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS depositos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            monto REAL NOT NULL,
            tx_hash TEXT NOT NULL DEFAULT '',
            foto_file_id TEXT NOT NULL DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'pendiente',
            fecha TEXT NOT NULL,
            fecha_revision TEXT,
            revisado_por INTEGER,
            plan_monto REAL NOT NULL DEFAULT 0,
            inversion_id INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS retiros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            monto REAL NOT NULL,
            direccion TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'pendiente',
            fecha TEXT NOT NULL,
            fecha_revision TEXT,
            revisado_por INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS inversiones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            capital REAL NOT NULL,
            ganancia_acumulada REAL NOT NULL DEFAULT 0,
            tasa_diaria REAL NOT NULL,
            multiplicador_objetivo REAL NOT NULL,
            estado TEXT NOT NULL DEFAULT 'activa',
            fecha_inicio TEXT NOT NULL,
            ultimo_calculo TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS movimientos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            monto REAL NOT NULL DEFAULT 0,
            descripcion TEXT NOT NULL DEFAULT '',
            fecha TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referido_id INTEGER NOT NULL,
            referidor_id INTEGER NOT NULL,
            nivel INTEGER NOT NULL DEFAULT 1,
            bono REAL NOT NULL DEFAULT 0,
            fecha TEXT NOT NULL,
            UNIQUE(referido_id, referidor_id)
        )
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS sistema (clave TEXT PRIMARY KEY, valor TEXT NOT NULL DEFAULT '')")
    cur.execute("CREATE TABLE IF NOT EXISTS pagos_diarios (fecha TEXT PRIMARY KEY, fecha_proceso TEXT NOT NULL, total REAL NOT NULL DEFAULT 0, inversiones INTEGER NOT NULL DEFAULT 0)")
    try:
        cur.execute("ALTER TABLE pagos_diarios ADD COLUMN cuota REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE usuarios ADD COLUMN ganancias_disponibles REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for column_sql in [
        "ALTER TABLE usuarios ADD COLUMN email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE usuarios ADD COLUMN telefono TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE usuarios ADD COLUMN pais TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE usuarios ADD COLUMN wallet_retiro TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE usuarios ADD COLUMN registro_completo INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            cur.execute(column_sql)
        except sqlite3.OperationalError:
            pass
    try:
        cur.execute("ALTER TABLE depositos ADD COLUMN plan_monto REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE depositos ADD COLUMN inversion_id INTEGER")
    except sqlite3.OperationalError:
        pass
    # Los depósitos aprobados antiguos que aún no están vinculados a una inversión
    # se consideran planes disponibles por el mismo importe del depósito.
    cur.execute("""
        UPDATE depositos
        SET plan_monto = monto
        WHERE estado = 'aprobado'
          AND COALESCE(plan_monto, 0) <= 0
          AND inversion_id IS NULL
    """)
    cur.execute("INSERT OR IGNORE INTO sistema(clave, valor) VALUES ('mantenimiento','0')")
    cur.execute("INSERT OR IGNORE INTO sistema(clave, valor) VALUES ('cuota_diaria_actual','')")
    conn.commit()
    conn.close()


# =========================================================
# UTILIDADES
# =========================================================

def money(value):
    return f"{float(value):,.2f}"


def tx_explorer_url(tx_hash):
    return TRONSCAN_TX_URL + quote(str(tx_hash).strip(), safe="")


def admin_wallet_url():
    # Solo abre la wallet configurada por el administrador.
    # No se usa TRONSCAN como sustituto.
    return ADMIN_WALLET_URL


def is_admin(user_id):
    return ADMIN_TELEGRAM_ID != 0 and user_id == ADMIN_TELEGRAM_ID


def private_only(update):
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


def generate_ref_code(telegram_id):
    return f"ref{telegram_id}"


def get_bot_username(context):
    return context.application.bot_data.get("bot_username", "")


def get_user(telegram_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM usuarios WHERE telegram_id = ?",
        (telegram_id,)
    ).fetchone()
    conn.close()
    return row


def ensure_user(user, start_ref=None):
    conn = db()
    existing = conn.execute(
        "SELECT * FROM usuarios WHERE telegram_id = ?",
        (user.id,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE usuarios
            SET username = ?
            WHERE telegram_id = ?
        """, (
            user.username or "",
            user.id
        ))
        conn.commit()
        conn.close()
        return False

    referido_por = None
    if start_ref:
        candidate = start_ref.strip()
        referrer = conn.execute(
            "SELECT telegram_id FROM usuarios WHERE codigo_referido = ?",
            (candidate,)
        ).fetchone()

        if referrer and int(referrer["telegram_id"]) != user.id:
            referido_por = candidate

    ref_code = generate_ref_code(user.id)

    conn.execute("""
        INSERT INTO usuarios (
            telegram_id, nombre, username, codigo_referido,
            referido_por, fecha_registro, registro_completo
        )
        VALUES (?, ?, ?, ?, ?, ?, 0)
    """, (
        user.id,
        "",
        user.username or "",
        ref_code,
        referido_por,
        now_iso()
    ))

    if referido_por:
        referrer = conn.execute(
            "SELECT telegram_id FROM usuarios WHERE codigo_referido = ?",
            (referido_por,)
        ).fetchone()

        if referrer:
            conn.execute("""
                INSERT OR IGNORE INTO referidos (
                    referido_id, referidor_id, nivel, bono, fecha
                )
                VALUES (?, ?, 1, 0, ?)
            """, (
                user.id,
                referrer["telegram_id"],
                now_iso()
            ))

    conn.commit()
    conn.close()
    return True


def add_movement(telegram_id, tipo, amount, description):
    conn = db()
    conn.execute("""
        INSERT INTO movimientos (
            telegram_id, tipo, monto, descripcion, fecha
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        telegram_id, tipo, amount, description, now_iso()
    ))
    conn.commit()
    conn.close()


def withdrawal_wait_info(telegram_id):
    """Devuelve (puede_retirar, segundos_restantes) usando retiros pendientes/aprobados."""
    conn = db()
    row = conn.execute("""
        SELECT fecha
        FROM retiros
        WHERE telegram_id = ?
          AND estado IN ('pendiente', 'aprobado')
        ORDER BY id DESC
        LIMIT 1
    """, (telegram_id,)).fetchone()
    conn.close()
    if not row or not row["fecha"]:
        return True, 0
    try:
        last = datetime.fromisoformat(row["fecha"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return True, 0
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    remaining = max(0, WITHDRAWAL_INTERVAL_DAYS * 86400 - elapsed)
    return remaining <= 0, int(remaining)


def withdrawal_wait_text(seconds):
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days} día(s) y {hours} hora(s)"
    if hours > 0:
        return f"{hours} hora(s) y {minutes} minuto(s)"
    return f"{minutes} minuto(s)"


def user_balance(telegram_id):
    row = get_user(telegram_id)
    if not row:
        return 0.0
    return float(row["saldo"])


def registration_complete(telegram_id):
    row = get_user(telegram_id)
    if not row:
        return False
    return bool(int(row["registro_completo"] or 0)) and bool((row["username"] or "").strip())


def registration_missing_step(row, telegram_user=None):
    username = (telegram_user.username if telegram_user else row["username"]) or ""
    if not username.strip():
        return "username"
    if not (row["nombre"] or "").strip():
        return "nombre"
    if not (row["email"] or "").strip():
        return "email"
    if not (row["telefono"] or "").strip():
        return "telefono"
    if not (row["pais"] or "").strip():
        return "pais"
    if not (row["wallet_retiro"] or "").strip():
        return "wallet"
    return None


def save_registration_field(telegram_id, field, value):
    allowed = {"nombre", "email", "telefono", "pais", "wallet_retiro"}
    if field not in allowed:
        raise ValueError("Campo de registro no permitido")
    conn = db()
    conn.execute(f"UPDATE usuarios SET {field}=? WHERE telegram_id=?", (value.strip(), telegram_id))
    conn.commit(); conn.close()


def mark_registration_complete_if_ready(telegram_id, telegram_user=None):
    conn = db()
    row = conn.execute("SELECT * FROM usuarios WHERE telegram_id=?", (telegram_id,)).fetchone()
    if not row:
        conn.close(); return False
    username = (telegram_user.username if telegram_user else row["username"]) or ""
    complete = bool(username.strip() and row["nombre"].strip() and row["email"].strip() and row["telefono"].strip() and row["pais"].strip() and row["wallet_retiro"].strip())
    conn.execute("UPDATE usuarios SET username=?, registro_completo=? WHERE telegram_id=?", (username.strip(), 1 if complete else 0, telegram_id))
    conn.commit(); conn.close()
    return complete


async def prompt_registration(update, context, force_step=None, user=None):
    user = user or getattr(update, "effective_user", None)
    if user is None:
        raise ValueError("No se pudo identificar al usuario para el registro")
    row = get_user(user.id)
    if not row:
        ensure_user(user)
        row = get_user(user.id)
    step = force_step or registration_missing_step(row, user)
    context.user_data["registration_step"] = step
    messages = {
        "username": (
            "📝 *REGISTRO OBLIGATORIO*\n\n"
            "Antes de utilizar el sistema debes configurar un *nombre de usuario de Telegram (@usuario)*.\n\n"
            "Ve a *Ajustes de Telegram → Editar perfil → Nombre de usuario*, configura uno y después vuelve al bot y pulsa /start."
        ),
        "nombre": "👤 *REGISTRO — NOMBRE*\n\nEscribe tu nombre y apellidos.",
        "email": "📧 *REGISTRO — CORREO*\n\nEscribe tu dirección de correo electrónico.",
        "telefono": "📱 *REGISTRO — TELÉFONO*\n\nEscribe tu número de teléfono con el que podamos contactarte por WhatsApp.",
        "pais": "🌎 *REGISTRO — PAÍS*\n\nEscribe el país donde resides.",
        "wallet": "💳 *REGISTRO — WALLET DE RETIRO*\n\nEscribe tu dirección de *USDT TRC20* donde deseas recibir tus retiros.",
    }
    message = getattr(update, "effective_message", None) or update
    await message.reply_text(messages[step], parse_mode="Markdown")


async def registration_text(update, context, text):
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        ensure_user(user)
        row = get_user(user.id)
    # Telegram username debe existir; el bot no puede crearlo por el usuario.
    if not (user.username or "").strip():
        await prompt_registration(update, context, "username")
        return True
    conn = db(); conn.execute("UPDATE usuarios SET username=? WHERE telegram_id=?", (user.username.strip(), user.id)); conn.commit(); conn.close()
    row = get_user(user.id)
    step = context.user_data.get("registration_step") or registration_missing_step(row, user)
    if not step:
        mark_registration_complete_if_ready(user.id, user)
        context.user_data.pop("registration_step", None)
        return False
    if step == "username":
        await prompt_registration(update, context, "nombre")
        return True
    if step == "nombre":
        if len(text) < 2:
            await update.message.reply_text("⚠️ Escribe tu nombre y apellidos.")
            return True
        save_registration_field(user.id, "nombre", text)
        await prompt_registration(update, context, "email")
        return True
    if step == "email":
        import re
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", text):
            await update.message.reply_text("⚠️ Ese correo no parece válido. Escríbelo nuevamente.")
            return True
        save_registration_field(user.id, "email", text)
        await prompt_registration(update, context, "telefono")
        return True
    if step == "telefono":
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 7:
            await update.message.reply_text("⚠️ Introduce un número de teléfono válido para WhatsApp.")
            return True
        save_registration_field(user.id, "telefono", text)
        await prompt_registration(update, context, "pais")
        return True
    if step == "pais":
        if len(text) < 2:
            await update.message.reply_text("⚠️ Escribe el nombre de tu país.")
            return True
        save_registration_field(user.id, "pais", text)
        await prompt_registration(update, context, "wallet")
        return True
    if step == "wallet":
        address = text.strip()
        if len(address) < 20 or not address.startswith("T"):
            await update.message.reply_text("⚠️ Introduce una dirección USDT TRC20 válida. Normalmente comienza por T.")
            return True
        save_registration_field(user.id, "wallet_retiro", address)
        complete = mark_registration_complete_if_ready(user.id, user)
        context.user_data.pop("registration_step", None)
        if complete:
            await update.message.reply_text("✅ *REGISTRO COMPLETADO*\n\nYa tienes acceso al panel del sistema.", parse_mode="Markdown")
            try:
                registered = get_user(user.id)
                await context.bot.send_message(
                    chat_id=ADMIN_TELEGRAM_ID,
                    text=(
                        "🆕 *NUEVO USUARIO REGISTRADO*\n\n"
                        f"👤 Nombre: {registered['nombre'] or '-'}\n"
                        f"👤 User: @{registered['username'] or '-'}\n"
                        f"🆔 ID Telegram: `{registered['telegram_id']}`\n"
                        f"📧 Correo: {registered['email'] or '-'}\n"
                        f"📱 WhatsApp: {registered['telefono'] or '-'}\n"
                        f"🌎 País: {registered['pais'] or '-'}\n"
                        f"💳 Wallet USDT TRC20: `{registered['wallet_retiro'] or '-'}`\n"
                        f"📅 Registro: {registered['fecha_registro'] or '-'}"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"⚠️ No se pudo notificar el nuevo registro al admin: {e}")
            await send_user_menu(user.id, context)
        return True
    return False


# =========================================================
# MANTENIMIENTO Y ESTADO DEL SISTEMA
# =========================================================
def is_maintenance():
    try:
        conn = db(); row = conn.execute("SELECT valor FROM sistema WHERE clave='mantenimiento'").fetchone(); conn.close()
        return bool(row and row["valor"] == "1")
    except Exception:
        return False

def set_maintenance(enabled):
    conn = db(); conn.execute("INSERT OR REPLACE INTO sistema(clave,valor) VALUES('mantenimiento',?)", ("1" if enabled else "0",)); conn.commit(); conn.close()


def get_system_value(key, default=""):
    conn = db()
    row = conn.execute("SELECT valor FROM sistema WHERE clave=?", (key,)).fetchone()
    conn.close()
    return row["valor"] if row else default


def set_system_value(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO sistema(clave,valor) VALUES(?,?)", (key, str(value)))
    conn.commit(); conn.close()

async def broadcast_users(bot, text):
    conn = db(); rows = conn.execute("SELECT telegram_id FROM usuarios ORDER BY id ASC").fetchall(); conn.close()
    sent = failed = 0
    for row in rows:
        try:
            await bot.send_message(chat_id=row["telegram_id"], text=text)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"⚠️ Broadcast fallo {row['telegram_id']}: {e}")
    return sent, failed

async def send_image_to_user(bot, chat_id, image_path, caption=None):
    if not image_path.exists():
        print(f"⚠️ Imagen no encontrada: {image_path}")
        return False
    try:
        with open(image_path, "rb") as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption or None, parse_mode="Markdown" if caption else None)
        return True
    except Exception as e:
        print(f"⚠️ Error enviando imagen a {chat_id}: {e}")
        return False


async def broadcast_image(bot, image_path, caption=None):
    conn = db(); rows = conn.execute("SELECT telegram_id FROM usuarios ORDER BY id ASC").fetchall(); conn.close()
    sent = failed = 0
    for row in rows:
        if await send_image_to_user(bot, row["telegram_id"], image_path, caption):
            sent += 1
        else:
            failed += 1
    return sent, failed


async def maintenance_guard(update):
    if is_admin(update.effective_user.id):
        return False
    if is_maintenance():
        if update.message:
            await send_image_to_user(update.get_bot(), update.effective_user.id, LOCK_IMAGE)
        elif update.callback_query:
            await update.callback_query.answer("🔧 Bot en mantenimiento. Intenta más tarde.", show_alert=True)
        return True
    return False

# =========================================================
# MENÚS
# =========================================================

def user_keyboard():
    return ReplyKeyboardMarkup([
        ["👤 Mi cuenta", "📈 Inversiones"],
        ["💰 Planes de Inversión"],
        ["🔄 Reinvertir saldo", "💸 Retirar"],
        ["🤝 Referidos", "📜 Historial"],
        ["📊 Ganancias Diarias"],
        ["🆘 Soporte", "ℹ️ Información"],
    ], resize_keyboard=True, is_persistent=True)


def admin_keyboard():
    return ReplyKeyboardMarkup([
        ["👥 Usuarios", "📥 Depósitos"],
        ["📤 Retiros", "📈 Inversiones"],
        ["💾 Crear respaldo", "♻️ Restaurar respaldo"],
        ["🔒 Bloquear bot", "🔓 Desbloquear bot"],
        ["💰 Pago Diario", "📊 Cuotas Diarias"],
        ["📊 Ganancias Diarias"],
        ["📊 Estado"],
        ["📢 Enviar mensaje"],
    ], resize_keyboard=True, is_persistent=True)

def back_inline(callback="admin_home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data=callback)]])


async def send_user_menu(chat_id, context, text=None):
    if text is None:
        text = "🏦 *MENÚ PRINCIPAL*\n\nSelecciona una opción:"
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=user_keyboard(), parse_mode="Markdown"
    )


async def send_admin_menu(chat_id, context, text=None):
    if text is None:
        text = "👑 *PANEL DE ADMINISTRACIÓN*\n\nSelecciona una opción:"
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=admin_keyboard(), parse_mode="Markdown"
    )


# =========================================================
# /START
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_only(update):
        return

    user = update.effective_user

    if await maintenance_guard(update):
        return

    start_ref = None
    if context.args:
        start_ref = context.args[0]

    is_new = ensure_user(user, start_ref)

    if is_admin(user.id):
        await send_admin_menu(
            user.id,
            context,
            "👑 *PANEL DE ADMINISTRACIÓN*\n\n"
            "Este usuario tiene acceso exclusivamente al panel administrativo."
        )
        return

    if is_new and WELCOME_IMAGE.exists():
        try:
            with open(WELCOME_IMAGE, "rb") as photo:
                await context.bot.send_photo(chat_id=user.id, photo=photo)
        except Exception as e:
            print(f"⚠️ No se pudo enviar bienvenida: {e}")

    row = get_user(user.id)
    step = registration_missing_step(row, user) if row else "username"
    if step:
        await prompt_registration(update, context, step)
        return

    mark_registration_complete_if_ready(user.id, user)
    await send_user_menu(
        user.id,
        context,
        f"👋 *Bienvenido, {user.first_name or 'usuario'}*\n\n"
        "Tu registro está completo. Selecciona una opción:"
    )


# =========================================================
# /ADMIN
# =========================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_only(update):
        return

    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return

    await send_admin_menu(user.id, context)


# =========================================================
# CUENTA
# =========================================================

async def show_account(query):
    user_id = query.from_user.id
    # Reparación: si por alguna razón el registro no existe, se crea/actualiza aquí.
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)

    if not row:
        return await query.edit_message_text(
            "⚠️ No se pudo cargar tu cuenta. Pulsa /start y vuelve a intentarlo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
        )

    conn = db()
    total_depositado_db = conn.execute(
        "SELECT COALESCE(SUM(monto), 0) AS s FROM depositos WHERE telegram_id=? AND estado='aprobado'",
        (user_id,)
    ).fetchone()["s"]
    total_retirado_db = conn.execute(
        "SELECT COALESCE(SUM(monto), 0) AS s FROM retiros WHERE telegram_id=? AND estado='aprobado'",
        (user_id,)
    ).fetchone()["s"]
    conn.close()
    total_depositado = max(float(row["total_depositado"] or 0), float(total_depositado_db or 0))
    total_retirado = max(float(row["total_retirado"] or 0), float(total_retirado_db or 0))
    ganancias = float(row["ganancias"] or 0)
    texto = (
        "👤 *MI CUENTA*\n\n"
        f"🆔 ID: `{row['telegram_id']}`\n"
        f"👤 User: @{row['username'] or '-'}\n"
        f"👤 Nombre: {row['nombre'] or '-'}\n"
        f"💰 Saldo disponible: *{money(row['saldo'])} USDT*\n"
        f"📥 Total depositado: *{money(total_depositado)} USDT*\n"
        f"📈 Capital invertido: *{money(row['invertido'])} USDT*\n"
        f"💵 Ganancias acumuladas: *{money(ganancias)} USDT*\n"
        
        f"📤 Total retirado: *{money(total_retirado)} USDT*\n\n"
        "El saldo disponible es el dinero acreditado que todavía no está invertido y puede utilizarse según los planes disponibles."
    )

    await query.edit_message_text(
        texto,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Planes de Inversión", callback_data="user_plans")],
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


# =========================================================
# REINVERSIÓN DE GANANCIAS
# =========================================================

async def show_reinvest(query):
    user_id = query.from_user.id
    row = get_user(user_id)
    gains = float(row["ganancias_disponibles"] or 0) if row else 0.0
    if gains < MIN_INVESTMENT:
        await query.edit_message_text(
            "🔄 *REINVERTIR SALDO*\n\n"
            f"Tu saldo acumulado de ganancias es de *{money(gains)} USDT*.\n\n"
            f"⚠️ No es suficiente para una nueva inversión. El mínimo para reinvertir es de *{money(MIN_INVESTMENT)} USDT*.\n\n"
            "Puedes seguir acumulando ganancias o retirarlas cuando cumplas las condiciones de retiro.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
        )
        return
    buttons=[]; row_buttons=[]
    for amount in INVESTMENT_PLANS:
        if amount <= gains:
            row_buttons.append(InlineKeyboardButton(f"🟢 {money(amount)} USDT", callback_data=f"reinvest_{amount}"))
            if len(row_buttons)==3:
                buttons.append(row_buttons); row_buttons=[]
    if row_buttons: buttons.append(row_buttons)
    buttons.append([InlineKeyboardButton("⬅️ Atrás", callback_data="user_home")])
    await query.edit_message_text(
        "🔄 *REINVERTIR SALDO*\n\n"
        f"Ganancias disponibles para reinvertir: *{money(gains)} USDT*\n\n"
        "Selecciona el nuevo plan. El importe se descontará únicamente de tus ganancias disponibles y se creará como una nueva inversión independiente.",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
    )


def credit_referral_bonus(conn, referred_user_id, investment_amount, investment_id):
    """Acredita bono únicamente por las dos primeras inversiones del referido.
    Plan 1 = 3%; Plan 2 = 1,5%; desde Plan 3 no hay más bono.
    El orden se determina por el ID de activación de la inversión.
    """
    row = conn.execute("SELECT referido_por FROM usuarios WHERE telegram_id=?", (referred_user_id,)).fetchone()
    if not row or not row["referido_por"]:
        return 0.0, None, 0
    referrer = conn.execute("SELECT telegram_id FROM usuarios WHERE codigo_referido=?", (row["referido_por"],)).fetchone()
    if not referrer or int(referrer["telegram_id"]) == int(referred_user_id):
        return 0.0, None, 0

    activation_number = conn.execute(
        "SELECT COUNT(*) AS c FROM inversiones WHERE telegram_id=? AND id<=?",
        (referred_user_id, investment_id)
    ).fetchone()["c"]
    if activation_number == 1:
        rate = REFERRAL_BONUS_FIRST_RATE
    elif activation_number == 2:
        rate = REFERRAL_BONUS_SECOND_RATE
    else:
        return 0.0, None, activation_number

    bonus = round(float(investment_amount) * rate, 2)
    if bonus <= 0:
        return 0.0, None, activation_number
    conn.execute("UPDATE usuarios SET saldo=saldo+?, ganancias_disponibles=ganancias_disponibles+?, ganancias=ganancias+? WHERE telegram_id=?", (bonus, bonus, bonus, referrer["telegram_id"]))
    conn.execute("UPDATE referidos SET bono=bono+? WHERE referido_id=? AND referidor_id=?", (bonus, referred_user_id, referrer["telegram_id"]))
    conn.execute("INSERT INTO movimientos(telegram_id,tipo,monto,descripcion,fecha) VALUES(?,?,?,?,?)", (referrer["telegram_id"], "bono_referido", bonus, f"Bono de referido Plan {activation_number} ({rate*100:.2f}%) por inversión #{investment_id}", now_iso()))
    return bonus, int(referrer["telegram_id"]), activation_number


async def perform_reinvestment(query, amount):
    user_id = query.from_user.id
    if amount not in INVESTMENT_PLANS or amount < MIN_INVESTMENT:
        await query.answer("Plan no disponible.", show_alert=True); return
    conn=db()
    row=conn.execute("SELECT ganancias_disponibles FROM usuarios WHERE telegram_id=?",(user_id,)).fetchone()
    gains=float(row["ganancias_disponibles"] or 0) if row else 0.0
    if gains < amount:
        conn.close()
        await query.answer(f"No tienes {money(amount)} USDT de ganancias disponibles para este plan.", show_alert=True); return
    now=now_iso()
    cur=conn.execute("INSERT INTO inversiones(telegram_id,plan,capital,ganancia_acumulada,tasa_diaria,multiplicador_objetivo,estado,fecha_inicio,ultimo_calculo) VALUES(?,?,?,0,?,2.0,'activa',?,?)",(user_id,f"Plan {money(amount)} USDT",amount,DAILY_RATE,now,now))
    investment_id=cur.lastrowid
    conn.execute("UPDATE usuarios SET saldo=saldo-?, ganancias_disponibles=ganancias_disponibles-? WHERE telegram_id=?",(amount,amount,user_id))
    bonus, referrer_id, referral_plan_number=credit_referral_bonus(conn,user_id,amount,investment_id)
    conn.commit(); conn.close()
    add_movement(user_id,"reinversion",amount,f"Reinversión de ganancias: Plan {money(amount)} USDT")
    if bonus and referrer_id:
        try:
            await query.get_bot().send_message(chat_id=referrer_id,text=("🎁 *BONO DE REFERIDO ACREDITADO*\n\n" f"Has recibido *{money(bonus)} USDT* por el *Plan {referral_plan_number}* de *{money(amount)} USDT* realizado por un usuario que se registró con tu enlace."),parse_mode="Markdown")
        except Exception as e: print(f"Error notificando bono: {e}")
    await query.edit_message_text("✅ *REINVERSIÓN CREADA*\n\n" f"Plan: *{money(amount)} USDT*\n" f"Ganancias utilizadas: *{money(amount)} USDT*\n" "\nLa inversión es independiente de tus demás planes.",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📈 Ver inversiones",callback_data="user_invest")],[InlineKeyboardButton("🏠 Menú Principal",callback_data="user_home")]]))


# =========================================================
# REFERIDOS
# =========================================================

async def show_referrals(query, context):
    user_id = query.from_user.id
    row = get_user(user_id)

    if not row:
        return

    username = get_bot_username(context)
    ref_code = row["codigo_referido"] or generate_ref_code(user_id)

    link = f"https://t.me/{username}?start={ref_code}" if username else (
        f"Abre el bot y usa tu código: {ref_code}"
    )

    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM referidos WHERE referidor_id = ?",
        (user_id,)
    ).fetchone()["c"]

    bonus = conn.execute(
        "SELECT COALESCE(SUM(bono), 0) AS total "
        "FROM referidos WHERE referidor_id = ?",
        (user_id,)
    ).fetchone()["total"]
    conn.close()

    share_text = (
        "Únete a este bot para conocer el sistema de inversión:\n\n"
        f"{link}"
    )

    share_url = (
        "https://t.me/share/url?"
        f"url={link.replace(' ', '%20')}"
        f"&text={share_text.replace(' ', '%20')}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Compartir mi enlace", url=share_url)],
        [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")],
    ])

    texto = (
        "🤝 *PROGRAMA DE REFERIDOS*\n\n"
        "Tu enlace personal:\n"
        f"`{link}`\n\n"
        f"👥 Personas registradas: *{count}*\n"
        f"🎁 Bonos acumulados: *{money(bonus)} USDT*\n"
        f"📈 Bonificación por inversión referida: *Plan 1 = {REFERRAL_BONUS_FIRST_RATE*100:.2f}%* y *Plan 2 = {REFERRAL_BONUS_SECOND_RATE*100:.2f}%*.\n\n"
        "La bonificación se acredita cuando el usuario referido realiza la inversión, no cuando solamente deposita. Desde el tercer plan no se genera bono.\n\n"
        "Comparte el enlace para que otros puedan registrarse usando tu referencia."
    )

    await query.edit_message_text(
        texto,
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# =========================================================
# SOPORTE
# =========================================================

async def show_support(query, context):
    context.user_data["support_waiting"] = True
    await query.edit_message_text(
        "🆘 *SOPORTE*\n\n"
        "Escribe ahora tu consulta o el problema que deseas enviar al administrador.\n\n"
        "Tu mensaje será enviado de forma privada al administrador y su respuesta llegará únicamente a ti.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="user_home")]])
    )


# =========================================================
# INFORMACIÓN
# =========================================================

async def show_info(query):
    temas = [
        "🔹 *¿QUÉ ES EL SISTEMA?*\nEs un sistema de inversión en el que se gestiona el capital mediante operaciones de trading.",
        "🔹 *CUOTAS DIARIAS*\nLas cuotas son variables y dependen de los resultados diarios obtenidos en el mercado.",
        f"🔹 *PLANES DE INVERSIÓN*\nLa inversión mínima es de *{money(MIN_INVESTMENT)} USDT* por plan, mediante la red *TRC20*.",
        "🔹 *FINALIZACIÓN DEL PLAN*\nCada plan termina cuando la ganancia acumulada alcanza el 100% del capital inicial, es decir, cuando el valor total del plan llega al 200% de la inversión inicial.",
        "🔹 *ACREDITACIÓN DE GANANCIAS*\nLas ganancias se generan de lunes a viernes. La acreditación se realizará durante el día y puede efectuarse hasta las 18:00. No se realizarán acreditaciones después de esa hora.",
        f"🔹 *RETIROS*\nEl retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT* y se permite *una solicitud cada 7 días*. Se aplica una comisión del *3%* por cada retiro realizado.",
        "🔹 *REINVERSIÓN*\nPuedes reinvertir el saldo acumulado de tus ganancias como un nuevo plan cuando alcances el mínimo de *50 USDT*.",
        "🔹 *TRANSFERENCIAS INTERNAS*\nNo existe transferencia de saldo entre usuarios dentro del sistema.",
        "🔹 *DEPÓSITOS Y RED*\nLos depósitos se realizan en USDT mediante la red TRC20 y son revisados manualmente por el administrador.",
        f"🔹 *REFERIDOS*\nEl primer plan que active un referido genera una bonificación del *{REFERRAL_BONUS_FIRST_RATE*100:.2f}%* y el segundo plan genera *{REFERRAL_BONUS_SECOND_RATE*100:.2f}%*. Desde el tercer plan no se genera bono.",
    ]
    texto = "ℹ️ *INFORMACIÓN DEL SISTEMA*\n\n" + "\n\n".join(temas)
    await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
    ]))


# =========================================================
# PLANES DE INVERSIÓN
# =========================================================

async def show_plans(query):
    user_id = query.from_user.id
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)
    balance = float(row["saldo"]) if row else 0.0

    buttons = []
    # Telegram no permite colocar una imagen dentro de un botón inline.
    # Usamos un icono visual junto al importe; la imagen USDT TRC20 completa
    # se muestra encima del listado de planes.
    row_buttons = []
    for amount in INVESTMENT_PLANS:
        row_buttons.append(InlineKeyboardButton(f"🟢 {money(amount)} USDT", callback_data=f"plan_{amount}"))
        if len(row_buttons) == 3:
            buttons.append(row_buttons)
            row_buttons = []
    if row_buttons:
        buttons.append(row_buttons)

    buttons.append([
        InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"),
        InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")
    ])

    texto = (
        "💰 *PLANES DE INVERSIÓN*\n\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        "Rendimiento diario: *variable* según la cuota seleccionada por el administrador.\n\n"
        "Selecciona el plan que deseas contratar. Cada vez que eliges un plan se crea una inversión independiente; tus planes anteriores no se reutilizan ni bloquean la compra de otro plan."
    )

    # Mostramos el icono circular enviado por el administrador al entrar en Planes.
    if USDT_ICON_IMAGE.exists():
        try:
            with open(USDT_ICON_IMAGE, "rb") as photo:
                await query.message.reply_photo(photo=photo)
        except Exception as e:
            print(f"⚠️ No se pudo enviar el icono USDT TRC20: {e}")

    await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def select_plan(query, amount):
    user_id = query.from_user.id
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)

    if amount not in INVESTMENT_PLANS:
        await query.answer("Plan no disponible.", show_alert=True)
        return

    # REGLA FUNDAMENTAL: cada selección de plan inicia un depósito NUEVO
    # por el 100% del importe del plan. Nunca se descuenta saldo, ganancias,
    # depósitos anteriores ni otros planes para calcular el nuevo depósito.
    conn = db()
    pending = conn.execute("""
        SELECT COUNT(*) AS c
        FROM depositos
        WHERE telegram_id = ?
          AND estado = 'pendiente'
          AND COALESCE(plan_monto, 0) = ?
    """, (user_id, amount)).fetchone()["c"]
    conn.close()

    pending_text = (
        f"\n\n⏳ Ya tienes *{pending} depósito(s) pendiente(s)* de este mismo plan. "
        "Eso no cambia el nuevo depósito: este también será por el importe completo."
    ) if pending else ""

    await query.edit_message_text(
        "💎 *NUEVO PLAN DE INVERSIÓN*\n\n"
        f"Plan seleccionado: *{money(amount)} USDT*\n"
        f"Monto exacto a depositar: *{money(amount)} USDT*\n\n"
        "🔒 Este depósito es independiente de todos los anteriores.\n"
        "El saldo disponible, las ganancias acumuladas, otros depósitos y otros planes "
        "NO se descuentan para calcular este importe.\n\n"
        "Si vuelves a seleccionar este mismo plan, se crea otro depósito independiente "
        "por el importe completo del plan."
        + pending_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💰 Depositar {money(amount)} USDT", callback_data=f"deposit_plan_{amount}")],
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


async def confirm_plan_investment(query, amount):
    # Compatibilidad con botones antiguos: busca un depósito disponible del importe exacto.
    user_id = query.from_user.id
    conn = db()
    row = conn.execute("""
        SELECT id FROM depositos
        WHERE telegram_id = ? AND estado = 'aprobado'
          AND COALESCE(plan_monto, 0) = ? AND inversion_id IS NULL
        ORDER BY id ASC LIMIT 1
    """, (user_id, amount)).fetchone()
    conn.close()
    if not row:
        await query.answer("No hay un depósito aprobado disponible para este plan.", show_alert=True)
        return
    await invest_available_plan(query, row["id"])


async def invest_available_plan(query, deposit_id):
    user_id = query.from_user.id
    conn = db()
    row = conn.execute("""
        SELECT id, monto, plan_monto, inversion_id
        FROM depositos
        WHERE id = ? AND telegram_id = ? AND estado = 'aprobado'
    """, (deposit_id, user_id)).fetchone()

    if not row or row["inversion_id"] is not None:
        conn.close()
        await query.answer("Este plan ya fue invertido o no está disponible.", show_alert=True)
        return

    amount = float(row["plan_monto"] or row["monto"])
    balance_row = conn.execute("SELECT saldo FROM usuarios WHERE telegram_id = ?", (user_id,)).fetchone()
    balance = float(balance_row["saldo"]) if balance_row else 0.0
    if balance < amount:
        conn.close()
        await query.answer(
            f"El saldo disponible ({money(balance)} USDT) no alcanza para invertir este plan de {money(amount)} USDT.",
            show_alert=True
        )
        return

    now = now_iso()
    cur = conn.execute("""
        INSERT INTO inversiones (
            telegram_id, plan, capital, ganancia_acumulada,
            tasa_diaria, multiplicador_objetivo, estado,
            fecha_inicio, ultimo_calculo
        ) VALUES (?, ?, ?, 0, ?, 2.0, 'activa', ?, ?)
    """, (user_id, f"Plan {money(amount)} USDT", amount, DAILY_RATE, now, now))
    investment_id = cur.lastrowid

    conn.execute("""
        UPDATE usuarios
        SET saldo = saldo - ?, invertido = invertido + ?
        WHERE telegram_id = ?
    """, (amount, amount, user_id))
    conn.execute("UPDATE depositos SET inversion_id = ? WHERE id = ?", (investment_id, deposit_id))
    bonus, referrer_id, referral_plan_number = credit_referral_bonus(conn, user_id, amount, investment_id)
    conn.commit()
    conn.close()

    add_movement(user_id, "inversion", amount, f"Inversión creada: Plan {money(amount)} USDT")
    if bonus and referrer_id:
        try:
            await query.get_bot().send_message(chat_id=referrer_id, text=("🎁 *BONO DE REFERIDO ACREDITADO*\n\n" f"Has recibido *{money(bonus)} USDT* por el *Plan {referral_plan_number}* de *{money(amount)} USDT* realizado por un usuario que se registró con tu enlace."), parse_mode="Markdown")
        except Exception as e:
            print(f"Error notificando bono de referido: {e}")

    await query.edit_message_text(
        "✅ *INVERSIÓN CREADA*\n\n"
        f"Plan: *{money(amount)} USDT*\n"
        "El depósito aprobado fue utilizado para activar este plan.\n"
        f"Capital invertido: *{money(amount)} USDT*\n"
        f"Ganancia inicial: *0.00 USDT*\n"
        "📅 Rendimiento diario: *variable* según la cuota seleccionada por el administrador.\n\n"
        "Este plan es independiente de tus demás planes y depósitos.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Ver inversiones", callback_data="user_invest")],
            [InlineKeyboardButton("💎 Elegir otro Plan", callback_data="user_plans")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


async def start_plan_deposit(query, context, plan_amount):
    context.user_data.clear()
    context.user_data["manual_flow"] = "deposit_tx_fixed"
    # IMPORTANTE: cada nuevo plan exige el 100% del importe del plan.
    # Nunca se resta saldo de depósitos anteriores.
    context.user_data["deposit_amount"] = float(plan_amount)
    context.user_data["deposit_plan_amount"] = float(plan_amount)
    await query.edit_message_text(
        "💰 *DEPÓSITO DEL PLAN*\n\n"
        f"Plan seleccionado: *{money(plan_amount)} USDT*\n"
        f"Monto exacto a depositar: *{money(plan_amount)} USDT*\n\n"
        "Este depósito es independiente de cualquier depósito anterior. No se descuenta ni se completa con tu saldo anterior.\n\n"
        "🟢 *WALLET DE DEPÓSITO*\n"
        f"`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
        "Realiza el depósito por el monto indicado y después envía aquí el TXID. No necesitas escribir la cantidad.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


# =========================================================
# INVERSIONES
# =========================================================

def investment_summary(telegram_id):
    conn = db()
    rows = conn.execute("""
        SELECT *
        FROM inversiones
        WHERE telegram_id = ?
        ORDER BY id DESC
    """, (telegram_id,)).fetchall()
    conn.close()
    return rows


async def show_investments(query):
    user_id = query.from_user.id
    row = get_user(user_id)
    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)
    rows = investment_summary(user_id)
    # Numeración estable según el orden en que cada inversión fue activada.
    plan_numbers = {inv["id"]: idx + 1 for idx, inv in enumerate(sorted(rows, key=lambda r: r["id"]))}

    conn = db()
    total_deposited = conn.execute("SELECT COALESCE(SUM(monto), 0) AS s FROM depositos WHERE telegram_id=? AND estado='aprobado'", (user_id,)).fetchone()["s"]
    active_invested = conn.execute("SELECT COALESCE(SUM(capital), 0) AS s FROM inversiones WHERE telegram_id=? AND estado='activa'", (user_id,)).fetchone()["s"]
    total_gains = conn.execute("SELECT COALESCE(SUM(ganancia_acumulada), 0) AS s FROM inversiones WHERE telegram_id=?", (user_id,)).fetchone()["s"]
    available_plans = conn.execute("""
        SELECT id, plan_monto, monto
        FROM depositos
        WHERE telegram_id=? AND estado='aprobado'
          AND COALESCE(plan_monto, 0) > 0
          AND inversion_id IS NULL
        ORDER BY id ASC
    """, (user_id,)).fetchall()
    conn.close()

    balance = float(row["saldo"]) if row else 0.0

    # Primero se muestran los planes activos; después, el resumen de la cuenta.
    lines = ["📈 *MIS INVERSIONES*", ""]

    active_rows = sorted((inv for inv in rows if inv["estado"] == "activa"), key=lambda r: r["id"])
    if active_rows:
        lines += ["🔥 *PLANES ACTIVOS*", ""]
        for inv in active_rows:
            capital = float(inv["capital"])
            gain = float(inv["ganancia_acumulada"])
            target_profit = capital
            progress = min(100.0, gain / target_profit * 100) if target_profit else 0.0
            lines.append(
                f"💎 *Plan {plan_numbers.get(inv['id'], 1)} — {money(capital)} USDT*\n"
                f"Capital: {money(capital)} USDT\n"
                f"Ganancia: {money(gain)} USDT\n"
                f"Progreso hacia el 200%: {progress:.2f}%\n"
                f"Estado: 🟢 Activa\n"
            )
    else:
        lines += ["🔥 *PLANES ACTIVOS*", "", "No tienes planes activos actualmente.", ""]

    lines += [
        "📊 *RESUMEN DE LA CUENTA*", "",
        f"💰 Total depositado aprobado: *{money(total_deposited)} USDT*",
        f"💳 Saldo disponible: *{money(balance)} USDT*",
        f"📊 Capital actualmente invertido: *{money(active_invested)} USDT*",
        f"💵 Ganancia acumulada: *{money(total_gains)} USDT*",
        "📅 Rendimiento diario: *variable* según la cuota seleccionada por el administrador.",
        ""
    ]

    if available_plans:
        lines += ["💎 *PLANES DISPONIBLES PARA INVERTIR*", ""]
        for dep in available_plans:
            amount = float(dep["plan_monto"] or dep["monto"])
            lines.append(f"• 💎 Plan disponible — {money(amount)} USDT")
        lines.append("")

    completed_rows = sorted((inv for inv in rows if inv["estado"] != "activa"), key=lambda r: r["id"])
    if completed_rows:
        lines += ["📚 *HISTORIAL DE PLANES COMPLETADOS*", ""]
        for inv in completed_rows:
            capital = float(inv["capital"])
            gain = float(inv["ganancia_acumulada"])
            lines.append(
                f"• Plan {plan_numbers.get(inv['id'], 1)} — {inv['plan']} — Capital: {money(capital)} USDT — "
                f"Ganancia: {money(gain)} USDT — Estado: {inv['estado']}"
            )

    buttons = []
    # Cada depósito aprobado que todavía no se ha invertido tiene su propio botón.
    for dep in available_plans:
        amount = float(dep["plan_monto"] or dep["monto"])
        buttons.append([InlineKeyboardButton(
            f"🚀 Invertir Plan {money(amount)} USDT",
            callback_data=f"invest_deposit_{dep['id']}"
        )])
    buttons.append([InlineKeyboardButton("💎 Elegir otro Plan de Inversión", callback_data="user_plans")])
    buttons.append([InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def create_investment(query):
    user_id = query.from_user.id
    row = get_user(user_id)

    if not row:
        ensure_user(query.from_user)
        row = get_user(user_id)
    if not row:
        await query.edit_message_text("⚠️ No se pudo cargar tu cuenta.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]]))
        return

    balance = float(row["saldo"])

    if balance < MIN_INVESTMENT:
        await query.edit_message_text(
            "⚠️ *SALDO INSUFICIENTE*\n\n"
            f"Saldo disponible: {money(balance)} USDT\n"
            f"Mínimo para invertir: {money(MIN_INVESTMENT)} USDT\n\n"
            "Primero realiza un depósito y espera su aprobación.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Depositar", callback_data="user_deposit")],
                [InlineKeyboardButton("⬅️ Volver", callback_data="user_invest")]
            ])
        )
        return

    # Para evitar inversiones accidentales, mostramos confirmación.
    await query.edit_message_text(
        "🚀 *CONFIRMAR INVERSIÓN*\n\n"
        f"Plan: *Saldo disponible completo*\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        "Rendimiento diario: *variable* según la cuota seleccionada por el administrador.\n"
        f"Objetivo configurado: *{TARGET_MULTIPLIER:.2f}x*\n\n"
        "La inversión utilizará el saldo disponible completo.\n\n"
        "⚠️ Estos son parámetros del sistema, no una garantía de resultado.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ Confirmar inversión",
                callback_data="user_confirm_invest"
            )],
            [InlineKeyboardButton("❌ Cancelar", callback_data="user_invest")]
        ])
    )


async def confirm_investment(query):
    user_id = query.from_user.id
    conn = db()

    row = conn.execute(
        "SELECT saldo FROM usuarios WHERE telegram_id = ?",
        (user_id,)
    ).fetchone()

    if not row:
        conn.close()
        return

    amount = float(row["saldo"])

    if amount < MIN_INVESTMENT:
        conn.close()
        await query.answer(
            "Saldo insuficiente.",
            show_alert=True
        )
        return

    if amount > MAX_INVESTMENT:
        amount = MAX_INVESTMENT

    conn.execute("""
        UPDATE usuarios
        SET saldo = saldo - ?, invertido = invertido + ?
        WHERE telegram_id = ?
    """, (amount, amount, user_id))

    conn.execute("""
        INSERT INTO inversiones (
            telegram_id, plan, capital, ganancia_acumulada,
            tasa_diaria, multiplicador_objetivo,
            estado, fecha_inicio, ultimo_calculo
        )
        VALUES (?, ?, ?, 0, ?, ?, 'activa', ?, ?)
    """, (
        user_id,
        PLAN_NAME,
        amount,
        DAILY_RATE,
        TARGET_MULTIPLIER,
        now_iso(),
        now_iso()
    ))

    conn.commit()
    conn.close()

    add_movement(
        user_id,
        "inversion",
        amount,
        f"Inversión creada: {PLAN_NAME}"
    )

    await query.edit_message_text(
        "✅ *INVERSIÓN CREADA*\n\n"
        f"Plan: *{PLAN_NAME}*\n"
        f"Capital: *{money(amount)} USDT*\n"
        "📊 Rendimiento diario: *variable según la cuota seleccionada por el administrador*\n\n"
        "La inversión ya aparece en tu panel.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Ver inversiones", callback_data="user_invest")],
            [InlineKeyboardButton("🏠 Menú principal", callback_data="user_home")]
        ])
    )


# =========================================================
# CUOTAS Y GANANCIAS
# =========================================================
def parse_quota(value):
    """Convierte 0,30 / 0.30 / 30 en porcentaje. Devuelve 0.003 para 0,30%."""
    try:
        raw = str(value).strip().replace("%", "").replace(",", ".")
        n = float(raw)
    except (TypeError, ValueError):
        raise ValueError("Cuota inválida.")
    # La interfaz usa 0,25 ... 1,00 para representar porcentajes.
    if n <= 0 or n > 1.0:
        raise ValueError("La cuota debe estar entre 0,25% y 1,00%.")
    return n / 100.0


def quota_label(rate_decimal):
    # Etiqueta visible fija: 0,25%, 0,30%, ..., 1,00%.
    # rate_decimal interno es 0.0025 para 0,25%, 0.0030 para 0,30%, etc.
    pct = round(rate_decimal * 100, 2)
    for value, label in DAILY_QUOTA_LABELS.items():
        if abs(pct - value) < 0.0001:
            return label
    return f"{pct:.2f}%".replace(".", ",")


def get_current_quota_decimal():
    conn = db()
    row = conn.execute("SELECT valor FROM sistema WHERE clave='cuota_diaria_actual'").fetchone()
    conn.close()
    try:
        value = float(row["valor"]) if row and str(row["valor"]).strip() else 0.0
        return value
    except Exception:
        return 0.0


def set_current_quota(rate_decimal):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO sistema(clave,valor) VALUES('cuota_diaria_actual',?)", (str(rate_decimal),))
    conn.commit(); conn.close()


def process_daily_quota(rate_decimal):
    """Acredita una sola cuota por día. rate_decimal es 0.003 para 0,30%."""
    global LAST_DAILY_PROFITS
    LAST_DAILY_PROFITS = {}
    try:
        tz = ZoneInfo(PROFIT_TIMEZONE)
    except Exception:
        tz = timezone.utc
    local_now = datetime.now(tz)
    # Las ganancias se generan de lunes a viernes.
    if local_now.weekday() >= 5:
        return 0, 0.0, {}, False, "weekend"
    date_key = local_now.date().isoformat()
    conn = db()
    if conn.execute("SELECT 1 FROM pagos_diarios WHERE fecha=?", (date_key,)).fetchone():
        conn.close()
        return 0, 0.0, {}, True, "already"
    rows = conn.execute("SELECT * FROM inversiones WHERE estado='activa'").fetchall()
    processed = 0; total_profit = 0.0; credited_by_user = {}; details_by_user = {}
    for inv in rows:
        capital = float(inv["capital"]); accumulated = float(inv["ganancia_acumulada"])
        target_profit = max(0.0, capital * (float(inv["multiplicador_objetivo"]) - 1.0))
        remaining = max(0.0, target_profit - accumulated)
        profit = min(capital * rate_decimal, remaining)
        if profit <= 0:
            conn.execute("UPDATE inversiones SET estado='completada', ultimo_calculo=? WHERE id=?", (now_iso(), inv["id"]))
            continue
        conn.execute("UPDATE inversiones SET ganancia_acumulada=ganancia_acumulada+?, tasa_diaria=?, ultimo_calculo=? WHERE id=?", (profit, rate_decimal, now_iso(), inv["id"]))
        conn.execute("UPDATE usuarios SET ganancias=ganancias+?, ganancias_disponibles=ganancias_disponibles+?, saldo=saldo+? WHERE telegram_id=?", (profit, profit, profit, inv["telegram_id"]))
        conn.execute("INSERT INTO movimientos(telegram_id,tipo,monto,descripcion,fecha) VALUES(?,?,?,?,?)", (inv["telegram_id"], "ganancia", profit, f"Cuota diaria {quota_label(rate_decimal)} inversión #{inv['id']}", now_iso()))
        if accumulated + profit >= target_profit - 1e-12:
            conn.execute("UPDATE inversiones SET estado='completada' WHERE id=?", (inv["id"],))
        processed += 1; total_profit += profit
        uid = int(inv["telegram_id"]); credited_by_user[uid] = credited_by_user.get(uid, 0.0) + profit
        plan_number = conn.execute(
            "SELECT COUNT(*) AS c FROM inversiones WHERE telegram_id=? AND id<=?",
            (uid, inv["id"])
        ).fetchone()["c"]
        details_by_user.setdefault(uid, []).append((f"Plan {plan_number} — {money(capital)} USDT", profit))
    conn.execute("INSERT INTO pagos_diarios(fecha,fecha_proceso,total,inversiones,cuota) VALUES(?,?,?,?,?)", (date_key, now_iso(), total_profit, processed, rate_decimal))
    conn.commit(); conn.close()
    LAST_DAILY_PROFITS = credited_by_user
    return processed, total_profit, credited_by_user, False, details_by_user


async def send_daily_quota_notifications(application, credited_by_user, rate_decimal, details_by_user=None):
    sent = 0
    image = IMAGES_DIR / (f"{rate_decimal * 100:.2f}".replace(".", ",") + ".jpg")
    details_by_user = details_by_user or {}
    for user_id, profit in credited_by_user.items():
        lines = []
        for plan, plan_profit in details_by_user.get(user_id, []):
            lines.append(f"• *{plan}*: +{money(plan_profit)} USDT")
        if details_by_user.get(user_id):
            ordered_details = sorted(
                details_by_user[user_id],
                key=lambda item: int(str(item[0]).split()[1]) if str(item[0]).startswith("Plan ") else 999999
            )
            lines = [f"• *{plan}*: +{money(plan_profit)} USDT" for plan, plan_profit in ordered_details]
        detail_text = "\n".join(lines) if lines else f"• Ganancia: +{money(profit)} USDT"
        caption = (f"✅ *GANANCIAS DEL DÍA ACREDITADAS*\n\n"
                   f"📊 Cuota aplicada: *{quota_label(rate_decimal)}*\n\n"
                   "📋 *Detalle por plan:*\n" + detail_text + "\n\n"
                   f"💰 *TOTAL DE GANANCIA ACREDITADA: +{money(profit)} USDT*\n\n"
                   "La ganancia ya fue acreditada en tu cuenta.")
        if await send_image_to_user(application.bot, user_id, image, caption):
            sent += 1
    return sent


def quota_keyboard():
    buttons=[]; row=[]
    for pct in DAILY_QUOTA_OPTIONS:
        row.append(InlineKeyboardButton(DAILY_QUOTA_LABELS[pct], callback_data=f"quota_{int(round(pct*100))}"))
        if len(row)==4:
            buttons.append(row); row=[]
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")])
    return InlineKeyboardMarkup(buttons)



def _quota_from_movement_description(desc):
    m = re.search(r"Cuota diaria\s+([0-9]+(?:,[0-9]+)?)%", desc or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", ".")) / 100.0
    except Exception:
        return 0.0


def _daily_user_gain_history(telegram_id):
    conn = db()
    rows = conn.execute(
        "SELECT fecha,monto,descripcion FROM movimientos WHERE telegram_id=? AND tipo='ganancia' ORDER BY fecha ASC, id ASC",
        (telegram_id,)
    ).fetchall()
    conn.close()
    grouped = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r['fecha'])
            day = dt.astimezone(ZoneInfo(PROFIT_TIMEZONE)).date().isoformat()
        except Exception:
            day = str(r['fecha'])[:10]
        q = _quota_from_movement_description(r['descripcion'])
        g = grouped.setdefault(day, {'total':0.0, 'quota':q})
        g['total'] += float(r['monto'] or 0)
        if q:
            g['quota'] = q
    return grouped


def _admin_daily_gain_history():
    conn = db()
    rows = conn.execute("SELECT fecha,total,inversiones,cuota FROM pagos_diarios ORDER BY fecha ASC").fetchall()
    # Fallback de cuota para respaldos antiguos que no tenían la columna.
    movement_rows = conn.execute("SELECT fecha,descripcion FROM movimientos WHERE tipo='ganancia' ORDER BY fecha ASC, id ASC").fetchall()
    conn.close()
    fallback = {}
    for r in movement_rows:
        try:
            day = datetime.fromisoformat(r['fecha']).astimezone(ZoneInfo(PROFIT_TIMEZONE)).date().isoformat()
        except Exception:
            day = str(r['fecha'])[:10]
        q = _quota_from_movement_description(r['descripcion'])
        if q:
            fallback.setdefault(day, q)
    out=[]
    for r in rows:
        q=float(r['cuota'] or 0) or fallback.get(r['fecha'], 0.0)
        out.append((r['fecha'], q, float(r['total'] or 0), int(r['inversiones'] or 0)))
    return out


async def show_user_daily_gains(query):
    uid=query.from_user.id
    history=_daily_user_gain_history(uid)
    if not history:
        text="📊 *GANANCIAS DIARIAS*\n\n*Todavía no tienes ganancias diarias acreditadas.*"
    else:
        lines=["📊 *GANANCIAS DIARIAS*", "", "*Detalle cronológico de tus ganancias acreditadas:*", ""]
        total=0.0
        ordered_days=list(history.keys())
        for idx, day in enumerate(ordered_days):
            data=history[day]
            total += data['total']
            q=quota_label(data['quota']) if data['quota'] else 'No registrada'
            try: display=datetime.fromisoformat(day).strftime('%d/%m/%Y')
            except Exception: display=day
            lines += [
                f"📅 *{display}*",
                f"📊 *Cuota: {q}*",
                f"💰 *Ganancia acreditada: {money(data['total'])} USDT*",
            ]
            if idx == len(ordered_days) - 1:
                lines.append("⭐ *ÚLTIMA GANANCIA ACREDITADA HASTA EL MOMENTO DE LA CONSULTA* ⭐")
            lines.append("")
        lines += ["━━━━━━━━━━━━", f"💵 *TOTAL ACUMULADO: {money(total)} USDT*"]
        text='\n'.join(lines)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú Principal", callback_data="user_home")]]))


async def show_admin_daily_gains(query):
    history=_admin_daily_gain_history()
    total=sum(x[2] for x in history)
    lines=["📊 *GANANCIAS DIARIAS — GENERAL*", ""]
    if not history:
        lines.append("Todavía no se ha acreditado ninguna ganancia.")
    else:
        for day,q,amount,count in history:
            try: display=datetime.fromisoformat(day).strftime('%d/%m/%Y')
            except Exception: display=day
            lines += [f"📅 *{display}*", f"📊 Cuota: *{quota_label(q) if q else 'No registrada'}*", f"💰 Total acreditado: *{money(amount)} USDT*", f"📈 Inversiones procesadas: *{count}*", ""]
        lines += ["━━━━━━━━━━━━", f"💵 *TOTAL GENERAL ACREDITADO: {money(total)} USDT*"]
    await query.edit_message_text('\n'.join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Consultar por ID", callback_data="admin_daily_user")],
        [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
    ]))


async def _send_admin_history(query, text, back_callback, back_label):
    """Envía todo el historial, dividido en mensajes si supera el límite de Telegram."""
    chunks = [text[i:i+3800] for i in range(0, len(text), 3800)] or [text]
    await query.edit_message_text(chunks[0], parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⬅️ {back_label}", callback_data=back_callback)],
        [InlineKeyboardButton("🏠 Panel", callback_data="admin_home")]
    ]))
    for extra in chunks[1:]:
        await query.message.reply_text(extra, parse_mode="Markdown")

async def show_admin_user_daily_gains(query, telegram_id):
    history = _daily_user_gain_history(telegram_id)
    conn=db(); user=conn.execute("SELECT nombre,username FROM usuarios WHERE telegram_id=?",(telegram_id,)).fetchone(); conn.close()
    if not user:
        await query.edit_message_text("⚠️ No se encontró ningún usuario con ese ID.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ganancias Diarias",callback_data="admin_daily_gains")]])); return
    lines=["📊 *HISTORIAL COMPLETO DE GANANCIAS DIARIAS*","",f"👤 Nombre: *{user['nombre'] or '-'}*",f"👤 Usuario: @{user['username'] or '-'}",f"🆔 ID: `{telegram_id}`","","📌 *Todas las acreditaciones registradas, desde la primera hasta la última:*",""]
    total=0.0
    if not history: lines.append("No tiene ganancias diarias acreditadas.")
    else:
        ordered=list(history.items())
        for idx,(day,data) in enumerate(ordered):
            total+=data['total']
            try: display=datetime.fromisoformat(day).strftime('%d/%m/%Y')
            except Exception: display=day
            lines += [f"📅 *{display}*",f"📊 Cuota: *{quota_label(data['quota']) if data['quota'] else 'No registrada'}*",f"💰 Ganancia acreditada: *{money(data['total'])} USDT*"]
            if idx==len(ordered)-1: lines.append("⭐ *ÚLTIMA GANANCIA ACREDITADA HASTA EL MOMENTO DE LA CONSULTA* ⭐")
            lines.append("")
        lines += ["━━━━━━━━━━━━",f"💵 *TOTAL ACREDITADO HISTÓRICO: {money(total)} USDT*"]
    await _send_admin_history(query,'\n'.join(lines),"admin_daily_gains","Ganancias Diarias")

async def show_admin_deposit_history(query, telegram_id):
    user=_user_identity(telegram_id)
    if not user:
        await query.edit_message_text("⚠️ No se encontró ningún usuario con ese ID.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Depósitos",callback_data="admin_deposits")]])); return
    rows=_history_rows('depositos',telegram_id,'id ASC')
    lines=["📥 *HISTORIAL COMPLETO DE DEPÓSITOS*","",f"👤 Nombre: *{user['nombre'] or '-'}*",f"👤 Usuario: @{user['username'] or '-'}",f"🆔 ID: `{telegram_id}`","","📌 *Todos los depósitos registrados, desde el primero hasta el último:*",""]
    total=0.0
    if not rows: lines.append("No hay depósitos registrados.")
    for idx,r in enumerate(rows,1):
        amount=float(r['monto'] or 0); total+=amount
        lines += [f"📥 *Depósito {idx}*",f"📅 Fecha: *{str(r['fecha'])[:19]}*",f"💰 Monto: *{money(amount)} USDT*",f"📌 Estado: *{r['estado']}*"]
        if r['tx_hash']: lines.append(f"🔗 TX: `{r['tx_hash']}`")
        lines.append("")
    lines += ["━━━━━━━━━━━━",f"💵 *TOTAL HISTÓRICO DE DEPÓSITOS: {money(total)} USDT*"]
    await _send_admin_history(query,'\n'.join(lines),"admin_deposits","Depósitos")

async def show_admin_withdraw_history(query, telegram_id):
    user=_user_identity(telegram_id)
    if not user:
        await query.edit_message_text("⚠️ No se encontró ningún usuario con ese ID.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retiros",callback_data="admin_withdrawals")]])); return
    rows=_history_rows('retiros',telegram_id,'id ASC')
    lines=["📤 *HISTORIAL COMPLETO DE RETIROS*","",f"👤 Nombre: *{user['nombre'] or '-'}*",f"👤 Usuario: @{user['username'] or '-'}",f"🆔 ID: `{telegram_id}`","","📌 *Todos los retiros registrados, desde el primero hasta el último:*",""]
    total=0.0
    if not rows: lines.append("No hay retiros registrados.")
    for idx,r in enumerate(rows,1):
        amount=float(r['monto'] or 0); total+=amount
        lines += [f"📤 *Retiro {idx}*",f"📅 Fecha: *{str(r['fecha'])[:19]}*",f"💰 Monto: *{money(amount)} USDT*",f"📌 Estado: *{r['estado']}*",f"🏦 Wallet: `{r['direccion']}`",""]
    lines += ["━━━━━━━━━━━━",f"💵 *TOTAL HISTÓRICO DE RETIROS: {money(total)} USDT*"]
    await _send_admin_history(query,'\n'.join(lines),"admin_withdrawals","Retiros")

async def show_admin_investment_history(query, telegram_id):
    user=_user_identity(telegram_id)
    if not user:
        await query.edit_message_text("⚠️ No se encontró ningún usuario con ese ID.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Inversiones",callback_data="admin_investments")]])); return
    rows=_history_rows('inversiones',telegram_id,'id ASC')
    lines=["📈 *HISTORIAL COMPLETO DE INVERSIONES*","",f"👤 Nombre: *{user['nombre'] or '-'}*",f"👤 Usuario: @{user['username'] or '-'}",f"🆔 ID: `{telegram_id}`","","📌 *Todas las inversiones registradas, desde Plan 1 hasta la última:*",""]
    total=0.0
    if not rows: lines.append("No hay inversiones registradas.")
    for plan,r in enumerate(rows,1):
        capital=float(r['capital'] or 0); total+=capital
        lines += [f"💎 *Plan {plan} — {money(capital)} USDT*",f"📅 Inicio: *{str(r['fecha_inicio'])[:19]}*",f"🎁 Ganancia acumulada: *{money(r['ganancia_acumulada'])} USDT*",f"📌 Estado: *{r['estado']}*",""]
    lines += ["━━━━━━━━━━━━",f"💰 *CAPITAL HISTÓRICO INVERTIDO: {money(total)} USDT*"]
    await _send_admin_history(query,'\n'.join(lines),"admin_investments","Inversiones")


async def show_daily_quotas(query):
    current = get_current_quota_decimal()
    await query.edit_message_text(
        "📊 *CUOTAS DIARIAS*\n\n"
        f"Cuota configurada actualmente: *{quota_label(current) if current else 'Sin seleccionar'}*\n\n"
        "Selecciona una cuota para acreditar hoy las ganancias de todas las inversiones activas.\n"
        "⚠️ La cuota se interpreta como porcentaje: 0,30 = 0,30%, no 30%.\n"
        "Cada usuario que reciba una acreditación recibirá la imagen correspondiente a la cuota seleccionada.\n\n"
        "No se puede ejecutar dos veces la acreditación del mismo día.",
        parse_mode="Markdown", reply_markup=quota_keyboard())


async def process_quota_callback(query, context, rate_decimal):
    if rate_decimal not in [round(x/100.0, 6) for x in DAILY_QUOTA_OPTIONS]:
        await query.answer("Cuota no disponible.", show_alert=True); return
    processed, total, credited, already, details_or_status = process_daily_quota(rate_decimal)
    if already:
        await query.edit_message_text("⚠️ *PAGO DIARIO YA REALIZADO*\n\nLa acreditación de hoy ya fue ejecutada. No se volverá a pagar una segunda vez el mismo día.", parse_mode="Markdown", reply_markup=back_inline())
        return
    if details_or_status == "weekend":
        await query.edit_message_text("📅 *PAGOS DE GANANCIAS*\n\nLas ganancias se generan de lunes a viernes. Hoy no corresponde realizar una acreditación diaria.", parse_mode="Markdown", reply_markup=back_inline())
        return
    details_by_user = details_or_status
    set_current_quota(rate_decimal)
    notified = await send_daily_quota_notifications(context.application, credited, rate_decimal, details_by_user) if credited else 0
    # El respaldo posterior es independiente del respaldo diario programado.
    # Se ejecuta después de completar la acreditación manual.
    await backup_after_accreditation(context.application, "manual")
    await query.edit_message_text(
        "✅ *CUOTA DIARIA APLICADA*\n\n"
        f"📊 Cuota: *{quota_label(rate_decimal)}*\n"
        f"📈 Inversiones acreditadas: *{processed}*\n"
        f"💰 Total acreditado: *{money(total)} USDT*\n"
        f"👥 Usuarios notificados: *{notified}*\n\n"
        "La acreditación fue aplicada sobre el capital de cada inversión activa y limitada al objetivo restante de cada plan.",
        parse_mode="Markdown", reply_markup=back_inline())


async def show_daily_payment_info(query, context, rate_decimal=None):
    if rate_decimal is None:
        buttons=[]; row_buttons=[]
        for rate in DAILY_QUOTA_OPTIONS:
            row_buttons.append(InlineKeyboardButton(DAILY_QUOTA_LABELS[rate], callback_data=f"calcquota_{int(round(rate*100)):02d}"))
            if len(row_buttons) == 4:
                buttons.append(row_buttons); row_buttons=[]
        if row_buttons:
            buttons.append(row_buttons)
        buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")])
        context.user_data.pop("admin_payment_info", None)
        await query.edit_message_text(
            "💰 *PAGO DIARIO — CONSULTA*\n\n"
            "Selecciona una de las cuotas disponibles para calcular cuánto correspondería pagar hoy sobre el capital actualmente invertido.\n\n"
            "ℹ️ Esta opción es *solo informativa*: no acredita fondos, no cambia saldos y no envía imágenes.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return
    conn=db(); active_capital=conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]; active=conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]; conn.close()
    due=float(active_capital)*rate_decimal
    await query.edit_message_text(
        "💰 *PAGO DIARIO — CÁLCULO INFORMATIVO*\n\n"
        f"📈 Capital total actualmente invertido: *{money(active_capital)} USDT*\n"
        f"👥 Inversiones activas: *{active}*\n"
        f"📊 Cuota consultada: *{quota_label(rate_decimal)}*\n"
        f"💵 Total que correspondería pagar hoy: *{money(due)} USDT*\n\n"
        "ℹ️ Este cálculo NO acredita fondos, NO cambia saldos y NO envía imágenes.",
        parse_mode="Markdown", reply_markup=back_inline())


# =========================================================
# DEPÓSITOS
# =========================================================

async def start_deposit(update, context):
    if not private_only(update):
        return ConversationHandler.END
    if await maintenance_guard(update):
        return ConversationHandler.END
    if not private_only(update):
        return ConversationHandler.END
    if not is_admin(update.effective_user.id) and not registration_complete(update.effective_user.id):
        await prompt_registration(update, context)
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "💰 *NUEVO DEPÓSITO*\n\n"
        "🟢 *WALLET DE DEPÓSITO*\n"
        f"Red: *TRC20*\n"
        f"`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
        "Envía ahora el monto que vas a depositar en USDT.",
        parse_mode="Markdown"
    )
    return DEP_AMOUNT


async def receive_deposit_amount(update, context):
    if not private_only(update):
        return ConversationHandler.END

    try:
        amount = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text("⚠️ Introduce solamente un número.")
        return DEP_AMOUNT

    if amount <= 0:
        await update.message.reply_text("⚠️ El monto debe ser mayor que 0.")
        return DEP_AMOUNT

    context.user_data["deposit_amount"] = amount

    await update.message.reply_text(
        "🔗 Ahora envía el *hash de la transacción (TXID)*.",
        parse_mode="Markdown"
    )
    return DEP_TX


async def receive_deposit_tx(update, context):
    if not private_only(update):
        return ConversationHandler.END

    tx = update.message.text.strip()

    if len(tx) < 5:
        await update.message.reply_text(
            "⚠️ El TXID parece demasiado corto. Envíalo nuevamente."
        )
        return DEP_TX

    context.user_data["deposit_tx"] = tx

    await update.message.reply_text(
        "📸 Ahora envía una *captura del comprobante* del depósito.\n\n"
        "Si no puedes enviar captura, escribe /skip.",
        parse_mode="Markdown"
    )
    return DEP_PHOTO


async def receive_deposit_photo(update, context):
    if not private_only(update):
        return ConversationHandler.END

    photo = update.message.photo[-1]
    context.user_data["deposit_photo"] = photo.file_id

    return await finish_deposit(update, context)


async def skip_deposit_photo(update, context):
    if not private_only(update):
        return ConversationHandler.END

    context.user_data["deposit_photo"] = ""
    return await finish_deposit(update, context)


async def finish_deposit(update, context):
    user_id = update.effective_user.id
    amount = float(context.user_data.get("deposit_amount", 0))
    plan_amount = float(context.user_data.get("deposit_plan_amount", 0) or 0)
    tx = context.user_data.get("deposit_tx", "")
    photo_id = context.user_data.get("deposit_photo", "")

    conn = db()
    cur = conn.execute("""
        INSERT INTO depositos (
            telegram_id, monto, tx_hash, foto_file_id,
            estado, fecha, plan_monto
        )
        VALUES (?, ?, ?, ?, 'pendiente', ?, ?)
    """, (
        user_id,
        amount,
        tx,
        photo_id,
        now_iso(),
        plan_amount
    ))
    deposit_id = cur.lastrowid
    conn.commit()
    conn.close()

    add_movement(
        user_id,
        "deposito_pendiente",
        amount,
        "Nuevo depósito enviado para revisión"
    )

    await update.message.reply_text(
        "⏳ *DEPÓSITO ENVIADO*\n\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"TXID: `{tx}`\n\n"
        "El administrador revisará el depósito manualmente.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Ver TXID en TRONSCAN", url=tx_explorer_url(tx))]])
    )

    admin_text = (
        "📥 *NUEVA SOLICITUD DE DEPÓSITO*\n\n"
        f"Usuario: `{user_id}`\n"
        f"Nombre: {update.effective_user.full_name}\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"TXID: `{tx}`"
    )

    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Ver TXID en TRONSCAN", url=tx_explorer_url(tx))],
        [
            InlineKeyboardButton(
                "✅ Aprobar",
                callback_data=f"dep_approve_{deposit_id}"
            ),
            InlineKeyboardButton(
                "❌ Rechazar",
                callback_data=f"dep_reject_{deposit_id}"
            )
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=admin_text,
            parse_mode="Markdown",
            reply_markup=admin_keyboard
        )

        if photo_id:
            await context.bot.send_photo(
                chat_id=ADMIN_TELEGRAM_ID,
                photo=photo_id,
                caption="📸 Comprobante del depósito"
            )
    except Exception as e:
        print(f"Error notificando depósito al admin: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_deposit(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Depósito cancelado.")
    return ConversationHandler.END


# =========================================================
# RETIROS
# =========================================================

async def start_withdraw(update, context):
    if not private_only(update):
        return ConversationHandler.END
    if await maintenance_guard(update):
        return ConversationHandler.END
    if not private_only(update):
        return ConversationHandler.END
    if not is_admin(update.effective_user.id) and not registration_complete(update.effective_user.id):
        await prompt_registration(update, context)
        return ConversationHandler.END

    row = get_user(update.effective_user.id)
    balance = float(row["saldo"]) if row else 0

    await update.message.reply_text(
        "💸 *SOLICITAR RETIRO*\n\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        f"Mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
        "Frecuencia: *1 retiro cada 7 días*\n"
        f"Wallet registrada: `{row['wallet_retiro'] if row and row['wallet_retiro'] else 'No registrada'}`\n\n"
        "Introduce el monto que deseas retirar.",
        parse_mode="Markdown"
    )

    context.user_data.clear()
    return WITHDRAW_AMOUNT


async def receive_withdraw_amount(update, context):
    if not private_only(update):
        return ConversationHandler.END

    try:
        amount = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text("⚠️ Introduce un monto válido.")
        return WITHDRAW_AMOUNT

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.",
            parse_mode="Markdown"
        )
        return WITHDRAW_AMOUNT

    can_withdraw, remaining = withdrawal_wait_info(update.effective_user.id)
    if not can_withdraw:
        await update.message.reply_text(
            "⏳ *RETIRO SEMANAL*\n\n"
            f"Ya realizaste una solicitud de retiro.\n"
            f"Podrás solicitar otro retiro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
            parse_mode="Markdown"
        )
        return WITHDRAW_AMOUNT

    balance = user_balance(update.effective_user.id)

    row = get_user(update.effective_user.id)
    gains_available = float(row["ganancias_disponibles"] or 0) if row else 0.0
    if amount > gains_available:
        await update.message.reply_text(
            f"⚠️ Solo puedes retirar ganancias disponibles.\nDisponible para retirar: {money(gains_available)} USDT"
        )
        return WITHDRAW_AMOUNT

    context.user_data["withdraw_amount"] = amount
    row = get_user(update.effective_user.id)
    registered_wallet = (row["wallet_retiro"] or "").strip() if row else ""
    if registered_wallet:
        await finish_withdraw_manual(update, context, registered_wallet)
        return ConversationHandler.END
    await update.message.reply_text("📍 Envía ahora tu dirección *USDT TRC20*.", parse_mode="Markdown")
    return WITHDRAW_ADDRESS


async def receive_withdraw_address(update, context):
    if not private_only(update):
        return ConversationHandler.END

    address = update.message.text.strip()
    amount = float(context.user_data.get("withdraw_amount", 0))
    user_id = update.effective_user.id

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.", parse_mode="Markdown")
        return WITHDRAW_AMOUNT

    can_withdraw, remaining = withdrawal_wait_info(user_id)
    if not can_withdraw:
        await update.message.reply_text(
            "⏳ *RETIRO SEMANAL*\n\n"
            f"Ya realizaste una solicitud de retiro.\nPodrás solicitar otro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if len(address) < 20:
        await update.message.reply_text(
            "⚠️ La dirección parece inválida. Envíala nuevamente."
        )
        return WITHDRAW_ADDRESS

    conn = db()

    row = conn.execute(
        "SELECT saldo FROM usuarios WHERE telegram_id = ?",
        (user_id,)
    ).fetchone()

    if not row or float(row["saldo"]) < amount:
        conn.close()
        await update.message.reply_text(
            "⚠️ El saldo ya no es suficiente para esta solicitud."
        )
        return ConversationHandler.END

    conn.execute("""
        UPDATE usuarios
        SET saldo = saldo - ?, ganancias_disponibles = ganancias_disponibles - ?
        WHERE telegram_id = ?
    """, (amount, amount, user_id))

    cur = conn.execute("""
        INSERT INTO retiros (
            telegram_id, monto, direccion, estado, fecha
        )
        VALUES (?, ?, ?, 'pendiente', ?)
    """, (
        user_id,
        amount,
        address,
        now_iso()
    ))

    withdrawal_id = cur.lastrowid
    conn.commit()
    conn.close()

    add_movement(
        user_id,
        "retiro_pendiente",
        amount,
        f"Retiro #{withdrawal_id} enviado para revisión"
    )

    await update.message.reply_text(
        "⏳ *RETIRO SOLICITADO*\n\n"
        f"ID: `{withdrawal_id}`\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"Red: *TRC20*\n"
        f"Dirección: `{address}`\n\n"
        "El administrador revisará y procesará la solicitud.",
        parse_mode="Markdown"
    )

    withdrawal_buttons = []
    if admin_wallet_url():
        withdrawal_buttons.append([InlineKeyboardButton("💼 Abrir mi wallet", url=admin_wallet_url())])
    withdrawal_buttons.append([
        InlineKeyboardButton("✅ Aprobar", callback_data=f"wd_approve_{withdrawal_id}"),
        InlineKeyboardButton("❌ Rechazar", callback_data=f"wd_reject_{withdrawal_id}")
    ])
    keyboard = InlineKeyboardMarkup(withdrawal_buttons)

    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=(
                "📤 *NUEVO RETIRO PENDIENTE*\n\n"
                f"ID: `{withdrawal_id}`\n"
                f"Usuario: `{user_id}`\n"
                f"Monto: *{money(amount)} USDT*\n"
                f"Dirección TRC20:\n`{address}`"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"Error notificando retiro al admin: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_withdraw(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Retiro cancelado.")
    return ConversationHandler.END


# =========================================================
# HISTORIAL
# =========================================================

async def show_history(query):
    user_id = query.from_user.id
    conn = db()
    rows = conn.execute("""
        SELECT tipo, monto, descripcion, fecha
        FROM movimientos
        WHERE telegram_id = ?
        ORDER BY id ASC
    """, (user_id,)).fetchall()
    conn.close()

    if not rows:
        texto = "📜 *HISTORIAL COMPLETO*\n\nTodavía no tienes movimientos."
    else:
        texto = "📜 *HISTORIAL COMPLETO*\n\n"
        for r in rows:
            try:
                fecha = datetime.fromisoformat(r["fecha"]).astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M")
            except Exception:
                fecha = r["fecha"]
            texto += (
                f"🕒 {fecha} UTC\n"
                f"• *{r['tipo']}* — {money(r['monto'])} USDT\n"
                f"  {r['descripcion']}\n\n"
            )

    # Telegram tiene límite de 4096 caracteres; si hay mucho historial, enviamos por páginas.
    chunks = [texto[i:i+3800] for i in range(0, len(texto), 3800)]
    if not chunks:
        chunks = [texto]
    await query.edit_message_text(
        chunks[0], parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )
    for extra in chunks[1:]:
        await query.message.reply_text(extra, parse_mode="Markdown")


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.message is None:
        return

    # TODO EL PANEL PRIVADO
    if query.message.chat.type != ChatType.PRIVATE:
        await query.answer(
            "Este menú solo funciona en el chat privado con el bot.",
            show_alert=True
        )
        return

    user_id = query.from_user.id
    data = query.data or ""
    if not is_admin(user_id) and is_maintenance():
        await query.answer("🔧 Bot en mantenimiento. Intenta más tarde.", show_alert=True)
        return
    if not is_admin(user_id) and not registration_complete(user_id):
        await query.answer("📝 Primero debes completar tu registro.", show_alert=True)
        await prompt_registration(query.message, context, user=query.from_user)
        return

    print(
        f"[CALLBACK] user_id={user_id} "
        f"chat_id={query.message.chat.id} data={data}"
    )

    # -----------------------------------------------------
    # FLUJOS ADMINISTRATIVOS DE RETIROS
    # -----------------------------------------------------
    if is_admin(user_id) and data.startswith("wd_approve_"):
        wid=int(data.rsplit("_",1)[1]); conn=db(); row=conn.execute("SELECT * FROM retiros WHERE id=? AND estado='pendiente'",(wid,)).fetchone(); conn.close()
        if not row: await query.answer("Este retiro ya fue procesado.",show_alert=True); return
        context.user_data["withdraw_admin_action"]="approve"; context.user_data["withdraw_admin_id"]=wid
        await query.edit_message_text(f"✅ *APROBAR RETIRO #{wid}*\n\nEscribe primero el mensaje que deseas enviar al usuario. Después te pediré la captura del comprobante.",parse_mode="Markdown")
        return
    if is_admin(user_id) and data.startswith("wd_reject_"):
        wid=int(data.rsplit("_",1)[1]); conn=db(); row=conn.execute("SELECT * FROM retiros WHERE id=? AND estado='pendiente'",(wid,)).fetchone(); conn.close()
        if not row: await query.answer("Este retiro ya fue procesado.",show_alert=True); return
        context.user_data["withdraw_admin_action"]="reject"; context.user_data["withdraw_admin_id"]=wid
        await query.edit_message_text(f"❌ *RECHAZAR RETIRO #{wid}*\n\nEscribe ahora el motivo del rechazo que se enviará al usuario.",parse_mode="Markdown")
        return

    # -----------------------------------------------------
    # SOPORTE ADMIN
    # -----------------------------------------------------
    if data.startswith("support_reply_"):
        if not is_admin(user_id):
            await query.answer("⛔ No autorizado.", show_alert=True); return
        try:
            target=int(data.rsplit("_",1)[1])
        except ValueError:
            await query.answer("Usuario inválido.", show_alert=True); return
        context.user_data["support_reply_user_id"] = target
        await query.edit_message_text("💬 *RESPONDER SOPORTE*\n\nEscribe ahora la respuesta que deseas enviar al usuario.\n\nLa respuesta llegará únicamente a ese usuario.",parse_mode="Markdown")
        return

    # -----------------------------------------------------
    # GANANCIAS DIARIAS / CONSULTAS
    # -----------------------------------------------------
    if data == "user_daily_gains":
        await show_user_daily_gains(query)
        return
    if data == "admin_daily_gains":
        if not is_admin(user_id):
            await query.answer("⛔ No autorizado.", show_alert=True); return
        await show_admin_daily_gains(query)
        return
    if data == "admin_daily_user":
        if not is_admin(user_id):
            await query.answer("⛔ No autorizado.", show_alert=True); return
        context.user_data["await_admin_daily_user_id"] = True
        await query.edit_message_text("🔎 *CONSULTAR GANANCIAS POR ID*\n\nEscribe el ID de Telegram del usuario que quieres consultar.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="admin_daily_gains")]]))
        return
    if data == "admin_deposit_user":
        context.user_data["await_admin_deposit_user_id"] = True
        await query.edit_message_text("🔎 *CONSULTAR DEPÓSITOS POR ID*\n\nEscribe el ID de Telegram del usuario.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="admin_deposits")]])); return
    if data == "admin_withdraw_user":
        context.user_data["await_admin_withdraw_user_id"] = True
        await query.edit_message_text("🔎 *CONSULTAR RETIROS POR ID*\n\nEscribe el ID de Telegram del usuario.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="admin_withdrawals")]])); return
    if data == "admin_invest_user":
        context.user_data["await_admin_invest_user_id"] = True
        await query.edit_message_text("🔎 *CONSULTAR INVERSIONES POR ID*\n\nEscribe el ID de Telegram del usuario.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="admin_investments")]])); return

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    if (data.startswith("admin_") or data.startswith("dep_approve_") or
            data.startswith("dep_reject_") or data.startswith("wd_approve_") or
            data.startswith("wd_reject_")):
        if not is_admin(user_id):
            await query.answer(
                "⛔ No autorizado.",
                show_alert=True
            )
            return

    if data == "admin_users":
        conn = db()
        count = conn.execute("SELECT COUNT(*) AS c FROM usuarios").fetchone()["c"]
        with_deposit = conn.execute("SELECT COUNT(*) AS c FROM usuarios WHERE total_depositado > 0").fetchone()["c"]
        total = conn.execute("SELECT COALESCE(SUM(total_depositado), 0) AS s FROM usuarios").fetchone()["s"]
        balance = conn.execute("SELECT COALESCE(SUM(saldo), 0) AS s FROM usuarios").fetchone()["s"]
        invested = conn.execute("SELECT COALESCE(SUM(invertido), 0) AS s FROM usuarios").fetchone()["s"]
        earnings = conn.execute("SELECT COALESCE(SUM(ganancias), 0) AS s FROM usuarios").fetchone()["s"]
        withdrawn = conn.execute("SELECT COALESCE(SUM(total_retirado), 0) AS s FROM usuarios").fetchone()["s"]
        conn.close()

        text = (
            "👥 *RESUMEN DE USUARIOS*\n\n"
            f"👤 Usuarios registrados: *{count}*\n"
            f"💰 Usuarios que han depositado: *{with_deposit}*\n\n"
            f"📥 Total depositado: *{money(total)} USDT*\n"
            f"💵 Saldo disponible de usuarios: *{money(balance)} USDT*\n"
            f"📈 Capital actualmente invertido: *{money(invested)} USDT*\n"
            f"🎁 Ganancias acumuladas: *{money(earnings)} USDT*\n"
            f"💸 Total retirado: *{money(withdrawn)} USDT*"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_inline())
        return

    if data == "admin_deposits":
        conn = db()
        total_count = conn.execute("SELECT COUNT(*) AS c FROM depositos").fetchone()["c"]
        approved_count, approved_amount = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM depositos WHERE estado='aprobado'").fetchone()
        pending_count, pending_amount = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM depositos WHERE estado='pendiente'").fetchone()
        rejected_count, rejected_amount = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM depositos WHERE estado='rechazado'").fetchone()
        conn.close()
        text = (
            "📥 *RESUMEN DE DEPÓSITOS*\n\n"
            f"📊 Total de solicitudes: *{total_count}*\n\n"
            f"✅ Aprobados: *{approved_count}* — *{money(approved_amount)} USDT*\n"
            f"⏳ Pendientes: *{pending_count}* — *{money(pending_amount)} USDT*\n"
            f"❌ Rechazados: *{rejected_count}* — *{money(rejected_amount)} USDT*\n\n"
            "Pulsa el botón de abajo para revisar los depósitos pendientes."
        )
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 Ver pendientes", callback_data="admin_deposits_pending")],
                [InlineKeyboardButton("🔎 Consultar por ID", callback_data="admin_deposit_user")],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_deposits_pending":
        conn = db()
        rows = conn.execute("SELECT * FROM depositos WHERE estado='pendiente' ORDER BY id DESC LIMIT 20").fetchall()
        conn.close()
        if not rows:
            text = "📥 *DEPÓSITOS PENDIENTES*\n\nNo hay depósitos pendientes."
        else:
            text = "📥 *DEPÓSITOS PENDIENTES*\n\n"
            for r in rows:
                text += (f"Usuario `{r['telegram_id']}`\n"
                         f"Monto: *{money(r['monto'])} USDT*\n"
                         f"TX: `{r['tx_hash']}`\n\n")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Depósitos", callback_data="admin_deposits")],[InlineKeyboardButton("🏠 Panel", callback_data="admin_home")]]))
        return

    if data == "admin_withdrawals":
        conn = db()
        total_count = conn.execute("SELECT COUNT(*) AS c FROM retiros").fetchone()["c"]
        pending_count, pending_amount = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM retiros WHERE estado='pendiente'").fetchone()
        approved_count, approved_amount = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM retiros WHERE estado='aprobado'").fetchone()
        rejected_count, rejected_amount = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM retiros WHERE estado='rechazado'").fetchone()
        conn.close()
        text = (
            "📤 *RESUMEN DE RETIROS*\n\n"
            f"📊 Total de solicitudes: *{total_count}*\n\n"
            f"⏳ Pendientes: *{pending_count}* — *{money(pending_amount)} USDT*\n"
            f"✅ Aprobados: *{approved_count}* — *{money(approved_amount)} USDT*\n"
            f"❌ Rechazados: *{rejected_count}* — *{money(rejected_amount)} USDT*\n\n"
            f"💸 Total retirado aprobado: *{money(approved_amount)} USDT*"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Ver pendientes", callback_data="admin_withdrawals_pending")],[InlineKeyboardButton("🔎 Consultar por ID", callback_data="admin_withdraw_user")],[InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]]))
        return

    if data == "admin_withdrawals_pending":
        conn = db()
        rows = conn.execute("SELECT * FROM retiros WHERE estado='pendiente' ORDER BY id DESC LIMIT 20").fetchall()
        conn.close()
        if not rows:
            text = "📤 *RETIROS PENDIENTES*\n\nNo hay retiros pendientes."
        else:
            text = "📤 *RETIROS PENDIENTES*\n\n"
            for r in rows:
                text += (f"#{r['id']} — Usuario `{r['telegram_id']}`\n"
                         f"Monto: *{money(r['monto'])} USDT*\n"
                         f"Dirección: `{r['direccion']}`\n\n")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retiros", callback_data="admin_withdrawals")],[InlineKeyboardButton("🏠 Panel", callback_data="admin_home")]]))
        return

    if data == "admin_investments":
        conn = db()
        active_count = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]
        active_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
        total_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones").fetchone()["s"]
        profit = conn.execute("SELECT COALESCE(SUM(ganancia_acumulada),0) s FROM inversiones").fetchone()["s"]
        finished = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='finalizada'").fetchone()["c"]
        conn.close()
        daily = float(active_capital) * get_current_quota_decimal()
        text = (
            "📈 *RESUMEN DE INVERSIONES*\n\n"
            f"🟢 Inversiones activas: *{active_count}*\n"
            f"💰 Capital actualmente invertido: *{money(active_capital)} USDT*\n"
            f"📊 Capital invertido histórico: *{money(total_capital)} USDT*\n"
            f"🎁 Ganancias acumuladas: *{money(profit)} USDT*\n"
            f"🏁 Planes finalizados: *{finished}*\n"
            "📅 Rendimiento diario: *variable* según la cuota seleccionada por el administrador.\n"
            f"🎯 Objetivo de cada plan: 200% del capital inicial"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar por ID", callback_data="admin_invest_user")],[InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]]))
        return

    if data == "admin_status":
        conn = db()
        users = conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
        approved_dep = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE estado='aprobado'").fetchone()["s"]
        pending_dep = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE estado='pendiente'").fetchone()["s"]
        active_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
        earnings = conn.execute("SELECT COALESCE(SUM(ganancia_acumulada),0) s FROM inversiones").fetchone()["s"]
        pending_wd = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM retiros WHERE estado='pendiente'").fetchone()["s"]
        approved_wd = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM retiros WHERE estado='aprobado'").fetchone()["s"]
        conn.close()
        text = (
            "📊 *RESUMEN GENERAL DEL SISTEMA*\n\n"
            f"👥 Usuarios: *{users}*\n"
            f"📥 Depósitos aprobados: *{money(approved_dep)} USDT*\n"
            f"⏳ Depósitos pendientes: *{money(pending_dep)} USDT*\n"
            f"📈 Capital invertido activo: *{money(active_capital)} USDT*\n"
            f"🎁 Ganancias acumuladas: *{money(earnings)} USDT*\n"
            f"📤 Retiros pendientes: *{money(pending_wd)} USDT*\n"
            f"💸 Retiros aprobados: *{money(approved_wd)} USDT*\n\n"
            "🟢 Bot: activo\n"
            "📊 Rendimiento diario: *variable*"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_inline())
        return

    if data == "admin_broadcast":
        context.user_data["admin_broadcast"] = True
        await query.edit_message_text(
            "📢 *ENVIAR MENSAJE A TODOS*\n\n"
            "Escribe ahora el mensaje que quieres enviar a todos los usuarios registrados.\n\n"
            "⚠️ El mensaje se enviará a todos los usuarios que tengan una cuenta en el bot.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="admin_home")]])
        )
        return

    if data == "admin_quotas":
        await show_daily_quotas(query)
        return

    if data == "admin_profit":
        await show_daily_payment_info(query, context)
        return

    if data.startswith("calcquota_"):
        try:
            code = int(data.rsplit("_", 1)[1])
            rate_decimal = code / 10000.0
        except ValueError:
            await query.answer("Cuota inválida.", show_alert=True)
            return
        if rate_decimal not in [round(x/100.0, 6) for x in DAILY_QUOTA_OPTIONS]:
            await query.answer("Cuota no disponible.", show_alert=True)
            return
        conn = db()
        active_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
        active_count = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]
        conn.close()
        due = float(active_capital) * rate_decimal
        await query.edit_message_text(
            "💰 *PAGO DIARIO — CÁLCULO INFORMATIVO*\n\n"
            f"📊 Cuota seleccionada: *{quota_label(rate_decimal)}*\n"
            f"📈 Capital total actualmente invertido: *{money(active_capital)} USDT*\n"
            f"👥 Inversiones activas: *{active_count}*\n"
            f"💵 Total que correspondería pagar hoy: *{money(due)} USDT*\n\n"
            "ℹ️ Este cálculo *NO acredita fondos*, *NO cambia saldos* y *NO envía imágenes*.\n\n"
            "Para acreditar las ganancias debes utilizar *📊 Cuotas Diarias*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Volver a Pago Diario", callback_data="admin_profit")],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data.startswith("quota_"):
        try:
            code = int(data.rsplit("_", 1)[1])
            rate_decimal = code / 10000.0
        except ValueError:
            await query.answer("Cuota inválida.", show_alert=True); return
        await process_quota_callback(query, context, rate_decimal)
        return

    if data == "admin_unlock_restore":
        if not is_admin(user_id):
            await query.answer("⛔ No autorizado.", show_alert=True); return
        if not is_maintenance():
            await query.edit_message_text("🔓 El bot ya está desbloqueado.", reply_markup=admin_keyboard()); return
        try:
            await restore_lock_backup(context.application)
        except Exception as e:
            await query.edit_message_text(
                f"❌ *NO SE PUDO RESTAURAR EL RESPALDO DEL BLOQUEO*\n\n`{e}`\n\n"
                "El bot sigue bloqueado para evitar cambios accidentales. Puedes usar ♻️ Restaurar respaldo para subir el `.db` manualmente.",
                parse_mode="Markdown",
                reply_markup=admin_keyboard()
            )
            return
        set_maintenance(False)
        sent=failed=0
        conn=db(); rows=conn.execute("SELECT telegram_id FROM usuarios WHERE telegram_id != ?",(ADMIN_TELEGRAM_ID,)).fetchall(); conn.close()
        for row in rows:
            try:
                if await send_image_to_user(context.bot, row["telegram_id"], UNLOCK_IMAGE):
                    sent += 1
                else:
                    failed += 1
            except Exception as e: failed+=1; print(e)
        await query.edit_message_text(
            f"♻️ *RESPALDO RESTAURADO*\n\n"
            "La base volvió al estado exacto que tenía cuando el bot fue bloqueado.\n\n"
            f"🔓 Bot desbloqueado.\nUsuarios notificados: {sent}\nNo enviados: {failed}",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if data == "admin_unlock_no_restore":
        if not is_admin(user_id):
            await query.answer("⛔ No autorizado.", show_alert=True); return
        if not is_maintenance():
            await query.edit_message_text("🔓 El bot ya está desbloqueado.", reply_markup=admin_keyboard()); return
        set_maintenance(False)
        sent=failed=0
        conn=db(); rows=conn.execute("SELECT telegram_id FROM usuarios WHERE telegram_id != ?",(ADMIN_TELEGRAM_ID,)).fetchall(); conn.close()
        for row in rows:
            try:
                if await send_image_to_user(context.bot, row["telegram_id"], UNLOCK_IMAGE):
                    sent += 1
                else:
                    failed += 1
            except Exception as e: failed+=1; print(e)
        await query.edit_message_text(
            f"🔓 *BOT DESBLOQUEADO SIN RESTAURAR*\n\n"
            "Se conservaron todos los cambios realizados mientras el bot estuvo bloqueado.\n\n"
            f"Usuarios notificados: {sent}\nNo enviados: {failed}",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if data == "admin_home":
        context.user_data.pop("admin_broadcast", None)
        await send_admin_menu(
            ADMIN_TELEGRAM_ID,
            context
        )
        return

    # -----------------------------------------------------
    # APROBAR/RECHAZAR DEPÓSITO
    # -----------------------------------------------------

    if data.startswith("dep_approve_"):
        deposit_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM depositos WHERE id = ?",
            (deposit_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este depósito ya fue procesado.",
                show_alert=True
            )
            return

        amount = float(row["monto"])
        uid = row["telegram_id"]

        conn.execute("""
            UPDATE depositos
            SET estado='aprobado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, deposit_id))

        conn.execute("""
            UPDATE usuarios
            SET saldo = saldo + ?,
                total_depositado = total_depositado + ?
            WHERE telegram_id = ?
        """, (amount, amount, uid))

        conn.commit()
        conn.close()

        add_movement(
            uid,
            "deposito",
            amount,
            "Depósito aprobado"
        )

        plan_amount = float(row["plan_monto"] or 0)
        plan_line = f"\n💎 Plan disponible para invertir: *{money(plan_amount)} USDT*" if plan_amount > 0 else ""

        await query.edit_message_text(
            "✅ *DEPÓSITO APROBADO*\n\n"
            f"Monto acreditado: *{money(amount)} USDT*" + plan_line,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "✅ *DEPÓSITO APROBADO*\n\n"
                    f"Tu depósito de *{money(amount)} USDT* fue aprobado y acreditado a tu saldo."
                    + (f"\n\n💎 Tu Plan {money(plan_amount)} USDT ya está disponible para invertir desde *📈 Inversiones*." if plan_amount > 0 else "")
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando aprobación de depósito: {e}")

        return

    if data.startswith("dep_reject_"):
        deposit_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM depositos WHERE id=?",
            (deposit_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este depósito ya fue procesado.",
                show_alert=True
            )
            return

        conn.execute("""
            UPDATE depositos
            SET estado='rechazado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, deposit_id))
        conn.commit()
        conn.close()

        await query.edit_message_text(
            "❌ *DEPÓSITO RECHAZADO*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=row["telegram_id"],
                text=(
                    "❌ *DEPÓSITO RECHAZADO*\n\n"
                    "El depósito fue rechazado. "
                    "Contacta al administrador si necesitas revisar el caso."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando rechazo: {e}")

        return

    # -----------------------------------------------------
    # APROBAR/RECHAZAR RETIRO
    # -----------------------------------------------------

    if data.startswith("wd_approve_"):
        withdrawal_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM retiros WHERE id=?",
            (withdrawal_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este retiro ya fue procesado.",
                show_alert=True
            )
            return

        conn.execute("""
            UPDATE retiros
            SET estado='aprobado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, withdrawal_id))

        conn.execute("""
            UPDATE usuarios
            SET total_retirado = total_retirado + ?
            WHERE telegram_id=?
        """, (row["monto"], row["telegram_id"]))

        conn.commit()
        conn.close()

        add_movement(
            row["telegram_id"],
            "retiro",
            row["monto"],
            f"Retiro #{withdrawal_id} aprobado"
        )

        await query.edit_message_text(
            f"✅ *RETIRO #{withdrawal_id} APROBADO*\n\n"
            f"Monto: *{money(row['monto'])} USDT*\n\n"
            "El administrador debe realizar el envío a la dirección indicada.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=row["telegram_id"],
                text=(
                    "✅ *RETIRO APROBADO*\n\n"
                    f"Tu solicitud #{withdrawal_id} por "
                    f"*{money(row['monto'])} USDT* fue aprobada.\n\n"
                    "El envío será procesado por el administrador."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando retiro aprobado: {e}")

        return

    if data.startswith("wd_reject_"):
        withdrawal_id = int(data.rsplit("_", 1)[1])

        conn = db()
        row = conn.execute(
            "SELECT * FROM retiros WHERE id=?",
            (withdrawal_id,)
        ).fetchone()

        if not row or row["estado"] != "pendiente":
            conn.close()
            await query.answer(
                "Este retiro ya fue procesado.",
                show_alert=True
            )
            return

        conn.execute("""
            UPDATE retiros
            SET estado='rechazado', fecha_revision=?, revisado_por=?
            WHERE id=?
        """, (now_iso(), user_id, withdrawal_id))

        conn.execute("""
            UPDATE usuarios
            SET saldo = saldo + ?, ganancias_disponibles = ganancias_disponibles + ?
            WHERE telegram_id=?
        """, (row["monto"], row["monto"], row["telegram_id"]))

        conn.commit()
        conn.close()

        add_movement(
            row["telegram_id"],
            "devolucion_retiro",
            row["monto"],
            f"Retiro #{withdrawal_id} rechazado; saldo devuelto"
        )

        await query.edit_message_text(
            f"❌ *RETIRO #{withdrawal_id} RECHAZADO*\n\n"
            "El saldo fue devuelto al usuario.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⬅️ Panel",
                    callback_data="admin_home"
                )]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=row["telegram_id"],
                text=(
                    "❌ *RETIRO RECHAZADO*\n\n"
                    f"El retiro #{withdrawal_id} fue rechazado y "
                    f"{money(row['monto'])} USDT fueron devueltos a tu saldo."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error notificando retiro rechazado: {e}")

        return

    # -----------------------------------------------------
    # USUARIO
    # -----------------------------------------------------

    if data == "user_home":
        # Cancelar soporte debe cancelar realmente el modo de captura;
        # las siguientes opciones del menú no deben interpretarse como soporte.
        context.user_data.pop("support_waiting", None)
        await send_user_menu(
            user_id,
            context,
            "🏦 *MENÚ PRINCIPAL*\n\nSelecciona una opción:"
        )
        return

    if data == "user_account":
        await show_account(query)
        return

    if data == "user_invest":
        await show_investments(query)
        return

    if data == "user_plans":
        await show_plans(query)
        return

    if data.startswith("plan_"):
        try:
            amount = float(data.split("_", 1)[1])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True)
            return
        await select_plan(query, amount)
        return

    if data.startswith("confirm_plan_"):
        try:
            amount = float(data.split("_", 2)[2])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True)
            return
        await confirm_plan_investment(query, amount)
        return

    if data.startswith("deposit_plan_"):
        try:
            amount = float(data.split("_", 2)[2])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True)
            return
        await start_plan_deposit(query, context, amount)
        return

    if data.startswith("invest_deposit_"):
        try:
            deposit_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True)
            return
        await invest_available_plan(query, deposit_id)
        return

    if data == "user_new_investment":
        await show_plans(query)
        return

    if data == "user_confirm_invest":
        await confirm_investment(query)
        return

    if data == "user_referrals":
        await show_referrals(query, context)
        return

    if data == "user_reinvest":
        await show_reinvest(query)
        return

    if data.startswith("reinvest_"):
        try:
            amount=float(data.split("_",1)[1])
        except ValueError:
            await query.answer("Plan inválido.", show_alert=True); return
        await perform_reinvestment(query, amount)
        return

    if data == "user_support":
        await show_support(query, context)
        return

    if data == "user_history":
        await show_history(query)
        return

    if data == "user_info":
        await show_info(query)
        return

    if data == "user_deposit":
        await query.edit_message_text(
            "💰 *NUEVO DEPÓSITO*\n\n"
            "🟢 *WALLET DE DEPÓSITO*\n"
            f"`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
            "Puedes elegir un plan para que el monto quede fijado automáticamente.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Ver Planes de Inversión", callback_data="user_plans")],
                [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
            ])
        )
        context.user_data.clear()
        return

    if data == "user_withdraw":
        await query.edit_message_text(
            "💸 *RETIRO*\n\n"
            "Escribe ahora el monto que deseas retirar.\n"
            "Tu wallet de retiro registrada se utilizará automáticamente.",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        context.user_data["manual_flow"] = "withdraw_amount"
        return


# =========================================================
# TEXTO PRIVADO
# =========================================================

async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_only(update):
        return
    if await maintenance_guard(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    if not is_admin(update.effective_user.id):
        row = get_user(update.effective_user.id)
        if not row:
            ensure_user(update.effective_user)
            row = get_user(update.effective_user.id)
        if not registration_complete(update.effective_user.id):
            handled = await registration_text(update, context, text)
            if handled:
                return

    if is_admin(update.effective_user.id) and context.user_data.get("support_reply_user_id"):
        target = int(context.user_data.pop("support_reply_user_id"))
        try:
            await context.bot.send_message(chat_id=target, text=("💬 RESPUESTA DE SOPORTE\n\n" + text))
            await update.message.reply_text("✅ Respuesta enviada al usuario.", reply_markup=admin_keyboard())
        except Exception as e:
            await update.message.reply_text(f"⚠️ No se pudo enviar la respuesta: {e}", reply_markup=admin_keyboard())
        return

    if is_admin(update.effective_user.id) and context.user_data.get("withdraw_admin_action"):
        action=context.user_data.get("withdraw_admin_action"); wid=int(context.user_data.get("withdraw_admin_id",0))
        if text == "/cancelar":
            context.user_data.pop("withdraw_admin_action",None); context.user_data.pop("withdraw_admin_id",None); await update.message.reply_text("❌ Acción cancelada.",reply_markup=admin_keyboard()); return
        if action == "reject":
            conn=db(); row=conn.execute("SELECT * FROM retiros WHERE id=? AND estado='pendiente'",(wid,)).fetchone()
            if not row: conn.close(); await update.message.reply_text("⚠️ Retiro no disponible.",reply_markup=admin_keyboard()); context.user_data.clear(); return
            conn.execute("UPDATE retiros SET estado='rechazado',fecha_revision=?,revisado_por=? WHERE id=?",(now_iso(),update.effective_user.id,wid))
            conn.execute("UPDATE usuarios SET saldo=saldo+?,ganancias_disponibles=ganancias_disponibles+? WHERE telegram_id=?",(row['monto'],row['monto'],row['telegram_id']))
            conn.commit(); conn.close(); add_movement(row['telegram_id'],'devolucion_retiro',row['monto'],f'Retiro #{wid} rechazado')
            context.user_data.clear(); await update.message.reply_text(f"❌ Retiro #{wid} rechazado y saldo devuelto.",reply_markup=admin_keyboard())
            try: await context.bot.send_message(chat_id=row['telegram_id'],text=f"❌ *RETIRO RECHAZADO*\n\nSolicitud #{wid}.\nMotivo: {text}\n\n{money(row['monto'])} USDT fueron devueltos a tus ganancias disponibles.",parse_mode="Markdown")
            except Exception as e: print(e)
            return
        if action == "approve":
            context.user_data["withdraw_admin_message"]=text; context.user_data["withdraw_admin_action"]="approve_photo"
            await update.message.reply_text("📸 Ahora envía la captura del comprobante de pago.",reply_markup=admin_keyboard()); return

    if is_admin(update.effective_user.id) and context.user_data.get("admin_payment_info"):
        if text == "/cancelar":
            context.user_data.pop("admin_payment_info", None)
            await update.message.reply_text("❌ Cálculo cancelado.", reply_markup=admin_keyboard())
            return
        try:
            rate_decimal = parse_quota(text)
        except ValueError as e:
            await update.message.reply_text("⚠️ " + str(e) + " Ejemplo válido: 0,30", reply_markup=admin_keyboard())
            return
        context.user_data.pop("admin_payment_info", None)
        # cálculo informativo únicamente
        conn=db(); active_capital=conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]; active=conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]; conn.close()
        due=float(active_capital)*rate_decimal
        await update.message.reply_text(
            "💰 *PAGO DIARIO — CÁLCULO INFORMATIVO*\n\n"
            f"📈 Capital total actualmente invertido: *{money(active_capital)} USDT*\n"
            f"👥 Inversiones activas: *{active}*\n"
            f"📊 Cuota consultada: *{quota_label(rate_decimal)}*\n"
            f"💵 Total que correspondería pagar hoy: *{money(due)} USDT*\n\n"
            "ℹ️ Este cálculo NO acredita fondos, NO cambia saldos y NO envía imágenes.",
            parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    # El administrador no entra nunca en el flujo de usuario.
    if is_admin(update.effective_user.id) and context.user_data.get("admin_broadcast"):
        if text == "/cancelar":
            context.user_data.pop("admin_broadcast", None)
            await update.message.reply_text("❌ Envío cancelado.", reply_markup=admin_keyboard())
            return

        context.user_data.pop("admin_broadcast", None)
        conn = db()
        rows = conn.execute("SELECT telegram_id FROM usuarios ORDER BY id ASC").fetchall()
        conn.close()

        sent = 0
        failed = 0
        for row in rows:
            try:
                await context.bot.send_message(
                    chat_id=row["telegram_id"],
                    text=("📢 MENSAJE DEL ADMINISTRADOR\n\n" + text)
                )
                sent += 1
            except Exception as e:
                failed += 1
                print(f"⚠️ No se pudo enviar mensaje a {row['telegram_id']}: {e}")

        await update.message.reply_text(
            "✅ *MENSAJE ENVIADO*\n\n"
            f"👥 Usuarios encontrados: *{len(rows)}*\n"
            f"✅ Enviados correctamente: *{sent}*\n"
            f"⚠️ No enviados: *{failed}*",
            parse_mode="Markdown",
            reply_markup=admin_keyboard()
        )
        return

    if is_admin(update.effective_user.id) and context.user_data.get("admin_payment_info"):
        if text == "/cancelar":
            context.user_data.pop("admin_payment_info", None)
            await update.message.reply_text("❌ Consulta cancelada.", reply_markup=admin_keyboard())
            return
        rate=parse_quota(text)
        if rate is None:
            await update.message.reply_text("⚠️ Cuota inválida. Usa un valor entre 0,25 y 1,00; por ejemplo *0,35*.", parse_mode="Markdown")
            return
        conn=db(); active_capital=conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]; active_count=conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]; conn.close()
        due=float(active_capital)*rate
        context.user_data.pop("admin_payment_info",None)
        await update.message.reply_text(
            "💰 *CONSULTA DE PAGO DIARIO*\n\n"
            f"📊 Cuota consultada: *{quota_label(rate)}*\n"
            f"📈 Capital activo: *{money(active_capital)} USDT*\n"
            f"👥 Inversiones activas: *{active_count}*\n"
            f"💵 Total correspondiente: *{money(due)} USDT*\n\n"
            "ℹ️ No se ha acreditado ningún pago. Para acreditar y notificar, usa *📊 Cuotas Diarias*.",
            parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    if is_admin(update.effective_user.id):
        query_flags = [
            ("await_admin_daily_user_id", show_admin_user_daily_gains, "Ganancias"),
            ("await_admin_deposit_user_id", show_admin_deposit_history, "Depósitos"),
            ("await_admin_withdraw_user_id", show_admin_withdraw_history, "Retiros"),
            ("await_admin_invest_user_id", show_admin_investment_history, "Inversiones"),
        ]
        for flag, func, _label in query_flags:
            if context.user_data.get(flag):
                context.user_data.pop(flag, None)
                try:
                    target_id = int(text.strip())
                except ValueError:
                    await update.message.reply_text("⚠️ El ID debe ser numérico. Inténtalo nuevamente.", reply_markup=admin_keyboard())
                    context.user_data[flag] = True
                    return
                class AdminTextQuery:
                    def __init__(self, message, user): self.message=message; self.from_user=user
                    async def edit_message_text(self, *args, **kwargs): return await update.message.reply_text(*args, **kwargs)
                    async def answer(self, *args, **kwargs): return None
                await func(AdminTextQuery(update.message, update.effective_user), target_id)
                return

    if context.user_data.get("support_waiting") and not is_admin(update.effective_user.id):
        context.user_data.pop("support_waiting", None)
        user = update.effective_user
        await update.message.reply_text("⏳ Tu mensaje fue enviado al administrador. Te responderemos por este mismo chat.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Responder", callback_data=f"support_reply_{user.id}")]])
        try:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=("🆘 NUEVO MENSAJE DE SOPORTE\n\n"
                      f"👤 Nombre: {user.full_name or '-'}\n"
                      f"🔢 ID Telegram: {user.id}\n"
                      f"👤 Usuario: @{user.username or '-'}\n\n"
                      f"💬 Mensaje:\n{text}"),
                reply_markup=kb
            )
        except Exception as e:
            print(f"Error enviando soporte al admin: {e}")
        return

    ensure_user(update.effective_user)

    admin_actions = {
        "👥 Usuarios": "admin_users", "📥 Depósitos": "admin_deposits",
        "📤 Retiros": "admin_withdrawals", "📈 Inversiones": "admin_investments",
        "💾 Crear respaldo": "admin_backup", "♻️ Restaurar respaldo": "admin_restore",
        "🔒 Bloquear bot": "admin_lock", "🔓 Desbloquear bot": "admin_unlock",
        "💰 Pago Diario": "admin_profit", "📊 Cuotas Diarias": "admin_quotas", "📊 Ganancias Diarias": "admin_daily_gains",
        "📊 Estado": "admin_status",
        "📢 Enviar mensaje": "admin_broadcast",
    }
    user_actions = {
        "👤 Mi cuenta": "user_account", "📈 Inversiones": "user_invest",
        "🤝 Referidos": "user_referrals", "📜 Historial": "user_history",
        "ℹ️ Información": "user_info", "💰 Planes de Inversión": "user_plans",
        "🔄 Reinvertir saldo": "user_reinvest", "📊 Ganancias Diarias": "user_daily_gains", "🆘 Soporte": "user_support",
    }

    # El teclado inferior funciona como panel fijo.
    if is_admin(update.effective_user.id) and text in admin_actions:
        await handle_text_panel_action(update, context, admin_actions[text])
        return
    if text in user_actions:
        if is_admin(update.effective_user.id):
            await send_admin_menu(update.effective_chat.id, context, "👑 *PANEL DE ADMINISTRACIÓN*\n\nEste usuario tiene acceso exclusivamente al panel administrativo.")
            return
        await handle_text_panel_action(update, context, user_actions[text])
        return
    if text == "💰 Depositar":
        context.user_data.clear()
        await update.message.reply_text(
            "💰 *NUEVO DEPÓSITO*\n\n"
            "🟢 *WALLET DE DEPÓSITO*\n"
            f"`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
            "Selecciona un plan y el monto del depósito quedará fijado automáticamente.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Ver Planes de Inversión", callback_data="user_plans")],
                [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
            ])
        )
        return
    if text == "💸 Retirar":
        can_withdraw, remaining = withdrawal_wait_info(update.effective_user.id)
        if not can_withdraw:
            await update.message.reply_text(
                "⏳ *RETIRO SEMANAL*\n\n"
                f"Ya realizaste una solicitud de retiro.\nPodrás solicitar otro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
            )
            return
        context.user_data.clear()
        context.user_data["manual_flow"] = "withdraw_amount"
        row = get_user(update.effective_user.id)
        balance = float(row["saldo"]) if row else 0
        await update.message.reply_text(
            "💸 *SOLICITAR RETIRO*\n\n"
            f"Saldo disponible: *{money(balance)} USDT*\n"
            f"Mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
            "Frecuencia: *1 retiro cada 7 días*\n\n"
            "Escribe ahora el monto que deseas retirar.",
            parse_mode="Markdown"
        )
        return

    flow = context.user_data.get("manual_flow")
    if flow == "deposit_tx_fixed":
        if len(text) < 5:
            await update.message.reply_text("⚠️ El TXID parece demasiado corto. Envíalo nuevamente.")
            return
        context.user_data["deposit_tx"] = text
        context.user_data["manual_flow"] = "deposit_photo"
        await update.message.reply_text(
            "📸 Ahora envía una *captura del comprobante*.\n\nSi no puedes enviar captura, escribe /skip.",
            parse_mode="Markdown"
        )
        return
    if flow == "deposit_amount":
        context.user_data["deposit_amount"] = None
        try:
            amount = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("⚠️ Introduce solamente un número.")
            return
        if amount <= 0:
            await update.message.reply_text("⚠️ El monto debe ser mayor que 0.")
            return
        context.user_data["deposit_amount"] = amount
        context.user_data["manual_flow"] = "deposit_tx"
        await update.message.reply_text("🔗 Ahora envía el *hash de la transacción (TXID)*.", parse_mode="Markdown")
        return
    if flow == "deposit_tx":
        if len(text) < 5:
            await update.message.reply_text("⚠️ El TXID parece demasiado corto. Envíalo nuevamente.")
            return
        context.user_data["deposit_tx"] = text
        context.user_data["manual_flow"] = "deposit_photo"
        await update.message.reply_text(
            "📸 Ahora envía una *captura del comprobante*.\n\nSi no puedes enviar captura, escribe /skip.",
            parse_mode="Markdown"
        )
        # La captura será atendida por el ConversationHandler cuando se use /depositar;
        # para el flujo del teclado aceptamos también la siguiente foto en photo_text_handler.
        return
    if flow == "withdraw_amount":
        try:
            amount = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("⚠️ Introduce un monto válido.")
            return
        if amount < MIN_WITHDRAWAL:
            await update.message.reply_text(
                f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.",
                parse_mode="Markdown"
            )
            return
        can_withdraw, remaining = withdrawal_wait_info(update.effective_user.id)
        if not can_withdraw:
            await update.message.reply_text(
                "⏳ *RETIRO SEMANAL*\n\n"
                f"Ya realizaste una solicitud de retiro.\n"
                f"Podrás solicitar otro retiro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
                parse_mode="Markdown"
            )
            return
        balance = user_balance(update.effective_user.id)
        if amount > balance:
            await update.message.reply_text(f"⚠️ Saldo insuficiente.\nDisponible: {money(balance)} USDT")
            return
        row = get_user(update.effective_user.id)
        registered_wallet = (row["wallet_retiro"] or "").strip() if row else ""
        if not registered_wallet:
            await update.message.reply_text(
                "⚠️ No tienes una wallet de retiro registrada. Completa tu registro antes de solicitar un retiro."
            )
            context.user_data.clear()
            return
        context.user_data["withdraw_amount"] = amount
        await finish_withdraw_manual(update, context, registered_wallet)
        return
    if flow == "withdraw_address":
        # Compatibilidad con sesiones antiguas: nunca se solicita una wallet nueva.
        row = get_user(update.effective_user.id)
        registered_wallet = (row["wallet_retiro"] or "").strip() if row else ""
        context.user_data.pop("manual_flow", None)
        if not registered_wallet:
            await update.message.reply_text("⚠️ No tienes una wallet de retiro registrada. Completa tu registro.")
            context.user_data.clear()
            return
        context.user_data["withdraw_amount"] = context.user_data.get("withdraw_amount", 0)
        await finish_withdraw_manual(update, context, registered_wallet)
        return


async def handle_text_panel_action(update, context, action):
    # Los botones inferiores del administrador muestran el mismo resumen
    # general que sus respectivos botones inline.
    if action == "admin_users":
        conn = db()
        count = conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
        with_deposit = conn.execute("SELECT COUNT(*) c FROM usuarios WHERE total_depositado > 0").fetchone()["c"]
        total = conn.execute("SELECT COALESCE(SUM(total_depositado),0) s FROM usuarios").fetchone()["s"]
        balance = conn.execute("SELECT COALESCE(SUM(saldo),0) s FROM usuarios").fetchone()["s"]
        invested = conn.execute("SELECT COALESCE(SUM(invertido),0) s FROM usuarios").fetchone()["s"]
        earnings = conn.execute("SELECT COALESCE(SUM(ganancias),0) s FROM usuarios").fetchone()["s"]
        withdrawn = conn.execute("SELECT COALESCE(SUM(total_retirado),0) s FROM usuarios").fetchone()["s"]
        conn.close()
        await update.message.reply_text(
            "👥 *RESUMEN DE USUARIOS*\n\n"
            f"👤 Usuarios registrados: *{count}*\n"
            f"💰 Usuarios que han depositado: *{with_deposit}*\n\n"
            f"📥 Total depositado: *{money(total)} USDT*\n"
            f"💵 Saldo disponible: *{money(balance)} USDT*\n"
            f"📈 Capital invertido: *{money(invested)} USDT*\n"
            f"🎁 Ganancias acumuladas: *{money(earnings)} USDT*\n"
            f"💸 Total retirado: *{money(withdrawn)} USDT*",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if action == "admin_deposits":
        conn = db()
        total_count = conn.execute("SELECT COUNT(*) c FROM depositos").fetchone()["c"]
        ac, aa = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM depositos WHERE estado='aprobado'").fetchone()
        pc, pa = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM depositos WHERE estado='pendiente'").fetchone()
        rc, ra = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM depositos WHERE estado='rechazado'").fetchone()
        conn.close()
        await update.message.reply_text(
            "📥 *RESUMEN DE DEPÓSITOS*\n\n"
            f"📊 Total de solicitudes: *{total_count}*\n"
            f"✅ Aprobados: *{ac}* — *{money(aa)} USDT*\n"
            f"⏳ Pendientes: *{pc}* — *{money(pa)} USDT*\n"
            f"❌ Rechazados: *{rc}* — *{money(ra)} USDT*",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar por ID", callback_data="admin_deposit_user")],[InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]])
        )
        return

    if action == "admin_withdrawals":
        conn = db()
        total_count = conn.execute("SELECT COUNT(*) c FROM retiros").fetchone()["c"]
        pc, pa = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM retiros WHERE estado='pendiente'").fetchone()
        ac, aa = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM retiros WHERE estado='aprobado'").fetchone()
        rc, ra = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(monto),0) s FROM retiros WHERE estado='rechazado'").fetchone()
        conn.close()
        await update.message.reply_text(
            "📤 *RESUMEN DE RETIROS*\n\n"
            f"📊 Total de solicitudes: *{total_count}*\n"
            f"⏳ Pendientes: *{pc}* — *{money(pa)} USDT*\n"
            f"✅ Aprobados: *{ac}* — *{money(aa)} USDT*\n"
            f"❌ Rechazados: *{rc}* — *{money(ra)} USDT*\n"
            f"💸 Total retirado aprobado: *{money(aa)} USDT*",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar por ID", callback_data="admin_withdraw_user")],[InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]])
        )
        return

    if action == "admin_investments":
        conn = db()
        active_count = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]
        active_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
        total_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones").fetchone()["s"]
        profit = conn.execute("SELECT COALESCE(SUM(ganancia_acumulada),0) s FROM inversiones").fetchone()["s"]
        finished = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='finalizada'").fetchone()["c"]
        conn.close()
        await update.message.reply_text(
            "📈 *RESUMEN DE INVERSIONES*\n\n"
            f"🟢 Inversiones activas: *{active_count}*\n"
            f"💰 Capital actualmente invertido: *{money(active_capital)} USDT*\n"
            f"📊 Capital invertido histórico: *{money(total_capital)} USDT*\n"
            f"🎁 Ganancias acumuladas: *{money(profit)} USDT*\n"
            f"🏁 Planes finalizados: *{finished}*\n"
            "📅 Rendimiento diario: *variable* según la cuota seleccionada por el administrador.\n"
            "🎯 Objetivo por plan: 200% del capital inicial",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Consultar por ID", callback_data="admin_invest_user")],[InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]])
        )
        return

    if action == "admin_daily_gains":
        class TQ:
            def __init__(self, message, user): self.message=message; self.from_user=user
            async def edit_message_text(self,*args,**kwargs): return await update.message.reply_text(*args,**kwargs)
            async def answer(self,*args,**kwargs): return None
        await show_admin_daily_gains(TQ(update.message, update.effective_user))
        return

    if action == "admin_backup":
        await send_full_backup(context.bot, "Respaldo solicitado por el administrador")
        await update.message.reply_text("💾 Respaldo completo enviado: `.db` y `.xlsx`.", parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    if action == "admin_status":
        conn = db()
        users = conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
        deposits = conn.execute("SELECT COUNT(*) c FROM depositos WHERE estado='pendiente'").fetchone()["c"]
        withdrawals = conn.execute("SELECT COUNT(*) c FROM retiros WHERE estado='pendiente'").fetchone()["c"]
        investments = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]
        conn.close()
        await update.message.reply_text(
            "📊 *ESTADO DEL SISTEMA*\n\n"
            f"👥 Usuarios: *{users}*\n"
            f"📥 Depósitos pendientes: *{deposits}*\n"
            f"📤 Retiros pendientes: *{withdrawals}*\n"
            f"📈 Inversiones activas: *{investments}*\n"
            "📊 Rendimiento diario: *variable*\n"
            f"🛠️ Mantenimiento: *{'ACTIVO' if is_maintenance() else 'INACTIVO'}*",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if action == "admin_profit":
        buttons=[]; row_buttons=[]
        for rate in DAILY_QUOTA_OPTIONS:
            row_buttons.append(InlineKeyboardButton(DAILY_QUOTA_LABELS[rate], callback_data=f"calcquota_{int(round(rate*100)):02d}"))
            if len(row_buttons) == 4:
                buttons.append(row_buttons); row_buttons=[]
        if row_buttons:
            buttons.append(row_buttons)
        buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")])
        await update.message.reply_text(
            "💰 *PAGO DIARIO — CONSULTA*\n\n"
            "Selecciona la cuota que deseas utilizar únicamente para calcular cuánto correspondería pagar hoy sobre todo el capital invertido.\n\n"
            "ℹ️ Esta opción es *solo informativa*: no acredita fondos, no modifica saldos y no envía imágenes.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "admin_quotas":
        buttons=[]; row=[]
        for rate in DAILY_QUOTA_OPTIONS:
            row.append(InlineKeyboardButton(DAILY_QUOTA_LABELS[rate], callback_data=f"quota_{int(round(rate*100)):02d}"))
            if len(row)==4:
                buttons.append(row); row=[]
        if row: buttons.append(row)
        current=get_current_quota_decimal()
        await update.message.reply_text(
            "📊 *CUOTAS DIARIAS*\n\n"
            "Selecciona la cuota que vas a pagar hoy. Se acreditará a todas las inversiones activas y se enviará la imagen correspondiente.\n\n"
            f"Cuota actual: *{quota_label(current) if current else 'No seleccionada'}*",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "admin_restore":
        context.user_data["await_restore_db"] = True
        await update.message.reply_text("♻️ *RESTAURAR RESPALDO*\n\nEnvía ahora el archivo `.db` de respaldo. Primero se creará un respaldo de seguridad de la base actual.\n\n/cancelar para cancelar.", parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    if action == "admin_lock":
        if is_maintenance():
            await update.message.reply_text("🔒 El bot ya está bloqueado.", reply_markup=admin_keyboard()); return
        # El respaldo se crea ANTES de activar el mantenimiento, para conservar
        # exactamente el estado al momento de bloquear. Si falla, no se bloquea.
        try:
            await create_and_send_lock_backup(context.application)
        except Exception as e:
            await update.message.reply_text(
                f"❌ *NO SE PUDO CREAR EL RESPALDO*\n\nEl bot NO fue bloqueado para evitar riesgos.\n\nError: `{e}`",
                parse_mode="Markdown", reply_markup=admin_keyboard()
            )
            return
        set_maintenance(True)
        sent=failed=0
        conn=db(); rows=conn.execute("SELECT telegram_id FROM usuarios WHERE telegram_id != ?",(ADMIN_TELEGRAM_ID,)).fetchall(); conn.close()
        for row in rows:
            try:
                if await send_image_to_user(context.bot, row["telegram_id"], LOCK_IMAGE):
                    sent += 1
                else:
                    failed += 1
            except Exception as e: failed+=1; print(e)
        await update.message.reply_text(
            f"🔒 *BOT BLOQUEADO*\n\n"
            "💾 Se creó y envió automáticamente el respaldo del momento del bloqueo.\n"
            f"\nUsuarios notificados: {sent}\nNo enviados: {failed}\n\n"
            "Al pulsar *Desbloquear bot* se te pedirá restaurar este respaldo.",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if action == "admin_unlock":
        if not is_maintenance():
            await update.message.reply_text("🔓 El bot ya está desbloqueado.", reply_markup=admin_keyboard()); return
        lock_path = get_system_value("lock_backup_db", "")
        if lock_path:
            await update.message.reply_text(
                "🔓 *DESBLOQUEAR BOT*\n\n"
                "Antes de desbloquear, debes decidir qué hacer con el respaldo creado al bloquear.\n\n"
                "♻️ *Restaurar respaldo del bloqueo*: vuelve la base exactamente al estado que tenía cuando bloqueaste el bot.\n\n"
                "⚠️ Esto descartará los cambios realizados después del bloqueo.\n\n"
                "🔓 *Desbloquear sin restaurar*: conserva los cambios realizados durante el bloqueo.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("♻️ Restaurar respaldo del bloqueo", callback_data="admin_unlock_restore")],
                    [InlineKeyboardButton("🔓 Desbloquear sin restaurar", callback_data="admin_unlock_no_restore")],
                    [InlineKeyboardButton("⬅️ Cancelar", callback_data="admin_home")],
                ])
            )
        else:
            await update.message.reply_text(
                "⚠️ No se encontró el respaldo local del último bloqueo.\n\n"
                "Puedes desbloquear sin restaurar o cancelar y usar ♻️ Restaurar respaldo para subir el `.db` que recibiste.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔓 Desbloquear sin restaurar", callback_data="admin_unlock_no_restore")],
                    [InlineKeyboardButton("⬅️ Cancelar", callback_data="admin_home")],
                ])
            )
        return

    if action == "admin_broadcast":
        context.user_data["admin_broadcast"] = True
        await update.message.reply_text(
            "📢 *ENVIAR MENSAJE A TODOS*\n\n"
            "Escribe ahora el mensaje que quieres enviar a todos los usuarios registrados.\n\n"
            "Puedes cancelar usando el botón ❌ Cancelar.",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    # Acciones de usuario: nunca se ejecutan para el administrador.
    if is_admin(update.effective_user.id):
        await send_admin_menu(update.effective_chat.id, context, "👑 *PANEL DE ADMINISTRACIÓN*\n\nEste usuario tiene acceso exclusivamente al panel administrativo.")
        return

    class FakeQuery:
        def __init__(self, message, user):
            self.message = message
            self.from_user = user
        async def edit_message_text(self, *args, **kwargs):
            return await update.message.reply_text(*args, **kwargs)
        async def answer(self, *args, **kwargs):
            return None
    fake = FakeQuery(update.message, update.effective_user)
    if action == "user_account": await show_account(fake)
    elif action == "user_invest": await show_investments(fake)
    elif action == "user_plans": await show_plans(fake)
    elif action == "user_referrals": await show_referrals(fake, context)
    elif action == "user_history": await show_history(fake)
    elif action == "user_info": await show_info(fake)
    elif action == "user_reinvest": await show_reinvest(fake)
    elif action == "user_daily_gains": await show_user_daily_gains(fake)
    elif action == "user_support": await show_support(fake, context)


async def finish_withdraw_manual(update, context, address):
    user_id = update.effective_user.id
    amount = float(context.user_data.get("withdraw_amount", 0))
    if amount < MIN_WITHDRAWAL:
        context.user_data["manual_flow"] = "withdraw_amount"
        await update.message.reply_text(f"⚠️ El retiro mínimo es de *{money(MIN_WITHDRAWAL)} USDT*.", parse_mode="Markdown")
        return
    can_withdraw, remaining = withdrawal_wait_info(user_id)
    if not can_withdraw:
        context.user_data.clear()
        await update.message.reply_text(
            "⏳ *RETIRO SEMANAL*\n\n"
            f"Ya realizaste una solicitud de retiro.\nPodrás solicitar otro en aproximadamente *{withdrawal_wait_text(remaining)}*.",
            parse_mode="Markdown"
        )
        return
    if len(address) < 20:
        context.user_data["manual_flow"] = "withdraw_address"
        await update.message.reply_text("⚠️ La dirección parece inválida. Envíala nuevamente.")
        return
    conn = db()
    row = conn.execute("SELECT saldo, ganancias_disponibles FROM usuarios WHERE telegram_id=?", (user_id,)).fetchone()
    if not row or float(row["ganancias_disponibles"]) < amount:
        conn.close()
        await update.message.reply_text(f"⚠️ Solo puedes retirar ganancias disponibles: {money(float(row['ganancias_disponibles']) if row else 0)} USDT.")
        return
    conn.execute("UPDATE usuarios SET saldo=saldo-?, ganancias_disponibles=ganancias_disponibles-? WHERE telegram_id=?", (amount,amount,user_id))
    cur = conn.execute("INSERT INTO retiros (telegram_id,monto,direccion,estado,fecha) VALUES (?,?,?,'pendiente',?)", (user_id,amount,address,now_iso()))
    wid = cur.lastrowid
    conn.commit()
    conn.close()
    add_movement(user_id,"retiro_pendiente",amount,f"Retiro #{wid} enviado para revisión")
    await update.message.reply_text(
        "⏳ *RETIRO SOLICITADO*\n\n"
        f"ID: `{wid}`\nMonto: *{money(amount)} USDT*\nRed: *TRC20*\nDirección: `{address}`\n\n"
        "El administrador revisará y procesará la solicitud.", parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
    )
    withdrawal_buttons = []
    if admin_wallet_url():
        withdrawal_buttons.append([InlineKeyboardButton("💼 Abrir mi wallet", url=admin_wallet_url())])
    withdrawal_buttons.append([InlineKeyboardButton("✅ Aprobar", callback_data=f"wd_approve_{wid}"), InlineKeyboardButton("❌ Rechazar", callback_data=f"wd_reject_{wid}")])
    kb=InlineKeyboardMarkup(withdrawal_buttons)
    try:
        await context.bot.send_message(chat_id=ADMIN_TELEGRAM_ID,text=("📤 *NUEVO RETIRO PENDIENTE*\n\n" f"ID: `{wid}`\nUsuario: `{user_id}`\nMonto: *{money(amount)} USDT*\nDirección TRC20:\n`{address}`"),parse_mode="Markdown",reply_markup=kb)
    except Exception as e:
        print(f"Error notificando retiro: {e}")
    context.user_data.clear()

# =========================================================
# RESPALDO EXCEL AUTOMÁTICO
# =========================================================

def create_excel_backup():
    """Crea el respaldo Excel con los datos de registro al principio de cada hoja de datos."""
    conn = db()
    wb = Workbook()
    wb.remove(wb.active)
    tables = ["usuarios", "depositos", "retiros", "inversiones", "movimientos", "referidos", "pagos_diarios"]
    identity_headers = ["Nombre", "Nombre de usuario", "ID Telegram", "Teléfono", "Correo"]

    # Para cada tabla obtenemos las columnas reales de SQLite y conservamos todos
    # sus datos originales después de las cinco columnas de identidad.
    for table in tables:
        ws = wb.create_sheet(table[:31])
        columns = [d[1] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()

        # En tablas con telegram_id, esa es la persona de la fila. En referidos,
        # usamos referido_id como la persona principal de la fila. pagos_diarios
        # no pertenece a un usuario, por lo que las cinco columnas quedan vacías.
        identity_mode = "telegram_id" if "telegram_id" in columns else ("referido_id" if table == "referidos" else None)
        data_columns = [c for c in columns if c != "telegram_id"]
        # Evitamos duplicar referido_id en la zona de datos cuando ya se utilizó
        # como ID Telegram de identidad; referidor_id y el resto se conservan.
        if table == "referidos":
            data_columns = [c for c in columns if c != "referido_id"]

        ordered = identity_headers + data_columns
        ws.append(ordered)

        for row in rows:
            telegram_id = row[identity_mode] if identity_mode else None
            identity = {"Nombre": "", "Nombre de usuario": "", "ID Telegram": telegram_id or "", "Teléfono": "", "Correo": ""}
            if telegram_id:
                u = conn.execute(
                    "SELECT nombre, username, telefono, email FROM usuarios WHERE telegram_id=?",
                    (telegram_id,)
                ).fetchone()
                if u:
                    identity["Nombre"] = u["nombre"] or ""
                    identity["Nombre de usuario"] = (f"@{u['username']}" if u["username"] else "")
                    identity["Teléfono"] = u["telefono"] or ""
                    identity["Correo"] = u["email"] or ""

            values = [identity[h] for h in identity_headers]
            values.extend(row[c] for c in data_columns)
            ws.append(values)

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col_cells in ws.columns:
            max_len = max([len(str(c.value or "")) for c in list(col_cells)[:200]] + [12])
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 40)

    ws = wb.create_sheet("resumen")
    active = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
    daily = float(active) * get_current_quota_decimal()
    ws.append(["Indicador", "Valor"])
    ws.append(["Fecha UTC", now_iso()])
    ws.append(["Cuota diaria actual", get_current_quota_decimal() if get_current_quota_decimal() else "Sin seleccionar"])
    ws.append(["Capital activo invertido (USDT)", float(active)])
    ws.append(["Pago diario estimado con cuota actual (USDT)", daily])
    ws.append(["Ganancias disponibles para retiro (USDT)", float(conn.execute("SELECT COALESCE(SUM(ganancias_disponibles),0) s FROM usuarios").fetchone()["s"])])
    conn.close()
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output

async def send_excel_backup(bot, reason="Respaldo automático"):
    filename = f"respaldo_inversion_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.xlsx"
    output = create_excel_backup()
    await bot.send_document(
        chat_id=ADMIN_TELEGRAM_ID,
        document=InputFile(output, filename=filename),
        caption=f"💾 {reason}\n📊 Respaldo Excel completo (.xlsx)."
    )

async def send_db_backup(bot, reason="Respaldo de base de datos", filename=None):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    filename = filename or f"database_backup_{stamp}.db"
    path = os.path.join(BACKUP_DIR, filename)
    conn = db()
    try:
        backup_conn = sqlite3.connect(path)
        conn.backup(backup_conn)
        backup_conn.close()
    finally:
        conn.close()
    with open(path, "rb") as f:
        data = f.read()
    stream = BytesIO(data)
    stream.name = filename
    await bot.send_document(
        chat_id=ADMIN_TELEGRAM_ID,
        document=InputFile(stream, filename=filename),
        caption=f"💾 {reason}\n🗄️ Base SQLite completa (.db) para restauración."
    )
    return path


async def send_full_backup(bot, reason="Respaldo solicitado", db_filename=None):
    # SIEMPRE se envían DOS archivos independientes: primero .db y luego .xlsx.
    db_path = await send_db_backup(bot, reason + " — base SQLite .db", filename=db_filename)
    await send_excel_backup(bot, reason + " — Excel .xlsx")
    return db_path


async def backup_after_accreditation(application, source):
    try:
        await send_full_backup(
            application.bot,
            f"Respaldo posterior a la acreditación {source}"
        )
        print(f"💾 Respaldo posterior a acreditación enviado al administrador ({source}).")
    except Exception as e:
        print(f"❌ Error enviando respaldo posterior a acreditación ({source}): {e}")


async def create_and_send_lock_backup(application):
    """Crea y envía el respaldo EXACTO del estado anterior al bloqueo.
    El .db se conserva localmente con un nombre estable para poder restaurarlo
    desde el botón Desbloquear Bot.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    filename = "lock_backup.db"
    path = await send_db_backup(
        application.bot,
        "Respaldo automático creado al bloquear el bot — base SQLite .db",
        filename=filename
    )
    await send_excel_backup(
        application.bot,
        "Respaldo automático creado al bloquear el bot — Excel .xlsx"
    )
    set_system_value("lock_backup_db", path)
    set_system_value("lock_backup_created_at", now_iso())
    return path


async def restore_lock_backup(application):
    path = get_system_value("lock_backup_db", "")
    if not path:
        raise FileNotFoundError("No existe un respaldo asociado al último bloqueo.")
    if not os.path.exists(path):
        raise FileNotFoundError("El respaldo del bloqueo ya no está disponible en el servidor. Puedes usar ♻️ Restaurar respaldo para subir el archivo .db que recibiste.")

    temp = os.path.join(BACKUP_DIR, "lock_restore_temp.db")
    shutil.copy2(path, temp)
    try:
        test = sqlite3.connect(temp)
        integrity = test.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in test.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        test.close()
        required = {"usuarios", "depositos", "retiros", "inversiones", "movimientos", "referidos"}
        if integrity != "ok" or not required.issubset(tables):
            raise ValueError("El respaldo del bloqueo no es una base válida del sistema.")

        # Restaurar directamente el respaldo creado al momento de bloquear.
        # No se crea otro respaldo aquí: el respaldo del bloqueo es el punto
        # exacto al que el administrador decidió volver.
        conn = db(); conn.close()
        shutil.copy2(temp, DB_FILE)
        init_db()
        return True
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


async def automatic_profit_loop(application):
    """Respaldo automático diario: a las 18:00 acredita la cuota mínima si aún no se pagó hoy."""
    AUTO_QUOTA_RATE = min(DAILY_QUOTA_OPTIONS) / 100.0  # 0,25%
    while True:
        try:
            tz = ZoneInfo(PROFIT_TIMEZONE)
        except Exception:
            print(f"⚠️ Zona horaria inválida: {PROFIT_TIMEZONE}. Se usará UTC.")
            tz = timezone.utc
        try:
            now = datetime.now(tz)
            # Solo se acredita de lunes a viernes y nunca antes de las 18:00.
            if now.weekday() < 5 and (now.hour, now.minute) >= (18, 0):
                processed, total, credited, already, details_or_status = process_daily_quota(AUTO_QUOTA_RATE)
                if not already and details_or_status != "weekend":
                    set_current_quota(AUTO_QUOTA_RATE)
                    notified = await send_daily_quota_notifications(
                        application, credited, AUTO_QUOTA_RATE, details_or_status
                    ) if credited else 0
                    # Respaldo independiente de la acreditación automática de las 18:00.
                    await backup_after_accreditation(application, "automática de las 18:00")
                    print(
                        f"🤖 Acreditación automática 18:00: cuota {quota_label(AUTO_QUOTA_RATE)}, "
                        f"inversiones={processed}, total={total:.2f}, usuarios={notified}"
                    )
        except Exception as e:
            print(f"❌ Error en acreditación automática de las 18:00: {e}")
        # Revisa periódicamente para cubrir también reinicios del bot después de las 18:00.
        await asyncio.sleep(30)


async def automatic_backup_loop(application):
    while True:
        try:
            tz = ZoneInfo(BACKUP_TIMEZONE)
        except Exception:
            print(f"⚠️ Zona horaria inválida: {BACKUP_TIMEZONE}. Se usará UTC.")
            tz = timezone.utc
        now = datetime.now(tz)
        try:
            hour, minute = [int(x) for x in BACKUP_TIME.split(":", 1)]
        except Exception:
            hour, minute = 6, 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = max(1, (target - now).total_seconds())
        print(f"💾 Próximo respaldo automático: {target.isoformat()}")
        await asyncio.sleep(wait_seconds)
        try:
            await send_full_backup(application.bot, "Respaldo automático diario")
            await send_admin_menu(ADMIN_TELEGRAM_ID, application, "✅ *RESPALDO AUTOMÁTICO ENVIADO*\\n\\nPanel administrativo:")
        except Exception as e:
            print(f"❌ Error en respaldo automático: {e}")


# =========================================================
# COMANDOS ADMIN
# =========================================================

async def manual_deposit_photo(update, context):
    if not private_only(update):
        return
    if is_admin(update.effective_user.id) and context.user_data.get("withdraw_admin_action") == "approve_photo":
        wid=int(context.user_data.get("withdraw_admin_id",0)); msg=context.user_data.get("withdraw_admin_message",""); photo=update.message.photo[-1].file_id
        conn=db(); row=conn.execute("SELECT * FROM retiros WHERE id=? AND estado='pendiente'",(wid,)).fetchone()
        if not row: conn.close(); context.user_data.clear(); await update.message.reply_text("⚠️ Retiro no disponible.",reply_markup=admin_keyboard()); return
        conn.execute("UPDATE retiros SET estado='aprobado',fecha_revision=?,revisado_por=? WHERE id=?",(now_iso(),update.effective_user.id,wid)); conn.execute("UPDATE usuarios SET total_retirado=total_retirado+? WHERE telegram_id=?",(row['monto'],row['telegram_id'])); conn.commit(); conn.close(); add_movement(row['telegram_id'],'retiro',row['monto'],f'Retiro #{wid} aprobado')
        context.user_data.clear(); await update.message.reply_text(f"✅ Retiro #{wid} aprobado y comprobante enviado.",reply_markup=admin_keyboard())
        try:
            await context.bot.send_message(chat_id=row['telegram_id'],text=f"✅ *RETIRO APROBADO*\n\nSolicitud #{wid}.\nMonto: *{money(row['monto'])} USDT*\n\n{msg}",parse_mode="Markdown")
            await context.bot.send_photo(chat_id=row['telegram_id'],photo=photo,caption=f"📸 Comprobante del retiro #{wid}")
            await send_image_to_user(context.bot, row['telegram_id'], WITHDRAW_SENT_IMAGE)
        except Exception as e: print(e)
        return
    if context.user_data.get("manual_flow") != "deposit_photo":
        return
    photo = update.message.photo[-1]
    context.user_data["deposit_photo"] = photo.file_id
    await finish_deposit(update, context)


async def skip_manual_deposit_photo(update, context):
    if context.user_data.get("deposit_amount") and context.user_data.get("deposit_tx") and context.user_data.get("manual_flow") == "deposit_photo":
        context.user_data["deposit_photo"] = ""
        await finish_deposit(update, context)


async def backup_command(update, context):
    if not private_only(update) or not is_admin(update.effective_user.id):
        return
    try:
        await send_full_backup(context.bot,"Respaldo solicitado por el administrador")
        await send_admin_menu(ADMIN_TELEGRAM_ID,context,"✅ *RESPALDO COMPLETO CREADO*\n\nSe enviaron `.db` y `.xlsx`.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error creando respaldo: `{e}`",parse_mode="Markdown")


async def cancel_admin_restore(update, context):
    if is_admin(update.effective_user.id):
        context.user_data.pop("await_restore_db",None); await update.message.reply_text("❌ Restauración cancelada.",reply_markup=admin_keyboard())

async def restore_database_document(update, context):
    if not private_only(update) or not is_admin(update.effective_user.id) or not context.user_data.get("await_restore_db"):
        return
    doc=update.message.document
    if not doc or not doc.file_name.lower().endswith(".db"):
        await update.message.reply_text("⚠️ Envía un archivo SQLite con extensión `.db`.",parse_mode="Markdown"); return
    os.makedirs(BACKUP_DIR,exist_ok=True); temp=os.path.join(BACKUP_DIR,"restore_temp.db")
    try:
        tgfile=await doc.get_file(); await tgfile.download_to_drive(temp)
        test=sqlite3.connect(temp); test.execute("PRAGMA integrity_check"); tables={r[0] for r in test.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}; test.close()
        required={"usuarios","depositos","retiros","inversiones","movimientos","referidos"}
        if not required.issubset(tables): raise ValueError("La base no contiene las tablas requeridas del sistema.")
        await send_db_backup(context.bot,"Respaldo de seguridad antes de restaurar")
        context.user_data.pop("await_restore_db",None)
        conn=db(); conn.close()
        shutil.copy2(DB_FILE, os.path.join(BACKUP_DIR,f"before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"))
        shutil.copy2(temp,DB_FILE)
        init_db()
        await update.message.reply_text("✅ *BASE DE DATOS RESTAURADA*\n\nSe conservó un respaldo de seguridad de la base anterior.",parse_mode="Markdown",reply_markup=admin_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudo restaurar la base: `{e}`",parse_mode="Markdown",reply_markup=admin_keyboard())
    finally:
        try: os.remove(temp)
        except OSError: pass

async def status_command(update, context):
    if not private_only(update):
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return

    conn = db()
    users = conn.execute("SELECT COUNT(*) AS c FROM usuarios").fetchone()["c"]
    deposits = conn.execute(
        "SELECT COUNT(*) AS c FROM depositos WHERE estado='pendiente'"
    ).fetchone()["c"]
    withdrawals = conn.execute(
        "SELECT COUNT(*) AS c FROM retiros WHERE estado='pendiente'"
    ).fetchone()["c"]
    conn.close()

    await update.message.reply_text(
        "📊 *ESTADO*\n\n"
        f"Usuarios: *{users}*\n"
        f"Depósitos pendientes: *{deposits}*\n"
        f"Retiros pendientes: *{withdrawals}*",
        parse_mode="Markdown"
    )


# =========================================================
# ERRORES
# =========================================================

async def error_handler(update, context):
    print(f"❌ Error: {context.error}")


# =========================================================
# MAIN
# =========================================================

async def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Falta la variable de entorno TELEGRAM_TOKEN."
        )

    if ADMIN_TELEGRAM_ID == 0:
        raise RuntimeError(
            "Falta la variable de entorno ADMIN_TELEGRAM_ID."
        )

    init_db()
    await start_web_server()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Obtener username del bot para construir links de referido.
    try:
        me = await app.bot.get_me()
        app.bot_data["bot_username"] = me.username or ""
        print(f"🤖 Bot: @{me.username}")
    except Exception as e:
        print(f"⚠️ No se pudo obtener username del bot: {e}")
        app.bot_data["bot_username"] = ""

    # -------------------------------
    # /start
    # -------------------------------
    app.add_handler(
        CommandHandler("start", start_command, filters=filters.ChatType.PRIVATE)
    )

    # -------------------------------
    # Admin
    # -------------------------------
    app.add_handler(
        CommandHandler("admin", admin_command, filters=filters.ChatType.PRIVATE)
    )
    app.add_handler(
        CommandHandler("backup", backup_command, filters=filters.ChatType.PRIVATE)
    )
    app.add_handler(
        CommandHandler("status", status_command, filters=filters.ChatType.PRIVATE)
    )
    app.add_handler(CommandHandler("cancelar", cancel_admin_restore, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, restore_database_document), group=0)

    # -------------------------------
    # Depósitos
    # -------------------------------
    deposit_handler = ConversationHandler(
        entry_points=[
            CommandHandler(
                "depositar",
                start_deposit,
                filters=filters.ChatType.PRIVATE
            )
        ],
        states={
            DEP_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_deposit_amount
                )
            ],
            DEP_TX: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_deposit_tx
                )
            ],
            DEP_PHOTO: [
                MessageHandler(
                    filters.PHOTO & filters.ChatType.PRIVATE,
                    receive_deposit_photo
                ),
                CommandHandler(
                    "skip",
                    skip_deposit_photo,
                    filters=filters.ChatType.PRIVATE
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancelar",
                cancel_deposit,
                filters=filters.ChatType.PRIVATE
            )
        ],
        allow_reentry=True,
    )
    app.add_handler(deposit_handler)

    # -------------------------------
    # Retiros
    # -------------------------------
    withdraw_handler = ConversationHandler(
        entry_points=[
            CommandHandler(
                "retirar",
                start_withdraw,
                filters=filters.ChatType.PRIVATE
            )
        ],
        states={
            WITHDRAW_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_withdraw_amount
                )
            ],
            WITHDRAW_ADDRESS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_withdraw_address
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancelar",
                cancel_withdraw,
                filters=filters.ChatType.PRIVATE
            )
        ],
        allow_reentry=True,
    )
    app.add_handler(withdraw_handler)

    app.add_handler(CommandHandler("skip", skip_manual_deposit_photo, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, manual_deposit_photo))

    # -------------------------------
    # Botones
    # -------------------------------
    app.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    # -------------------------------
    # Texto normal
    # -------------------------------
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            private_text
        )
    )

    app.add_error_handler(error_handler)

    print("==============================================")
    print("🤖 BOT DE INVERSIÓN INICIADO")
    print(f"👑 ADMIN_TELEGRAM_ID = {ADMIN_TELEGRAM_ID}")
    print(f"💾 DB = {DB_FILE}")
    print(f"💰 Wallet TRC20 configurada = {bool(USDT_TRC20_ADDRESS)}")
    print("🔒 Funciones privadas: SOLO CHAT PRIVADO")
    print("💵 Pago de ganancias: MANUAL Y VARIABLE DESDE EL PANEL DE CUOTAS")
    print("==============================================")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    profit_task = asyncio.create_task(automatic_profit_loop(app))
    backup_task = asyncio.create_task(automatic_backup_loop(app))
    try:
        await asyncio.Event().wait()
    finally:
        profit_task.cancel()
        backup_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
