"""Pocket Ledger — personal accounting Telegram bot."""
import logging
import sys

import httpx
from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from ledger import handlers as h
from ledger.ai import AI
from ledger.config import config
from ledger.db import DB
from ledger.jobs import schedule

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("pocket-ledger")

COMMANDS = [
    ("balance", "موجودی حساب‌ها"), ("today", "گزارش امروز"), ("week", "گزارش این هفته"),
    ("month", "گزارش این ماه"), ("report", "انتخاب دوره گزارش"), ("analyze", "تحلیل هوشمند"),
    ("last", "آخرین تراکنش‌ها"), ("undo", "حذف آخرین تراکنش"), ("accounts", "حساب‌ها"),
    ("addaccount", "ساخت حساب"), ("debts", "طلب و بدهی"), ("recurring", "اقساط و تکراری‌ها"),
    ("export", "خروجی اکسل"), ("help", "راهنما"),
]


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    me = await app.bot.get_me()
    log.info("Running as @%s (owner id: %s, AI: %s)", me.username, config.owner_id or "NOT SET",
             config.gemini_model if config.gemini_api_key else "disabled")


def telegram_request(read_timeout: float = 20) -> HTTPXRequest:
    kwargs = {}
    if not config.telegram_proxy:
        # Bind to an IPv4 address: some hosts have IPv6 routes to Telegram that hang during TLS.
        kwargs["transport"] = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return HTTPXRequest(connect_timeout=20, read_timeout=read_timeout, write_timeout=30, pool_timeout=20,
                        proxy=config.telegram_proxy or None, httpx_kwargs=kwargs)


def main() -> None:
    if not config.bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")

    app = (ApplicationBuilder().token(config.bot_token).post_init(post_init)
           .request(telegram_request()).get_updates_request(telegram_request(read_timeout=40))
           .build())
    app.bot_data["db"] = DB(config.db_path)
    app.bot_data["ai"] = AI(config.gemini_api_key, config.gemini_model, list(config.gemini_fallback_models))

    commands = {
        "start": h.cmd_start, "help": h.cmd_help, "balance": h.cmd_balance, "today": h.cmd_today,
        "week": h.cmd_week, "month": h.cmd_month, "report": h.cmd_report, "analyze": h.cmd_analyze,
        "last": h.cmd_last, "undo": h.cmd_undo, "accounts": h.cmd_accounts, "addaccount": h.cmd_addaccount,
        "debts": h.cmd_debts, "recurring": h.cmd_recurring, "export": h.cmd_export,
    }
    for name, func in commands.items():
        app.add_handler(CommandHandler(name, func))
    app.add_handler(CallbackQueryHandler(h.on_callback))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, h.on_text))
    app.add_error_handler(h.on_error)
    schedule(app)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
