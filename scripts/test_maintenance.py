"""Real transaction checks for maintenance; never touches the production database."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import maintenance, state_repo
from bot_modules.db import db_cursor
from test_regressions import SqliteConnection


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.conn = SqliteConnection()
        self.addCleanup(self.conn.db.close)
        self.conn.db.executescript("""
            ALTER TABLE users ADD COLUMN rescue_count INTEGER DEFAULT 0;
            CREATE TABLE bot_settings (setting_key TEXT PRIMARY KEY,value_json TEXT);
            INSERT INTO bot_settings VALUES ('casino_open','false');
            INSERT INTO users (user_id,balance,wins) VALUES ('1',1000,2),('2',2000,3);
            CREATE TABLE bot_game_sessions (session_id TEXT PRIMARY KEY,phase TEXT);
            CREATE TABLE bot_admin_operations (request_id TEXT PRIMARY KEY,actor_id TEXT,action TEXT,result_json TEXT);
            CREATE TABLE bot_admin_snapshots (request_id TEXT,user_id TEXT,snapshot_json TEXT,PRIMARY KEY(request_id,user_id));
        """)
        self.conn.fetchall = lambda: self.conn.cur.fetchall()
        self.patch = patch.object(maintenance, "db_cursor", lambda **kw: db_cursor(connect=lambda: self.conn, **kw))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def balances(self):
        return self.conn.db.execute("SELECT balance FROM users ORDER BY user_id").fetchall()

    def test_reset_snapshots_logs_and_repeat_request_are_atomic(self):
        result = maintenance.reset_balances("request", 123, 50000, True)
        self.assertEqual(result["accounts"], 2)
        self.assertEqual(self.balances(), [(50000,), (50000,)])
        self.assertEqual(self.conn.db.execute("SELECT SUM(amount) FROM logs").fetchone()[0], 97000)
        snapshot = json.loads(self.conn.db.execute("SELECT snapshot_json FROM bot_admin_snapshots WHERE user_id='1'").fetchone()[0])
        self.assertEqual(snapshot["balance"], 1000)
        self.assertEqual(snapshot["wins"], 2)
        self.assertEqual(maintenance.reset_balances("request", 123, 50000, True), result)
        self.assertEqual(self.conn.db.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 2)

    def test_zero_reset_preserves_stats(self):
        maintenance.reset_balances("zero", 123, 0)
        self.assertEqual(self.balances(), [(0,), (0,)])
        self.assertEqual(self.conn.db.execute("SELECT wins FROM users ORDER BY user_id").fetchall(), [(2,), (3,)])

    def test_failure_rolls_back_snapshots_balances_and_ledger(self):
        real_log = maintenance.log_transaction_in_tx
        def fail_second(c, uid, *args):
            if uid == "2":
                raise RuntimeError("injected failure")
            return real_log(c, uid, *args)
        with patch.object(maintenance, "log_transaction_in_tx", side_effect=fail_second), self.assertRaises(RuntimeError):
            maintenance.reset_balances("fail", 123, 0)
        self.assertEqual(self.balances(), [(1000,), (2000,)])
        for table in ("logs", "casino_logs", "bot_admin_snapshots", "bot_admin_operations"):
            self.assertEqual(self.conn.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_open_casino_and_unsettled_games_block_reset(self):
        for table, insert in [
            ("bot_game_sessions", "INSERT INTO bot_game_sessions VALUES ('game','settling')"),
            ("russian_roulette_active_matches", "INSERT INTO russian_roulette_active_matches VALUES ('rr','playing','{}')"),
        ]:
            with self.subTest(table=table):
                self.conn.db.execute(insert)
                self.conn.db.commit()
                with self.assertRaises(ValueError):
                    maintenance.reset_balances("busy", 123, 0)
                self.assertEqual(self.balances(), [(1000,), (2000,)])
                self.conn.db.execute("DELETE FROM " + table)
                self.conn.db.commit()
        self.conn.db.execute("UPDATE bot_settings SET value_json='true'")
        self.conn.db.commit()
        with self.assertRaises(ValueError):
            maintenance.reset_balances("open", 123, 0)

    def test_closed_casino_blocks_new_escrow_before_any_debit(self):
        with patch.object(state_repo, "db_cursor", lambda **kw: db_cursor(connect=lambda: self.conn, **kw)):
            self.assertFalse(state_repo.GameStore().debit("new", "bj", 1, 100, "start"))
        self.assertEqual(self.balances(), [(1000,), (2000,)])
