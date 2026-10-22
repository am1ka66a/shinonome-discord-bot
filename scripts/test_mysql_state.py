"""Opt-in integration checks for the disposable CI MySQL, never production."""
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import db, state_repo


@unittest.skipUnless(os.getenv("RUN_MYSQL_INTEGRATION") == "1", "requires disposable CI MySQL")
class MySQLStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if (os.getenv("MYSQLDATABASE") != "shinonome_test" or os.getenv("MYSQLHOST") != "127.0.0.1"
                or os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL")):
            raise RuntimeError("Refusing to run outside the dedicated localhost test database")
        db.init_db()

    def setUp(self):
        with db.db_cursor(commit=True) as c:
            for table in ("bot_game_events", "bot_game_sessions", "casino_logs", "logs", "users"):
                c.execute("DELETE FROM " + table)
            c.execute("INSERT INTO users (user_id,balance) VALUES ('1',1000),('2',1000)")
        self.store = state_repo.GameStore()

    def balances(self):
        with db.db_cursor() as c:
            c.execute("SELECT balance FROM users ORDER BY user_id")
            return [int(row[0]) for row in c.fetchall()]

    def test_concurrent_payout_credits_once(self):
        self.store.debit("ci:bj", "bj", 1, 100, "open")
        self.store.prepare("ci:bj", {"1": 200}, {"1": [True, 100, False]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.store.apply("ci:bj"), range(2)))
        self.assertEqual(self.balances(), [1100, 1000])

    def test_concurrent_claim_by_same_user_is_once(self):
        self.store.debit("ci:packet", "redpacket", 1, 100, "open", {"creator_id": 1, "count": 1, "claims": {}})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.claim("ci:packet", 2)[0], range(2)))
        self.assertEqual(results, [100, 100])
        self.assertEqual(self.balances(), [900, 1100])

    def test_payout_failure_rolls_back_then_retries(self):
        self.store.debit("ci:duel", "duel", 1, 100, "a")
        self.store.debit("ci:duel", "duel", 2, 100, "b")
        self.store.prepare("ci:duel", {}, refund=True)
        real_log = state_repo.log_transaction_in_tx
        def fail_second(c, uid, amount, reason):
            if str(uid) == "2":
                raise RuntimeError("injected log failure")
            real_log(c, uid, amount, reason)
        with patch.object(state_repo, "log_transaction_in_tx", side_effect=fail_second), self.assertRaises(RuntimeError):
            self.store.apply("ci:duel")
        self.assertEqual(self.balances(), [900, 900])
        self.store.apply("ci:duel")
        self.store.apply("ci:duel")
        self.assertEqual(self.balances(), [1000, 1000])


if __name__ == "__main__":
    unittest.main()
