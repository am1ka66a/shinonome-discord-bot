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
            for table in ("bot_admin_snapshots", "bot_admin_operations", "bot_settings", "bot_game_events", "bot_game_sessions", "casino_logs", "logs", "users"):
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

    def test_mysql_instance_lock_rejects_second_process(self):
        from bot_modules.runtime.instance_lease import InstanceLease
        first, second = InstanceLease(), InstanceLease()
        try:
            first.acquire()
            with self.assertRaises(RuntimeError):
                second.acquire()
            first.verify()
            first.close()
            second.acquire()
            second.verify()
        finally:
            first.close()
            second.close()

    def test_reset_retry_after_lost_commit_response_does_not_pay_twice(self):
        import contextlib
        from bot_modules import maintenance
        with db.db_cursor(commit=True) as c:
            c.execute("INSERT INTO bot_settings (setting_key,value_json) VALUES ('casino_open','false')")
        real_cursor = maintenance.db_cursor
        @contextlib.contextmanager
        def ambiguous_commit(**kwargs):
            with real_cursor(**kwargs) as c:
                yield c
            raise ConnectionError("response lost after commit")
        with patch.object(maintenance, "db_cursor", ambiguous_commit), self.assertRaises(ConnectionError):
            maintenance.reset_balances("ci-reset", "admin", 50000, True)
        maintenance.reset_balances("ci-reset", "admin", 50000, True)
        self.assertEqual(self.balances(), [50000, 50000])
        with db.db_cursor() as c:
            c.execute("SELECT COUNT(*), SUM(amount) FROM logs")
            self.assertEqual(c.fetchone(), (2, 98000))

    def test_export_restores_to_an_empty_local_database(self):
        import tempfile
        import pymysql
        from bot_modules.backup import export_database, restore_test_database
        kwargs = db._mysql_connect_kwargs()
        root_kwargs = dict(kwargs, user="root", password=os.environ["MYSQL_TEST_ROOT_PASSWORD"])
        admin = pymysql.connect(**root_kwargs)
        source = pymysql.connect(**kwargs)
        target = None
        created = False
        try:
            c = admin.cursor()
            c.execute("CREATE DATABASE shinonome_restore_test")
            created = True
            target = pymysql.connect(**dict(root_kwargs, database="shinonome_restore_test"))
            with db.db_cursor(commit=True) as c:
                c.execute("INSERT INTO logs (user_id,amount,reason) VALUES (%s,%s,%s)", ("1", 1, "quote'; semi; newline\n測試"))
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "backup.jsonl.gz"
                summary = export_database(source, path)
                restored = restore_test_database(target, path)
                self.assertEqual(summary["rows"], restored["rows"])
            c = target.cursor()
            c.execute("SELECT balance FROM users ORDER BY user_id")
            self.assertEqual([row[0] for row in c.fetchall()], [1000, 1000])
            c.execute("SELECT reason FROM logs")
            self.assertEqual(c.fetchone()[0], "quote'; semi; newline\n測試")
        finally:
            source.close()
            if target is not None:
                target.close()
            if created:
                admin.cursor().execute("DROP DATABASE shinonome_restore_test")
            admin.close()


if __name__ == "__main__":
    unittest.main()
