"""Settings loaded from the .env file."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    owner_id: int = _int("OWNER_ID", 0)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    telegram_proxy: str = os.getenv("TELEGRAM_PROXY", "")
    db_path: str = os.getenv("DB_PATH", str(BASE_DIR / "data" / "ledger.db"))
    font_path: str = os.getenv("CHART_FONT_PATH", "")
    # Times are Asia/Tehran
    daily_report_hour: int = _int("DAILY_REPORT_HOUR", 22)
    reminder_hour: int = _int("REMINDER_HOUR", 9)
    daily_report_enabled: bool = os.getenv("DAILY_REPORT_ENABLED", "1") == "1"


config = Config()

EXPENSE_CATEGORIES = [
    "خوراک و رستوران",
    "سوپرمارکت و خواربار",
    "حمل‌ونقل",
    "خودرو و سوخت",
    "قبوض",
    "اجاره و مسکن",
    "خرید و پوشاک",
    "سلامت و درمان",
    "آموزش",
    "تفریح و سفر",
    "موبایل و اینترنت",
    "قسط و وام",
    "هدیه و خیریه",
    "کارمزد بانکی",
    "سایر",
]

INCOME_CATEGORIES = [
    "حقوق",
    "درآمد آزاد و پروژه",
    "سود و سرمایه‌گذاری",
    "هدیه",
    "سایر درآمد",
]
