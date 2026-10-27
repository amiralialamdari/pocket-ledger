"""SQLite storage. All amounts are integer Toman; timestamps are Tehran local time."""
import datetime as dt
import json
import sqlite3
from pathlib import Path

from .utils import add_interval, jalali_first, jmonth_label, now, to_jalali

# Categories of debt_in/debt_out movements (they never count as income or expense).
DEBT_NEW = "ایجاد طلب/بدهی"
DEBT_PAY = "تسویه طلب/بدهی"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    bank TEXT,
    card_last4 TEXT,
    initial_balance INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
-- kind: expense | income | transfer | adjust | debt_in | debt_out
-- adjust amounts are signed; every other kind stores a positive amount.
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    account_id INTEGER REFERENCES accounts(id),
    to_account_id INTEGER REFERENCES accounts(id),
    category TEXT,
    description TEXT,
    occurred_at TEXT NOT NULL,
    source TEXT,
    raw_text TEXT,
    debt_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_time ON transactions(occurred_at);
-- direction: lent (they owe me) | borrowed (I owe them)
CREATE TABLE IF NOT EXISTS debts (
    id INTEGER PRIMARY KEY,
    person TEXT NOT NULL,
    direction TEXT NOT NULL,
    amount INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    description TEXT,
    account_id INTEGER,
    due_date TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
);
-- interval: weekly | monthly | yearly ; remaining_count NULL = endless
CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'expense',
    amount INTEGER NOT NULL,
    account_id INTEGER,
    category TEXT,
    interval TEXT NOT NULL DEFAULT 'monthly',
    anchor_day INTEGER,
    next_due TEXT NOT NULL,
    remaining_count INTEGER,
    last_reminded TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Signed effect of each transaction kind on its source account.
_BALANCE_SQL = """
SELECT COALESCE(SUM(CASE
    WHEN kind IN ('income', 'debt_in') THEN amount
    WHEN kind IN ('expense', 'debt_out', 'transfer') THEN -amount
    WHEN kind = 'adjust' THEN amount
    ELSE 0 END), 0)
FROM transactions WHERE account_id = ?
"""


def _ts(value: dt.datetime | dt.date | None = None) -> str:
    if value is None:
        value = now()
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        current = now()
        value = dt.datetime.combine(value, current.time()) if value == current.date() \
            else dt.datetime.combine(value, dt.time(12, 0))
    return value.strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def _all(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params=()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ---------- accounts ----------
    def add_account(self, name: str, bank: str | None = None, card_last4: str | None = None,
                    initial_balance: int = 0) -> int:
        return self._exec(
            "INSERT INTO accounts(name, bank, card_last4, initial_balance, created_at) VALUES (?,?,?,?,?)",
            (name, bank, card_last4, initial_balance, _ts()),
        ).lastrowid

    def accounts(self, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else "WHERE archived = 0"
        return self._all(f"SELECT * FROM accounts {where} ORDER BY id")

    def account(self, account_id: int) -> dict | None:
        return self._one("SELECT * FROM accounts WHERE id = ?", (account_id,))

    def archive_account(self, account_id: int) -> None:
        self._exec("UPDATE accounts SET archived = 1 WHERE id = ?", (account_id,))

    def balance(self, account_id: int) -> int:
        acc = self.account(account_id)
        if not acc:
            return 0
        own = self.conn.execute(_BALANCE_SQL, (account_id,)).fetchone()[0]
        incoming = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind='transfer' AND to_account_id = ?",
            (account_id,),
        ).fetchone()[0]
        return acc["initial_balance"] + own + incoming

    def balances(self) -> list[tuple[dict, int]]:
        return [(a, self.balance(a["id"])) for a in self.accounts()]

    # ---------- transactions ----------
    def add_transaction(self, kind: str, amount: int, account_id: int | None,
                        to_account_id: int | None = None, category: str | None = None,
                        description: str = "", occurred_at=None, source: str = "text",
                        raw_text: str | None = None, debt_id: int | None = None) -> int:
        return self._exec(
            """INSERT INTO transactions(kind, amount, account_id, to_account_id, category, description,
               occurred_at, source, raw_text, debt_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, int(amount), account_id, to_account_id, category, description,
             _ts(occurred_at), source, raw_text, debt_id, _ts()),
        ).lastrowid

    def transaction(self, tx_id: int) -> dict | None:
        return self._one("SELECT * FROM transactions WHERE id = ?", (tx_id,))

    def delete_transaction(self, tx_id: int) -> bool:
        tx = self.transaction(tx_id)
        if not tx:
            return False
        debt = self.debt(tx["debt_id"]) if tx["debt_id"] else None
        self._exec("DELETE FROM transactions WHERE id = ?", (tx_id,))
        if debt:
            # Undo this movement's effect on its debt: creation removes the debt, a payment restores it.
            if tx["category"] == DEBT_NEW:
                self._exec("UPDATE transactions SET debt_id = NULL WHERE debt_id = ?", (debt["id"],))
                self.delete_debt(debt["id"])
            else:
                self._exec("UPDATE debts SET remaining = MIN(amount, remaining + ?), settled_at = NULL WHERE id = ?",
                           (tx["amount"], debt["id"]))
        return True

    def last_transactions(self, limit: int = 10) -> list[dict]:
        return self._all(
            """SELECT t.*, a.name AS account_name, b.name AS to_account_name FROM transactions t
               LEFT JOIN accounts a ON a.id = t.account_id
               LEFT JOIN accounts b ON b.id = t.to_account_id
               ORDER BY t.id DESC LIMIT ?""",
            (limit,),
        )

    def transactions_between(self, start: dt.date, end: dt.date) -> list[dict]:
        return self._all(
            """SELECT t.*, a.name AS account_name, b.name AS to_account_name FROM transactions t
               LEFT JOIN accounts a ON a.id = t.account_id
               LEFT JOIN accounts b ON b.id = t.to_account_id
               WHERE occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at""",
            (start.isoformat(), end.isoformat()),
        )

    def all_transactions(self) -> list[dict]:
        return self.transactions_between(dt.date(1900, 1, 1), dt.date(3000, 1, 1))

    def summary(self, start: dt.date, end: dt.date) -> dict:
        rows = self.transactions_between(start, end)
        by_exp: dict[str, int] = {}
        by_inc: dict[str, int] = {}
        income = expense = 0
        for r in rows:
            if r["kind"] == "expense":
                expense += r["amount"]
                by_exp[r["category"] or "سایر"] = by_exp.get(r["category"] or "سایر", 0) + r["amount"]
            elif r["kind"] == "income":
                income += r["amount"]
                by_inc[r["category"] or "سایر درآمد"] = by_inc.get(r["category"] or "سایر درآمد", 0) + r["amount"]
        expenses = sorted((r for r in rows if r["kind"] == "expense"), key=lambda r: -r["amount"])
        return {
            "income": income,
            "expense": expense,
            "net": income - expense,
            "by_expense": dict(sorted(by_exp.items(), key=lambda kv: -kv[1])),
            "by_income": dict(sorted(by_inc.items(), key=lambda kv: -kv[1])),
            "count": len(rows),
            "top_expenses": expenses[:5],
            "days": max((end - start).days, 1),
        }

    def monthly_trend(self, months: int = 6, ref: dt.date | None = None) -> list[dict]:
        j = to_jalali(ref or now().date())
        out = []
        for back in range(months - 1, -1, -1):
            start = jalali_first(j.year, j.month - back)
            end = jalali_first(j.year, j.month - back + 1)
            s = self.summary(start, end)
            out.append({"label": jmonth_label(to_jalali(start)), "income": s["income"], "expense": s["expense"]})
        return out

    # ---------- debts ----------
    def add_debt(self, person: str, direction: str, amount: int, description: str = "",
                 account_id: int | None = None, due_date: dt.date | None = None) -> int:
        return self._exec(
            """INSERT INTO debts(person, direction, amount, remaining, description, account_id, due_date, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (person, direction, amount, amount, description, account_id,
             due_date.isoformat() if due_date else None, _ts()),
        ).lastrowid

    def debt(self, debt_id: int) -> dict | None:
        return self._one("SELECT * FROM debts WHERE id = ?", (debt_id,))

    def debts(self, open_only: bool = True) -> list[dict]:
        where = "WHERE remaining > 0" if open_only else ""
        return self._all(f"SELECT * FROM debts {where} ORDER BY created_at")

    def find_open_debts(self, person: str, direction: str | None = None) -> list[dict]:
        sql = "SELECT * FROM debts WHERE remaining > 0 AND person LIKE ?"
        params: list = [f"%{person.strip()}%"]
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        return self._all(sql + " ORDER BY created_at", params)

    def pay_debt(self, debt_id: int, amount: int) -> dict:
        debt = self.debt(debt_id)
        paid = min(amount, debt["remaining"])
        remaining = debt["remaining"] - paid
        self._exec(
            "UPDATE debts SET remaining = ?, settled_at = ? WHERE id = ?",
            (remaining, _ts() if remaining == 0 else None, debt_id),
        )
        return {**debt, "remaining": remaining, "paid": paid}

    def delete_debt(self, debt_id: int) -> None:
        self._exec("DELETE FROM debts WHERE id = ?", (debt_id,))

    # ---------- recurring ----------
    def add_recurring(self, title: str, amount: int, next_due: dt.date, kind: str = "expense",
                      account_id: int | None = None, category: str | None = None,
                      interval: str = "monthly", remaining_count: int | None = None) -> int:
        anchor = to_jalali(next_due).day if interval != "weekly" else None
        return self._exec(
            """INSERT INTO recurring(title, kind, amount, account_id, category, interval, anchor_day,
               next_due, remaining_count, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (title, kind, amount, account_id, category, interval, anchor,
             next_due.isoformat(), remaining_count, _ts()),
        ).lastrowid

    def recurring(self, rec_id: int) -> dict | None:
        return self._one("SELECT * FROM recurring WHERE id = ?", (rec_id,))

    def recurring_list(self) -> list[dict]:
        return self._all("SELECT * FROM recurring WHERE active = 1 ORDER BY next_due")

    def due_recurring(self, on: dt.date) -> list[dict]:
        return self._all(
            "SELECT * FROM recurring WHERE active = 1 AND next_due <= ? AND (last_reminded IS NULL OR last_reminded < ?)",
            (on.isoformat(), on.isoformat()),
        )

    def mark_reminded(self, rec_id: int, on: dt.date) -> None:
        self._exec("UPDATE recurring SET last_reminded = ? WHERE id = ?", (on.isoformat(), rec_id))

    def advance_recurring(self, rec_id: int) -> dict:
        rec = self.recurring(rec_id)
        nxt = add_interval(dt.date.fromisoformat(rec["next_due"]), rec["interval"], rec["anchor_day"])
        count = rec["remaining_count"]
        if count is not None:
            count -= 1
        active = 0 if count is not None and count <= 0 else 1
        self._exec(
            "UPDATE recurring SET next_due = ?, remaining_count = ?, active = ?, last_reminded = NULL WHERE id = ?",
            (nxt.isoformat(), count, active, rec_id),
        )
        return self.recurring(rec_id)

    def deactivate_recurring(self, rec_id: int) -> None:
        self._exec("UPDATE recurring SET active = 0 WHERE id = ?", (rec_id,))

    # ---------- pending AI actions ----------
    def save_pending(self, payload: dict) -> int:
        return self._exec(
            "INSERT INTO pending(payload, created_at) VALUES (?, ?)",
            (json.dumps(payload, ensure_ascii=False), _ts()),
        ).lastrowid

    def pending(self, pid: int) -> dict | None:
        row = self._one("SELECT payload FROM pending WHERE id = ?", (pid,))
        return json.loads(row["payload"]) if row else None

    def update_pending(self, pid: int, payload: dict) -> None:
        self._exec("UPDATE pending SET payload = ? WHERE id = ?", (json.dumps(payload, ensure_ascii=False), pid))

    def delete_pending(self, pid: int) -> None:
        self._exec("DELETE FROM pending WHERE id = ?", (pid,))

    # ---------- settings ----------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))
