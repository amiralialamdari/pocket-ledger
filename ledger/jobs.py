"""Scheduled jobs: installment reminders and automatic daily/weekly/monthly reports."""
import datetime as dt
import html
import logging

from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from .config import config
from .handlers import due_reminder_markup, send_analysis, send_report
from .utils import TZ, fa, jdate, money, period_range, to_jalali, today

log = logging.getLogger(__name__)


async def remind_due(ctx: ContextTypes.DEFAULT_TYPE):
    db = ctx.bot_data["db"]
    d = today()
    for r in db.due_recurring(d):
        due = dt.date.fromisoformat(r["next_due"])
        late = (d - due).days
        when = "امروز" if late == 0 else f"{fa(late)} روز گذشته ({jdate(due)})"
        count = f"\nاقساط باقی‌مانده: {fa(r['remaining_count'])}" if r["remaining_count"] is not None else ""
        verb = "دریافت" if r["kind"] == "income" else "پرداخت"
        await ctx.bot.send_message(
            config.owner_id,
            f"⏰ <b>یادآوری {verb}</b>\n«{html.escape(r['title'])}» — {money(r['amount'])}\nسررسید: {when}{count}",
            parse_mode=ParseMode.HTML, reply_markup=due_reminder_markup(r["id"]),
        )
        db.mark_reminded(r["id"], d)


async def daily_report(ctx: ContextTypes.DEFAULT_TYPE):
    db = ctx.bot_data["db"]
    start, end, _ = period_range("today")
    if db.transactions_between(start, end):
        await send_report(ctx, config.owner_id, "today")


async def weekly_report(ctx: ContextTypes.DEFAULT_TYPE):
    await send_report(ctx, config.owner_id, "week")


async def monthly_report(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs daily; on the 1st of each Jalali month sends last month's report and an AI analysis."""
    if to_jalali(today()).day != 1:
        return
    await ctx.bot.send_message(config.owner_id, "🗓 ماه جدید شروع شد! گزارش ماه گذشته:")
    await send_report(ctx, config.owner_id, "last_month", analysis_button=False)
    await send_analysis(ctx, config.owner_id, "last_month")


def schedule(app: Application) -> None:
    if not config.owner_id:
        log.warning("OWNER_ID not set; scheduled reports disabled")
        return
    jq = app.job_queue
    jq.run_daily(remind_due, dt.time(config.reminder_hour, 0, tzinfo=TZ), name="reminders")
    jq.run_daily(remind_due, dt.time(18, 0, tzinfo=TZ), name="reminders-evening")
    jq.run_daily(monthly_report, dt.time(config.reminder_hour, 5, tzinfo=TZ), name="monthly")
    # PTB weekdays: 0 = Sunday … 5 = Friday
    jq.run_daily(weekly_report, dt.time(21, 0, tzinfo=TZ), days=(5,), name="weekly")
    if config.daily_report_enabled:
        jq.run_daily(daily_report, dt.time(config.daily_report_hour, 0, tzinfo=TZ), name="daily")
