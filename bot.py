import os
import sqlite3
import shutil
import uuid
import asyncio
from io import BytesIO
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
IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
DAILY_QUOTA_OPTIONS = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00]

# Plan de prueba/configurable.
# No representa una promesa de rentabilidad: son parámetros del sistema.
PLAN_NAME = os.getenv("PLAN_NAME", "Plan Inicial")
DAILY_RATE = 0.005  # parámetro heredado para compatibilidad con inversiones antiguas; las cuotas nuevas son variables
BACKUP_TIME = os.getenv("BACKUP_TIME", "06:00").strip()
BACKUP_TIMEZONE = os.getenv("BACKUP_TIMEZONE", "America/Sao_Paulo").strip()
TARGET_MULTIPLIER = float(os.getenv("TARGET_MULTIPLIER", "2.0"))
MIN_INVESTMENT = 50.0  # inversión mínima: 50 USDT
MIN_WITHDRAWAL = 15.0  # retiro mínimo: 15 USDT
WITHDRAWAL_INTERVAL_DAYS = 7
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
            revisado_por INTEGER
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

    # Nuevos campos para planes independientes, comprobantes de retiro y mantenimiento.
    for sql in [
        "ALTER TABLE depositos ADD COLUMN plan_monto REAL",
        "ALTER TABLE depositos ADD COLUMN usado INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE inversiones ADD COLUMN deposito_id INTEGER",
    ]:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pagos_diarios (
            fecha TEXT PRIMARY KEY,
            tasa REAL NOT NULL,
            total REAL NOT NULL DEFAULT 0,
            fecha_hora TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS configuracion (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL DEFAULT ''
        )
    """)
    cur.execute("INSERT OR IGNORE INTO configuracion (clave, valor) VALUES ('mantenimiento', '0')")

    # Migración de depósitos históricos: marca como usados los depósitos aprobados
    # necesarios para cubrir el capital ya invertido, conservando los restantes como disponibles.
    try:
        users = cur.execute("SELECT DISTINCT telegram_id FROM usuarios").fetchall()
        for u in users:
            uid = u[0]
            invested = float(cur.execute("SELECT COALESCE(SUM(capital),0) FROM inversiones WHERE telegram_id=?", (uid,)).fetchone()[0] or 0)
            deps = cur.execute("SELECT id, monto FROM depositos WHERE telegram_id=? AND estado='aprobado' AND COALESCE(usado,0)=0 ORDER BY id ASC", (uid,)).fetchall()
            remaining = invested
            for dep in deps:
                if remaining <= 0:
                    break
                amount = float(dep[1] or 0)
                cur.execute("UPDATE depositos SET usado=1 WHERE id=?", (dep[0],))
                remaining -= amount
    except Exception as e:
        print(f"⚠️ Migración de depósitos independientes: {e}")

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


def is_maintenance():
    conn = db()
    row = conn.execute("SELECT valor FROM configuracion WHERE clave='mantenimiento'").fetchone()
    conn.close()
    return bool(row and str(row["valor"]) == "1")


def set_maintenance(enabled):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO configuracion (clave, valor) VALUES ('mantenimiento', ?)", ("1" if enabled else "0",))
    conn.commit()
    conn.close()


def today_key():
    try:
        tz = ZoneInfo(BACKUP_TIMEZONE)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date().isoformat()


def quota_image(rate):
    text = f"{rate:.2f}".replace(".", ",")
    filename = f"{text}.jpg"
    path = os.path.join(IMAGE_DIR, filename)
    return path if os.path.exists(path) else None


def general_image(name):
    path = os.path.join(IMAGE_DIR, name)
    return path if os.path.exists(path) else None


def parse_quota(text):
    try:
        value = float(str(text).strip().replace("%", "").replace(",", "."))
    except ValueError:
        return None
    if value > 1.0:
        value /= 100.0
    return value if any(abs(value-x) < 1e-9 for x in DAILY_QUOTA_OPTIONS) else None


def available_plan_deposits(telegram_id):
    conn = db()
    rows = conn.execute("""
        SELECT id, monto, COALESCE(plan_monto, monto) AS plan_monto
        FROM depositos
        WHERE telegram_id=? AND estado='aprobado' AND COALESCE(usado,0)=0
        ORDER BY id ASC
    """, (telegram_id,)).fetchall()
    conn.close()
    return rows


async def send_maintenance_image(bot, filename):
    path = general_image(filename)
    if not path:
        print(f"⚠️ Imagen no encontrada: {path}")
        return 0, 0
    conn = db()
    rows = conn.execute("SELECT telegram_id FROM usuarios ORDER BY id ASC").fetchall()
    conn.close()
    sent = failed = 0
    for row in rows:
        if int(row["telegram_id"]) == ADMIN_TELEGRAM_ID:
            continue
        try:
            with open(path, "rb") as f:
                await bot.send_photo(chat_id=row["telegram_id"], photo=f)
            sent += 1
        except Exception as e:
            failed += 1
            print(f"⚠️ No se pudo enviar {filename} a {row['telegram_id']}: {e}")
    return sent, failed


async def send_quota_image(bot, telegram_id, rate, amount):
    path = quota_image(rate)
    if not path:
        print(f"⚠️ Imagen de cuota no encontrada para {rate*100:.2f}%")
        return False
    try:
        with open(path, "rb") as f:
            await bot.send_photo(chat_id=telegram_id, photo=f, caption=f"💵 Ganancia diaria acreditada: *{money(amount)} USDT*", parse_mode="Markdown")
        return True
    except Exception as e:
        print(f"⚠️ Error enviando imagen de cuota a {telegram_id}: {e}")
        return False


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


# =========================================================
# MENÚS
# =========================================================

def user_keyboard():
    return ReplyKeyboardMarkup([
        ["👤 Mi cuenta", "📈 Inversiones"],
        ["💎 Planes de Inversión"],
        ["💰 Depositar", "💸 Retirar"],
        ["🤝 Referidos", "📜 Historial"],
        ["ℹ️ Información"],
    ], resize_keyboard=True, is_persistent=True)


def admin_keyboard():
    # El administrador SOLO recibe el panel administrativo.
    return ReplyKeyboardMarkup([
        ["👥 Usuarios", "📥 Depósitos"],
        ["📤 Retiros", "📈 Inversiones"],
        ["💾 Crear respaldo", "♻️ Restaurar respaldo"],
        ["📊 Estado"],
        ["💰 Pago Diario", "📊 Cuotas Diarias"],
        ["🔒 Bloquear bot", "🔓 Desbloquear bot"],
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
    else:
        if is_maintenance():
            path = general_image("bot bloqueado.jpg")
            if path:
                with open(path, "rb") as f:
                    await context.bot.send_photo(chat_id=user.id, photo=f)
            return
        if is_new:
            path = general_image("bienvenida.jpg")
            if path:
                with open(path, "rb") as f:
                    await context.bot.send_photo(chat_id=user.id, photo=f)
        await send_user_menu(
            user.id, context,
            f"👋 *Bienvenido, {user.first_name or 'usuario'}*\n\n"
            "Desde aquí puedes administrar tu cuenta, depositar, solicitar retiros, "
            "consultar inversiones y compartir tu enlace de referido."
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
    porcentaje = (ganancias / total_depositado * 100) if total_depositado > 0 else 0.0

    texto = (
        "👤 *MI CUENTA*\n\n"
        f"🆔 ID: `{row['telegram_id']}`\n"
        f"👤 Nombre: {row['nombre'] or '-'}\n"
        f"💰 Saldo disponible: *{money(row['saldo'])} USDT*\n"
        f"📥 Total depositado: *{money(total_depositado)} USDT*\n"
        f"📈 Capital invertido: *{money(row['invertido'])} USDT*\n"
        f"💵 Ganancias acumuladas: *{money(ganancias)} USDT*\n"
        f"📊 Rendimiento de la cuenta: *{porcentaje:.2f}%*\n"
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
        "📊 La cuota diaria es variable y es seleccionada manualmente por el administrador.\n"
        "🎯 Cada plan permanece activo hasta alcanzar un rendimiento total equivalente al *200% del capital del plan* (capital + ganancias).\n"
        f"💵 Inversión mínima: *{money(MIN_INVESTMENT)} USDT*\n"
        f"💸 Retiro mínimo: *{money(MIN_WITHDRAWAL)} USDT*\n"
        "🗓️ Frecuencia de retiros: *1 solicitud cada 7 días*\n\n"
        "🌐 Red de depósitos y retiros: *TRC20*\n"
        "📥 Los depósitos son revisados manualmente por el administrador.\n"
        "📤 Los retiros también son revisados manualmente."
    )
    await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]]))


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
    plan_row = []
    for amount in INVESTMENT_PLANS:
        plan_row.append(InlineKeyboardButton(
            f"💎 {money(amount)} USDT",
            callback_data=f"plan_{amount}"
        ))
        if len(plan_row) == 3:
            buttons.append(plan_row)
            plan_row = []
    if plan_row:
        buttons.append(plan_row)

    buttons.append([
        InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"),
        InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")
    ])

    texto = (
        "💎 *PLANES DE INVERSIÓN*\n\n"
        f"Saldo disponible: *{money(balance)} USDT*\n"
        "Cada plan se deposita e invierte de forma independiente.\n"
        "Selecciona el plan que deseas contratar."
    )
    await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def select_plan(query, amount):
    user_id = query.from_user.id
    if amount not in INVESTMENT_PLANS:
        await query.answer("Plan no disponible.", show_alert=True)
        return
    available = [r for r in available_plan_deposits(user_id) if abs(float(r["plan_monto"])-amount) < 0.000001]
    if available:
        dep = available[0]
        await query.edit_message_text(
            "🚀 *PLAN DISPONIBLE PARA INVERTIR*\n\n"
            f"Plan: *{money(amount)} USDT*\n"
            f"Depósito disponible: *#{dep['id']}*\n\n"
            "Este plan es independiente de otros depósitos o ganancias. Puedes invertirlo completo desde Inversiones.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🚀 Invertir Plan {money(amount)} USDT", callback_data=f"invest_deposit_{dep['id']}")],
                [InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
            ])
        )
        return
    await query.edit_message_text(
        "💰 *DEPÓSITO PARA ESTE PLAN*\n\n"
        f"Plan seleccionado: *{money(amount)} USDT*\n"
        f"Monto exacto a depositar: *{money(amount)} USDT*\n\n"
        "Este plan es independiente. El bot no descontará depósitos anteriores, ganancias ni saldos para completar este nuevo plan. \n\n"
        f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💰 Depositar {money(amount)} USDT", callback_data=f"deposit_plan_{amount:g}")],
            [InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]
        ])
    )


async def start_plan_deposit(query, context, plan_amount):
    context.user_data.clear()
    context.user_data["manual_flow"] = "deposit_tx_fixed"
    context.user_data["deposit_amount"] = float(plan_amount)
    context.user_data["plan_amount"] = float(plan_amount)
    await query.edit_message_text(
        "💰 *DEPÓSITO DEL PLAN*\n\n"
        f"Plan seleccionado: *{money(plan_amount)} USDT*\n"
        f"Monto exacto a depositar: *{money(plan_amount)} USDT*\n\n"
        f"Wallet USDT TRC20:\n`{USDT_TRC20_ADDRESS or 'NO CONFIGURADA'}`\n\n"
        "Realiza el depósito por el monto indicado y después envía aquí el TXID. No se utilizará ningún saldo anterior para completar este plan.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Atrás", callback_data="user_plans"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")]])
    )

async def confirm_plan_investment(query, amount):
    available = [r for r in available_plan_deposits(query.from_user.id) if abs(float(r["plan_monto"])-amount) < 0.000001]
    if not available:
        await query.answer("No hay un depósito disponible para este plan.", show_alert=True)
        return
    await invest_from_deposit(query, available[0]["id"])



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
    available = available_plan_deposits(user_id)
    conn = db()
    total_deposited = conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE telegram_id=? AND estado='aprobado'", (user_id,)).fetchone()["s"]
    active_invested = conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE telegram_id=? AND estado='activa'", (user_id,)).fetchone()["s"]
    total_gains = conn.execute("SELECT COALESCE(SUM(ganancia_acumulada),0) s FROM inversiones WHERE telegram_id=?", (user_id,)).fetchone()["s"]
    conn.close()
    balance = float(row["saldo"]) if row else 0.0
    pct = (float(total_gains) / float(total_deposited) * 100) if float(total_deposited) > 0 else 0.0
    lines=["📈 *MIS INVERSIONES*","",f"💰 Total depositado aprobado: *{money(total_deposited)} USDT*",f"💳 Saldo disponible: *{money(balance)} USDT*",f"📊 Capital actualmente invertido: *{money(active_invested)} USDT*",f"💵 Ganancia acumulada: *{money(total_gains)} USDT*",f"📈 Rendimiento de la cuenta: *{pct:.2f}%*",""]
    if available:
        lines += ["📦 *PLANES DISPONIBLES PARA INVERTIR*",""]
        for dep in available:
            amount=float(dep["plan_monto"])
            lines.append(f"💎 Plan *{money(amount)} USDT* — Depósito #{dep['id']}")
        lines.append("")
    if rows:
        for inv in rows:
            capital=float(inv["capital"]); gain=float(inv["ganancia_acumulada"]); target_profit=capital
            progress=min(100.0, gain/target_profit*100) if target_profit else 0.0
            lines.append(f"💎 *{inv['plan']}*\nCapital: {money(capital)} USDT\nGanancia: {money(gain)} USDT\nProgreso hacia el 200%: {progress:.2f}%\nEstado: {inv['estado']}\n")
    else:
        lines.append("No tienes inversiones activas o registradas todavía.")
    buttons=[]
    for dep in available:
        buttons.append([InlineKeyboardButton(f"🚀 Invertir Plan {money(dep['plan_monto'])} USDT", callback_data=f"invest_deposit_{dep['id']}")])
    buttons.append([InlineKeyboardButton("💎 Planes de Inversión", callback_data="user_plans")])
    buttons.append([InlineKeyboardButton("⬅️ Atrás", callback_data="user_home"), InlineKeyboardButton("🏠 Menú Principal", callback_data="user_home")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def invest_from_deposit(query, deposit_id):
    user_id=query.from_user.id
    conn=db()
    dep=conn.execute("SELECT * FROM depositos WHERE id=? AND telegram_id=? AND estado='aprobado' AND COALESCE(usado,0)=0",(deposit_id,user_id)).fetchone()
    if not dep:
        conn.close(); await query.answer("Este plan ya fue invertido o no está disponible.", show_alert=True); return
    amount=float(dep["plan_monto"] or dep["monto"])
    row=conn.execute("SELECT saldo FROM usuarios WHERE telegram_id=?",(user_id,)).fetchone()
    if not row or float(row["saldo"]) < amount:
        conn.close(); await query.answer("El saldo asociado al depósito no está disponible.", show_alert=True); return
    now=now_iso()
    conn.execute("UPDATE depositos SET usado=1 WHERE id=?",(deposit_id,))
    conn.execute("UPDATE usuarios SET saldo=saldo-?, invertido=invertido+? WHERE telegram_id=?",(amount,amount,user_id))
    conn.execute("INSERT INTO inversiones (telegram_id,plan,capital,ganancia_acumulada,tasa_diaria,multiplicador_objetivo,estado,fecha_inicio,ultimo_calculo,deposito_id) VALUES (?,?,?,?,?,?,'activa',?,?,?)",(user_id,f"Plan {money(amount)} USDT",amount,0,DAILY_RATE,TARGET_MULTIPLIER,now,now,deposit_id))
    conn.commit(); conn.close()
    add_movement(user_id,"inversion",amount,f"Inversión creada desde depósito #{deposit_id}: Plan {money(amount)} USDT")
    await query.edit_message_text("✅ *PLAN INVERTIDO*\n\n" f"Plan: *{money(amount)} USDT*\n" f"Capital invertido: *{money(amount)} USDT*\n" "Ganancia inicial: *0.00 USDT*\n\n" "La inversión es independiente de otros planes y aparecerá en tu panel.",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📈 Ver inversiones",callback_data="user_invest")],[InlineKeyboardButton("🏠 Menú Principal",callback_data="user_home")]]))


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

def process_profits():
    """
    Calcula ganancias acumuladas desde el último cálculo.
    El proceso se ejecuta al presionar el botón administrativo.
    """
    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM inversiones
        WHERE estado = 'activa'
    """).fetchall()

    processed = 0
    total_profit = 0.0

    now = datetime.now(timezone.utc)

    for inv in rows:
        try:
            last = datetime.fromisoformat(inv["ultimo_calculo"])
        except Exception:
            last = now

        elapsed_seconds = max(0, (now - last).total_seconds())
        days = elapsed_seconds / 86400.0

        if days <= 0:
            continue

        capital = float(inv["capital"])
        accumulated = float(inv["ganancia_acumulada"])
        target_profit = capital * (float(inv["multiplicador_objetivo"]) - 1)

        available_target = max(0.0, target_profit - accumulated)

        if available_target <= 0:
            conn.execute("""
                UPDATE inversiones
                SET estado = 'completada', ultimo_calculo = ?
                WHERE id = ?
            """, (now.isoformat(), inv["id"]))
            continue

        profit = capital * float(inv["tasa_diaria"]) * days
        profit = min(profit, available_target)

        if profit <= 0:
            continue

        conn.execute("""
            UPDATE inversiones
            SET ganancia_acumulada = ganancia_acumulada + ?,
                ultimo_calculo = ?
            WHERE id = ?
        """, (profit, now.isoformat(), inv["id"]))

        conn.execute("""
            UPDATE usuarios
            SET ganancias = ganancias + ?,
                saldo = saldo + ?
            WHERE telegram_id = ?
        """, (profit, profit, inv["telegram_id"]))

        conn.execute("""
            INSERT INTO movimientos (
                telegram_id, tipo, monto, descripcion, fecha
            )
            VALUES (?, 'ganancia', ?, ?, ?)
        """, (
            inv["telegram_id"],
            profit,
            f"Ganancia procesada: inversión #{inv['id']}",
            now.isoformat()
        ))

        new_total = accumulated + profit
        if new_total >= target_profit:
            conn.execute("""
                UPDATE inversiones
                SET estado = 'completada'
                WHERE id = ?
            """, (inv["id"],))

        processed += 1
        total_profit += profit

    conn.commit()
    conn.close()

    return processed, total_profit


# =========================================================
# DEPÓSITOS
# =========================================================

async def start_deposit(update, context):
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
    tx = context.user_data.get("deposit_tx", "")
    photo_id = context.user_data.get("deposit_photo", "")

    conn = db()
    cur = conn.execute("""
        INSERT INTO depositos (
            telegram_id, monto, tx_hash, foto_file_id, plan_monto,
            estado, fecha
        )
        VALUES (?, ?, ?, ?, ?, 'pendiente', ?)
    """, (
        user_id,
        amount,
        tx,
        photo_id,
        context.user_data.get("plan_amount"),
        now_iso()
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
        SET saldo = saldo - ?
        WHERE telegram_id = ?
    """, (amount, user_id))

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
# CUOTAS DIARIAS / MANTENIMIENTO / COMPROBANTE DE RETIRO
# =========================================================

async def show_daily_payment_info(update_or_query, context, edit=False):
    conn=db()
    active=conn.execute("SELECT id,telegram_id,capital,ganancia_acumulada,multiplicador_objetivo FROM inversiones WHERE estado='activa'").fetchall()
    conn.close()
    rate=context.user_data.get("admin_quota_rate")
    if rate is None:
        prompt="💰 *PAGO DIARIO*\n\nEscribe la cuota que deseas consultar (por ejemplo: *0,35* o *0,35%*).\n\nEste apartado es informativo: *no acredita ganancias y no envía imágenes*."
        if edit: await update_or_query.edit_message_text(prompt,parse_mode="Markdown",reply_markup=back_inline())
        else: await update_or_query.message.reply_text(prompt,parse_mode="Markdown",reply_markup=admin_keyboard())
        return
    total=0.0
    capital=0.0
    for inv in active:
        remaining=max(0.0,float(inv["capital"])*(float(inv["multiplicador_objetivo"])-1)-float(inv["ganancia_acumulada"]))
        total += min(float(inv["capital"])*rate, remaining)
        capital += float(inv["capital"])
    text=("💰 *PAGO DIARIO*\n\n" f"📊 Cuota consultada: *{rate*100:.2f}%*\n" f"📈 Capital activo invertido: *{money(capital)} USDT*\n" f"💵 Total que correspondería acreditar hoy: *{money(total)} USDT*\n\n" "ℹ️ Este botón solo informa. Para acreditar el pago debes entrar en *Cuotas Diarias* y pulsar la cuota correspondiente.")
    if edit: await update_or_query.edit_message_text(text,parse_mode="Markdown",reply_markup=back_inline())
    else: await update_or_query.message.reply_text(text,parse_mode="Markdown",reply_markup=admin_keyboard())


async def process_daily_quota(context, rate):
    date_key=today_key()
    conn=db()
    try:
        conn.execute("INSERT INTO pagos_diarios (fecha,tasa,total,fecha_hora) VALUES (?,?,0,?)",(date_key,rate,now_iso()))
    except sqlite3.IntegrityError:
        old=conn.execute("SELECT tasa,total FROM pagos_diarios WHERE fecha=?",(date_key,)).fetchone()
        conn.close()
        return False,0.0,0, f"Ya se realizó el pago diario de hoy con la cuota {float(old['tasa'])*100:.2f}%. Total acreditado: {money(old['total'])} USDT."
    rows=conn.execute("SELECT * FROM inversiones WHERE estado='activa' ORDER BY id ASC").fetchall()
    user_totals={}
    total=0.0; processed=0
    for inv in rows:
        capital=float(inv["capital"]); accumulated=float(inv["ganancia_acumulada"]); target=capital*(float(inv["multiplicador_objetivo"])-1)
        remaining=max(0.0,target-accumulated)
        credit=min(capital*rate,remaining)
        if credit<=0:
            conn.execute("UPDATE inversiones SET estado='completada' WHERE id=?",(inv["id"]))
            continue
        new_gain=accumulated+credit
        conn.execute("UPDATE inversiones SET ganancia_acumulada=?,ultimo_calculo=? WHERE id=?",(new_gain,now_iso(),inv["id"]))
        if new_gain>=target-1e-9:
            conn.execute("UPDATE inversiones SET estado='completada' WHERE id=?",(inv["id"]))
        conn.execute("UPDATE usuarios SET ganancias=ganancias+?,saldo=saldo+? WHERE telegram_id=?",(credit,credit,inv["telegram_id"]))
        conn.execute("INSERT INTO movimientos (telegram_id,tipo,monto,descripcion,fecha) VALUES (?, 'ganancia', ?, ?, ?)",(inv["telegram_id"],credit,f"Cuota diaria {rate*100:.2f}% — inversión #{inv['id']}",now_iso()))
        user_totals[inv["telegram_id"]]=user_totals.get(inv["telegram_id"],0.0)+credit
        total+=credit; processed+=1
    conn.execute("UPDATE pagos_diarios SET total=? WHERE fecha=?",(total,date_key))
    conn.commit(); conn.close()
    sent=0
    for uid,amount in user_totals.items():
        if await send_quota_image(context.bot,uid,rate,amount): sent+=1
    return True,total,processed,f"Pago acreditado correctamente. Usuarios notificados: {sent}."


async def block_bot(context):
    set_maintenance(True)
    return await send_maintenance_image(context.bot,"bot bloqueado.jpg")


async def unblock_bot(context):
    set_maintenance(False)
    return await send_maintenance_image(context.bot,"bot operativo.jpg")


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
        path=general_image("bot bloqueado.jpg")
        if path:
            with open(path,"rb") as f:
                await context.bot.send_photo(chat_id=user_id,photo=f)
        return

    print(
        f"[CALLBACK] user_id={user_id} "
        f"chat_id={query.message.chat.id} data={data}"
    )

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
        text = (
            "📈 *RESUMEN DE INVERSIONES*\n\n"
            f"🟢 Inversiones activas: *{active_count}*\n"
            f"💰 Capital actualmente invertido: *{money(active_capital)} USDT*\n"
            f"📊 Capital invertido histórico: *{money(total_capital)} USDT*\n"
            f"🎁 Ganancias acumuladas: *{money(profit)} USDT*\n"
            f"🏁 Planes finalizados: *{finished}*\n"
            "📊 La cuota diaria se selecciona manualmente desde Cuotas Diarias.\n"
            "🎯 Objetivo de cada plan: 200% del capital inicial"
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

    if data == "admin_payment_info":
        context.user_data.pop("admin_quota_rate", None)
        await show_daily_payment_info(query, context, edit=True)
        return

    if data == "admin_quota_menu":
        buttons=[]
        quota_row=[]
        for rate in DAILY_QUOTA_OPTIONS:
            quota_row.append(InlineKeyboardButton(f"{rate:.2f}%".replace(".",","), callback_data=f"quota_{int(round(rate*100)):02d}"))
            if len(quota_row) == 4:
                buttons.append(quota_row)
                quota_row=[]
        if quota_row:
            buttons.append(quota_row)
        buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")])
        await query.edit_message_text("📊 *CUOTAS DIARIAS*\n\nSelecciona la cuota que se acreditará hoy.\n\n⚠️ Solo puede acreditarse una cuota por día.",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("quota_"):
        cents=int(data.split("_")[1]); rate=cents/10000.0
        ok,total,processed,msg=await process_daily_quota(context,rate)
        if not ok:
            await query.edit_message_text("⚠️ *PAGO DIARIO YA REALIZADO*\n\n"+msg,parse_mode="Markdown",reply_markup=back_inline())
            return
        await query.edit_message_text("✅ *CUOTA DIARIA ACREDITADA*\n\n" f"📊 Cuota: *{rate*100:.2f}%*\n" f"📈 Inversiones acreditadas: *{processed}*\n" f"💵 Total acreditado: *{money(total)} USDT*\n\n" f"{msg}",parse_mode="Markdown",reply_markup=back_inline())
        return

    if data == "admin_block":
        sent,failed=await block_bot(context)
        await query.edit_message_text(f"🔒 *BOT BLOQUEADO*\n\nImagen enviada a {sent} usuarios.",parse_mode="Markdown",reply_markup=back_inline())
        return

    if data == "admin_unblock":
        sent,failed=await unblock_bot(context)
        await query.edit_message_text(f"🔓 *BOT DESBLOQUEADO*\n\nImagen enviada a {sent} usuarios.",parse_mode="Markdown",reply_markup=back_inline())
        return

    if data == "admin_backup":
        await query.edit_message_text("💾 *PREPARANDO RESPALDOS...*",parse_mode="Markdown")
        try:
            await send_db_backup(context.bot,"Respaldo solicitado por el administrador")
            await send_excel_backup(context.bot,"Respaldo solicitado por el administrador")
            await send_admin_menu(ADMIN_TELEGRAM_ID,context,"✅ *RESPALDO DB + EXCEL CREADO Y ENVIADO*\n\nPanel administrativo:")
        except Exception as e:
            await query.edit_message_text(f"❌ *ERROR AL CREAR RESPALDO*\n\n`{e}`",parse_mode="Markdown",reply_markup=back_inline())
        return

    if data == "admin_restore":
        context.user_data["await_restore_db"] = True
        await query.edit_message_text(
            "♻️ *RESTAURAR RESPALDO*\n\n"
            "Envía ahora el archivo `.db` que quieres restaurar.\n\n"
            "⚠️ Antes de reemplazar la base actual se conservará una copia de seguridad automática.\n"
            "Usa /cancelar si deseas salir.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="admin_home")]])
        )
        return

    if data == "admin_home":
        context.user_data.pop("admin_broadcast", None)
        context.user_data.pop("await_restore_db", None)
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

        await query.edit_message_text(
            f"✅ *DEPÓSITO #{deposit_id} APROBADO*\n\n"
            f"Monto acreditado: *{money(amount)} USDT*",
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
                    f"Tu depósito de *{money(amount)} USDT* "
                    "fue aprobado y acreditado a tu saldo."
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
            f"Monto: *{money(row['monto'])} USDT*\n"
            "Ahora realiza el envío manual y después pulsa *Enviar comprobante* para adjuntar la prueba directamente al usuario.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📸 Enviar comprobante", callback_data=f"wd_proof_{withdrawal_id}")],
                [InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")]
            ])
        )

        return

    if data.startswith("wd_proof_"):
        withdrawal_id=int(data.rsplit("_",1)[1])
        conn=db(); row=conn.execute("SELECT * FROM retiros WHERE id=? AND estado='aprobado'",(withdrawal_id,)).fetchone(); conn.close()
        if not row:
            await query.answer("Retiro no encontrado o no está aprobado.",show_alert=True); return
        context.user_data["admin_withdraw_proof_id"]=withdrawal_id
        await query.edit_message_text("📸 *COMPROBANTE DE PAGO*\n\nEnvía ahora aquí la foto del comprobante.\n\nSe enviará *exclusivamente* al usuario de este retiro.",parse_mode="Markdown",reply_markup=back_inline())
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
            SET saldo = saldo + ?
            WHERE telegram_id=?
        """, (row["monto"], row["telegram_id"]))

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
        await invest_from_deposit(query, deposit_id)
        return

    if data == "user_new_investment":
        await create_investment(query)
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
    if not is_admin(update.effective_user.id) and is_maintenance():
        path=general_image("bot bloqueado.jpg")
        if path:
            with open(path,"rb") as f:
                await context.bot.send_photo(chat_id=update.effective_chat.id,photo=f)
        return
    text = (update.message.text or "").strip()
    if not text:
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

    ensure_user(update.effective_user)

    admin_actions = {
        "👥 Usuarios": "admin_users", "📥 Depósitos": "admin_deposits",
        "📤 Retiros": "admin_withdrawals", "📈 Inversiones": "admin_investments",
        "💾 Crear respaldo": "admin_backup", "♻️ Restaurar respaldo": "admin_restore", "📊 Estado": "admin_status",
        "💰 Pago Diario": "admin_payment_info", "📊 Cuotas Diarias": "admin_quota_menu",
        "🔒 Bloquear bot": "admin_block", "🔓 Desbloquear bot": "admin_unblock",
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

    if is_admin(update.effective_user.id) and context.user_data.get("admin_payment_info"):
        rate=parse_quota(text)
        if rate is None:
            await update.message.reply_text("⚠️ Cuota no válida. Usa una de las cuotas disponibles, por ejemplo 0,35.",reply_markup=admin_keyboard())
            return
        context.user_data.pop("admin_payment_info",None)
        context.user_data["admin_quota_rate"]=rate
        await show_daily_payment_info(update,context,edit=False)
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
            "📊 La cuota diaria se selecciona manualmente desde Cuotas Diarias.\n"
            "🎯 Objetivo por plan: 200% del capital inicial",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    if action == "admin_backup":
        await send_excel_backup(context.bot, "Respaldo solicitado por el administrador")
        await update.message.reply_text("💾 Respaldo enviado correctamente.", reply_markup=admin_keyboard())
        return

    if action == "admin_restore":
        context.user_data["await_restore_db"] = True
        await update.message.reply_text(
            "♻️ *RESTAURAR RESPALDO*\n\nEnvía ahora el archivo `.db` que quieres restaurar.\n\n⚠️ Se guardará primero una copia de seguridad de la base actual.\nUsa /cancelar si deseas salir.",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
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
            f"📊 Tasa diaria: *{DAILY_RATE*100:.2f}%*",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return


    if action == "admin_payment_info":
        context.user_data.pop("admin_quota_rate", None)
        context.user_data["admin_payment_info"] = True
        await update.message.reply_text("💰 *PAGO DIARIO*\n\nEscribe la cuota que deseas consultar (por ejemplo: *0,35* o *0,35%*).\n\nEste apartado es informativo y no acredita ganancias.",parse_mode="Markdown",reply_markup=admin_keyboard())
        return

    if action == "admin_quota_menu":
        buttons=[]
        quota_row=[]
        for rate in DAILY_QUOTA_OPTIONS:
            quota_row.append(InlineKeyboardButton(f"{rate:.2f}%".replace(".",","), callback_data=f"quota_{int(round(rate*100)):02d}"))
            if len(quota_row) == 4:
                buttons.append(quota_row)
                quota_row=[]
        if quota_row:
            buttons.append(quota_row)
        buttons.append([InlineKeyboardButton("⬅️ Panel", callback_data="admin_home")])
        await update.message.reply_text("📊 *CUOTAS DIARIAS*\n\nSelecciona la cuota que se acreditará hoy.",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "admin_block":
        sent,_=await block_bot(context)
        await update.message.reply_text(f"🔒 Bot bloqueado. Imagen enviada a {sent} usuarios.",reply_markup=admin_keyboard())
        return

    if action == "admin_unblock":
        sent,_=await unblock_bot(context)
        await update.message.reply_text(f"🔓 Bot desbloqueado. Imagen enviada a {sent} usuarios.",reply_markup=admin_keyboard())
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
    row = conn.execute("SELECT saldo FROM usuarios WHERE telegram_id=?", (user_id,)).fetchone()
    if not row or float(row["saldo"]) < amount:
        conn.close()
        await update.message.reply_text("⚠️ El saldo ya no es suficiente para esta solicitud.")
        return
    conn.execute("UPDATE usuarios SET saldo=saldo-? WHERE telegram_id=?", (amount,user_id))
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
    conn = db()
    wb = Workbook()
    wb.remove(wb.active)
    tables = ["usuarios", "depositos", "retiros", "inversiones", "movimientos", "referidos"]
    for table in tables:
        ws = wb.create_sheet(table[:31])
        raw_columns = [d[1] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        display_rows = []
        remaining_columns = list(raw_columns)
        if table == "referidos":
            query = """SELECT r.*, u.nombre AS _nombre, u.username AS _username, r.referido_id AS _telegram_id FROM referidos r LEFT JOIN usuarios u ON u.telegram_id=r.referido_id"""
            rows = conn.execute(query).fetchall()
            remaining_columns = raw_columns
            identity_source = {"_nombre":"Nombre", "_username":"Nombre de usuario", "_telegram_id":"ID Telegram"}
        elif "telegram_id" in raw_columns:
            query = f"SELECT t.*, u.nombre AS _nombre, u.username AS _username FROM {table} t LEFT JOIN usuarios u ON u.telegram_id=t.telegram_id"
            rows = conn.execute(query).fetchall()
            identity_source = {"_nombre":"Nombre", "_username":"Nombre de usuario", "telegram_id":"ID Telegram"}
        else:
            identity_source = {}
        headers = ["Nombre", "Nombre de usuario", "ID Telegram"] + [c for c in remaining_columns if c != "telegram_id"]
        ws.append(headers)
        for row in rows:
            if table == "referidos":
                values = [row["_nombre"] or "", row["_username"] or "", row["_telegram_id"]]
            elif "telegram_id" in raw_columns:
                values = [row["_nombre"] or "", row["_username"] or "", row["telegram_id"]]
            else:
                values = ["", "", ""]
            values += [row[c] for c in remaining_columns if c != "telegram_id"]
            ws.append(values)
        ws.freeze_panes="A2"
        ws.auto_filter.ref=ws.dimensions
        for col_cells in ws.columns:
            max_len=max([len(str(c.value or "")) for c in list(col_cells)[:200]]+[12])
            ws.column_dimensions[col_cells[0].column_letter].width=min(max_len+2,40)

    ws=wb.create_sheet("resumen")
    approved=conn.execute("SELECT COALESCE(SUM(monto),0) s FROM depositos WHERE estado='aprobado'").fetchone()["s"]
    active=conn.execute("SELECT COALESCE(SUM(capital),0) s FROM inversiones WHERE estado='activa'").fetchone()["s"]
    last=conn.execute("SELECT fecha,tasa,total FROM pagos_diarios ORDER BY fecha DESC LIMIT 1").fetchone()
    ws.append(["Indicador","Valor"])
    ws.append(["Fecha UTC",now_iso()])
    ws.append(["Cuota diaria", "Variable; seleccionada manualmente por el administrador"])
    ws.append(["Última cuota acreditada", f"{float(last['tasa'])*100:.2f}%" if last else "Ninguna"])
    ws.append(["Total última cuota (USDT)", float(last["total"]) if last else 0.0])
    ws.append(["Total depósitos aprobados (USDT)",float(approved)])
    ws.append(["Capital activo invertido (USDT)",float(active)])
    ws.freeze_panes="A2"; ws.column_dimensions["A"].width=48; ws.column_dimensions["B"].width=30
    conn.close()
    output=BytesIO(); wb.save(output); output.seek(0); return output


async def send_db_backup(bot, reason="Respaldo de base de datos"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    filename=f"database_backup_{stamp}.db"
    path=os.path.join(BACKUP_DIR,filename)
    conn=db()
    try:
        backup_conn=sqlite3.connect(path)
        conn.backup(backup_conn)
        backup_conn.close()
    finally:
        conn.close()
    with open(path,"rb") as f:
        data=f.read()
    stream=BytesIO(data); stream.name=filename
    await bot.send_document(chat_id=ADMIN_TELEGRAM_ID,document=InputFile(stream,filename=filename),caption=f"💾 {reason}\n🗄️ Base SQLite completa (.db) para restauración.")


async def send_excel_backup(bot, reason="Respaldo automático"):
    filename = f"respaldo_inversion_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    output = create_excel_backup()
    await bot.send_document(
        chat_id=ADMIN_TELEGRAM_ID,
        document=InputFile(output, filename=filename),
        caption=f"💾 {reason}\n📊 Respaldo completo en Excel (.xlsx)."
    )


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
            await send_db_backup(application.bot, "Respaldo automático diario de las 06:00")
            await send_excel_backup(application.bot, "Respaldo automático diario de las 06:00")
            await send_admin_menu(ADMIN_TELEGRAM_ID, application, "✅ *RESPALDO AUTOMÁTICO ENVIADO*\\n\\nPanel administrativo:")
        except Exception as e:
            print(f"❌ Error en respaldo automático: {e}")


# =========================================================
# COMANDOS ADMIN
# =========================================================

async def restore_database_document(update, context):
    """Restaura una copia SQLite enviada por el administrador."""
    if not private_only(update) or not is_admin(update.effective_user.id):
        return False
    if not context.user_data.get("await_restore_db"):
        return False
    document = update.message.document
    if not document or not (document.file_name or "").lower().endswith(".db"):
        await update.message.reply_text("⚠️ Envía un archivo de respaldo SQLite con extensión .db.", reply_markup=admin_keyboard())
        return True

    temp = os.path.join(BACKUP_DIR, f"restore_temp_{uuid.uuid4().hex}.db")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(temp)

        check = sqlite3.connect(temp)
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
        check.close()
        if str(result).lower() != "ok":
            raise RuntimeError("el archivo SQLite no pasó la comprobación de integridad")

        await send_db_backup(context.bot, "Respaldo de seguridad antes de restaurar")
        if os.path.exists(DB_FILE):
            shutil.copy2(DB_FILE, os.path.join(BACKUP_DIR, f"before_restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.db"))
        shutil.copy2(temp, DB_FILE)
        init_db()
        context.user_data.pop("await_restore_db", None)
        await send_admin_menu(ADMIN_TELEGRAM_ID, context, "✅ *BASE DE DATOS RESTAURADA*\n\nSe conservó un respaldo de seguridad de la base anterior.\n\nPanel administrativo:")
    except Exception as e:
        await update.message.reply_text(f"❌ *NO SE PUDO RESTAURAR LA BASE*\n\n`{e}`", parse_mode="Markdown", reply_markup=admin_keyboard())
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass
    return True

async def cancel_admin_restore(update, context):
    if not private_only(update) or not is_admin(update.effective_user.id):
        return
    context.user_data.pop("await_restore_db", None)
    await update.message.reply_text("❌ Restauración cancelada.", reply_markup=admin_keyboard())

async def admin_withdraw_proof_photo(update, context):
    if not private_only(update) or not is_admin(update.effective_user.id):
        return False
    withdrawal_id=context.user_data.get("admin_withdraw_proof_id")
    if not withdrawal_id:
        return False
    conn=db(); row=conn.execute("SELECT * FROM retiros WHERE id=? AND estado='aprobado'",(withdrawal_id,)).fetchone(); conn.close()
    if not row:
        context.user_data.pop("admin_withdraw_proof_id",None)
        await update.message.reply_text("⚠️ No se encontró el retiro aprobado.",reply_markup=admin_keyboard())
        return True
    try:
        if update.message.photo:
            file_id=update.message.photo[-1].file_id
            await context.bot.send_photo(chat_id=row["telegram_id"],photo=file_id,caption=f"📸 Comprobante de pago del retiro #{withdrawal_id}\n\n💸 Monto enviado: {money(row['monto'])} USDT")
        elif update.message.document:
            await context.bot.send_document(chat_id=row["telegram_id"],document=update.message.document.file_id,caption=f"📸 Comprobante de pago del retiro #{withdrawal_id}\n\n💸 Monto enviado: {money(row['monto'])} USDT")
        img=general_image("retiro enviado.jpg")
        if img:
            with open(img,"rb") as f:
                await context.bot.send_photo(chat_id=row["telegram_id"],photo=f)
        await update.message.reply_text(f"✅ Comprobante del retiro #{withdrawal_id} enviado exclusivamente al usuario.",reply_markup=admin_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudo enviar el comprobante: {e}",reply_markup=admin_keyboard())
    context.user_data.pop("admin_withdraw_proof_id",None)
    return True


async def manual_deposit_photo(update, context):
    if not private_only(update):
        return
    if is_admin(update.effective_user.id) and context.user_data.get("admin_withdraw_proof_id"):
        await admin_withdraw_proof_photo(update, context)
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
    if not private_only(update):
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    filename = f"database_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    path = os.path.join(BACKUP_DIR, filename)

    try:
        conn = db()
        backup_conn = sqlite3.connect(path)
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()

        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_TELEGRAM_ID,
                document=InputFile(f, filename=filename),
                caption="💾 Respaldo de la base de datos."
            )

        await send_admin_menu(
            ADMIN_TELEGRAM_ID,
            context,
            "✅ *RESPALDO CREADO*\n\nPanel administrativo:"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error creando respaldo:\n`{e}`",
            parse_mode="Markdown"
        )


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
    app.add_handler(CommandHandler("cancelar", cancel_admin_restore, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, manual_deposit_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, restore_database_document), group=0)
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, admin_withdraw_proof_photo), group=1)

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
