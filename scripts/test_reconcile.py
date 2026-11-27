"""Offline tests for balance-vs-ledger reconciliation. No Discord, no production DB."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import admin_audit_repo as repo


class _Conn:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript("""
            CREATE TABLE users (user_id TEXT PRIMARY KEY, balance INTEGER);
            CREATE TABLE casino_logs (id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER, reason TEXT);
            CREATE TABLE bot_reconcile_baseline (user_id TEXT PRIMARY KEY, offset_amount INTEGER NOT NULL);
            CREATE TABLE bot_reconcile_meta (id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL,
                accounts INTEGER NOT NULL, total_offset INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)

    def cursor(self):
        return self

    def execute(self, sql, args=()):
        self.cur = self.db.execute(sql.replace('%s', '?'), args)

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.conn = _Conn()
        self.connect = lambda: self.conn
        self.addCleanup(self.conn.db.close)
        db = self.conn.db
        db.executemany("INSERT INTO users VALUES (?,?)", [('1', 50_000), ('2', 700), ('3', 0)])
        db.executemany("INSERT INTO casino_logs (user_id, amount, reason) VALUES (?,?,?)", [
            ('1', 50_000, 'start'), ('1', -2_000, 'bet'), ('1', 2_000, 'win'),   # matches
            ('2', 500, 'redpacket'),                                              # 200 unexplained
            ('9', 123, 'orphan'),                                                 # no account
        ])
        db.commit()

    def test_report_finds_drift_and_orphans(self):
        report = repo.reconcile_report(self.connect)
        self.assertEqual((report.accounts, report.matched), (3, 2))
        self.assertEqual(report.drifted, [('2', 700, 500, 0, 200)])
        self.assertEqual(report.total_drift, 200)
        self.assertEqual((report.orphan_users, report.orphan_amount), (1, 123))
        self.assertIsNone(report.baseline)

    def test_baseline_absorbs_history_and_reveals_new_drift(self):
        result = repo.set_reconcile_baseline(42, self.connect)
        self.assertEqual(result, {"accounts": 1, "total_offset": 200})
        report = repo.reconcile_report(self.connect)
        self.assertEqual(report.drifted, [])
        self.assertEqual(report.baseline[0], '42')
        # A later balance change without a ledger row shows up again.
        self.conn.db.execute("UPDATE users SET balance=balance+5 WHERE user_id='1'")
        report = repo.reconcile_report(self.connect)
        self.assertEqual(report.drifted, [('1', 50_005, 50_000, 0, 5)])
        # Properly logged movements stay matched.
        self.conn.db.execute("UPDATE users SET balance=balance+100 WHERE user_id='2'")
        self.conn.db.execute("INSERT INTO casino_logs (user_id, amount, reason) VALUES ('2',100,'x')")
        self.assertEqual([r[0] for r in repo.reconcile_report(self.connect).drifted], ['1'])

    def test_baseline_never_touches_balances_or_ledger(self):
        before = (self.conn.db.execute("SELECT * FROM users ORDER BY user_id").fetchall(),
                  self.conn.db.execute("SELECT * FROM casino_logs ORDER BY id").fetchall())
        repo.set_reconcile_baseline(42, self.connect)
        repo.set_reconcile_baseline(42, self.connect)   # re-running replaces, never accumulates
        after = (self.conn.db.execute("SELECT * FROM users ORDER BY user_id").fetchall(),
                 self.conn.db.execute("SELECT * FROM casino_logs ORDER BY id").fetchall())
        self.assertEqual(before, after)
        self.assertEqual(self.conn.db.execute("SELECT COUNT(*) FROM bot_reconcile_baseline").fetchone()[0], 1)

    def test_report_is_read_only(self):
        sql = []
        self.conn.db.set_trace_callback(sql.append)
        repo.reconcile_report(self.connect)
        self.assertTrue(sql)
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in sql), sql)


if __name__ == '__main__':
    unittest.main()
