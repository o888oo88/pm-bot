import re
import asyncio
import logging
import sqlite3
import requests
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = "8273670933:AAHxaLl92JcNm9nfDd2mOlMA8DEMLBiCQpo"
POLL_INTERVAL_SEC = 2

DATA_API = "https://data-api.polymarket.com/activity"
DB_PATH = Path(__file__).resolve().with_name("watch.db")

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pm-bot")

BOT_VERSION = "BUTTON_PANEL_v6_EDIT_LIST_ONE_MESSAGE"
print("=== RUNNING:", BOT_VERSION, "DB:", DB_PATH, "===")

# ====== состояния ввода ======
WAITING_ADDR = "WAITING_ADDR"
WAITING_MIN = "WAITING_MIN"
PENDING_MIN_ADDR = "PENDING_MIN_ADDR"

# message_id списка, чтобы обновлять его (на чат)
LIST_MSG_ID = "LIST_MSG_ID"

def reset_wait_states(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(WAITING_ADDR, None)
    context.user_data.pop(WAITING_MIN, None)
    context.user_data.pop(PENDING_MIN_ADDR, None)

# ================= БАЗА =================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            last_seen_ts INTEGER NOT NULL DEFAULT 0,
            min_usdc REAL NOT NULL DEFAULT 0,
            paused INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, address)
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(watches)").fetchall()]
    if "paused" not in cols:
        conn.execute("ALTER TABLE watches ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
    return conn


def normalize(addr: str) -> str:
    return addr.lower().strip()


def parse_amount(s: str) -> float:
    s = s.strip().replace("_", "").replace(",", "")
    return float(s)


def trade_usdc(t: dict) -> float:
    try:
        return float(t.get("usdcSize") or 0)
    except Exception:
        return 0.0

# ================= POLYMARKET API =================

def fetch_latest_trades(address: str, limit: int = 30):
    r = requests.get(DATA_API, params={
        "user": address,
        "type": "TRADE",
        "limit": limit,
        "offset": 0,
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    }, timeout=15)

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "2"))
        raise RuntimeError(f"RATE_LIMIT:{retry_after}")

    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def polymarket_url(t: dict):
    e = t.get("eventSlug")
    m = t.get("slug")
    if e and m:
        return f"https://polymarket.com/event/{e}/{m}"
    if e:
        return f"https://polymarket.com/event/{e}"
    return None

# ================= ФОРМАТ (как на скрине) =================

def format_trade_like_screenshot(addr: str, t: dict) -> str:
    title = t.get("title") or "(без названия)"
    outcome = t.get("outcome") or "-"
    side = t.get("side") or "TRADE"
    usdc = trade_usdc(t)

    lines = [
        addr,
        "🧾 Сделка",
        f"📌 {title}",
        f"🎯 {outcome}",
        f"🧭 {side}",
        f"💰 {round(usdc, 2)} USDC",
    ]

    url = polymarket_url(t)
    if url:
        lines.append("🌐 Открыть событие")
        lines.append(url)

    return "\n".join(lines)

# ================= UI =================

def panel_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Watch", callback_data="panel:watch")],
        [InlineKeyboardButton("📋 List", callback_data="panel:list")],
        [InlineKeyboardButton("🗑 Clear all", callback_data="panel:clear_confirm")],
    ])

def back_to_panel_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="nav:panel")]
    ])

def clear_confirm_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить всё", callback_data="panel:clear_yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="nav:panel")],
    ])

# ================= DB helpers =================

async def add_watch(chat_id: int, addr: str):
    last_seen_ts = 0
    try:
        trades = fetch_latest_trades(addr, limit=1)
        if trades:
            last_seen_ts = int(trades[0].get("timestamp") or 0)
    except Exception:
        pass

    conn = db()
    with conn:
        cur = conn.execute(
            "SELECT min_usdc, paused FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        row = cur.fetchone()
        old_min = float(row[0]) if row else 0.0
        old_paused = int(row[1]) if row else 0

        conn.execute(
            "INSERT OR REPLACE INTO watches(chat_id, address, last_seen_ts, min_usdc, paused) VALUES(?,?,?,?,?)",
            (chat_id, addr, last_seen_ts, old_min, old_paused)
        )
    conn.close()

def delete_watch(chat_id: int, addr: str) -> bool:
    conn = db()
    with conn:
        cur = conn.execute(
            "DELETE FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        deleted = cur.rowcount
    conn.close()
    return bool(deleted)

def set_min(chat_id: int, addr: str, val: float) -> bool:
    conn = db()
    with conn:
        cur = conn.execute(
            "SELECT 1 FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        )
        if not cur.fetchone():
            conn.close()
            return False
        conn.execute(
            "UPDATE watches SET min_usdc=? WHERE chat_id=? AND address=?",
            (float(val), chat_id, addr)
        )
    conn.close()
    return True

def toggle_pause(chat_id: int, addr: str) -> bool | None:
    conn = db()
    with conn:
        row = conn.execute(
            "SELECT paused FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr)
        ).fetchone()
        if not row:
            conn.close()
            return None
        new_val = 0 if int(row[0]) else 1
        conn.execute(
            "UPDATE watches SET paused=? WHERE chat_id=? AND address=?",
            (new_val, chat_id, addr)
        )
    conn.close()
    return bool(new_val)

def clear_all(chat_id: int) -> int:
    conn = db()
    with conn:
        cur = conn.execute("DELETE FROM watches WHERE chat_id=?", (chat_id,))
        deleted = cur.rowcount
    conn.close()
    return int(deleted)

# ================= LIST SCREEN (build + render) =================

def build_list_screen(chat_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT address, min_usdc, paused FROM watches WHERE chat_id=? ORDER BY address",
        (chat_id,)
    ).fetchall()
    conn.close()

    if not rows:
        text = "Список пуст."
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="nav:panel")]
        ])
        return text, markup

    text_lines = ["📌 Отслеживаемые адреса:"]
    buttons = []

    for addr, min_usdc, paused in rows:
        status = "⏸ paused" if int(paused) else "▶️ active"
        text_lines.append(f"• {addr} — min {float(min_usdc)} USDC — {status}")

        pause_btn = InlineKeyboardButton(("▶️ Resume" if int(paused) else "⏸ Pause"), callback_data=f"pause:{addr}")
        buttons.append([
            pause_btn,
            InlineKeyboardButton("💰 Min", callback_data=f"min:{addr}"),
            InlineKeyboardButton("❌ Unwatch", callback_data=f"del:{addr}"),
        ])

    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="nav:panel")])
    return "\n".join(text_lines), InlineKeyboardMarkup(buttons)

async def show_list_screen(chat_id: int, context: ContextTypes.DEFAULT_TYPE, *, prefer_edit: bool = True, edit_from=None):
    """
    prefer_edit=True: пытаемся редактировать уже существующее сообщение списка
    edit_from: callback_query.message (если вызвано из кнопок), тогда редактируем его в первую очередь
    """
    text, markup = build_list_screen(chat_id)

    # 1) если пришло из callback — лучше редактировать текущее сообщение
    if prefer_edit and edit_from is not None:
        try:
            await edit_from.edit_text(text=text, reply_markup=markup)
            context.user_data[LIST_MSG_ID] = edit_from.message_id
            return
        except Exception:
            pass

    # 2) пробуем редактировать сохранённое сообщение списка
    if prefer_edit:
        mid = context.user_data.get(LIST_MSG_ID)
        if mid:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=mid,
                    text=text,
                    reply_markup=markup
                )
                return
            except Exception:
                # если сообщение удалили/слишком старое — отправим новое
                context.user_data.pop(LIST_MSG_ID, None)

    # 3) отправляем новое и запоминаем message_id
    msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    context.user_data[LIST_MSG_ID] = msg.message_id

# ================= CALLBACKS =================

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id

    if data == "nav:panel":
        reset_wait_states(context)
        await q.message.reply_text("Панель:", reply_markup=ReplyKeyboardRemove())
        return await q.message.reply_text("Выбери действие:", reply_markup=panel_markup())

    if data == "panel:watch":
        reset_wait_states(context)
        context.user_data[WAITING_ADDR] = True
        return await q.message.reply_text("Введи адрес 0x... (просто сообщением):", reply_markup=back_to_panel_markup())

    if data == "panel:list":
        reset_wait_states(context)
        # редактируем текущее (панельное) сообщение в список
        return await show_list_screen(chat_id, context, prefer_edit=True, edit_from=q.message)

    if data == "panel:clear_confirm":
        reset_wait_states(context)
        return await q.message.reply_text("Точно удалить ВСЕ адреса из этого чата?", reply_markup=clear_confirm_markup())

    if data == "panel:clear_yes":
        reset_wait_states(context)
        n = clear_all(chat_id)
        await q.message.reply_text(f"🗑 Удалено адресов: {n}")
        await q.message.reply_text("Панель:", reply_markup=ReplyKeyboardRemove())
        return await q.message.reply_text("Выбери действие:", reply_markup=panel_markup())

    if data.startswith("del:"):
        reset_wait_states(context)
        addr = data.split(":", 1)[1]
        delete_watch(chat_id, addr)
        # обновим текущий экран списка (edit)
        return await show_list_screen(chat_id, context, prefer_edit=True, edit_from=q.message)

    if data.startswith("pause:"):
        reset_wait_states(context)
        addr = data.split(":", 1)[1]
        toggle_pause(chat_id, addr)
        return await show_list_screen(chat_id, context, prefer_edit=True, edit_from=q.message)

    if data.startswith("min:"):
        addr = data.split(":", 1)[1]
        reset_wait_states(context)
        context.user_data[WAITING_MIN] = True
        context.user_data[PENDING_MIN_ADDR] = addr
        return await q.message.reply_text(f"Введи новый min (USDC) для:\n{addr}\nНапример: 10000", reply_markup=back_to_panel_markup())

# ================= TEXT INPUT =================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    if context.user_data.get(WAITING_ADDR):
        addr = normalize(txt)

        if not ADDR_RE.match(addr):
            context.user_data[WAITING_ADDR] = True
            return await update.message.reply_text("❌ Неверный адрес. Введи 0x... ещё раз:", reply_markup=back_to_panel_markup())

        context.user_data[WAITING_ADDR] = False
        await add_watch(chat_id, addr)
        reset_wait_states(context)

        await update.message.reply_text(f"✅ Добавил {addr}")
        # после добавления — сразу показываем список (в одном сообщении)
        return await show_list_screen(chat_id, context, prefer_edit=True)

    if context.user_data.get(WAITING_MIN):
        addr = context.user_data.get(PENDING_MIN_ADDR)
        if not addr:
            reset_wait_states(context)
            return await update.message.reply_text("Ошибка. Нажми 💰 Min заново.", reply_markup=panel_markup())

        try:
            val = parse_amount(txt)
            if val < 0:
                raise ValueError
        except Exception:
            return await update.message.reply_text("❌ Введи число, например 10000", reply_markup=back_to_panel_markup())

        set_min(chat_id, addr, val)
        reset_wait_states(context)

        await update.message.reply_text("✅ Порог обновлён")
        return await show_list_screen(chat_id, context, prefer_edit=True)

# ================= COMMANDS =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_wait_states(context)
    await update.message.reply_text("Панель:", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("✅ Панель управления\nВыбери действие:", reply_markup=panel_markup())

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_wait_states(context)
    await update.message.reply_text("Панель:", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("Выбери действие:", reply_markup=panel_markup())

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Версия: {BOT_VERSION}\nDB: {DB_PATH}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_wait_states(context)
    await show_list_screen(update.effective_chat.id, context, prefer_edit=True)

# ================= POLL =================

async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, address, last_seen_ts, min_usdc, paused FROM watches"
    ).fetchall()
    conn.close()

    for chat_id, addr, last_ts, min_usdc, paused in rows:
        if int(paused) == 1:
            continue

        try:
            trades = fetch_latest_trades(addr)
        except RuntimeError as e:
            if str(e).startswith("RATE_LIMIT:"):
                wait_s = int(str(e).split(":")[1])
                log.warning("Rate limit, sleep %s sec", wait_s)
                await asyncio.sleep(wait_s)
            continue
        except Exception as e:
            log.warning("Fetch error: %s", e)
            continue

        new_all = [t for t in trades if int(t.get("timestamp") or 0) > int(last_ts)]
        if not new_all:
            continue

        max_ts_all = max(int(t.get("timestamp") or 0) for t in new_all)

        for t in sorted(new_all, key=lambda x: int(x.get("timestamp") or 0)):
            if trade_usdc(t) < float(min_usdc):
                continue

            msg = format_trade_like_screenshot(addr, t)
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg,
                disable_web_page_preview=False
            )

        conn2 = db()
        with conn2:
            conn2.execute(
                "UPDATE watches SET last_seen_ts=? WHERE chat_id=? AND address=?",
                (max_ts_all, chat_id, addr)
            )
        conn2.close()

# ================= MAIN =================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("❌ Вставь токен в BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("list", cmd_list))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SEC, first=3)

    log.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
