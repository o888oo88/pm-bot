import os
import re
import asyncio
import logging
import sqlite3
import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= НАСТРОЙКИ =================

BOT_TOKEN = "8273670933:AAHxaLl92JcNm9nfDd2mOlMA8DEMLBiCQpo"
POLL_INTERVAL_SEC = 2

DATA_API = "https://data-api.polymarket.com/activity"
DB_PATH = "watch.db"

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pm-bot")

# ================= БАЗА =================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            last_seen_ts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, address)
        )
    """)
    # настройки на чат: порог суммы в USDC
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            min_usdc REAL NOT NULL DEFAULT 0
        )
    """)
    return conn


def normalize(addr: str) -> str:
    return addr.lower().strip()


def parse_amount(s: str) -> float:
    """
    /min 10_000
    /min 10,000
    /min 10000
    """
    s = s.strip().replace("_", "").replace(",", "")
    return float(s)


def get_min_usdc(conn, chat_id: int) -> float:
    cur = conn.execute("SELECT min_usdc FROM chat_settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    return float(row[0]) if row else 0.0


def set_min_usdc(conn, chat_id: int, value: float):
    with conn:
        conn.execute(
            "INSERT INTO chat_settings(chat_id, min_usdc) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET min_usdc=excluded.min_usdc",
            (chat_id, value),
        )

# ================= POLYMARKET API =================

def fetch_latest_trades(address: str, limit: int = 30):
    params = {
        "user": address,
        "type": "TRADE",
        "limit": limit,
        "offset": 0,
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    }

    r = requests.get(DATA_API, params=params, timeout=15)

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "2"))
        raise RuntimeError(f"RATE_LIMIT:{retry_after}")

    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []

# ================= ФОРМАТ СИГНАЛА =================

def format_trade(t: dict) -> str:
    title = t.get("title") or "(без названия)"
    side = t.get("side") or "TRADE"
    outcome = t.get("outcome") or "(не указан)"
    price = t.get("price")
    usdc = t.get("usdcSize")
    size = t.get("size")
    tx = t.get("transactionHash")

    lines = [
        "🧾 *Новая сделка*",
        f"📌 *Событие:* {title}",
        f"🎯 *Куда ставка:* {outcome}",
        f"🧭 *Действие:* {side}",
    ]

    if usdc is not None:
        try:
            usdc_val = round(float(usdc), 2)
            lines.append(f"💵 *Сумма:* {usdc_val} USDC")
        except Exception:
            pass

    if price is not None:
        lines.append(f"🏷 *Цена:* {price}")

    if size is not None:
        lines.append(f"📦 *Size:* {size}")

    if tx:
        lines.append(f"🔗 *Tx:* `{tx}`")

    return "\n".join(lines)

# ================= TELEGRAM COMMANDS =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = db()
    try:
        min_usdc = get_min_usdc(conn, chat_id)
    finally:
        conn.close()

    await update.message.reply_text(
        "🤖 Polymarket Signal Bot\n\n"
        "Команды:\n"
        "/watch 0x...  — начать отслеживание\n"
        "/unwatch 0x... — убрать адрес\n"
        "/list — список адресов\n"
        "/min <сумма> — минимальная сумма сделки для алерта (USDC)\n\n"
        f"Текущий порог: {min_usdc} USDC\n"
        "Пример: /min 10000"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Пример: /watch 0x1234...")

    addr = normalize(context.args[0])
    if not ADDR_RE.match(addr):
        return await update.message.reply_text("❌ Неверный формат адреса.")

    chat_id = update.effective_chat.id

    last_seen_ts = 0
    try:
        trades = fetch_latest_trades(addr, limit=1)
        if trades:
            last_seen_ts = int(trades[0].get("timestamp") or 0)
    except Exception:
        pass

    conn = db()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO watches(chat_id, address, last_seen_ts) VALUES(?,?,?)",
            (chat_id, addr, last_seen_ts),
        )
    conn.close()

    await update.message.reply_text(f"✅ Начал следить за {addr}")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Пример: /unwatch 0x1234...")

    addr = normalize(context.args[0])
    chat_id = update.effective_chat.id

    conn = db()
    with conn:
        cur = conn.execute(
            "DELETE FROM watches WHERE chat_id=? AND address=?",
            (chat_id, addr),
        )
        deleted = cur.rowcount
    conn.close()

    if deleted:
        await update.message.reply_text(f"🛑 Убрал {addr}")
    else:
        await update.message.reply_text("Адрес не найден.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    conn = db()
    cur = conn.execute(
        "SELECT address FROM watches WHERE chat_id=? ORDER BY address",
        (chat_id,),
    )
    rows = cur.fetchall()
    min_usdc = get_min_usdc(conn, chat_id)
    conn.close()

    if not rows:
        return await update.message.reply_text(
            f"Список пуст.\nТекущий порог: {min_usdc} USDC"
        )

    msg = [f"📌 Отслеживаемые адреса (порог {min_usdc} USDC):"]
    for (addr,) in rows:
        msg.append(f"• {addr}")

    await update.message.reply_text("\n".join(msg))


async def cmd_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = db()
    try:
        if not context.args:
            current = get_min_usdc(conn, chat_id)
            return await update.message.reply_text(
                f"🎛 Текущий порог: {current} USDC\n"
                "Установить: /min 10000\n"
                "Сбросить: /min 0"
            )

        try:
            value = parse_amount(context.args[0])
            if value < 0:
                raise ValueError("negative")
        except Exception:
            return await update.message.reply_text("❌ Пример: /min 10000 (или /min 0 для сброса)")

        set_min_usdc(conn, chat_id, float(value))
        await update.message.reply_text(f"✅ Порог установлен: {float(value)} USDC")
    finally:
        conn.close()

# ================= POLLING JOB =================

def trade_usdc(t: dict) -> float:
    try:
        return float(t.get("usdcSize") or 0)
    except Exception:
        return 0.0


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.execute("SELECT chat_id, address, last_seen_ts FROM watches")
    watches = cur.fetchall()

    for chat_id, addr, last_ts in watches:
        # порог для этого чата
        min_usdc = get_min_usdc(conn, chat_id)

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

        # все новые сделки (для обновления last_seen_ts)
        new_all = [
            t for t in trades
            if int(t.get("timestamp") or 0) > int(last_ts)
        ]

        if not new_all:
            continue

        # обновим last_seen_ts по всем новым, даже если не алертим
        max_ts_all = max(int(t.get("timestamp") or 0) for t in new_all)

        # фильтр по сумме (для алерта)
        new_alerts = [t for t in new_all if trade_usdc(t) >= float(min_usdc)]

        if new_alerts:
            new_alerts.sort(key=lambda x: int(x.get("timestamp") or 0))
            for t in new_alerts:
                text = f"👤 `{addr}`\n" + format_trade(t)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown"
                )

        # важно: сохраняем max_ts_all, чтобы не повторять мелкие сделки каждый цикл
        with conn:
            conn.execute(
                "UPDATE watches SET last_seen_ts=? WHERE chat_id=? AND address=?",
                (max_ts_all, chat_id, addr)
            )

    conn.close()

# ================= START =================

def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Укажи BOT_TOKEN в переменной окружения (export BOT_TOKEN=8273670933:AAHxaLl92JcNm9nfDd2mOlMA8DEMLBiCQpo)")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("min", cmd_min))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SEC, first=3)

    log.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
