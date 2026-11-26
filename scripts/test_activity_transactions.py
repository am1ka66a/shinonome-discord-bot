"""Chat EXP, cooldown and cash rewards share a single transaction."""
import datetime
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot_modules import activity_repo
from bot_modules.tx_ops import log_transaction_in_tx
from test_regressions import SqliteConnection


class ActivityConnection(SqliteConnection):
    def execute(self,sql,args=()):
        sql=sql.replace('ON DUPLICATE KEY UPDATE msg_count=msg_count+%s',
                        'ON CONFLICT(user_id) DO UPDATE SET msg_count=msg_count+%s')
        sql=sql.replace('ON DUPLICATE KEY UPDATE balance=balance+500',
                        'ON CONFLICT(user_id) DO UPDATE SET balance=balance+500')
        return super().execute(sql,args)


class ActivityTransactionTests(unittest.TestCase):
    def setUp(self):
        self.conn=ActivityConnection()
        self.addCleanup(self.conn.db.close)
        self.conn.db.executescript("""
            ALTER TABLE users ADD COLUMN exp INTEGER DEFAULT 0;
            INSERT INTO users (user_id,balance) VALUES ('1',1000);
            CREATE TABLE activity_stats (user_id TEXT PRIMARY KEY,msg_count INTEGER DEFAULT 0,
                last_msg_reward TIMESTAMP,last_exp_reward TIMESTAMP);
        """)
        self.conn.commit()
        self.now=datetime.datetime(2026,11,26,12)

    def award(self,now=None,log=log_transaction_in_tx):
        return activity_repo.process_on_message_activity_sync(lambda:self.conn,log,lambda *a:None,
            Mock(side_effect=AssertionError('must not open another EXP connection')),
            '1',1,now or self.now,True,50)

    def test_cooldown_survives_new_runtime_and_allows_at_exact_boundary(self):
        self.assertTrue(self.award()['exp_awarded'])
        self.assertFalse(self.award(self.now+datetime.timedelta(seconds=44))['exp_awarded'])
        self.assertTrue(self.award(self.now+datetime.timedelta(seconds=45))['exp_awarded'])
        self.assertEqual(self.conn.db.execute("SELECT exp FROM users WHERE user_id='1'").fetchone()[0],100)

    def test_cash_log_failure_rolls_back_exp_and_cooldown_then_retry_awards_once(self):
        self.conn.db.execute("INSERT INTO activity_stats (user_id,msg_count) VALUES ('1',9)")
        self.conn.commit()
        with self.assertRaises(RuntimeError):
            self.award(log=Mock(side_effect=RuntimeError('ledger unavailable')))
        self.assertEqual(self.conn.db.execute("SELECT balance,exp FROM users WHERE user_id='1'").fetchone(),(1000,0))
        self.assertEqual(self.conn.db.execute("SELECT msg_count,last_exp_reward FROM activity_stats").fetchone(),(9,None))
        self.award()
        self.award(self.now+datetime.timedelta(seconds=1))
        self.assertEqual(self.conn.db.execute("SELECT balance,exp FROM users WHERE user_id='1'").fetchone(),(1500,50))
        self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM logs').fetchone()[0],1)
