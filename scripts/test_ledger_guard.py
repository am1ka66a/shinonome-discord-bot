"""Every balance change must go through tx_ops.change_balance.

change_balance updates the balance and writes logs + casino_logs in the same
transaction. Raw SQL that touches users.balance or the ledgers anywhere else is
how balances and the ledger drifted apart before, so this test fails on it.
"""
import io
import re
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BALANCE_WRITE = re.compile(r"\bbalance\s*=|ON\s+DUPLICATE\s+KEY\s+UPDATE\s+balance", re.I)
LEDGER_WRITE = re.compile(r"INSERT\s+(IGNORE\s+)?INTO\s+(casino_)?logs\b", re.I)
NEW_ACCOUNT_WITH_MONEY = re.compile(
    r"INSERT\s+(IGNORE\s+)?INTO\s+users\s*\([^)]*\bbalance\b[^)]*\)\s*VALUES\s*\((?![^)]*%s\s*,\s*0\b)", re.I)

# The helper itself, the legacy standalone ledger helpers and the one-off
# migration seed are the only places allowed to write these tables directly.
BALANCE_ALLOWED = {"bot_modules/tx_ops.py"}
LEDGER_ALLOWED = {"bot_modules/tx_ops.py", "bot_modules/db.py", "bot_modules/migrations.py"}


def string_literals(path):
    source = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.STRING:
            yield tok.start[0], tok.string


def source_files():
    return sorted(p for p in (ROOT / "bot_modules").rglob("*.py"))


class LedgerGuardTests(unittest.TestCase):
    def offending(self, pattern, allowed):
        found = []
        for path in source_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel in allowed:
                continue
            for line, literal in string_literals(path):
                if pattern.search(literal):
                    found.append(f"{rel}:{line}")
        return found

    def test_balances_change_only_through_change_balance(self):
        self.assertEqual(self.offending(BALANCE_WRITE, BALANCE_ALLOWED), [],
                         "Use tx_ops.change_balance instead of writing users.balance directly")

    def test_ledgers_are_written_only_by_tx_ops(self):
        self.assertEqual(self.offending(LEDGER_WRITE, LEDGER_ALLOWED), [],
                         "Use tx_ops.change_balance so the balance and both ledgers move together")

    def test_new_accounts_start_at_zero(self):
        self.assertEqual(self.offending(NEW_ACCOUNT_WITH_MONEY, BALANCE_ALLOWED), [],
                         "Create accounts with balance 0, then credit them with change_balance")

    def test_guard_catches_raw_writes(self):
        samples = [
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            "UPDATE users SET last_beg=%s, balance = balance + %s",
            "INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500",
            "UPDATE users SET balance=GREATEST(0, balance-%s)",
        ]
        for sql in samples:
            self.assertRegex(sql, BALANCE_WRITE)
        self.assertRegex("INSERT INTO casino_logs (user_id) VALUES (%s)", LEDGER_WRITE)
        self.assertRegex("INSERT INTO users (user_id, balance) VALUES (%s, %s)", NEW_ACCOUNT_WITH_MONEY)
        self.assertNotRegex("INSERT IGNORE INTO users (user_id,balance) VALUES (%s,0)", NEW_ACCOUNT_WITH_MONEY)
        self.assertNotRegex("SELECT COALESCE(balance,0) FROM users", BALANCE_WRITE)


class ChangeBalanceTests(unittest.TestCase):
    def setUp(self):
        from test_regressions import SqliteConnection
        self.conn = SqliteConnection()
        self.conn.db.execute("INSERT INTO users (user_id,balance) VALUES ('1',100)")
        self.addCleanup(self.conn.db.close)

    def state(self, uid="1"):
        row = self.conn.db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        logs = self.conn.db.execute("SELECT COALESCE(SUM(amount),0) FROM logs WHERE user_id=?", (uid,)).fetchone()[0]
        ledger = self.conn.db.execute("SELECT COALESCE(SUM(amount),0) FROM casino_logs WHERE user_id=?", (uid,)).fetchone()[0]
        return (row[0] if row else None), logs, ledger

    def test_credit_and_debit_move_balance_and_both_ledgers(self):
        from bot_modules.tx_ops import change_balance
        self.assertTrue(change_balance(self.conn, 1, 50, "in"))
        self.assertTrue(change_balance(self.conn, 1, -30, "out", require_funds=True))
        self.assertEqual(self.state(), (120, 20, 20))

    def test_short_or_missing_account_writes_nothing(self):
        from bot_modules.tx_ops import change_balance
        self.assertFalse(change_balance(self.conn, 1, -101, "too much", require_funds=True))
        self.assertFalse(change_balance(self.conn, 9, 10, "nobody"))
        self.assertEqual(self.state(), (100, 0, 0))
        self.assertEqual(self.state("9"), (None, 0, 0))

    def test_create_opens_account_at_zero_then_credits(self):
        from bot_modules.tx_ops import change_balance
        self.assertTrue(change_balance(self.conn, 9, 10, "reward", create=True))
        self.assertEqual(self.state("9"), (10, 10, 10))

    def test_zero_delta_is_a_no_op(self):
        from bot_modules.tx_ops import change_balance
        self.assertTrue(change_balance(self.conn, 1, 0, "nothing"))
        self.assertEqual(self.conn.db.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
