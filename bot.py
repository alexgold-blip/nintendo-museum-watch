"""Interactive, multi-user Telegram bot for Nintendo Museum ticket availability.

Anyone can talk to the bot and watch specific dates. A background job polls the
official calendar JSON endpoint every CHECK_INTERVAL_MIN minutes and notifies
each subscriber when a watched date turns from sold-out to available.

The public calendar SPA (https://museum-tickets.nintendo.com/en/calendar) reads
its data from:
    GET /en/api/calendar?target_year=YYYY&target_month=M
    (requires header  X-Requested-With: XMLHttpRequest)

Per-day status is derived as:
    open_status == 2                -> closed (museum "off" day)
    open_status == 1, sale_status 1 -> available   ✅
    open_status == 1, sale_status 2 -> sold out

Run:  TELEGRAM_BOT_TOKEN=... python bot.py
"""

import datetime as dt
import logging
import os

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from store import Store

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("nintendo-museum-bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHECK_INTERVAL_MIN = float(os.getenv("CHECK_INTERVAL_MIN", "10"))
DB_PATH = os.getenv("DB_PATH", "data/nintendo.db")
CALENDAR_URL = "https://museum-tickets.nintendo.com/en/calendar"
API_URL = "https://museum-tickets.nintendo.com/en/api/calendar"

ADMIN_CHAT_IDS = {
    int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x
}

ENTER_DATES = 0  # single conversation state for /watch

store = Store(DB_PATH)


def is_admin(chat_id) -> bool:
    return chat_id in ADMIN_CHAT_IDS


# --------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------
async def fetch_month(client, year, month):
    r = await client.get(
        API_URL,
        params={"target_year": year, "target_month": month},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "User-Agent": "nintendo-museum-watch-bot/1.0",
        },
    )
    r.raise_for_status()
    return r.json()["data"]["calendar"]


async def fetch_dates(dates):
    """Return {iso_date: status_string} for the given YYYY-MM-DD dates."""
    months = sorted({(int(d[:4]), int(d[5:7])) for d in dates})
    out = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for year, month in months:
            cal = await fetch_month(client, year, month)
            for iso, v in cal.items():
                out[iso] = classify(v)
    return out


def classify(v: dict) -> str:
    if v is None:
        return "unknown"
    if v.get("open_status") == 2 or v.get("is_temporary_closure"):
        return "closed"
    if v.get("sale_status") == 1:
        return "available"
    if v.get("sale_status") == 2:
        return "soldout"
    return "unknown"


STATUS_RU = {
    "available": "✅ доступно",
    "soldout": "разобрано",
    "closed": "музей закрыт",
    "unknown": "нет данных",
    None: "не проверено",
}


# --------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------
def valid_date(token: str):
    try:
        return dt.date.fromisoformat(token).isoformat()
    except ValueError:
        return None


def parse_dates(text: str):
    """Accept 'YYYY-MM-DD', comma lists, and 'start..end' ranges."""
    result = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if ".." in chunk:
            a, b = chunk.split("..", 1)
            da, db = valid_date(a), valid_date(b)
            if not da or not db:
                return None
            cur, last = dt.date.fromisoformat(da), dt.date.fromisoformat(db)
            if last < cur or (last - cur).days > 62:
                return None
            while cur <= last:
                result.append(cur.isoformat())
                cur += dt.timedelta(days=1)
        else:
            iso = valid_date(chunk)
            if not iso:
                return None
            result.append(iso)
    seen, ordered = set(), []
    for d in result:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered or None


def sub_line(sub) -> str:
    return f"• {sub['date']}: {STATUS_RU.get(sub['last_status'], sub['last_status'])}"


# --------------------------------------------------------------------------
# Basic commands
# --------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎮 *Nintendo Museum Ticket Watch*\n\n"
        "Я слежу за официальным календарём билетов и пишу, когда выбранная "
        "дата становится доступной.\n\n"
        "Команды:\n"
        "/watch — добавить дату(ы) для отслеживания\n"
        "/list — мои даты\n"
        "/status — текущий статус по моим датам\n"
        "/stop — удалить даты\n"
        "/help — помощь",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться:\n\n"
        "1. /watch — введи дату в формате ГГГГ-ММ-ДД. Можно несколько через "
        "запятую (2026-10-10,2026-10-12) или диапазоном (2026-10-10..2026-10-12).\n"
        "2. Когда дата станет доступной, придёт уведомление.\n\n"
        "/status — посмотреть текущий статус и время последней проверки.\n"
        "/whoami — узнать свой chat_id.\n"
        f"Проверка идёт автоматически каждые {int(CHECK_INTERVAL_MIN)} мин.\n\n"
        "Покупка билетов — вручную на официальном сайте, бот только уведомляет."
    )


# --------------------------------------------------------------------------
# /watch conversation (date only)
# --------------------------------------------------------------------------
async def watch_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введи дату(ы) в формате ГГГГ-ММ-ДД.\n"
        "Примеры:\n"
        "  2026-10-10\n"
        "  2026-10-10,2026-10-12\n"
        "  2026-10-10..2026-10-12\n\n"
        "Отмена — /cancel"
    )
    return ENTER_DATES


async def watch_enter_dates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dates = parse_dates(update.message.text)
    if not dates:
        await update.message.reply_text(
            "Не понял даты. Формат ГГГГ-ММ-ДД, например 2026-10-10. Попробуй ещё раз."
        )
        return ENTER_DATES

    chat_id = update.effective_chat.id
    added = sum(store.add_subscription(chat_id, d) for d in dates)
    await update.message.reply_text(
        f"Готово. Слежу за {len(dates)} дат (новых добавлено: {added}).\n"
        "Текущий статус — /status."
    )
    ctx.application.create_task(run_check(ctx.application))
    return ConversationHandler.END


async def watch_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# /list and /stop
# --------------------------------------------------------------------------
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = store.list_subscriptions(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("У тебя пока нет дат. Добавь через /watch.")
        return
    await update.message.reply_text(
        "Твои даты:\n" + "\n".join(sub_line(s) for s in subs)
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = store.list_subscriptions(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("У тебя нет дат.")
        return
    rows = [
        [InlineKeyboardButton(f"🗑 {s['date']}", callback_data=f"del:{s['id']}")]
        for s in subs
    ]
    rows.append([InlineKeyboardButton("🗑 Удалить все", callback_data="delall")])
    await update.message.reply_text(
        "Что удалить?", reply_markup=InlineKeyboardMarkup(rows)
    )


async def stop_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    if q.data == "delall":
        n = store.clear_subscriptions(chat_id)
        await q.edit_message_text(f"Удалено дат: {n}.")
        return
    sub_id = int(q.data.split(":", 1)[1])
    ok = store.delete_subscription(chat_id, sub_id)
    await q.edit_message_text("Удалено." if ok else "Уже удалено.")


# --------------------------------------------------------------------------
# /status, /whoami, /admin
# --------------------------------------------------------------------------
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = store.list_subscriptions(update.effective_chat.id)
    if not subs:
        await update.message.reply_text("Нет дат. Добавь через /watch.")
        return
    last = store.get_meta("last_check_iso")
    header = f"Последняя проверка: {last} UTC\n" if last else "Ещё не проверялось.\n"
    await update.message.reply_text(header + "\n".join(sub_line(s) for s in subs))


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    role = "админ" if is_admin(cid) else "пользователь"
    await update.message.reply_text(
        f"Твой chat_id: {cid}\nРоль: {role}\n\n"
        "Чтобы стать админом, добавь это число в переменную окружения "
        "ADMIN_CHAT_IDS на хостинге и передеплой."
    )


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if not is_admin(cid):
        await update.message.reply_text(
            "Команда только для админа. Узнать свой id — /whoami."
        )
        return

    subs = store.all_subscriptions()
    last = store.get_meta("last_check_iso")
    users = len({s["chat_id"] for s in subs})

    header = (
        "📊 *Сводка бота*\n"
        f"Пользователей: {users}\n"
        f"Отслеживаний: {len(subs)}\n"
        f"Последняя проверка: {last or '—'} UTC\n"
    )
    if not subs:
        await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN)
        return

    groups = {}
    for s in subs:
        g = groups.setdefault(s["date"], {"watchers": 0, "status": s["last_status"]})
        g["watchers"] += 1
        if s["last_status"]:
            g["status"] = s["last_status"]

    lines = ["", "*По датам:*"]
    for date, g in sorted(groups.items()):
        state = STATUS_RU.get(g["status"], g["status"])
        lines.append(f"• {date}: {state} (следят: {g['watchers']})")

    await update.message.reply_text(
        (header + "\n".join(lines))[:4000], parse_mode=ParseMode.MARKDOWN
    )


# --------------------------------------------------------------------------
# Background availability check
# --------------------------------------------------------------------------
async def run_check(app: Application):
    subs = store.all_subscriptions()
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    if not subs:
        store.set_meta("last_check_iso", now)
        return

    dates = sorted({s["date"] for s in subs})
    try:
        statuses = await fetch_dates(dates)
    except Exception as exc:
        log.warning("calendar fetch failed: %s", exc)
        return

    store.set_meta("last_check_iso", now)

    for s in subs:
        status = statuses.get(s["date"], "unknown")
        was_notified = bool(s["notified"])

        if status == "available" and not was_notified:
            try:
                await app.bot.send_message(
                    chat_id=s["chat_id"],
                    text=(
                        "🎮 NINTENDO MUSEUM: билеты доступны!\n\n"
                        f"📅 Дата: {s['date']}\n\n"
                        f"Бронируй сразу:\n{CALENDAR_URL}"
                    ),
                )
            except Exception as exc:
                log.warning("send to %s failed: %s", s["chat_id"], exc)
            store.update_state(s["id"], status, notified=1)
        elif status != "available" and was_notified:
            # Became unavailable again -> re-arm for the next opening.
            store.update_state(s["id"], status, notified=0)
        else:
            store.update_state(s["id"], status, s["notified"])


async def job_check(ctx: ContextTypes.DEFAULT_TYPE):
    await run_check(ctx.application)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("watch", watch_start)],
        states={ENTER_DATES: [MessageHandler(filters.TEXT & ~filters.COMMAND, watch_enter_dates)]},
        fallbacks=[CommandHandler("cancel", watch_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern=r"^(del:|delall)"))

    app.job_queue.run_repeating(job_check, interval=CHECK_INTERVAL_MIN * 60, first=10)

    log.info("Bot started. Check interval = %s min.", CHECK_INTERVAL_MIN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
