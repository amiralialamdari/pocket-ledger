"""Text reports and the data bundle sent to Gemini for analysis."""
import datetime as dt
import html

from . import charts
from .db import DB
from .utils import fa, jdate, money, period_range, previous_range, today

KIND_ICON = {"expense": "🔴", "income": "🟢", "transfer": "🔄", "adjust": "⚖️",
             "debt_in": "🤝⬅️", "debt_out": "🤝➡️"}


def _change(cur: int, prev: int) -> str:
    if not prev:
        return ""
    pct = round((cur - prev) * 100 / prev)
    arrow = "🔺" if pct > 0 else "🔻" if pct < 0 else "➖"
    return f" ({arrow} {fa(abs(pct))}٪ نسبت به دوره قبل)"


def balances_text(db: DB) -> str:
    rows = db.balances()
    if not rows:
        return "هنوز حسابی ثبت نکرده‌اید. با /addaccount یک حساب بسازید."
    lines = ["🏦 <b>موجودی حساب‌ها</b>", ""]
    for acc, bal in rows:
        card = f" (…{fa(acc['card_last4'])})" if acc.get("card_last4") else ""
        lines.append(f"• {html.escape(acc['name'])}{card}: <b>{money(bal)}</b>")
    lines += ["", f"💰 جمع کل: <b>{money(sum(b for _, b in rows))}</b>"]
    debts = db.debts()
    lent = sum(d["remaining"] for d in debts if d["direction"] == "lent")
    borrowed = sum(d["remaining"] for d in debts if d["direction"] == "borrowed")
    if lent or borrowed:
        lines.append(f"🤝 طلب: {money(lent)} | بدهی: {money(borrowed)}")
        lines.append(f"📌 دارایی خالص: <b>{money(sum(b for _, b in rows) + lent - borrowed)}</b>")
    return "\n".join(lines)


def tx_line(t: dict) -> str:
    icon = KIND_ICON.get(t["kind"], "•")
    acc = t.get("account_name") or "—"
    if t["kind"] == "transfer":
        acc = f"{acc} ← {t.get('to_account_name') or '—'}"
    desc = html.escape(t.get("description") or t.get("category") or "")
    return f"{icon} {money(t['amount'])} | {html.escape(t.get('category') or '')} | {html.escape(acc)}\n    {jdate(t['occurred_at'])} — {desc}"


def period_report(db: DB, period: str, ref: dt.date | None = None) -> tuple[str, list]:
    """Returns (html_text, [png buffers])."""
    start, end, label = period_range(period, ref)
    s = db.summary(start, end)
    ps, pe, _ = previous_range(period, ref)
    prev = db.summary(ps, pe)
    days_passed = max(min((today() - start).days + 1, s["days"]), 1)

    lines = [f"📊 <b>گزارش {label}</b>", ""]
    lines.append(f"🟢 درآمد: <b>{money(s['income'])}</b>{_change(s['income'], prev['income'])}")
    lines.append(f"🔴 هزینه: <b>{money(s['expense'])}</b>{_change(s['expense'], prev['expense'])}")
    lines.append(f"{'✅' if s['net'] >= 0 else '⚠️'} خالص: <b>{money(s['net'])}</b>")
    if period != "today" and s["expense"]:
        lines.append(f"📆 میانگین هزینه روزانه: {money(s['expense'] // days_passed)}")

    if s["by_expense"]:
        lines += ["", "<b>هزینه به تفکیک دسته:</b>"]
        for cat, amount in s["by_expense"].items():
            pct = round(amount * 100 / s["expense"])
            prev_amount = prev["by_expense"].get(cat, 0)
            diff = ""
            if prev_amount:
                d = round((amount - prev_amount) * 100 / prev_amount)
                diff = f" {'🔺' if d > 0 else '🔻'}{fa(abs(d))}٪" if d else ""
            lines.append(f"• {html.escape(cat)}: {money(amount)} ({fa(pct)}٪){diff}")
    if s["by_income"]:
        lines += ["", "<b>درآمد به تفکیک:</b>"]
        lines += [f"• {html.escape(c)}: {money(a)}" for c, a in s["by_income"].items()]
    if s["top_expenses"] and period != "today":
        lines += ["", "<b>بزرگ‌ترین هزینه‌ها:</b>"]
        lines += [f"• {money(t['amount'])} — {html.escape(t['description'] or t['category'] or '')} ({jdate(t['occurred_at'])})"
                  for t in s["top_expenses"][:3]]
    if period == "today":
        txs = db.transactions_between(start, end)
        if txs:
            lines += ["", "<b>تراکنش‌های امروز:</b>"] + [tx_line(t) for t in txs]
    if not s["count"]:
        lines += ["", "در این دوره تراکنشی ثبت نشده است."]

    images = []
    if period != "today":
        pie = charts.category_pie(s["by_expense"], f"هزینه‌های {label}")
        if pie:
            images.append(pie)
    if period in ("month", "last_month", "year"):
        trend = charts.trend_bars(db.monthly_trend(6, start))
        if trend:
            images.append(trend)
    return "\n".join(lines), images


def analysis_data(db: DB, period: str = "month") -> dict:
    start, end, label = period_range(period)
    ps, pe, plabel = previous_range(period)
    cur, prev = db.summary(start, end), db.summary(ps, pe)
    return {
        "دوره": label,
        "روزهای گذشته از دوره": max(min((today() - start).days + 1, cur["days"]), 1),
        "کل روزهای دوره": cur["days"],
        "این دوره": {"درآمد": cur["income"], "هزینه": cur["expense"], "هزینه به تفکیک": cur["by_expense"],
                     "درآمد به تفکیک": cur["by_income"],
                     "بزرگ‌ترین هزینه‌ها": [{"مبلغ": t["amount"], "شرح": t["description"], "دسته": t["category"]}
                                           for t in cur["top_expenses"]]},
        f"دوره قبل ({plabel})": {"درآمد": prev["income"], "هزینه": prev["expense"],
                                 "هزینه به تفکیک": prev["by_expense"]},
        "روند ۶ ماه اخیر": db.monthly_trend(6),
        "موجودی کل حساب‌ها": sum(b for _, b in db.balances()),
        "طلب‌ها": [{"از": d["person"], "مانده": d["remaining"]} for d in db.debts() if d["direction"] == "lent"],
        "بدهی‌ها": [{"به": d["person"], "مانده": d["remaining"]} for d in db.debts() if d["direction"] == "borrowed"],
        "اقساط و پرداخت‌های تکراری": [{"عنوان": r["title"], "مبلغ": r["amount"], "دوره": r["interval"],
                                       "اقساط باقی‌مانده": r["remaining_count"]} for r in db.recurring_list()],
    }
