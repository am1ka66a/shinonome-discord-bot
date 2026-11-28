"""Opt-in integration checks for the disposable CI MySQL, never production."""
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import db, state_repo
from bot_modules import tx_ops


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
            for table in ("bot_reconcile_baseline", "bot_reconcile_meta", "bot_admin_snapshots", "bot_admin_operations", "bot_settings", "bot_game_events", "bot_game_sessions", "russian_roulette_active_matches", "russian_roulette_settlements", "russian_roulette_stats", "casino_logs", "logs", "activity_stats", "blacklist", "users"):
                c.execute("DELETE FROM " + table)
            c.execute("INSERT INTO users (user_id,balance) VALUES ('1',1000),('2',1000)")
        self.store = state_repo.GameStore()

    def balances(self):
        with db.db_cursor() as c:
            c.execute("SELECT balance FROM users ORDER BY user_id")
            return [int(row[0]) for row in c.fetchall()]

    def test_startup_grant_and_reconcile_on_mysql(self):
        from bot_modules import admin_audit_repo as audit, user_repo
        user_repo.ensure_user_exists(3, 0)
        user_repo.ensure_user_exists(3, 50_000)
        user_repo.ensure_user_exists(3, 50_000)
        with db.db_cursor() as c:
            c.execute("SELECT balance, startup_granted FROM users WHERE user_id='3'")
            self.assertEqual(tuple(int(v) for v in c.fetchone()), (50_000, 1))
        report = audit.reconcile_report()
        # Seed accounts 1 and 2 were inserted without ledger rows; account 3 matches.
        self.assertEqual(sorted(r[0] for r in report.drifted), ['1', '2'])
        self.assertEqual(audit.set_reconcile_baseline(7), {"accounts": 2, "total_offset": 2000})
        self.assertEqual(audit.reconcile_report().drifted, [])
        self.assertEqual(self.balances(), [1000, 1000, 50_000])

    def test_audit_indexes_and_ledger_pagination_on_mysql(self):
        from bot_modules import admin_audit_repo as audit
        with db.db_cursor(commit=True) as c:
            for n in range(23):
                c.execute("INSERT INTO casino_logs (user_id,amount,reason) VALUES ('1',%s,'audit test')", (n,))
            c.execute("INSERT INTO casino_logs (user_id,amount,reason) VALUES ('2',999,'other player')")
        snapshot = audit.account_snapshot(1)
        self.assertEqual(snapshot['account'][0], 1000)
        page = snapshot['page']
        ids = [r[0] for r in page.rows]
        with db.db_cursor(commit=True) as c:
            c.execute("INSERT INTO casino_logs (user_id,amount,reason) VALUES ('1',999,'new entry')")
            new_id = c.lastrowid
        while page.has_more:
            page = audit.ledger_page(1, page.upper_id, page.rows[-1][0])
            ids.extend(r[0] for r in page.rows)
        self.assertEqual(len(ids), 23)
        self.assertEqual(len(set(ids)), 23)
        self.assertNotIn(new_id, ids)
        self.assertEqual(self.balances(), [1000, 1000])
        with db.db_cursor() as c:
            c.execute("SELECT version FROM bot_schema_migrations ORDER BY version")
            self.assertEqual([r[0] for r in c.fetchall()], [1, 2, 3, 4, 5])
            c.execute("SHOW INDEX FROM casino_logs WHERE Key_name='idx_casino_logs_user_id'")
            self.assertEqual([r[4] for r in c.fetchall()], ['user_id', 'id'])

    def test_concurrent_payout_credits_once(self):
        self.store.debit("ci:bj", "bj", 1, 100, "open")
        self.store.prepare("ci:bj", {"1": 200}, {"1": [True, 100, False]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.store.apply("ci:bj"), range(2)))
        self.assertEqual(self.balances(), [1100, 1000])

    def test_pool_disconnect_inside_transaction_cannot_replay_only_last_statement(self):
        import pymysql
        with self.assertRaises(pymysql.MySQLError):
            with db.db_cursor(commit=True) as c:
                c.execute("UPDATE users SET balance=balance-100 WHERE user_id='1'")
                # Close just the disposable test connection. Its uncommitted
                # debit must roll back; DBUtils must not reconnect and commit
                # only the following log as an orphaned partial operation.
                c._con._con.close()
                c.execute("INSERT INTO logs (user_id,amount,reason) VALUES ('1',-100,'must not replay')")
        self.assertEqual(self.balances(),[1000,1000])
        with db.db_cursor() as c:
            c.execute('SELECT COUNT(*) FROM logs')
            self.assertEqual(c.fetchone()[0],0)

    def test_concurrent_claim_by_same_user_is_once(self):
        self.store.debit("ci:packet", "redpacket", 1, 100, "open", {"creator_id": 1, "count": 1, "claims": {}})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.claim("ci:packet", 2)[0], range(2)))
        self.assertEqual(results, [100, 100])
        self.assertEqual(self.balances(), [900, 1100])

    def test_concurrent_roulette_recovery_pays_saved_result_once(self):
        import json
        from bot_modules import rr_match_repo as rr
        with db.db_cursor(commit=True) as c:
            c.execute("INSERT INTO russian_roulette_active_matches (match_id,guild_id,channel_id,mode,phase,payload) VALUES ('ci:rr','1','2','duel','playing',%s)",
                      (json.dumps(dict(participant_ids=[1,2])),))
        args=dict(match_id='ci:rr',event_key='duel',credits={1:200},results={1:(True,100),2:(False,-100)},
            reason='ci rr payout',payload=dict(participant_ids=[1,2],settled=True),finished=True)
        rr.prepare_event_sync(**args)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:rr.recover_pending_events_sync(),range(2)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.balances(),[1200,1000])
        with db.db_cursor() as c:
            c.execute("SELECT COUNT(*) FROM russian_roulette_settlements WHERE match_id='ci:rr'")
            self.assertEqual(c.fetchone()[0],1)

    def test_roulette_rematch_funds_both_players_or_neither(self):
        from bot_modules import rr_match_repo as rr
        args=dict(charges={1:100,2:1100},event_key='123',reason='ci stakes',match_id='ci:fund',
            guild_id=1,channel_id=2,message_id=None,mode='duel',phase='playing',
            payload=dict(participant_ids=[1,2],stakes={'1':100,'2':1100}))
        self.assertFalse(rr.fund_match_sync(**args))
        self.assertEqual(self.balances(),[1000,1000])
        args['charges']={1:100,2:100}
        args['payload']['stakes']={'1':100,'2':100}
        self.assertTrue(rr.fund_match_sync(**args))
        self.assertTrue(rr.fund_match_sync(**args))
        self.assertEqual(self.balances(),[900,900])

    def test_roulette_stake_retry_after_lost_commit_response_is_once(self):
        import contextlib
        from bot_modules import rr_match_repo as rr
        args=dict(charges={1:100,2:100},event_key='123',reason='ci stakes',match_id='ci:fund',
            guild_id=1,channel_id=2,message_id=None,mode='duel',phase='playing',
            payload=dict(participant_ids=[1,2],stakes={'1':100,'2':100}))
        real_cursor=rr.db_cursor
        @contextlib.contextmanager
        def lost_response(**kwargs):
            with real_cursor(**kwargs) as c:
                yield c
            raise ConnectionError('response lost after commit')
        with patch.object(rr,'db_cursor',lost_response),self.assertRaises(ConnectionError):
            rr.fund_match_sync(**args)
        self.assertTrue(rr.fund_match_sync(**args))
        self.assertEqual(self.balances(),[900,900])
        with db.db_cursor() as c:
            c.execute('SELECT COUNT(*) FROM logs')
            self.assertEqual(c.fetchone()[0],2)

    def test_payout_response_lost_then_retry_does_not_duplicate_experience(self):
        import contextlib
        self.store.debit('ci:exp','bj',1,100,'open')
        self.store.prepare('ci:exp',{1:200},{1:(True,100,False)},experience={1:200})
        real_cursor=state_repo.db_cursor
        @contextlib.contextmanager
        def lost_response(**kwargs):
            with real_cursor(**kwargs) as c:
                yield c
            raise ConnectionError('lost response after successful commit')
        with patch.object(state_repo,'db_cursor',lost_response),self.assertRaises(ConnectionError):
            self.store.apply('ci:exp')
        receipt=self.store.apply('ci:exp')
        self.assertEqual(receipt['experience'],{'1':200})
        with db.db_cursor() as c:
            c.execute("SELECT balance,exp FROM users WHERE user_id='1'")
            self.assertEqual(c.fetchone(),(1100,200))

    def test_concurrent_chat_messages_share_persisted_exp_cooldown(self):
        import datetime
        from unittest.mock import Mock
        from bot_modules import activity_repo
        from bot_modules.tx_ops import log_transaction_in_tx
        now=datetime.datetime(2026,11,26,12)
        def award(_):
            return activity_repo.process_on_message_activity_sync(db.get_db_connection,log_transaction_in_tx,
                lambda *a:None,Mock(side_effect=AssertionError('nested EXP connection')),'1',1,now,True,50)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(award,range(2)))
        self.assertEqual(sum(r['exp_awarded'] for r in results),1)
        with db.db_cursor() as c:
            c.execute("SELECT balance,exp FROM users WHERE user_id='1'")
            self.assertEqual(c.fetchone(),(1000,50))

    def test_payout_failure_rolls_back_then_retries(self):
        self.store.debit("ci:duel", "duel", 1, 100, "a")
        self.store.debit("ci:duel", "duel", 2, 100, "b")
        self.store.prepare("ci:duel", {}, refund=True)
        real_log = tx_ops.log_transaction_in_tx
        def fail_second(c, uid, amount, reason):
            if str(uid) == "2":
                raise RuntimeError("injected log failure")
            real_log(c, uid, amount, reason)
        with patch.object(tx_ops, "log_transaction_in_tx", side_effect=fail_second), self.assertRaises(RuntimeError):
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

    def test_abandoned_lock_expires_and_waiting_bot_acquires_without_restart(self):
        import asyncio
        from unittest.mock import Mock
        import pymysql
        from bot_modules.runtime.instance_lease import InstanceLease, LEASE_IDLE_TIMEOUT_SECONDS
        first, waiting = InstanceLease(), InstanceLease()
        try:
            first.acquire()
            c = first.connection.cursor()
            c.execute("SELECT @@session.wait_timeout,@@global.wait_timeout")
            session_timeout, global_timeout = c.fetchone()
            self.assertEqual(session_timeout, LEASE_IDLE_TIMEOUT_SECONDS)
            # Accelerate only this disposable owner's expiry. No close(), KILL,
            # RELEASE_LOCK or heartbeat: MySQL must release the stale lock itself.
            c.execute("SET SESSION wait_timeout=2")
            async def recover():
                await asyncio.wait_for(waiting.acquire_with_retry(Mock(), initial_delay=0.1, max_delay=0.25), timeout=10)
            asyncio.run(recover())
            waiting.verify()
            with self.assertRaises(pymysql.MySQLError):
                first.verify()
            with db.db_cursor() as c:
                c.execute("SELECT @@global.wait_timeout")
                self.assertEqual(c.fetchone()[0], global_timeout)
            self.assertEqual(self.balances(), [1000, 1000])
        finally:
            first.close()
            waiting.close()

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
