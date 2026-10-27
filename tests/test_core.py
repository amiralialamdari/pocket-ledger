import datetime as dt
import tempfile
import unittest
from pathlib import Path

from ledger import handlers as h
from ledger.ai import fallback_parse, normalize_actions
from ledger.db import DB
from ledger.export import build_excel
from ledger.reports import analysis_data, balances_text, period_report
from ledger.utils import add_interval, extract_amount, parse_jalali, period_range, to_jalali, today


class UtilsTest(unittest.TestCase):
    def test_extract_amount(self):
        self.assertEqual(extract_amount("۸۵ هزار ناهار"), 85_000)
        self.assertEqual(extract_amount("2.5 میلیون"), 2_500_000)
        self.assertEqual(extract_amount("مبلغ 1,250,000 ریال"), 125_000)

    def test_jalali(self):
        self.assertEqual(parse_jalali("۱۴۰۵/۰۱/۰۱"), dt.date(2026, 3, 21))
        self.assertIsNone(parse_jalali("نامعتبر"))

    def test_add_interval_clamps_month_end(self):
        d = parse_jalali("1405/06/31")
        nxt = add_interval(d, "monthly", 31)
        self.assertEqual((to_jalali(nxt).month, to_jalali(nxt).day), (7, 30))
        after = add_interval(nxt, "monthly", 31)
        self.assertEqual((to_jalali(after).month, to_jalali(after).day), (8, 30))

    def test_week_starts_saturday(self):
        start, end, _ = period_range("week", dt.date(2026, 10, 27))  # Tuesday
        self.assertEqual(start.weekday(), 5)
        self.assertEqual((end - start).days, 7)

    def test_month_is_jalali(self):
        start, end, _ = period_range("month", dt.date(2026, 10, 27))
        self.assertEqual(to_jalali(start).day, 1)
        self.assertEqual(to_jalali(end).day, 1)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(str(Path(self.tmp.name) / "t.db"))
        self.mellat = self.db.add_account("ملت", "ملت", "1234", 10_000_000)
        self.saman = self.db.add_account("سامان", "سامان", "5678", 0)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def act(self, **kw):
        base = {"description": "", "date": None, "balance_after": None, "category": None,
                "account_id": None, "to_account_id": None, "source": "text", "raw_text": "x"}
        return h.commit(self.db, {**base, **kw})

    def test_balances_and_transfer(self):
        self.act(type="expense", amount=85_000, account_id=self.mellat, category="خوراک و رستوران")
        self.act(type="income", amount=1_000_000, account_id=self.saman, category="حقوق")
        self.act(type="transfer", amount=500_000, account_id=self.mellat, to_account_id=self.saman)
        self.assertEqual(self.db.balance(self.mellat), 10_000_000 - 85_000 - 500_000)
        self.assertEqual(self.db.balance(self.saman), 1_500_000)
        s = self.db.summary(*period_range("month")[:2])
        self.assertEqual((s["income"], s["expense"]), (1_000_000, 85_000))  # transfers excluded

    def test_sms_reconcile_and_adjust(self):
        _, _, follow = self.act(type="expense", amount=100_000, account_id=self.mellat, balance_after=9_800_000)
        self.assertIn("اختلاف", follow[0][0])
        self.db.add_transaction("adjust", 9_800_000 - self.db.balance(self.mellat), self.mellat)
        self.assertEqual(self.db.balance(self.mellat), 9_800_000)

    def test_debt_lifecycle(self):
        self.act(type="debt", amount=5_000_000, person="علی", direction="lent", account_id=self.mellat)
        self.assertEqual(self.db.balance(self.mellat), 5_000_000)
        text, _, _ = self.act(type="debt_settle", amount=2_000_000, person="علی", direction="lent",
                              account_id=self.saman)
        self.assertIn("تسویه", text)
        self.assertEqual(self.db.debts()[0]["remaining"], 3_000_000)
        self.assertEqual(self.db.balance(self.saman), 2_000_000)
        # Undoing the repayment restores the debt; debt moves never count as expense/income.
        self.db.delete_transaction(self.db.last_transactions(1)[0]["id"])
        self.assertEqual(self.db.debts()[0]["remaining"], 5_000_000)
        self.assertEqual(self.db.summary(*period_range("month")[:2])["expense"], 0)

    def test_recurring(self):
        self.act(type="recurring", amount=4_000_000, description="قسط وام", interval="monthly", count=2,
                 first_due=None, recurring_kind="expense", account_id=self.mellat)
        rec = self.db.due_recurring(today())[0]
        self.db.advance_recurring(rec["id"])
        rec = self.db.advance_recurring(rec["id"])
        self.assertEqual(rec["active"], 0)

    def test_new_account_action(self):
        self.act(type="new_account", account_name="رسالت", bank="رسالت", card_last4="6037991234567890",
                 initial_balance=2_000_000)
        acc = self.db.accounts()[-1]
        self.assertEqual((acc["card_last4"], self.db.balance(acc["id"])), ("7890", 2_000_000))

    def test_reports_charts_excel(self):
        for i, cat in enumerate(["خوراک و رستوران", "حمل‌ونقل", "قبوض"]):
            self.act(type="expense", amount=(i + 1) * 100_000, account_id=self.mellat, category=cat)
        self.act(type="income", amount=3_000_000, account_id=self.mellat, category="حقوق")
        for period in ("today", "week", "month", "last_month", "year"):
            text, images = period_report(self.db, period)
            self.assertIn("گزارش", text)
        _, images = period_report(self.db, "month")
        self.assertEqual(len(images), 2)
        self.assertTrue(images[0].getvalue().startswith(b"\x89PNG"))
        self.assertIn("جمع کل", balances_text(self.db))
        self.assertTrue(analysis_data(self.db)["این دوره"]["هزینه"] == 600_000)
        self.assertTrue(build_excel(self.db).getvalue().startswith(b"PK"))

    def test_card_requires_account(self):
        a = {"type": "expense", "amount": 50_000, "account_id": None, "description": "قهوه"}
        text, markup = h.render_card(1, a, self.db)
        self.assertIn("حساب را انتخاب کنید", text)
        self.assertTrue(markup.inline_keyboard[0][0].callback_data.startswith("p:1:set:a:"))


class AIOutputTest(unittest.TestCase):
    def test_normalize(self):
        raw = {"actions": [
            {"type": "expense", "amount": "85000", "account_id": 1, "category": "خوراک و رستوران"},
            {"type": "expense", "amount": 0},
            {"type": "transfer", "amount": 10, "account_id": 99},
            {"type": "nonsense"},
            {"type": "report", "period": "bogus"},
        ]}
        out = normalize_actions(raw, {1})
        self.assertEqual([a["type"] for a in out], ["expense", "transfer", "report"])
        self.assertEqual(out[0]["amount"], 85_000)
        self.assertIsNone(out[1]["account_id"])
        self.assertEqual(out[2]["period"], "month")

    def test_fallback(self):
        a = fallback_parse("85 ناهار")[0]
        self.assertEqual((a["type"], a["amount"]), ("expense", 85_000))
        self.assertEqual(fallback_parse("حقوق ۳۰ میلیون")[0]["type"], "income")


if __name__ == "__main__":
    unittest.main()
