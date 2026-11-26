"""Durable roulette decisions survive payment failure, retries and restarts."""
import inspect
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import discord
import pymysql
from discord.ext import commands
from bot_modules import rr_match_repo as repo
from bot_modules.commands import russian_roulette
from test_regressions import SqliteConnection, context, interaction


class RoulettePendingTests(unittest.TestCase):
    def setUp(self):
        self.conn=SqliteConnection()
        self.addCleanup(self.conn.db.close)
        self.conn.fetchall=lambda:self.conn.cur.fetchall()
        self.conn.db.executescript("""
            INSERT INTO users (user_id,balance) VALUES ('1',0),('2',0),('3',0);
            INSERT INTO russian_roulette_active_matches VALUES ('match','playing','{}');
        """)
        self.conn.commit()
        p=patch('bot_modules.db.get_db_connection',return_value=self.conn)
        p.start()
        self.addCleanup(p.stop)
        self.args=dict(match_id='match',event_key='duel',credits={1:200},
            results={1:(True,100),2:(False,-100)},reason='test payout',
            payload=dict(participant_ids=[1,2],settled=True),finished=True)
        with repo._lock:
            repo._user_active.clear()
        self.addCleanup(repo._user_active.clear)

    def balances(self):
        return self.conn.db.execute('SELECT balance FROM users ORDER BY user_id').fetchall()

    def test_failed_payout_stays_durable_and_background_recovery_finishes_once(self):
        with patch.object(repo,'log_transaction_in_tx',side_effect=RuntimeError('ledger offline')):
            with self.assertRaises(RuntimeError):
                repo.settle_event_sync(**self.args)
        phase,raw=self.conn.db.execute("SELECT phase,payload FROM russian_roulette_active_matches").fetchone()
        self.assertEqual(phase,'settling')
        self.assertEqual(json.loads(raw)[repo.PENDING_EVENT_KEY]['credits'],{'1':200})
        self.assertEqual(self.balances(),[(0,),(0,),(0,)])
        self.assertEqual(repo.recover_pending_events_sync(),1)
        self.assertEqual(repo.recover_pending_events_sync(),0)
        self.assertEqual(self.balances(),[(200,),(0,),(0,)])
        self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM russian_roulette_settlements').fetchone()[0],1)

    def test_retry_cannot_change_a_saved_winner_or_payout(self):
        self.assertTrue(repo.prepare_event_sync(**self.args))
        changed=dict(self.args,credits={2:999},results={2:(True,899),1:(False,-100)})
        repo.settle_event_sync(**changed)
        self.assertEqual(self.balances(),[(200,),(0,),(0,)])

    def test_pending_decision_cannot_be_overwritten_by_a_refund(self):
        repo.prepare_event_sync(**self.args)
        with self.assertRaises(ValueError):
            repo.prepare_event_sync(**dict(self.args,event_key='refund',credits={1:100,2:100},results={}))
        repo.apply_pending_event_sync('match')
        self.assertEqual(self.balances(),[(200,),(0,),(0,)])

    def test_leave_refund_and_lobby_checkpoint_are_once_only(self):
        repo.sync_match_users('match',[1,2,3])
        args=dict(self.args,event_key='leave:123',credits={2:100},results={},
            payload=dict(participant_ids=[1,3],stakes={'1':100,'3':100}),finished=False,next_phase='lobby')
        self.assertTrue(repo.settle_event_sync(**args))
        self.assertFalse(repo.settle_event_sync(**args))
        phase,raw=self.conn.db.execute('SELECT phase,payload FROM russian_roulette_active_matches').fetchone()
        self.assertEqual(phase,'lobby')
        self.assertNotIn(repo.PENDING_EVENT_KEY,json.loads(raw))
        self.assertEqual(self.balances(),[(0,),(100,),(0,)])
        self.assertIsNone(repo.user_active_match_id(2))

    def test_negative_credit_does_not_create_a_pending_decision(self):
        with self.assertRaises(ValueError):
            repo.prepare_event_sync(**dict(self.args,credits={1:-1}))
        self.assertEqual(self.conn.db.execute('SELECT phase,payload FROM russian_roulette_active_matches').fetchone(),('playing','{}'))


class RouletteRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot=commands.Bot(command_prefix='!',intents=discord.Intents.none())
        self.ctx=context()
        russian_roulette.register_russian_roulette_commands(self.bot,self.ctx)
        callback=self.bot.tree.get_command('russian_roulette').get_command('duel').callback
        cls=inspect.getclosurevars(callback).nonlocals['RrMatch']
        self.match=cls(match_id='match',mode='duel',guild_id=1,channel_id=2,bet_base=100,
            participant_ids=[1,2],stakes={1:100,2:100},settled=True)
        self.invite_cls=inspect.getclosurevars(callback).nonlocals['RrInviteView']

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_transient_payment_failure_retries_exact_same_decision(self):
        with patch.object(repo,'settle_event_sync',side_effect=[pymysql.OperationalError(2013,'lost'),True]) as pay, \
                patch.object(russian_roulette.asyncio,'sleep',new=AsyncMock()) as sleep:
            await self.match.payout_duel(2,1)
        self.assertEqual(pay.call_count,2)
        self.assertEqual(pay.call_args_list[0],pay.call_args_list[1])
        sleep.assert_awaited_once_with(5)

    async def test_programming_error_is_not_retried_forever(self):
        with patch.object(repo,'settle_event_sync',side_effect=ValueError('invalid payment')) as pay, \
                patch.object(russian_roulette.asyncio,'sleep',new=AsyncMock()) as sleep:
            with self.assertRaises(ValueError):
                await self.match.payout_duel(2,1)
        pay.assert_called_once()
        sleep.assert_not_awaited()

    async def test_ambiguous_stake_commit_freezes_view_until_canonical_restore(self):
        self.match.settled=False
        self.bot._rr_recovered=True
        with patch.object(repo,'fund_match_sync',side_effect=pymysql.OperationalError(2013,'response lost')):
            with self.assertRaises(pymysql.OperationalError):
                await self.match.fund({1:100},123,'test','playing')
        self.assertTrue(self.match.settled)
        self.assertFalse(self.bot._rr_recovered)

    async def test_unrelated_recovery_preserves_the_live_match_and_view(self):
        # Initialize the startup cache once, then fund a live match through the
        # same path used by new games, accepts, and rematches.
        await self.bot.recover_rr_matches()
        self.match.settled=False
        view=self.invite_cls(self.match)
        self.match.view=view
        self.addCleanup(view.stop)
        with patch.object(repo,'fund_match_sync',return_value=True):
            self.assertTrue(await self.match.fund({1:100},123,'test','playing'))
        self.bot._rr_recovered=False
        with patch.object(repo,'fetch_active_matches_sync',return_value=[{'match_id':'match'}]) as scan, \
                patch.object(repo,'fetch_match_sync') as restore:
            await self.bot.recover_rr_matches()
        scan.assert_called_once_with(rebuild_cache=False)
        restore.assert_not_called()
        self.assertIs(self.match.view,view)
        self.assertFalse(self.match.settled)
        self.assertTrue(self.bot._rr_recovered)

    async def test_accept_waits_until_initial_publish_and_checkpoint_finish(self):
        import asyncio
        self.match.settled=False
        self.match.opponent_id=2
        self.match.challenger_id=1
        view=self.invite_cls(self.match)
        self.addCleanup(view.stop)
        self.match.view=view
        with patch.object(self.match,'fund',new=AsyncMock(return_value=True)) as fund:
            async with self.match.publishing():
                accept=asyncio.create_task(view.accept.callback(interaction()))
                await asyncio.sleep(0.01)
                fund.assert_not_awaited()
            await asyncio.wait_for(accept,timeout=3)
            fund.assert_awaited_once()
        if self.match.view:
            self.match.view.stop()


class RouletteFundingTests(unittest.TestCase):
    def setUp(self):
        RoulettePendingTests.setUp(self)
        self.conn.db.executescript("""
            DROP TABLE russian_roulette_active_matches;
            CREATE TABLE russian_roulette_active_matches (match_id TEXT PRIMARY KEY,guild_id TEXT,
                channel_id TEXT,message_id TEXT,mode TEXT,phase TEXT,payload TEXT);
            CREATE TABLE bot_settings (setting_key TEXT PRIMARY KEY,value_json TEXT);
            INSERT INTO bot_settings VALUES ('casino_open','true');
            UPDATE users SET balance=1000;
        """)
        self.conn.commit()
        execute=self.conn.execute
        def sqlite_execute(sql,args=()):
            if 'ON DUPLICATE KEY UPDATE message_id=VALUES' in sql:
                sql=sql.split('ON DUPLICATE KEY')[0]+('ON CONFLICT(match_id) DO UPDATE SET '
                    'message_id=excluded.message_id,phase=excluded.phase,payload=excluded.payload')
            return execute(sql,args)
        self.conn.execute=sqlite_execute
        self.fund_args=dict(charges={1:100,2:100},event_key='123',reason='test stakes',match_id='fund',
            guild_id=1,channel_id=2,message_id=None,mode='duel',phase='playing',
            payload=dict(participant_ids=[1,2],stakes={'1':100,'2':100}))

    def balances(self):
        return self.conn.db.execute('SELECT balance FROM users ORDER BY user_id').fetchall()

    def test_both_stakes_and_checkpoint_commit_once(self):
        self.assertTrue(repo.fund_match_sync(**self.fund_args))
        self.assertTrue(repo.fund_match_sync(**self.fund_args))
        self.assertEqual(self.balances(),[(900,),(900,),(1000,)])
        self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM logs').fetchone()[0],2)
        raw=self.conn.db.execute("SELECT payload FROM russian_roulette_active_matches WHERE match_id='fund'").fetchone()[0]
        self.assertEqual(json.loads(raw)['stakes'],{'1':100,'2':100})

    def test_second_player_insufficient_rolls_back_first_player_and_save(self):
        self.conn.db.execute("UPDATE users SET balance=50 WHERE user_id='2'")
        self.conn.commit()
        self.assertFalse(repo.fund_match_sync(**self.fund_args))
        self.assertEqual(self.balances(),[(1000,),(50,),(1000,)])
        for table in ('russian_roulette_settlements','russian_roulette_active_matches','logs'):
            self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_save_failure_rolls_back_stakes_logs_and_request_key(self):
        execute=self.conn.execute
        def fail_save(sql,args=()):
            if 'INSERT INTO russian_roulette_active_matches' in sql:
                raise RuntimeError('save unavailable')
            return execute(sql,args)
        with patch.object(self.conn,'execute',side_effect=fail_save),self.assertRaises(RuntimeError):
            repo.fund_match_sync(**self.fund_args)
        self.assertEqual(self.balances(),[(1000,),(1000,),(1000,)])
        self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM logs').fetchone()[0],0)

    def test_other_match_cannot_charge_a_reserved_player(self):
        repo.fund_match_sync(**self.fund_args)
        with self.assertRaises(ValueError):
            repo.fund_match_sync(**dict(self.fund_args,match_id='other',event_key='456'))
        self.assertEqual(self.balances(),[(900,),(900,),(1000,)])

    def test_recovery_listing_does_not_overwrite_concurrent_busy_cache_updates(self):
        repo.fund_match_sync(**self.fund_args)
        repo.sync_match_users('concurrent',[3])
        rows=repo.fetch_active_matches_sync(rebuild_cache=False)
        self.assertEqual(len(rows),1)
        self.assertEqual(repo.user_active_match_id(3),'concurrent')

    def test_finished_match_cannot_be_reopened_by_replaying_stake_request(self):
        repo.fund_match_sync(**self.fund_args)
        repo.settle_event_sync(match_id='fund',event_key='duel',credits={1:200},
            results={1:(True,100),2:(False,-100)},reason='win',
            payload=self.fund_args['payload'],finished=True)
        self.assertFalse(repo.fund_match_sync(**self.fund_args))
        self.assertEqual(self.balances(),[(1100,),(900,),(1000,)])
