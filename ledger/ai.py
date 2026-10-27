"""Gemini integration: turn free text / bank SMS into structured actions, and write analyses."""
import json
import logging
import re

from google import genai
from google.genai import types

from .config import EXPENSE_CATEGORIES, INCOME_CATEGORIES
from .utils import extract_amount, jdate, latin_digits, now

log = logging.getLogger(__name__)

ACTION_TYPES = {"expense", "income", "transfer", "new_account", "debt", "debt_settle",
                "recurring", "balance_check", "report"}
PERIODS = {"today", "week", "month", "last_month", "year"}

PARSE_PROMPT = """تو حسابدار شخصی یک کاربر ایرانی هستی. پیام کاربر (یا پیامک بانکی فوروارد شده) را به اقدام‌های ساختاریافته تبدیل کن.

امروز: {today_j} (شمسی) — ساعت {time}
حساب‌های کاربر (id، نام، بانک، چهار رقم آخر کارت):
{accounts}

دسته‌بندی‌های هزینه: {exp_cats}
دسته‌بندی‌های درآمد: {inc_cats}

قوانین:
- همه‌ی مبالغ خروجی به «تومان» و عدد صحیح باشند. «۸۵ تومن» در محاوره معمولاً یعنی ۸۵ هزار تومان مگر واضحاً غیر از آن باشد. «تومن»=تومان.
- پیامک‌های بانکی ایران مبلغ و مانده را به «ریال» می‌نویسند؛ آن‌ها را تقسیم بر ۱۰ کن.
- در پیامک بانکی: برداشت/خرید = expense، واریز = income. اگر «مانده» دارد آن را در balance_after (تومان) بگذار.
- حساب را از روی نام بانک یا شماره کارت/حساب پیدا کن و account_id بده؛ اگر مطمئن نیستی null بگذار.
- دسته‌بندی را فقط از لیست‌های بالا انتخاب کن.
- تاریخ‌ها را به شمسی با فرمت YYYY/MM/DD بده. اگر تاریخ ذکر نشده date را null بگذار (یعنی امروز). «دیروز» را حساب کن.
- انواع اقدام (type):
  expense / income: هزینه یا درآمد. فیلدها: amount, account_id, category, description, date, balance_after
  transfer: جابجایی پول بین دو حساب خود کاربر. فیلدها: amount, account_id (مبدا), to_account_id (مقصد), description, date
  new_account: ساخت حساب جدید. فیلدها: account_name, bank, card_last4, initial_balance
  debt: قرض دادن یا گرفتن. direction = "lent" (کاربر به کسی پول داده و طلبکار است) یا "borrowed" (کاربر بدهکار است). فیلدها: person, amount, account_id (اگر پول از/به حسابی جابجا شده)، description, date
  debt_settle: پس دادن یا پس گرفتن قرض. فیلدها: person, amount, direction (همان جهت قرض اصلی)، account_id
  recurring: قسط، اجاره، اشتراک یا هر پرداخت/دریافت تکراری. فیلدها: description (عنوان)، amount، interval ("monthly"/"weekly"/"yearly")، count (تعداد اقساط باقی‌مانده یا null)، first_due (تاریخ شمسی سررسید بعدی)، recurring_kind ("expense" یا "income")، account_id, category
  balance_check: پیامک یا پیامی که فقط موجودی یک حساب را می‌گوید بدون تراکنش. فیلدها: account_id, balance_after
  report: کاربر گزارش یا سؤالی درباره خرج‌ها می‌پرسد. فیلد period یکی از today, week, month, last_month, year
- یک پیام ممکن است چند اقدام داشته باشد.
- اگر پیام ربطی به امور مالی ندارد، actions را خالی بگذار و در reply یک جواب کوتاه فارسی بنویس.

فقط JSON با این ساختار برگردان:
{{"actions": [{{"type": "...", "amount": 0, "account_id": null, "to_account_id": null, "category": null,
"description": "", "date": null, "balance_after": null, "person": null, "direction": null,
"interval": null, "count": null, "first_due": null, "recurring_kind": null,
"account_name": null, "bank": null, "card_last4": null, "initial_balance": null, "period": null}}],
"reply": null}}

پیام کاربر{sms_hint}:
\"\"\"{text}\"\"\"
"""

ANALYSIS_PROMPT = """تو یک مشاور مالی شخصی دلسوز و دقیق هستی. داده‌های مالی کاربر (به تومان) را بررسی کن و به فارسی روان تحلیل بده.
ساختار جواب:
۱. خلاصه وضعیت این دوره در ۲-۳ جمله
۲. مقایسه با دوره قبل (کدام دسته‌ها بیشتر/کمتر شده و چقدر)
۳. نکات و هشدارها (خرج‌های غیرعادی، اقساط، بدهی‌ها)
۴. سه پیشنهاد عملی و مشخص برای مدیریت بهتر خرج
کوتاه و کاربردی بنویس (حداکثر ۲۵۰ کلمه)، بدون Markdown و بدون علامت ستاره؛ می‌توانی از ایموجی استفاده کنی. اعداد را با جداکننده هزارگان بنویس.

داده‌ها:
{data}
"""


def _clean_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(latin_digits(str(value)).replace(",", ""))))
    except ValueError:
        return None


def normalize_actions(raw: dict, account_ids: set[int]) -> list[dict]:
    """Validate the model output; drop anything malformed."""
    actions = []
    for a in raw.get("actions") or []:
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            continue
        a = dict(a)
        for key in ("amount", "balance_after", "initial_balance", "count"):
            a[key] = _to_int(a.get(key))
        for key in ("account_id", "to_account_id"):
            v = _to_int(a.get(key))
            a[key] = v if v in account_ids else None
        if a["type"] in ("expense", "income", "transfer", "debt", "debt_settle", "recurring") \
                and not (a["amount"] and a["amount"] > 0):
            continue
        if a["type"] == "new_account" and not a.get("account_name"):
            continue
        if a["type"] == "balance_check" and a["balance_after"] is None:
            continue
        if a["type"] in ("debt", "debt_settle") and not a.get("person"):
            continue
        if a["type"] == "report" and a.get("period") not in PERIODS:
            a["period"] = "month"
        if a.get("direction") not in ("lent", "borrowed"):
            a["direction"] = None if a["type"] == "debt_settle" else "lent"
        if a.get("interval") not in ("weekly", "monthly", "yearly"):
            a["interval"] = "monthly"
        if a.get("recurring_kind") not in ("expense", "income"):
            a["recurring_kind"] = "expense"
        a["description"] = (a.get("description") or "").strip()
        actions.append(a)
    return actions


INCOME_HINTS = ("واریز", "حقوق", "دریافت", "درآمد", "گرفتم", "سود")


def fallback_parse(text: str) -> list[dict]:
    """Minimal offline parser used when Gemini is not configured or fails."""
    amount = extract_amount(text)
    if not amount:
        return []
    # Bare small numbers in casual Persian ("85 ناهار") mean thousands of Toman.
    if amount < 1000 and not re.search(r"تومن|تومان|ریال", text):
        amount *= 1000
    is_income = any(h in text for h in INCOME_HINTS)
    desc = re.sub(r"[\d۰-۹٠-٩,٬.]+|هزار|میلیون|ملیون|تومان|تومن|ریال", " ", text)
    return [{
        "type": "income" if is_income else "expense",
        "amount": amount,
        "account_id": None,
        "category": "سایر درآمد" if is_income else "سایر",
        "description": " ".join(desc.split())[:80],
        "date": None,
        "balance_after": None,
    }]


class AI:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = genai.Client(api_key=api_key) if api_key else None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def _generate(self, prompt: str, json_mode: bool) -> str:
        cfg = types.GenerateContentConfig(
            temperature=0.1 if json_mode else 0.6,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        resp = await self.client.aio.models.generate_content(model=self.model, contents=prompt, config=cfg)
        return resp.text or ""

    async def parse(self, text: str, accounts: list[dict], is_sms: bool = False) -> tuple[list[dict], str | None]:
        """Returns (actions, reply). Falls back to a regex parser if AI is unavailable."""
        ids = {a["id"] for a in accounts}
        if not self.enabled:
            return fallback_parse(text), None
        acc_lines = "\n".join(
            f"- id={a['id']} | {a['name']} | {a.get('bank') or '-'} | {a.get('card_last4') or '-'}" for a in accounts
        ) or "(هنوز حسابی ثبت نشده)"
        n = now()
        prompt = PARSE_PROMPT.format(
            today_j=latin_digits(jdate(n.date())), time=n.strftime("%H:%M"), accounts=acc_lines,
            exp_cats="، ".join(EXPENSE_CATEGORIES), inc_cats="، ".join(INCOME_CATEGORIES),
            sms_hint=" (این یک پیامک بانکی فوروارد شده است)" if is_sms else "", text=text,
        )
        try:
            raw = _clean_json(await self._generate(prompt, json_mode=True))
        except Exception:  # network errors, quota, bad JSON
            log.exception("Gemini parse failed; using fallback parser")
            return fallback_parse(text), None
        return normalize_actions(raw, ids), raw.get("reply")

    async def analyze(self, data: dict) -> str:
        if not self.enabled:
            return "برای تحلیل هوشمند، GEMINI_API_KEY را در فایل .env تنظیم کنید."
        try:
            text = await self._generate(
                ANALYSIS_PROMPT.format(data=json.dumps(data, ensure_ascii=False, indent=1)), json_mode=False
            )
            return text.replace("**", "").replace("##", "").strip() or "تحلیلی دریافت نشد."
        except Exception:
            log.exception("Gemini analysis failed")
            return "⚠️ ارتباط با Gemini برقرار نشد. کلید API یا اتصال اینترنت/پراکسی را بررسی کنید."
