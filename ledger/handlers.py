"""Telegram handlers: free-text/SMS intake, confirmation cards, reports and management screens."""
import functools
import html
import logging
import re

from telegram import (InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, InputMediaPhoto,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from .ai import AI
from .config import EXPENSE_CATEGORIES, INCOME_CATEGORIES, config
from .db import DB, DEBT_NEW, DEBT_PAY
from .export import build_excel
from .reports import analysis_data, balances_text, period_report, tx_line
from .utils import PERIOD_LABELS, extract_amount, fa, jdate, money, parse_jalali, today

log = logging.getLogger(__name__)
HTML = ParseMode.HTML

BTN_BALANCE = "💰 موجودی"
BTN_TODAY = "📅 امروز"
BTN_MONTH = "📊 گزارش ماه"
BTN_REPORTS = "🗂 گزارش‌ها"
BTN_ANALYZE = "🧠 تحلیل هوشمند"
BTN_DEBTS = "🤝 طلب و بدهی"
BTN_RECURRING = "🔁 اقساط"
BTN_LAST = "🧾 آخرین تراکنش‌ها"
BTN_ACCOUNTS = "🏦 حساب‌ها"
BTN_EXPORT = "📤 اکسل"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_BALANCE, BTN_TODAY, BTN_MONTH],
     [BTN_REPORTS, BTN_ANALYZE, BTN_LAST],
     [BTN_DEBTS, BTN_RECURRING, BTN_ACCOUNTS],
     [BTN_EXPORT]],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="مثلاً: ۸۵ هزار ناهار با کارت ملت",
)

TYPE_LABEL = {
    "expense": "🔴 هزینه", "income": "🟢 درآمد", "transfer": "🔄 انتقال بین حساب‌ها",
    "new_account": "🏦 حساب جدید", "debt": "🤝 طلب/بدهی جدید", "debt_settle": "✅ تسویه طلب/بدهی",
    "recurring": "🔁 قسط / پرداخت تکراری", "balance_check": "⚖️ تطبیق موجودی",
}
INTERVAL_FA = {"weekly": "هفتگی", "monthly": "ماهانه", "yearly": "سالانه"}

HELP_TEXT = f"""<b>👋 حسابدار شخصی شما</b>

کافی است مثل حرف زدن معمولی بنویسید، هوش مصنوعی خودش ثبت می‌کند:
• <code>۸۵ هزار ناهار با کارت ملت</code>
• <code>حقوق ۳۵ میلیون واریز شد به سامان</code>
• <code>۲ میلیون از ملت به سامان منتقل کردم</code>
• <code>۵ میلیون به علی قرض دادم</code>  /  <code>علی ۲ میلیونش رو پس داد</code>
• <code>قسط وام ۴ میلیون هر ماه ۱۵ام، ۱۸ قسط مونده</code>
• <code>یه حساب ملت با کارت ۱۲۳۴ و موجودی ۱۰ میلیون بساز</code>
• <code>این ماه چقدر خرج کردم؟</code>

📩 پیامک‌های بانک را هم <b>فوروارد یا کپی</b> کنید؛ مبلغ و مانده خوانده می‌شود و موجودی ربات با بانک تطبیق داده می‌شود.

قبل از ثبت، کارت تأیید نمایش داده می‌شود و می‌توانید حساب یا دسته را عوض کنید.

<b>دستورها:</b>
/balance موجودی‌ها · /today امروز · /week هفته · /month ماه
/report گزارش دوره‌ها · /analyze تحلیل هوشمند
/last آخرین تراکنش‌ها · /undo حذف آخرین تراکنش
/accounts حساب‌ها · /addaccount ساخت حساب
/debts طلب و بدهی · /recurring اقساط · /export خروجی اکسل

گزارش روزانه ساعت {fa(config.daily_report_hour)}، گزارش هفتگی جمعه‌ها و گزارش ماهانه اول هر ماه شمسی خودکار ارسال می‌شود."""


def _db(ctx: ContextTypes.DEFAULT_TYPE) -> DB:
    return ctx.bot_data["db"]


def _ai(ctx: ContextTypes.DEFAULT_TYPE) -> AI:
    return ctx.bot_data["ai"]


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        if user.id != config.owner_id:
            if config.owner_id == 0 and update.effective_message:
                await update.effective_message.reply_text(
                    f"آیدی عددی شما: {user.id}\nآن را در فایل .env به عنوان OWNER_ID قرار دهید و ربات را ری‌استارت کنید."
                )
            elif update.callback_query:
                await update.callback_query.answer()
            return
        return await func(update, ctx)
    return wrapper


def _acc_name(db: DB, acc_id: int | None) -> str:
    if not acc_id:
        return "❓ انتخاب نشده"
    acc = db.account(acc_id)
    return html.escape(acc["name"]) if acc else "❓"


# ---------------------------------------------------------------- confirmation cards
def _needs(a: dict) -> list[str]:
    """Account fields that must be filled before the action can be saved."""
    if a["type"] in ("expense", "income", "balance_check"):
        return ["account_id"] if not a.get("account_id") else []
    if a["type"] == "transfer":
        return [f for f in ("account_id", "to_account_id") if not a.get(f)]
    return []


def render_card(pid: int, a: dict, db: DB) -> tuple[str, Markup]:
    t = a["type"]
    lines = [f"<b>{TYPE_LABEL.get(t, t)}</b>", ""]
    if t == "new_account":
        lines.append(f"نام: <b>{html.escape(a['account_name'])}</b>")
        if a.get("bank"):
            lines.append(f"بانک: {html.escape(a['bank'])}")
        if a.get("card_last4"):
            lines.append(f"کارت: …{fa(a['card_last4'])}")
        lines.append(f"موجودی اولیه: {money(a.get('initial_balance') or 0)}")
    elif t == "balance_check":
        lines.append(f"موجودی طبق پیامک: <b>{money(a['balance_after'])}</b>")
        lines.append(f"حساب: {_acc_name(db, a.get('account_id'))}")
    else:
        lines.append(f"مبلغ: <b>{money(a['amount'])}</b>")
        if t == "transfer":
            lines.append(f"از: {_acc_name(db, a.get('account_id'))}")
            lines.append(f"به: {_acc_name(db, a.get('to_account_id'))}")
        elif t in ("debt", "debt_settle"):
            kind = {"lent": "طلب من (به او داده‌ام)", "borrowed": "بدهی من (از او گرفته‌ام)"}.get(a.get("direction"), "")
            lines.append(f"شخص: <b>{html.escape(a['person'])}</b> {('— ' + kind) if kind else ''}")
            lines.append(f"حساب: {_acc_name(db, a.get('account_id')) if a.get('account_id') else 'بدون جابجایی حساب'}")
        elif t == "recurring":
            lines.append(f"عنوان: {html.escape(a['description'] or '—')}")
            lines.append(f"تکرار: {INTERVAL_FA[a['interval']]}"
                         + (f" — {fa(a['count'])} قسط" if a.get("count") else ""))
            first = parse_jalali(a.get("first_due")) or today()
            lines.append(f"سررسید بعدی: {jdate(first)}")
            lines.append(f"حساب: {_acc_name(db, a.get('account_id')) if a.get('account_id') else '—'}")
        else:
            lines.append(f"حساب: {_acc_name(db, a.get('account_id'))}")
            lines.append(f"دسته: {html.escape(a.get('category') or ('سایر' if t == 'expense' else 'سایر درآمد'))}")
        if t != "recurring" and a.get("description"):
            lines.append(f"شرح: {html.escape(a['description'])}")
        if t in ("expense", "income", "transfer", "debt") and a.get("date"):
            when = parse_jalali(a["date"])
            if when and when != today():
                lines.append(f"تاریخ: {jdate(when)}")
        if a.get("balance_after") is not None:
            lines.append(f"مانده طبق پیامک: {money(a['balance_after'])}")

    missing = _needs(a)
    rows: list[list[Btn]] = []
    if missing:
        field = missing[0]
        lines += ["", "👇 " + ("حساب مقصد را انتخاب کنید:" if field == "to_account_id" else "حساب را انتخاب کنید:")]
        rows += _account_buttons(db, f"p:{pid}:set:{'t' if field == 'to_account_id' else 'a'}")
        rows.append([Btn("❌ لغو", callback_data=f"p:{pid}:no")])
    else:
        rows.append([Btn("✅ ثبت", callback_data=f"p:{pid}:ok"), Btn("❌ لغو", callback_data=f"p:{pid}:no")])
        extra = []
        if t in ("expense", "income", "debt", "debt_settle", "recurring", "balance_check") and db.accounts():
            extra.append(Btn("🏦 تغییر حساب", callback_data=f"p:{pid}:acc"))
        if t in ("expense", "income", "recurring"):
            extra.append(Btn("🏷 تغییر دسته", callback_data=f"p:{pid}:cat"))
        if extra:
            rows.append(extra)
    return "\n".join(lines), Markup(rows)


def _account_buttons(db: DB, prefix: str) -> list[list[Btn]]:
    accs = db.accounts()
    buttons = [Btn(a["name"], callback_data=f"{prefix}:{a['id']}") for a in accs]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _categories_for(a: dict) -> list[str]:
    kind = a.get("recurring_kind", "expense") if a["type"] == "recurring" else a["type"]
    return INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES


# ---------------------------------------------------------------- committing actions
def reconcile_message(db: DB, account_id: int, sms_balance: int) -> tuple[str, Markup | None]:
    bal = db.balance(account_id)
    name = _acc_name(db, account_id)
    if bal == sms_balance:
        return f"✅ موجودی {name} با پیامک بانک مطابقت دارد: {money(bal)}", None
    diff = sms_balance - bal
    text = (f"⚠️ <b>اختلاف موجودی {name}</b>\n"
            f"طبق پیامک بانک: {money(sms_balance)}\n"
            f"طبق ربات: {money(bal)}\n"
            f"اختلاف: <b>{money(diff)}</b>\n\n"
            "احتمالاً تراکنشی ثبت نشده است. می‌توانید موجودی را با بانک یکی کنید.")
    return text, Markup([[Btn("⚖️ اصلاح موجودی طبق بانک", callback_data=f"adj:{account_id}:{sms_balance}")]])


def commit(db: DB, a: dict) -> tuple[str, Markup | None, list[tuple[str, Markup | None]]]:
    """Persist an action. Returns (card text, card markup, follow-up messages)."""
    t = a["type"]
    when = parse_jalali(a.get("date"))
    src, raw = a.get("source", "text"), a.get("raw_text")
    follow: list[tuple[str, Markup | None]] = []

    if t in ("expense", "income"):
        cat = a.get("category") or ("سایر" if t == "expense" else "سایر درآمد")
        tx = db.add_transaction(t, a["amount"], a["account_id"], category=cat, description=a["description"],
                                occurred_at=when, source=src, raw_text=raw)
        text = f"✅ ثبت شد: {TYPE_LABEL[t]} {money(a['amount'])} — {html.escape(cat)}\n" \
               f"🏦 موجودی {_acc_name(db, a['account_id'])}: {money(db.balance(a['account_id']))}"
        if a.get("balance_after") is not None:
            follow.append(reconcile_message(db, a["account_id"], a["balance_after"]))
        return text, Markup([[Btn("↩️ حذف این تراکنش", callback_data=f"tx:del:{tx}")]]), follow

    if t == "transfer":
        tx = db.add_transaction("transfer", a["amount"], a["account_id"], to_account_id=a["to_account_id"],
                                category="انتقال", description=a["description"], occurred_at=when,
                                source=src, raw_text=raw)
        text = (f"✅ انتقال {money(a['amount'])} ثبت شد\n"
                f"{_acc_name(db, a['account_id'])}: {money(db.balance(a['account_id']))}\n"
                f"{_acc_name(db, a['to_account_id'])}: {money(db.balance(a['to_account_id']))}")
        return text, Markup([[Btn("↩️ حذف", callback_data=f"tx:del:{tx}")]]), follow

    if t == "new_account":
        last4 = re.sub(r"\D", "", str(a.get("card_last4") or ""))[-4:] or None
        acc_id = db.add_account(a["account_name"], a.get("bank"), last4, a.get("initial_balance") or 0)
        return f"✅ حساب «{html.escape(a['account_name'])}» با موجودی {money(db.balance(acc_id))} ساخته شد.", None, follow

    if t == "debt":
        debt_id = db.add_debt(a["person"], a["direction"], a["amount"], a["description"], a.get("account_id"))
        if a.get("account_id"):
            kind = "debt_out" if a["direction"] == "lent" else "debt_in"
            db.add_transaction(kind, a["amount"], a["account_id"], category=DEBT_NEW,
                               description=f"{a['person']}: {a['description']}".strip(": "),
                               occurred_at=when, source=src, raw_text=raw, debt_id=debt_id)
        word = "طلب از" if a["direction"] == "lent" else "بدهی به"
        return f"✅ {word} {html.escape(a['person'])}: {money(a['amount'])} ثبت شد.", None, follow

    if t == "debt_settle":
        debts = db.find_open_debts(a["person"], a.get("direction"))
        if not debts:
            return f"❗️ طلب یا بدهی بازی با نام «{html.escape(a['person'])}» پیدا نشد. /debts", None, follow
        left = a["amount"]
        for d in debts:
            if left <= 0:
                break
            res = db.pay_debt(d["id"], left)
            left -= res["paid"]
            acc = a.get("account_id") or d["account_id"]
            if acc:
                kind = "debt_in" if d["direction"] == "lent" else "debt_out"
                db.add_transaction(kind, res["paid"], acc, category=DEBT_PAY,
                                   description=f"{d['person']}: {a['description']}".strip(": "),
                                   source=src, raw_text=raw, debt_id=d["id"])
        remaining = sum(d["remaining"] for d in db.find_open_debts(a["person"], a.get("direction")))
        text = f"✅ {money(a['amount'] - left)} از حساب «{html.escape(a['person'])}» تسویه شد. مانده: {money(remaining)}"
        if left > 0:
            text += f"\n(مبلغ اضافه {money(left)} ثبت نشد)"
        return text, None, follow

    if t == "recurring":
        first = parse_jalali(a.get("first_due")) or today()
        db.add_recurring(a["description"] or "پرداخت تکراری", a["amount"], first, kind=a["recurring_kind"],
                         account_id=a.get("account_id"), category=a.get("category") or "قسط و وام",
                         interval=a["interval"], remaining_count=a.get("count"))
        return (f"✅ «{html.escape(a['description'] or 'پرداخت تکراری')}» ثبت شد. "
                f"سررسید بعدی {jdate(first)} یادآوری می‌شود."), None, follow

    if t == "balance_check":
        text, markup = reconcile_message(db, a["account_id"], a["balance_after"])
        return text, markup, follow

    return "نوع ناشناخته", None, follow


# ---------------------------------------------------------------- message intake
def _looks_like_sms(text: str) -> bool:
    return bool(re.search(r"مانده|برداشت|واریز|انتقال|خرید", text)) and bool(re.search(r"[\d۰-۹]{4,}", text))


@owner_only
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    route = {
        BTN_BALANCE: cmd_balance, BTN_TODAY: cmd_today, BTN_MONTH: cmd_month, BTN_REPORTS: cmd_report,
        BTN_ANALYZE: cmd_analyze, BTN_DEBTS: cmd_debts, BTN_RECURRING: cmd_recurring, BTN_LAST: cmd_last,
        BTN_ACCOUNTS: cmd_accounts, BTN_EXPORT: cmd_export,
    }.get(text)
    if route:
        return await route(update, ctx)

    db, ai = _db(ctx), _ai(ctx)
    accounts = db.accounts()
    is_sms = bool(msg.forward_origin) or _looks_like_sms(text)
    await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    actions, reply = await ai.parse(text, accounts, is_sms=is_sms)

    if not actions:
        await msg.reply_text(reply or "متوجه نشدم 🤔 مثلاً بنویسید: «۸۵ هزار ناهار با کارت ملت». راهنما: /help")
        return
    if not ai.enabled:
        await msg.reply_text("ℹ️ Gemini تنظیم نشده؛ با تحلیل ساده ثبت می‌شود. دسته را در صورت نیاز تغییر دهید.")

    for a in actions:
        if a["type"] == "report":
            await send_report(ctx, msg.chat_id, a["period"])
            continue
        if not accounts and _needs(a):
            await msg.reply_text("اول یک حساب بسازید، مثلاً: «حساب ملت با موجودی ۱۰ میلیون بساز» یا /addaccount")
            return
        for field in _needs(a):
            if len(accounts) == 1 and field == "account_id":
                a["account_id"] = accounts[0]["id"]
        a["source"] = "sms" if is_sms else "text"
        a["raw_text"] = text
        pid = db.save_pending(a)
        card, markup = render_card(pid, a, db)
        await msg.reply_text(card, parse_mode=HTML, reply_markup=markup)


# ---------------------------------------------------------------- callbacks
@owner_only
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = _db(ctx)
    parts = q.data.split(":")
    head = parts[0]
    try:
        if head == "p":
            await _pending_callback(q, ctx, db, int(parts[1]), parts[2:])
        elif head == "tx" and parts[1] == "del":
            tx = db.transaction(int(parts[2]))
            if tx and db.delete_transaction(tx["id"]):
                await q.edit_message_text(f"🗑 تراکنش {money(tx['amount'])} ({html.escape(tx['category'] or '')}) حذف شد.",
                                          parse_mode=HTML)
            else:
                await q.answer("قبلاً حذف شده است.")
        elif head == "adj":
            acc_id, target = int(parts[1]), int(parts[2])
            diff = target - db.balance(acc_id)
            if diff:
                db.add_transaction("adjust", diff, acc_id, category="اصلاح موجودی",
                                   description="تطبیق با پیامک بانک", source="sms")
            await q.edit_message_text(f"⚖️ موجودی {_acc_name(db, acc_id)} روی {money(target)} تنظیم شد.",
                                      parse_mode=HTML)
        elif head == "rep":
            await q.answer()
            await send_report(ctx, q.message.chat_id, parts[1])
        elif head == "an":
            await q.answer("در حال تحلیل…")
            await send_analysis(ctx, q.message.chat_id, parts[1])
        elif head == "d":
            await _debt_callback(q, db, int(parts[1]), parts[2])
        elif head == "r":
            await _recurring_callback(q, db, int(parts[1]), parts[2])
        elif head == "acc" and parts[1] == "arch":
            db.archive_account(int(parts[2]))
            await q.edit_message_text("🗄 حساب بایگانی شد (تراکنش‌هایش در گزارش‌ها باقی می‌ماند).")
        else:
            await q.answer()
    except Exception:
        log.exception("callback failed: %s", q.data)
        await q.answer("خطا رخ داد", show_alert=True)


async def _pending_callback(q, ctx, db: DB, pid: int, rest: list[str]):
    a = db.pending(pid)
    if a is None:
        await q.answer("این مورد منقضی شده است.")
        await q.edit_message_reply_markup(None)
        return
    action = rest[0]
    if action == "no":
        db.delete_pending(pid)
        await q.edit_message_text("❌ لغو شد.")
        return
    if action == "ok":
        if _needs(a):
            await q.answer("اول حساب را انتخاب کنید.")
            return
        text, markup, follow = commit(db, a)
        db.delete_pending(pid)
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
        for f_text, f_markup in follow:
            await q.message.reply_text(f_text, parse_mode=HTML, reply_markup=f_markup)
        return
    if action == "acc":
        await q.edit_message_reply_markup(Markup(_account_buttons(db, f"p:{pid}:set:a")
                                                 + [[Btn("↩️ بازگشت", callback_data=f"p:{pid}:back")]]))
        return
    if action == "cat":
        cats = _categories_for(a)
        buttons = [Btn(c, callback_data=f"p:{pid}:c:{i}") for i, c in enumerate(cats)]
        await q.edit_message_reply_markup(Markup([buttons[i:i + 2] for i in range(0, len(buttons), 2)]
                                                 + [[Btn("↩️ بازگشت", callback_data=f"p:{pid}:back")]]))
        return
    if action == "set":
        a["to_account_id" if rest[1] == "t" else "account_id"] = int(rest[2])
    elif action == "c":
        a["category"] = _categories_for(a)[int(rest[1])]
    db.update_pending(pid, a)
    text, markup = render_card(pid, a, db)
    await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup)


async def _debt_callback(q, db: DB, debt_id: int, action: str):
    d = db.debt(debt_id)
    if not d:
        await q.answer("پیدا نشد")
        return
    if action == "settle":
        amount = d["remaining"]
        db.pay_debt(debt_id, amount)
        if d["account_id"]:
            kind = "debt_in" if d["direction"] == "lent" else "debt_out"
            db.add_transaction(kind, amount, d["account_id"], category=DEBT_PAY,
                               description=f"{d['person']}: تسویه کامل", source="manual", debt_id=debt_id)
        await q.edit_message_text(f"✅ حساب با {html.escape(d['person'])} ({money(amount)}) تسویه شد.", parse_mode=HTML)
    elif action == "del":
        db.delete_debt(debt_id)
        await q.edit_message_text(f"🗑 مورد {html.escape(d['person'])} حذف شد.", parse_mode=HTML)


async def _recurring_callback(q, db: DB, rec_id: int, action: str):
    r = db.recurring(rec_id)
    if not r:
        await q.answer("پیدا نشد")
        return
    title = html.escape(r["title"])
    if action == "paid":
        acc = r["account_id"] or (db.accounts()[0]["id"] if len(db.accounts()) == 1 else None)
        tx = db.add_transaction(r["kind"], r["amount"], acc, category=r["category"], description=r["title"],
                                source="recurring")
        r = db.advance_recurring(rec_id)
        tail = f"سررسید بعدی: {jdate(r['next_due'])}" if r["active"] else "🎉 این مورد تمام شد!"
        if r["remaining_count"] is not None and r["active"]:
            tail += f" — {fa(r['remaining_count'])} قسط مانده"
        warn = "" if acc else "\n(حسابی مشخص نبود؛ موجودی حساب‌ها تغییر نکرد)"
        await q.edit_message_text(f"✅ «{title}» {money(r['amount'])} پرداخت و ثبت شد.\n{tail}{warn}", parse_mode=HTML,
                                  reply_markup=Markup([[Btn("↩️ حذف تراکنش", callback_data=f"tx:del:{tx}")]]))
    elif action == "skip":
        r = db.advance_recurring(rec_id)
        await q.edit_message_text(f"⏭ «{title}» این دوره ثبت نشد. سررسید بعدی: {jdate(r['next_due'])}", parse_mode=HTML)
    elif action == "del":
        db.deactivate_recurring(rec_id)
        await q.edit_message_text(f"🗑 «{title}» حذف شد.", parse_mode=HTML)


# ---------------------------------------------------------------- commands
@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=HTML, reply_markup=MAIN_KEYBOARD)
    if not _db(ctx).accounts():
        await update.effective_message.reply_text(
            "🏁 برای شروع حساب‌هایتان را بسازید. مثلاً بنویسید:\n"
            "«حساب ملت با کارت ۱۲۳۴ و موجودی ۱۰ میلیون بساز»"
        )


@owner_only
async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(balances_text(_db(ctx)), parse_mode=HTML)


async def send_report(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, period: str, analysis_button: bool = True):
    text, images = period_report(_db(ctx), period)
    markup = Markup([[Btn("🧠 تحلیل هوشمند این دوره", callback_data=f"an:{period}")]]) \
        if analysis_button and period != "today" else None
    if len(images) == 1:
        await ctx.bot.send_photo(chat_id, images[0])
    elif images:
        await ctx.bot.send_media_group(chat_id, [InputMediaPhoto(img) for img in images])
    await ctx.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup)


async def send_analysis(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, period: str = "month"):
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    text = await _ai(ctx).analyze(analysis_data(_db(ctx), period))
    await ctx.bot.send_message(chat_id, f"🧠 تحلیل {PERIOD_LABELS.get(period, '')}\n\n{text}")


@owner_only
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_report(ctx, update.effective_chat.id, "today")


@owner_only
async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_report(ctx, update.effective_chat.id, "week")


@owner_only
async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_report(ctx, update.effective_chat.id, "month")


@owner_only
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    buttons = [Btn(label, callback_data=f"rep:{key}") for key, label in PERIOD_LABELS.items()]
    await update.effective_message.reply_text("کدام دوره؟", reply_markup=Markup([buttons[:3], buttons[3:]]))


@owner_only
async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("🧠 تحلیل کدام دوره؟", reply_markup=Markup([[
        Btn("این ماه", callback_data="an:month"), Btn("ماه قبل", callback_data="an:last_month"),
        Btn("این هفته", callback_data="an:week"), Btn("امسال", callback_data="an:year"),
    ]]))


@owner_only
async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txs = _db(ctx).last_transactions(10)
    if not txs:
        await update.effective_message.reply_text("هنوز تراکنشی ثبت نشده است.")
        return
    lines = ["🧾 <b>آخرین تراکنش‌ها</b>", ""] + [f"{fa(i)}. {tx_line(t)}" for i, t in enumerate(txs, 1)]
    buttons = [Btn(f"🗑 {fa(i)}", callback_data=f"tx:del:{t['id']}") for i, t in enumerate(txs, 1)]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=HTML,
                                              reply_markup=Markup([buttons[:5], buttons[5:]]))


@owner_only
async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = _db(ctx)
    txs = db.last_transactions(1)
    if not txs:
        await update.effective_message.reply_text("تراکنشی برای حذف نیست.")
        return
    db.delete_transaction(txs[0]["id"])
    await update.effective_message.reply_text("🗑 حذف شد:\n" + tx_line(txs[0]), parse_mode=HTML)


@owner_only
async def cmd_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = _db(ctx)
    rows = db.balances()
    if not rows:
        await update.effective_message.reply_text("حسابی ندارید. /addaccount")
        return
    await update.effective_message.reply_text(balances_text(db), parse_mode=HTML, reply_markup=Markup(
        [[Btn(f"🗄 بایگانی {a['name']}", callback_data=f"acc:arch:{a['id']}")] for a, _ in rows]
    ))


@owner_only
async def cmd_addaccount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(ctx.args or [])
    if not raw:
        await update.effective_message.reply_text(
            "دو روش:\n"
            "۱) بنویسید: «حساب ملت با کارت ۱۲۳۴ و موجودی ۱۰ میلیون بساز»\n"
            "۲) /addaccount نام | موجودی | ۴رقم‌آخرکارت\n"
            "مثال: <code>/addaccount ملت | 10000000 | 1234</code>", parse_mode=HTML)
        return
    parts = [p.strip() for p in raw.split("|")]
    balance = extract_amount(parts[1]) if len(parts) > 1 else 0
    last4 = re.sub(r"\D", "", parts[2])[-4:] if len(parts) > 2 else None
    db = _db(ctx)
    acc_id = db.add_account(parts[0], parts[0], last4 or None, balance or 0)
    await update.effective_message.reply_text(
        f"✅ حساب «{html.escape(parts[0])}» با موجودی {money(db.balance(acc_id))} ساخته شد.", parse_mode=HTML)


@owner_only
async def cmd_debts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    debts = _db(ctx).debts()
    if not debts:
        await update.effective_message.reply_text(
            "🤝 طلب یا بدهی بازی ندارید.\nبرای ثبت بنویسید: «۵ میلیون به علی قرض دادم»")
        return
    lent = [d for d in debts if d["direction"] == "lent"]
    borrowed = [d for d in debts if d["direction"] == "borrowed"]
    lines = ["🤝 <b>طلب و بدهی</b>"]
    for title, group in (("🟢 طلب‌های من", lent), ("🔴 بدهی‌های من", borrowed)):
        if group:
            lines += ["", f"<b>{title}</b> — جمع {money(sum(d['remaining'] for d in group))}"]
            lines += [f"• {html.escape(d['person'])}: {money(d['remaining'])}"
                      + (f" از {money(d['amount'])}" if d["remaining"] != d["amount"] else "")
                      + (f" — {html.escape(d['description'])}" if d["description"] else "")
                      + f" ({jdate(d['created_at'])})" for d in group]
    lines += ["", "برای تسویه بخشی بنویسید: «علی ۲ میلیون پس داد»"]
    rows = [[Btn(f"✅ تسویه {d['person']}", callback_data=f"d:{d['id']}:settle"),
             Btn("🗑", callback_data=f"d:{d['id']}:del")] for d in debts]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=HTML, reply_markup=Markup(rows))


@owner_only
async def cmd_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = _db(ctx).recurring_list()
    if not items:
        await update.effective_message.reply_text(
            "🔁 قسط یا پرداخت تکراری ثبت نشده.\nمثال: «قسط وام ۴ میلیون هر ماه ۱۵ام، ۱۸ قسط مونده»")
        return
    lines = ["🔁 <b>اقساط و پرداخت‌های تکراری</b>", ""]
    monthly_total = 0
    for r in items:
        count = f" — {fa(r['remaining_count'])} قسط مانده" if r["remaining_count"] is not None else ""
        sign = "🟢" if r["kind"] == "income" else "🔴"
        lines.append(f"{sign} {html.escape(r['title'])}: {money(r['amount'])} {INTERVAL_FA[r['interval']]}{count}\n"
                     f"    سررسید بعدی: {jdate(r['next_due'])}")
        if r["kind"] == "expense":
            monthly_total += {"weekly": r["amount"] * 52 // 12, "monthly": r["amount"],
                              "yearly": r["amount"] // 12}[r["interval"]]
    lines += ["", f"📌 تعهد ماهانه تقریبی: <b>{money(monthly_total)}</b>"]
    rows = [[Btn(f"✅ پرداخت {r['title'][:20]}", callback_data=f"r:{r['id']}:paid"),
             Btn("🗑", callback_data=f"r:{r['id']}:del")] for r in items]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=HTML, reply_markup=Markup(rows))


@owner_only
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    buf = build_excel(_db(ctx))
    name = f"ledger-{jdate(today()).replace('/', '-')}.xlsx"
    await update.effective_message.reply_document(buf, filename=name, caption="📤 خروجی کامل حساب‌ها")


@owner_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=HTML, reply_markup=MAIN_KEYBOARD)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


def due_reminder_markup(rec_id: int) -> Markup:
    return Markup([[Btn("✅ پرداخت شد", callback_data=f"r:{rec_id}:paid"),
                    Btn("⏭ رد این دوره", callback_data=f"r:{rec_id}:skip")]])
