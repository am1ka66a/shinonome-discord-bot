"""Robbery balances, cooldowns and both ledgers must commit together."""
import datetime
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_modules import rob_repo
from bot_modules.tx_ops import lock_user_rows, log_transaction_in_tx
from test_regressions import SqliteConnection


class RobberyTransactionTests(unittest.TestCase):
    def setUp(self):
        self.conn=SqliteConnection()
        self.addCleanup(self.conn.db.close)
        self.conn.db.create_function('LEAST',-1,min)
        self.conn.db.executescript("""
            ALTER TABLE users ADD COLUMN last_rob TIMESTAMP;
            ALTER TABLE users ADD COLUMN last_robbed TIMESTAMP;
            ALTER TABLE users ADD COLUMN last_five_robs TEXT;
            ALTER TABLE users ADD COLUMN wanted_stars INTEGER DEFAULT 0;
            ALTER TABLE users ADD COLUMN wanted_hunted_count INTEGER DEFAULT 0;
            ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'criminal';
            INSERT INTO users (user_id,balance) VALUES ('1',1000),('2',1000);
        """)
        self.conn.commit()
        self.now=datetime.datetime(2026,11,26,12)

    def run_rob(self, success, log=log_transaction_in_tx):
        if success:
            return rob_repo.apply_rob_success_db(lambda:self.conn,lock_user_rows,lambda:self.now,
                lambda x:x,log,0.3,100000,100000,50,5,1,2,self.now,60)
        return rob_repo.apply_rob_fail_db(lambda:self.conn,lock_user_rows,100000,1,2,self.now,log)

    def test_success_and_failure_have_balanced_atomic_logs(self):
        for success in (True,False):
            with self.subTest(success=success), patch.object(rob_repo.random,'uniform',return_value=0.2):
                before=self.conn.db.execute('SELECT balance FROM users ORDER BY user_id').fetchall()
                self.run_rob(success)
                rows=self.conn.db.execute('SELECT user_id,amount FROM logs ORDER BY id DESC LIMIT 2').fetchall()
                after=self.conn.db.execute('SELECT balance FROM users ORDER BY user_id').fetchall()
                self.assertEqual(sum(r[1] for r in rows),0)
                changes={uid:delta for uid,delta in rows}
                self.assertEqual([after[i][0]-before[i][0] for i in range(2)],[changes['1'],changes['2']])
                self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM logs').fetchone()[0],
                                 self.conn.db.execute('SELECT COUNT(*) FROM casino_logs').fetchone()[0])

    def test_second_log_failure_rolls_back_both_balances_and_cooldown(self):
        for success in (True,False):
            def failing_log(c,uid,*args):
                if str(uid)=='2':
                    raise RuntimeError('second ledger write failed')
                log_transaction_in_tx(c,uid,*args)
            with self.subTest(success=success), self.assertRaises(RuntimeError):
                self.run_rob(success,failing_log)
            self.assertEqual(self.conn.db.execute('SELECT balance,last_rob,last_robbed FROM users ORDER BY user_id').fetchall(),
                             [(1000,None,None),(1000,None,None)])
            self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM logs').fetchone()[0],0)
            self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM casino_logs').fetchone()[0],0)
