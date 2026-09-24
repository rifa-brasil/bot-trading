import os
import sqlite3
import shutil
import uuid
import asyncio
from io import BytesIO
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
DAILY_RATE = 0.005  # 0,5% diario
BACKUP_TIME = os.getenv("BACKUP_TIME", "06:00").strip()
BACKUP_TIMEZONE = os.getenv("BACKUP_TIMEZONE", "America/Sao_Paulo").strip()
TARGET_MULTIPLIER = float(os.getenv("TARGET_MULTIPLIER", "2.0"))
MIN_INVESTMENT = 50.0  # inversión mínima: 50 USDT
MIN_WITHDRAWAL = 15.0  # retiro mínimo: 15 USDT
WITHDRAWAL_INTERVAL_DAYS = 7
PROFIT_TIMEZONE = os.getenv("PROFIT_TIMEZONE", "America/Sao_Paulo").strip()
DAILY_PROFIT_IMAGE = Path(__file__).resolve().parent / "ganancia_diaria.jpg"

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
            fecha_registro TEXT NOT NULL
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
        cur.execute("ALTER TABLE usuarios ADD COLUMN ganancias_disponibles REAL NOT NULL DEFAULT 0")
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
    cur.execute("UPDATE inversiones SET tasa_diaria = ? WHERE estado = 'activa'", (DAILY_RATE,))
    conn.commit()
    conn.close()


# =========================================================
# UTILIDADES
# =========================================================

def money(value):
    return f"{float(value):,.2f}"


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
            SET nombre = ?, username = ?
            WHERE telegram_id = ?
        """, (
            user.full_name or "",
            user.username or "",
            user.id
        ))
        conn.commit()
        conn.close()
        return

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
            referido_por, fecha_registro
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user.id,
        user.full_name or "",
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

async def maintenance_guard(update):
    if is_admin(update.effective_user.id):
        return False
    if is_maintenance():
        if update.message:
            await update.message.reply_text("🔧 *BOT EN MANTENIMIENTO*\n\nEl sistema está temporalmente bloqueado. Intenta nuevamente más tarde.", parse_mode="Markdown")
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
        ["💎 Planes de Inversión"],
        ["💸 Retirar"],
        ["🤝 Referidos", "📜 Historial"],
        ["ℹ️ Información"],
    ], resize_keyboard=True, is_persistent=True)


def admin_keyboard():
    return ReplyKeyboardMarkup([
        ["👥 Usuarios", "📥 Depósitos"],
        ["📤 Retiros", "📈 Inversiones"],
        ["💾 Crear respaldo", "♻️ Restaurar respaldo"],
        ["🔒 Bloquear bot", "🔓 Desbloquear bot"],
        ["💰 Pago diario 0,5%", "💵 Pago manual"],
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

    ensure_user(user, start_ref)

    if is_admin(user.id):
        await send_admin_menu(
            user.id,
            context,
            "👑 *PANEL DE ADMINISTRACIÓN*\n\n"
            "Este usuario tiene acceso exclusivamente al panel administrativo."
        )
    else:
        await send_user_menu(
            user.id,
            context,
            f"👋 *Bienvenido, {user.first_name or 'usuario'}*\n\n"
            "Desde aquí puedes administrar tu cuenta, depositar, "
            "solicitar retiros, consultar inversiones y compartir tu enlace de referido."
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
        f"👤 Nombre: {row['nombre'] or '-'}\n"
        f"💰 Saldo disponible: *{money(row['saldo'])} USDT*\n"
        f"📥 Total depositado: *{money(total_depositado)} USDT*\n"
        f"📈 Capital invertido: *{money(row['invertido'])} USDT*\n"
        f"💵 Ganancias acumuladas: *{money(ganancias)} USDT*\n"
        f"💳 Ganancias disponibles para retirar: *{money(row['ganancias_disponibles'] or 0)} USDT*\n"
        
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
        f"🎁 Bonos acumulados: *{money(bonus)} USDT*\n\n"
        "Comparte el enlace para que otros puedan registrarse usando tu referencia."
    )

    await query.edit_message_text(
        texto,
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# =========================================================
# INFORMACIÓN
# =========================================================

async def show_info(query):
    texto = (
        "ℹ️ *INFORMACIÓN DEL SISTEMA*\n\n"
        f"📊 Tasa diaria fija: *{DAILY_RATE * 100:.1f}%*\n"
        "🎯 Cada plan permanece activo hasta alcanzar un rendimiento total equivalente al *200% del capital del plan* (capital + ganancias).\n"
        f"💵 Inversión mínima: *{money(MIN_INVESTMENT)} USDT*\n"
        f"💸 Retiro mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
        "🗓️ Frecuencia de retiros: *1 solicitud cada 7 días*\n"
        "📅 Ganancias generadas: *lunes a viernes a las 13:00*\n\n"
        "🌐 Red de depósitos y retiros: *TRC20*\n"
        "📥 Los depósitos son revisados manualmente por el administrador.\n"
        "📤 Los retiros también son revisados manualmente."
    )

    await query.edit_message_text(
        texto,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


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
    # Mostrar los planes en una cuadrícula de 2 columnas para aprovechar mejor el espacio.
    row_buttons = []
    for amount in INVESTMENT_PLANS:
        row_buttons.append(InlineKeyboardButton(
            f"🟢₮ {money(amount)} USDT",
            callback_data=f"plan_{amount}"
        ))
        if len(row_buttons) == 2:
            buttons.append(row_buttons)
            row_buttons = []
    if row_buttons:
        buttons.append(row_buttons)

    buttons.append([
        InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"),
        InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")
    ])

    texto = (
        "💎 *PLANES DE INVERSIÓN*\n\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        f"Tasa diaria fija: *{DAILY_RATE * 100:.1f}%*\n\n"
        "Selecciona el plan que deseas contratar. Cada vez que eliges un plan se crea una inversión independiente; tus planes anteriores no se reutilizan ni bloquean la compra de otro plan."
    )
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
    conn.commit()
    conn.close()

    add_movement(user_id, "inversion", amount, f"Inversión creada: Plan {money(amount)} USDT (depósito #{deposit_id})")

    await query.edit_message_text(
        "✅ *INVERSIÓN CREADA*\n\n"
        f"Plan: *{money(amount)} USDT*\n"
        f"Depósito utilizado: *#{deposit_id}*\n"
        f"Capital invertido: *{money(amount)} USDT*\n"
        f"Ganancia inicial: *0.00 USDT*\n"
        f"Ganancia diaria al {DAILY_RATE * 100:.1f}%: *{money(amount * DAILY_RATE)} USDT*\n\n"
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
        f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
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

    daily = float(active_invested) * DAILY_RATE
    balance = float(row["saldo"]) if row else 0.0

    # Primero se muestran los planes activos; después, el resumen de la cuenta.
    lines = ["📈 *MIS INVERSIONES*", ""]

    active_rows = [inv for inv in rows if inv["estado"] == "activa"]
    if active_rows:
        lines += ["🔥 *PLANES ACTIVOS*", ""]
        for inv in active_rows:
            capital = float(inv["capital"])
            gain = float(inv["ganancia_acumulada"])
            target_profit = capital
            progress = min(100.0, gain / target_profit * 100) if target_profit else 0.0
            lines.append(
                f"💎 *{inv['plan']}*\n"
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
        f"💵 Ganancia diaria estimada al {DAILY_RATE * 100:.1f}%: *{money(daily)} USDT*",
        ""
    ]

    if available_plans:
        lines += ["💎 *PLANES DISPONIBLES PARA INVERTIR*", ""]
        for dep in available_plans:
            amount = float(dep["plan_monto"] or dep["monto"])
            lines.append(f"• Depósito #{dep['id']} — Plan {money(amount)} USDT")
        lines.append("")

    completed_rows = [inv for inv in rows if inv["estado"] != "activa"]
    if completed_rows:
        lines += ["📚 *HISTORIAL DE PLANES COMPLETADOS*", ""]
        for inv in completed_rows:
            capital = float(inv["capital"])
            gain = float(inv["ganancia_acumulada"])
            lines.append(
                f"• {inv['plan']} — Capital: {money(capital)} USDT — "
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
        f"Tasa configurada: *{DAILY_RATE * 100:.4g}% diaria*\n"
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
        f"Tasa configurada: *{DAILY_RATE * 100:.4g}% diaria*\n\n"
        "La inversión ya aparece en tu panel.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Ver inversiones", callback_data="user_invest")],
            [InlineKeyboardButton("🏠 Menú principal", callback_data="user_home")]
        ])
    )


# =========================================================
# GANANCIAS
# =========================================================

def process_profits(force=False):
    """Acredita como máximo un pago por fecha local. El pago se ejecuta manualmente."""
    global LAST_DAILY_PROFITS
    LAST_DAILY_PROFITS = {}

    try:
        tz = ZoneInfo(PROFIT_TIMEZONE)
    except Exception:
        tz = timezone.utc
    local_now = datetime.now(tz)
    date_key = local_now.date().isoformat()

    if local_now.weekday() >= 5 and not force:
        return 0, 0.0

    conn = db()
    already = conn.execute(
        "SELECT 1 FROM pagos_diarios WHERE fecha=?",
        (date_key,)
    ).fetchone()
    if already:
        conn.close()
        return 0, 0.0

    rows = conn.execute(
        "SELECT * FROM inversiones WHERE estado='activa'"
    ).fetchall()

    processed = 0
    total_profit = 0.0
    credited_by_user = {}

    for inv in rows:
        capital = float(inv['capital'])
        accumulated = float(inv['ganancia_acumulada'])
        target_profit = capital * (float(inv['multiplicador_objetivo']) - 1)
        remaining = max(0.0, target_profit - accumulated)
        profit = min(capital * DAILY_RATE, remaining)

        if profit <= 0:
            conn.execute(
                "UPDATE inversiones SET estado='completada', ultimo_calculo=? WHERE id=?",
                (now_iso(), inv['id'])
            )
            continue

        conn.execute(
            "UPDATE inversiones SET ganancia_acumulada=ganancia_acumulada+?, ultimo_calculo=? WHERE id=?",
            (profit, now_iso(), inv['id'])
        )
        conn.execute(
            "UPDATE usuarios SET ganancias=ganancias+?, ganancias_disponibles=ganancias_disponibles+?, saldo=saldo+? WHERE telegram_id=?",
            (profit, profit, profit, inv['telegram_id'])
        )
        conn.execute(
            "INSERT INTO movimientos(telegram_id,tipo,monto,descripcion,fecha) VALUES(?,?,?,?,?)",
            (
                inv['telegram_id'],
                'ganancia',
                profit,
                f'Pago diario 0,5% inversión #{inv["id"]}',
                now_iso()
            )
        )

        if accumulated + profit >= target_profit:
            conn.execute(
                "UPDATE inversiones SET estado='completada' WHERE id=?",
                (inv['id'],)
            )

        processed += 1
        total_profit += profit
        uid = int(inv['telegram_id'])
        credited_by_user[uid] = credited_by_user.get(uid, 0.0) + profit

    conn.execute(
        "INSERT INTO pagos_diarios(fecha,fecha_proceso,total,inversiones) VALUES(?,?,?,?)",
        (date_key, now_iso(), total_profit, processed)
    )
    conn.commit()
    conn.close()

    # Guardamos el total acreditado por usuario para enviar una sola imagen
    # personalizada a cada usuario que recibió ganancias en esta ejecución.
    LAST_DAILY_PROFITS = credited_by_user
    return processed, total_profit


async def send_daily_profit_notifications(application):
    """Envía la imagen de ganancias a cada usuario al que se le acreditó hoy."""
    if not LAST_DAILY_PROFITS:
        return 0

    if not DAILY_PROFIT_IMAGE.exists():
        print(f"⚠️ No se encontró la imagen de ganancias: {DAILY_PROFIT_IMAGE}")
        return 0

    sent = 0
    try:
        tz = ZoneInfo(PROFIT_TIMEZONE)
    except Exception:
        tz = timezone.utc

    local_now = datetime.now(tz)
    date_text = local_now.strftime('%d/%m/%Y')

    for user_id, profit in list(LAST_DAILY_PROFITS.items()):
        try:
            with open(DAILY_PROFIT_IMAGE, 'rb') as photo:
                await application.bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=(
                        "✅ *GANANCIA DIARIA ACREDITADA*\n\n"
                        f"📅 Fecha: *{date_text}*\n"
                        f"💰 Ganancia acreditada hoy: *{money(profit)} USDT*\n\n"
                        "La ganancia ya fue acreditada en tu cuenta."
                    ),
                    parse_mode="Markdown"
                )
            sent += 1
        except Exception as e:
            print(f"⚠️ No se pudo enviar la notificación de ganancias a {user_id}: {e}")

    return sent


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

    context.user_data.clear()
    await update.message.reply_text(
        "💰 *NUEVO DEPÓSITO*\n\n"
        f"Red: *TRC20*\n"
        f"Wallet:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
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
        f"Depósito #{deposit_id} enviado para revisión"
    )

    await update.message.reply_text(
        "⏳ *DEPÓSITO ENVIADO*\n\n"
        f"ID: `{deposit_id}`\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"TXID: `{tx}`\n\n"
        "El administrador revisará el depósito manualmente.",
        parse_mode="Markdown"
    )

    admin_text = (
        "📥 *NUEVO DEPÓSITO PENDIENTE*\n\n"
        f"ID: `{deposit_id}`\n"
        f"Usuario: `{user_id}`\n"
        f"Nombre: {update.effective_user.full_name}\n"
        f"Monto: *{money(amount)} USDT*\n"
        f"TXID: `{tx}`"
    )

    admin_keyboard = InlineKeyboardMarkup([
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
                caption=f"📸 Comprobante del depósito #{deposit_id}"
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

    row = get_user(update.effective_user.id)
    balance = float(row["saldo"]) if row else 0

    await update.message.reply_text(
        "💸 *SOLICITAR RETIRO*\n\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        f"Mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
        "Frecuencia: *1 retiro cada 7 días*\n\n"
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

    if amount > balance:
        await update.message.reply_text(
            f"⚠️ Saldo insuficiente.\nDisponible: {money(balance)} USDT"
        )
        return WITHDRAW_AMOUNT

    context.user_data["withdraw_amount"] = amount

    await update.message.reply_text(
        "📍 Envía ahora tu dirección *USDT TRC20*.",
        parse_mode="Markdown"
    )
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

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Aprobar",
                callback_data=f"wd_approve_{withdrawal_id}"
            ),
            InlineKeyboardButton(
                "❌ Rechazar",
                callback_data=f"wd_reject_{withdrawal_id}"
            )
        ]
    ])

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
                text += (f"#{r['id']} — Usuario `{r['telegram_id']}`\n"
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
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Ver pendientes", callback_data="admin_withdrawals_pending")],[InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]]))
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
        daily = float(active_capital) * DAILY_RATE
        text = (
            "📈 *RESUMEN DE INVERSIONES*\n\n"
            f"🟢 Inversiones activas: *{active_count}*\n"
            f"💰 Capital actualmente invertido: *{money(active_capital)} USDT*\n"
            f"📊 Capital invertido histórico: *{money(total_capital)} USDT*\n"
            f"🎁 Ganancias acumuladas: *{money(profit)} USDT*\n"
            f"🏁 Planes finalizados: *{finished}*\n"
            f"📅 Ganancia diaria estimada al 0,5% sobre capital activo: *{money(daily)} USDT*\n"
            f"🎯 Objetivo de cada plan: 200% del capital inicial"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_inline())
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
            f"📊 Tasa diaria: *{DAILY_RATE * 100:.2f}%*"
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

    if data == "admin_profit":
        conn = db()
        active_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
        active_investments = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]
        conn.close()
        daily_due = float(active_capital) * DAILY_RATE
        await query.edit_message_text(
            "💰 *PAGO DIARIO 0,5%*\n\n"
            f"📈 Capital total actualmente invertido: *{money(active_capital)} USDT*\n"
            f"👥 Inversiones activas: *{active_investments}*\n"
            f"📊 Tasa diaria: *{DAILY_RATE * 100:.4g}%*\n"
            f"💵 Total que corresponde acreditar hoy: *{money(daily_due)} USDT*\n\n"
            "ℹ️ Este botón es solamente informativo. Aquí puedes consultar cuánto corresponde pagar hoy según el capital actualmente invertido.\n\n"
            "💵 Para realizar la acreditación, utiliza el botón *Pago manual* del panel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💵 Pago manual", callback_data="admin_profit_manual")],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_profit_manual":
        if not is_admin(user_id):
            await query.answer("⛔ No autorizado.", show_alert=True)
            return

        conn = db()
        active_capital = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
        active_investments = conn.execute("SELECT COUNT(*) c FROM inversiones WHERE estado='activa'").fetchone()["c"]
        conn.close()
        daily_due = float(active_capital) * DAILY_RATE

        await query.edit_message_text(
            "💵 *PAGO MANUAL DE GANANCIAS*\n\n"
            f"📈 Capital invertido activo: *{money(active_capital)} USDT*\n"
            f"👥 Inversiones activas: *{active_investments}*\n"
            f"📊 Tasa diaria: *{DAILY_RATE * 100:.4g}%*\n"
            f"💰 Total que se acreditará: *{money(daily_due)} USDT*\n\n"
            "⚠️ Al confirmar, el bot acreditará las ganancias pendientes de hoy usando la misma lógica del pago diario y enviará automáticamente la imagen de ganancia a los usuarios que reciban el pago.\n\n"
            "🔒 Solo se permite una acreditación por fecha para evitar pagos duplicados.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar pago manual", callback_data="admin_profit_manual_confirm")],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if data == "admin_profit_manual_confirm":
        if not is_admin(user_id):
            await query.answer("⛔ No autorizado.", show_alert=True)
            return

        await query.edit_message_text(
            "⏳ *PROCESANDO PAGO MANUAL...*\n\n"
            "Se están calculando las ganancias y enviando la notificación con la imagen a los usuarios correspondientes.",
            parse_mode="Markdown"
        )

        try:
            # Pago exclusivamente manual. force=True permite acreditar cualquier día,
            # mientras pagos_diarios evita una segunda acreditación en la misma fecha.
            processed, total = process_profits(force=True)

            if processed > 0 and total > 0:
                notified = await send_daily_profit_notifications(context.application)
                text = (
                    "✅ *PAGO MANUAL COMPLETADO*\n\n"
                    f"📈 Inversiones procesadas: *{processed}*\n"
                    f"💰 Total acreditado: *{money(total)} USDT*\n"
                    f"📸 Notificaciones con imagen enviadas: *{notified}* usuarios\n\n"
                    "Las ganancias ya fueron acreditadas y la imagen fue enviada a los usuarios correspondientes."
                )
            else:
                text = (
                    "ℹ️ *NO SE ACREDITARON GANANCIAS*\n\n"
                    "No hay inversiones activas con ganancias pendientes o ya se realizó el pago correspondiente a esta fecha.\n\n"
                    "Esto evita que una misma ganancia diaria sea pagada dos veces."
                )

            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Pago diario 0,5%", callback_data="admin_profit")],
                    [InlineKeyboardButton("💵 Pago manual", callback_data="admin_profit_manual")],
                    [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
                ])
            )
        except Exception as e:
            print(f"❌ Error en pago manual: {e}")
            await query.edit_message_text(
                f"❌ *ERROR EN EL PAGO MANUAL*\n\n`{str(e)}`",
                parse_mode="Markdown",
                reply_markup=back_inline()
            )
        return

    if data == "admin_backup":
        await query.edit_message_text("💾 *PREPARANDO RESPALDO COMPLETO...*", parse_mode="Markdown")
        try:
            await send_full_backup(context.bot, "Respaldo solicitado por el administrador")
            await send_admin_menu(ADMIN_TELEGRAM_ID, context, "✅ *RESPALDO COMPLETO CREADO Y ENVIADO*\n\nSe enviaron `.db` y `.xlsx`.\n\nPanel administrativo:")
        except Exception as e:
            await query.edit_message_text(
                f"❌ *ERROR AL CREAR RESPALDO*\n\n`{str(e)}`",
                parse_mode="Markdown",
                reply_markup=back_inline()
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
            f"Depósito #{deposit_id} aprobado"
        )

        plan_amount = float(row["plan_monto"] or 0)
        plan_line = f"\n💎 Plan disponible para invertir: *{money(plan_amount)} USDT*" if plan_amount > 0 else ""

        await query.edit_message_text(
            f"✅ *DEPÓSITO #{deposit_id} APROBADO*\n\n"
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
            f"❌ *DEPÓSITO #{deposit_id} RECHAZADO*",
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
                    f"El depósito #{deposit_id} fue rechazado. "
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

    if data == "user_history":
        await show_history(query)
        return

    if data == "user_info":
        await show_info(query)
        return

    if data == "user_deposit":
        await query.edit_message_text(
            "💰 *NUEVO DEPÓSITO*\n\n"
            f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
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
            "Escribe ahora el monto que deseas retirar.",
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

    ensure_user(update.effective_user)

    admin_actions = {
        "👥 Usuarios": "admin_users", "📥 Depósitos": "admin_deposits",
        "📤 Retiros": "admin_withdrawals", "📈 Inversiones": "admin_investments",
        "💾 Crear respaldo": "admin_backup", "♻️ Restaurar respaldo": "admin_restore",
        "🔒 Bloquear bot": "admin_lock", "🔓 Desbloquear bot": "admin_unlock",
        "💰 Pago diario 0,5%": "admin_profit", "💵 Pago manual": "admin_manual_profit",
        "📊 Estado": "admin_status",
        "📢 Enviar mensaje": "admin_broadcast",
    }
    user_actions = {
        "👤 Mi cuenta": "user_account", "📈 Inversiones": "user_invest",
        "🤝 Referidos": "user_referrals", "📜 Historial": "user_history",
        "ℹ️ Información": "user_info", "💎 Planes de Inversión": "user_plans",
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
            f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
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
        context.user_data["withdraw_amount"] = amount
        context.user_data["manual_flow"] = "withdraw_address"
        await update.message.reply_text("📍 Envía ahora tu dirección *USDT TRC20*.", parse_mode="Markdown")
        return
    if flow == "withdraw_address":
        context.user_data["manual_flow"] = None
        # Reutilizar la lógica existente de retiro.
        context.user_data["withdraw_amount"] = context.user_data.get("withdraw_amount", 0)
        await finish_withdraw_manual(update, context, text)
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
            parse_mode="Markdown", reply_markup=admin_keyboard()
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
            parse_mode="Markdown", reply_markup=admin_keyboard()
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
            f"📅 Ganancia diaria al 0,5%: *{money(float(active_capital)*DAILY_RATE)} USDT*\n"
            "🎯 Objetivo por plan: 200% del capital inicial",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
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
            f"📊 Tasa diaria: *{DAILY_RATE*100:.2f}%*\n"
            f"🛠️ Mantenimiento: *{'ACTIVO' if is_maintenance() else 'INACTIVO'}*",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if action == "admin_profit":
        conn = db()
        active_capital = conn.execute(
            "SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'"
        ).fetchone()["s"]
        active_investments = conn.execute(
            "SELECT COUNT(*) c FROM inversiones WHERE estado='activa'"
        ).fetchone()["c"]
        conn.close()
        daily_due = float(active_capital) * DAILY_RATE
        await update.message.reply_text(
            "💰 *PAGO DIARIO 0,5%*\n\n"
            f"📈 Capital total actualmente invertido: *{money(active_capital)} USDT*\n"
            f"👥 Inversiones activas: *{active_investments}*\n"
            f"📊 Tasa diaria: *{DAILY_RATE * 100:.4g}%*\n"
            f"💵 Total que corresponde acreditar hoy: *{money(daily_due)} USDT*\n\n"
            "ℹ️ Este botón es solamente informativo. Muestra el total que corresponde pagar hoy según el capital actualmente invertido.\n\n"
            "💵 Para acreditar las ganancias, utiliza *Pago manual*.",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if action == "admin_manual_profit":
        conn = db()
        active_capital = conn.execute(
            "SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'"
        ).fetchone()["s"]
        active_investments = conn.execute(
            "SELECT COUNT(*) c FROM inversiones WHERE estado='activa'"
        ).fetchone()["c"]
        conn.close()
        daily_due = float(active_capital) * DAILY_RATE
        await update.message.reply_text(
            "💵 *PAGO MANUAL DE GANANCIAS*\n\n"
            f"📈 Capital invertido activo: *{money(active_capital)} USDT*\n"
            f"👥 Inversiones activas: *{active_investments}*\n"
            f"📊 Tasa diaria: *{DAILY_RATE * 100:.4g}%*\n"
            f"💰 Total que se acreditará: *{money(daily_due)} USDT*\n\n"
            "⚠️ Pulsa el botón de abajo para confirmar. Se acreditarán las ganancias pendientes de hoy y se enviará automáticamente la imagen a los usuarios que reciban el pago.\n\n"
            "🔒 Solo se permite una acreditación por fecha para evitar pagos duplicados.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar pago manual", callback_data="admin_profit_manual_confirm")],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )
        return

    if action == "admin_restore":
        context.user_data["await_restore_db"] = True
        await update.message.reply_text("♻️ *RESTAURAR RESPALDO*\n\nEnvía ahora el archivo `.db` de respaldo. Primero se creará un respaldo de seguridad de la base actual.\n\n/cancelar para cancelar.", parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    if action == "admin_lock":
        if is_maintenance():
            await update.message.reply_text("🔒 El bot ya está bloqueado.", reply_markup=admin_keyboard()); return
        set_maintenance(True)
        sent, failed = await broadcast_users(context.bot, "🔧 *BOT EN MANTENIMIENTO*\n\nEl sistema está temporalmente bloqueado. No se procesarán nuevas solicitudes mientras dure el mantenimiento.")
        await update.message.reply_text(f"🔒 *BOT BLOQUEADO*\n\nUsuarios notificados: {sent}\nNo enviados: {failed}", parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    if action == "admin_unlock":
        if not is_maintenance():
            await update.message.reply_text("🔓 El bot ya está desbloqueado.", reply_markup=admin_keyboard()); return
        set_maintenance(False)
        sent, failed = await broadcast_users(context.bot, "✅ *BOT OPERATIVO*\n\nEl mantenimiento ha finalizado. El sistema vuelve a estar disponible.")
        await update.message.reply_text(f"🔓 *BOT DESBLOQUEADO*\n\nUsuarios notificados: {sent}\nNo enviados: {failed}", parse_mode="Markdown", reply_markup=admin_keyboard())
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
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aprobar",callback_data=f"wd_approve_{wid}"),InlineKeyboardButton("❌ Rechazar",callback_data=f"wd_reject_{wid}")]])
    try:
        await context.bot.send_message(chat_id=ADMIN_TELEGRAM_ID,text=("📤 *NUEVO RETIRO PENDIENTE*\n\n" f"ID: `{wid}`\nUsuario: `{user_id}`\nMonto: *{money(amount)} USDT*\nDirección TRC20:\n`{address}`"),parse_mode="Markdown",reply_markup=kb)
    except Exception as e:
        print(f"Error notificando retiro: {e}")
    context.user_data.clear()

# =========================================================
# RESPALDO EXCEL AUTOMÁTICO
# =========================================================

def create_excel_backup():
    conn = db(); wb = Workbook(); wb.remove(wb.active)
    tables = ["usuarios", "depositos", "retiros", "inversiones", "movimientos", "referidos", "pagos_diarios"]
    identity = {"usuarios":["nombre","username","telegram_id"],"depositos":["telegram_id"],"retiros":["telegram_id"],"inversiones":["telegram_id"],"movimientos":["telegram_id"],"referidos":["referido_id","referidor_id"],"pagos_diarios":[]}
    for table in tables:
        ws=wb.create_sheet(table[:31]); rows=conn.execute(f"SELECT * FROM {table}").fetchall(); columns=[d[1] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        preferred=identity.get(table,[]); ordered=[c for c in preferred if c in columns]+[c for c in columns if c not in preferred]
        ws.append(ordered)
        for row in rows: ws.append([row[c] for c in ordered])
        ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
        for col_cells in ws.columns:
            max_len=max([len(str(c.value or "")) for c in list(col_cells)[:200]]+[12]); ws.column_dimensions[col_cells[0].column_letter].width=min(max_len+2,40)
    ws=wb.create_sheet("resumen"); active=conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]; daily=float(active)*DAILY_RATE
    ws.append(["Indicador","Valor"]); ws.append(["Fecha UTC",now_iso()]); ws.append(["Tasa diaria",DAILY_RATE]); ws.append(["Capital activo invertido (USDT)",float(active)]); ws.append(["Ganancia diaria sobre capital activo (USDT)",daily]); ws.append(["Ganancias disponibles para retiro (USDT)",float(conn.execute("SELECT COALESCE(SUM(ganancias_disponibles),0) s FROM usuarios").fetchone()["s"])])
    conn.close(); output=BytesIO(); wb.save(output); output.seek(0); return output

async def send_excel_backup(bot, reason="Respaldo automático"):
    filename = f"respaldo_inversion_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.xlsx"
    output = create_excel_backup()
    await bot.send_document(
        chat_id=ADMIN_TELEGRAM_ID,
        document=InputFile(output, filename=filename),
        caption=f"💾 {reason}\n📊 Respaldo Excel completo (.xlsx)."
    )

async def send_db_backup(bot, reason="Respaldo de base de datos"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    filename = f"database_backup_{stamp}.db"
    path = os.path.join(BACKUP_DIR, filename)
    conn = db()
    try:
        backup_conn = sqlite3.connect(path)
        conn.backup(backup_conn)
        backup_conn.close()
    finally:
        conn.close()
    # Leer el archivo completo a memoria antes de enviarlo para que Telegram
    # reciba una copia independiente y el archivo .db quede realmente adjunto.
    with open(path, "rb") as f:
        data = f.read()
    stream = BytesIO(data)
    stream.name = filename
    await bot.send_document(
        chat_id=ADMIN_TELEGRAM_ID,
        document=InputFile(stream, filename=filename),
        caption=f"💾 {reason}\n🗄️ Base SQLite completa (.db) para restauración."
    )

async def send_full_backup(bot, reason="Respaldo solicitado"):
    # SIEMPRE se envían DOS archivos independientes: primero .db y luego .xlsx.
    await send_db_backup(bot, reason + " — base SQLite .db")
    await send_excel_backup(bot, reason + " — Excel .xlsx")


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
    print("💵 Pago de ganancias: SOLO MANUAL")
    print("==============================================")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    backup_task = asyncio.create_task(automatic_backup_loop(app))
    try:
        await asyncio.Event().wait()
    finally:
        backup_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
