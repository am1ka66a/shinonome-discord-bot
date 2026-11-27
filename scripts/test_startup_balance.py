"""Offline tests: every player receives the startup balance exactly once.

Run: python -m unittest scripts/test_startup_balance.py -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot_modules import config, economy_service, user_repo


class _Conn:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE users (user_id TEXT PRIMARY KEY, balance INTEGER, '
                        'startup_granted INTEGER NOT NULL DEFAULT 0)')
        self.db.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER, reason TEXT)')
        self.db.execute('CREATE TABLE casino_logs (user_id TEXT, amount INTEGER, reason TEXT, source_log_id INTEGER)')

    def cursor(self):
        return self

    def execute(self, sql, args=()):
        if 'ON DUPLICATE KEY UPDATE balance=COALESCE' in sql:
            sql = sql.split('ON DUPLICATE KEY')[0] + 'ON CONFLICT(user_id) DO UPDATE SET balance=COALESCE(balance,0)+?'
        sql = sql.replace('%s', '?').replace('INSERT IGNORE', 'INSERT OR IGNORE')
        self.cur = self.db.execute(sql, args)
        self.rowcount = self.cur.rowcount
        self.lastrowid = self.cur.lastrowid

    def fetchone(self):
        return self.cur.fetchone()

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass

    def row(self, uid):
        return self.db.execute('SELECT balance, startup_granted FROM users WHERE user_id=?', (uid,)).fetchone()

    def startup_logs(self, uid):
        return self.db.execute('SELECT COUNT(*) FROM logs WHERE user_id=? AND reason=?',
                               (uid, config.REASON_USER_INITIAL_BALANCE)).fetchone()[0]


class StartupBalance(unittest.TestCase):
    def setUp(self):
        self.conn = _Conn()
        patcher = patch('bot_modules.db.get_db_connection', return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.conn.db.close)

    def test_new_player_gets_startup_once(self):
        user_repo.ensure_user_exists(1, 50_000)
        user_repo.ensure_user_exists(1, 50_000)
        self.assertEqual(self.conn.row('1'), (50_000, 1))
        self.assertEqual(self.conn.startup_logs('1'), 1)

    def test_account_created_by_transfer_gets_startup_on_first_own_use(self):
        user_repo.ensure_user_exists(2, 0)          # e.g. created as a transfer receiver
        self.assertEqual(self.conn.row('2'), (0, 0))
        self.conn.execute("UPDATE users SET balance=balance+300 WHERE user_id='2'")
        user_repo.ensure_user_exists(2, 50_000)     # the player's own first command
        user_repo.ensure_user_exists(2, 50_000)
        self.assertEqual(self.conn.row('2'), (50_300, 1))
        self.assertEqual(self.conn.startup_logs('2'), 1)

    def test_account_created_by_credit_gets_startup_later(self):
        economy_service.credit_user(lambda: self.conn, user_repo.log_transaction_in_tx, 3, 120, 'redpacket')
        user_repo.ensure_user_exists(3, 50_000)
        self.assertEqual(self.conn.row('3'), (50_120, 1))
        self.assertEqual(self.conn.startup_logs('3'), 1)

    def test_zero_default_never_grants(self):
        user_repo.ensure_user_exists(4, 0)
        user_repo.ensure_user_exists(4, 0)
        self.assertEqual(self.conn.row('4'), (0, 0))
        self.assertEqual(self.conn.startup_logs('4'), 0)

    def test_startup_and_ledger_roll_back_together(self):
        with patch.object(user_repo, 'log_transaction_in_tx', side_effect=RuntimeError('log failed')):
            with self.assertRaises(RuntimeError):
                user_repo.ensure_user_exists(5, 50_000)
        self.assertIsNone(self.conn.row('5'))
        user_repo.ensure_user_exists(5, 50_000)
        self.assertEqual(self.conn.row('5'), (50_000, 1))
        self.assertEqual(self.conn.startup_logs('5'), 1)


class StartupMigration(unittest.TestCase):
    def test_existing_accounts_are_not_paid_retroactively(self):
        from bot_modules import migrations

        class Cursor:
            def __init__(self):
                self.sql = []

            def execute(self, sql, args=()):
                self.sql.append(sql)

        c = Cursor()
        migrations.startup_grant_flag(c)
        self.assertIn('startup_granted', c.sql[0])
        self.assertEqual(c.sql[1], 'UPDATE users SET startup_granted=1')
        self.assertIn((4, migrations.startup_grant_flag), migrations.MIGRATIONS)


if __name__ == '__main__':
    unittest.main()
