"""Helpers for Persian digits, money formatting and Jalali periods."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

import jdatetime

TZ = ZoneInfo("Asia/Tehran")

_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TO_FA = str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬")

JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def latin_digits(text: str) -> str:
    return text.translate(_TO_LATIN)


def fa(value) -> str:
    return str(value).translate(_TO_FA)


def money(amount: int, unit: bool = True) -> str:
    sign = "−" if amount < 0 else ""
    text = sign + fa(f"{abs(int(amount)):,}")
    return f"{text} تومان" if unit else text


def now() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def today() -> dt.date:
    return now().date()


def to_jalali(d: dt.date) -> jdatetime.date:
    return jdatetime.date.fromgregorian(date=d)


def jdate(d: dt.date | dt.datetime | str) -> str:
    if isinstance(d, str):
        d = dt.datetime.fromisoformat(d)
    if isinstance(d, dt.datetime):
        d = d.date()
    return fa(to_jalali(d).strftime("%Y/%m/%d"))


def jmonth_label(j: jdatetime.date) -> str:
    return f"{JALALI_MONTHS[j.month - 1]} {fa(j.year)}"


def parse_jalali(text: str | None) -> dt.date | None:
    """Parse 'YYYY/MM/DD' or 'YYYY-MM-DD' Jalali string into a Gregorian date."""
    if not text:
        return None
    m = re.match(r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})", latin_digits(str(text)))
    if not m:
        return None
    try:
        return jdatetime.date(*map(int, m.groups())).togregorian()
    except ValueError:
        return None


def jalali_first(year: int, month: int) -> dt.date:
    while month > 12:
        year, month = year + 1, month - 12
    while month < 1:
        year, month = year - 1, month + 12
    return jdatetime.date(year, month, 1).togregorian()


def safe_jalali(year: int, month: int, day: int) -> dt.date:
    """Jalali date with day clamped to the month length (e.g. 31 Mehr -> 30 Mehr)."""
    while month > 12:
        year, month = year + 1, month - 12
    for d in range(day, 0, -1):
        try:
            return jdatetime.date(year, month, d).togregorian()
        except ValueError:
            continue
    raise ValueError("invalid jalali date")


def add_interval(d: dt.date, interval: str, anchor_day: int | None = None) -> dt.date:
    """Advance a due date by one interval, using Jalali months for monthly/yearly."""
    if interval == "weekly":
        return d + dt.timedelta(days=7)
    j = to_jalali(d)
    day = anchor_day or j.day
    if interval == "yearly":
        return safe_jalali(j.year + 1, j.month, day)
    return safe_jalali(j.year, j.month + 1, day)


PERIOD_LABELS = {
    "today": "امروز",
    "week": "این هفته",
    "month": "این ماه",
    "last_month": "ماه قبل",
    "year": "امسال",
}


def period_range(period: str, ref: dt.date | None = None) -> tuple[dt.date, dt.date, str]:
    """Return (start, end_exclusive, label). Weeks start on Saturday, months are Jalali."""
    ref = ref or today()
    if period == "today":
        return ref, ref + dt.timedelta(days=1), f"امروز {jdate(ref)}"
    if period == "week":
        start = ref - dt.timedelta(days=(ref.weekday() - 5) % 7)
        end = start + dt.timedelta(days=7)
        return start, end, f"هفته {jdate(start)} تا {jdate(end - dt.timedelta(days=1))}"
    j = to_jalali(ref)
    if period == "month":
        return jalali_first(j.year, j.month), jalali_first(j.year, j.month + 1), jmonth_label(j)
    if period == "last_month":
        start = jalali_first(j.year, j.month - 1)
        return start, jalali_first(j.year, j.month), jmonth_label(to_jalali(start))
    if period == "year":
        return jalali_first(j.year, 1), jalali_first(j.year + 1, 1), f"سال {fa(j.year)}"
    raise ValueError(period)


def previous_range(period: str, ref: dt.date | None = None) -> tuple[dt.date, dt.date, str]:
    start, _, _ = period_range(period, ref)
    base = "month" if period == "last_month" else period
    return period_range(base, start - dt.timedelta(days=1))


_UNITS = {"هزار": 1_000, "تومن": 1, "تومان": 1, "میلیون": 1_000_000, "ملیون": 1_000_000,
          "میلیارد": 1_000_000_000, "k": 1_000, "m": 1_000_000}


def extract_amount(text: str) -> int | None:
    """Best-effort amount extraction used when AI is unavailable. Result is in Toman."""
    t = latin_digits(text).replace("٬", ",").replace("،", ",")
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(هزار|میلیون|ملیون|میلیارد|k|m|تومن|تومان|ریال)?", t, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit == "ریال":
        value /= 10
    else:
        value *= _UNITS.get(unit, 1)
    return int(value) if value > 0 else None
