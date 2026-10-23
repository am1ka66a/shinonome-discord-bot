"""Offline regression tests. Never connect to Discord or a production database.

Run: python -m unittest discover -s scripts -p test_*.py -v
"""
import asyncio
import datetime
import inspect
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord
from discord.ext import commands
from bot_modules import admin_commands, bot_core, config, economy_service, economy_repo, game_repo, lottery_repo, rr_match_repo
from bot_modules.commands import economy, duel, blackjack, russian_roulette, wanted
from bot_modules.discord_helpers import interaction_defer_if_needed, interaction_send
from bot_modules.runtime import app_lock
from bot_modules.runtime.locks import KeyedLocks

sqlite3.register_adapter(datetime.datetime, lambda value: value.isoformat(' '))
sqlite3.register_converter('TIMESTAMP', lambda value: datetime.datetime.fromisoformat(value.decode()))


def interaction(uid=2, iid=100, command='transfer'):
    response = Mock()
    response.is_done.return_value = False

    async def ack(*args, **kwargs):
        response.is_done.return_value = True

    response.defer = AsyncMock(side_effect=ack)
    response.send_message = AsyncMock(side_effect=ack)
    response.edit_message = AsyncMock(side_effect=ack)
    return SimpleNamespace(id=iid, user=SimpleNamespace(id=uid, bot=False),
        response=response, followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(), original_response=AsyncMock(),
        message=SimpleNamespace(edit=AsyncMock()), command=SimpleNamespace(name=command),
        channel=SimpleNamespace(guild=None), guild=None)


class Context(dict):
    def __missing__(self, key):
        self[key] = AsyncMock()
        return self[key]


def context():
    return Context(FEATURE_TOGGLES={}, MAX_LEVEL=100,
        RED_PACKET_MIN_SECONDS=10, red_packet_seq_ref=[0], logger=Mock(),
        SIDE_BET_RATIO=0.5,
        get_is_event_active=lambda: True, RUSSIAN_ROULETTE_CHAMBERS=6,
        COINFLIP_MIN_BET=100, COINFLIP_MAX_BET=10_000_000,
        interaction_defer_if_needed=interaction_defer_if_needed,
        interaction_send=interaction_send)


def nested_class(bot, command, name):
    callback = bot.tree.get_command(command).callback
    return inspect.getclosurevars(callback).nonlocals[name]


class AsyncRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        self.ctx = context()

    async def asyncTearDown(self):
        await self.bot.close()

    def redpacket(self, amount=100, count=1):
        economy.register_economy_commands(self.bot, self.ctx)
        return nested_class(self.bot, 'redpacket', 'RedPacketView')(1, amount, count)

    async def test_failed_redpacket_credit_can_be_retried(self):
        view = self.redpacket()
        credit = self.ctx['credit_balance_with_log_async']
        credit.side_effect = [RuntimeError('offline DB failure'), None]
        await view.children[0].callback(interaction())
        self.assertEqual((view.left_amount, view.left_count, view.claimed_users), (100, 1, set()))
        await view.children[0].callback(interaction())
        self.assertEqual((view.left_amount, view.left_count, view.claimed_users), (0, 0, {2}))
        view.stop()

    async def test_concurrent_redpacket_clicks_credit_only_once(self):
        view = self.redpacket()
        async def slow_credit(*args):
            await asyncio.sleep(0.01)
        self.ctx['credit_balance_with_log_async'].side_effect = slow_credit
        await asyncio.gather(*(view.children[0].callback(interaction()) for _ in range(5)))
        self.assertEqual(self.ctx['credit_balance_with_log_async'].await_count, 1)
        self.assertEqual(view.left_amount, 0)
        view.stop()

    async def test_expired_redpacket_cannot_be_claimed(self):
        view = self.redpacket()
        await view.on_timeout()
        await view.children[0].callback(interaction())
        self.ctx['credit_balance_with_log_async'].assert_not_awaited()
        view.stop()

    async def test_readonly_completion_does_not_unlock_transfer(self):
        app_lock.register_app_command_lock(self.bot, Mock())
        self.assertTrue(await self.bot.tree.interaction_check(interaction(iid=1)))
        readonly = interaction(iid=2, command='balance')
        self.assertTrue(await self.bot.tree.interaction_check(readonly))
        await self.bot.on_app_command_completion(readonly, None)
        self.assertFalse(await self.bot.tree.interaction_check(interaction(iid=3)))
        await self.bot.on_app_command_completion(interaction(iid=1), None)
        self.assertTrue(await self.bot.tree.interaction_check(interaction(iid=4)))

    async def test_expired_command_cannot_release_replacement_lock(self):
        app_lock.register_app_command_lock(self.bot, Mock(), timeout_seconds=5)
        with patch('bot_modules.runtime.app_lock.time.monotonic', side_effect=[0, 10, 11]):
            self.assertTrue(await self.bot.tree.interaction_check(interaction(iid=1)))
            self.assertTrue(await self.bot.tree.interaction_check(interaction(iid=2)))
            await self.bot.on_app_command_completion(interaction(iid=1), None)
            self.assertFalse(await self.bot.tree.interaction_check(interaction(iid=3)))

    async def test_repeated_ready_keeps_one_task_per_job(self):
        # Use real coroutines so the test also exercises task lifecycle handling.
        async def job():
            await asyncio.sleep(3600)
        factories = {name: Mock(side_effect=job) for name in ('vc_reward_task', 'logs_retention_task')}
        init = Mock()
        bot_core.register_on_ready(self.bot, logger=Mock(), dm_relay_channel_id=0,
            init_db=init, discord_log_register=Mock(), event_tasks=factories,
            snapshot_cache_tasks={})
        with patch.object(self.bot.tree, 'sync', new=AsyncMock(return_value=[])):
            await asyncio.gather(self.bot.on_ready(), self.bot.on_ready())
        init.assert_called_once()
        for fn in factories.values():
            fn.assert_called_once()
        tasks = [t for t in asyncio.all_tasks() if t.get_name() in factories]
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_failed_initialization_does_not_start_jobs(self):
        job = Mock()
        bot_core.register_on_ready(self.bot, logger=Mock(), dm_relay_channel_id=0,
            init_db=Mock(side_effect=RuntimeError('offline')), discord_log_register=Mock(),
            event_tasks={'vc_reward_task': job}, snapshot_cache_tasks={})
        await self.bot.on_ready()
        job.assert_not_called()

    async def test_transient_initialization_failure_retries_and_becomes_ready(self):
        from bot_modules.db import pymysql
        init = Mock(side_effect=[pymysql.OperationalError(2003, 'DNS unavailable'), None])
        bot_core.register_on_ready(self.bot, logger=Mock(), dm_relay_channel_id=0,
            init_db=init, discord_log_register=Mock(), event_tasks={}, snapshot_cache_tasks={})
        with patch.object(bot_core.asyncio, 'sleep', new=AsyncMock()) as sleep, \
                patch.object(self.bot.tree, 'sync', new=AsyncMock(return_value=[])):
            await self.bot.on_ready()
        self.assertEqual(init.call_count, 2)
        sleep.assert_awaited_once_with(5)
        self.assertTrue(self.bot._db_ready_event.is_set())

    async def test_duel_invite_accept_and_timeout_do_not_both_settle(self):
        duel.register_duel_commands(self.bot, self.ctx)
        cls = nested_class(self.bot, 'duel', 'EDuelInviteView')
        view = cls(SimpleNamespace(id=1), SimpleNamespace(id=2), 100)
        async def debit(*args):
            await asyncio.sleep(0.01)
            return True
        self.ctx['try_deduct_balance_async'].side_effect = debit
        inter = interaction()
        inter.user.display_name = 'opponent'
        view.challenger = SimpleNamespace(id=1, mention='<@1>', display_name='a')
        view.opponent = SimpleNamespace(id=2, mention='<@2>', display_name='b')
        await asyncio.gather(view.accept.callback(inter), view.on_timeout())
        self.ctx['try_deduct_balance_async'].assert_awaited_once()
        self.ctx['credit_balance_with_log_async'].assert_not_awaited()
        view.stop()

    async def test_blackjack_timeout_settles_paid_game(self):
        self.ctx.update(get_user_stats_async=AsyncMock(return_value=(100, 0, 0, 0)),
                        roll_gamble_exp_from_bet=lambda bet: 0)
        blackjack.register_blackjack_commands(self.bot, self.ctx)
        setup_cls = nested_class(self.bot, 'bj', 'SetupView')
        game_cls = inspect.getclosurevars(setup_cls.start.__wrapped__).nonlocals['BlackjackGame']
        game = game_cls(SimpleNamespace(id=1, display_name='a'), 100, 0, 0, upfront_cost=100)
        game.hands = [[{'rank': '10', 'suit': 's'}, {'rank': '8', 'suit': 'h'}]]
        game.d_hand = [{'rank': '10', 'suit': 'h'}, {'rank': '7', 'suit': 's'}]
        game.side_p = 0
        game.message = SimpleNamespace(channel=SimpleNamespace(guild=None), guild=None, edit=AsyncMock())
        with patch('bot_modules.commands.blackjack.asyncio.sleep', new=AsyncMock()):
            await game.on_timeout()
            await game.on_timeout()
        self.ctx['update_game_result_async'].assert_awaited_once_with(1, 200, 100, True, False)
        game.stop()

    async def test_keyed_locks_serialize_and_clean_up(self):
        locks = KeyedLocks()
        active = 0
        peak = 0
        async def work():
            nonlocal active, peak
            async with locks.hold(1):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1
        await asyncio.gather(*(work() for _ in range(10)))
        self.assertEqual(peak, 1)
        self.assertFalse(locks._entries)

    async def test_roulette_accept_double_click_deducts_once(self):
        russian_roulette.register_russian_roulette_commands(self.bot, self.ctx)
        command = self.bot.tree.get_command('russian_roulette').get_command('duel')
        classes = inspect.getclosurevars(command.callback).nonlocals
        match = classes['RrMatch'](match_id='test', mode='duel', guild_id=1,
            channel_id=1, bet_base=100, participant_ids=[1, 2], stakes={1: 100},
            challenger_id=1, opponent_id=2)
        view = classes['RrInviteView'](match)
        match.view = view
        async def debit(*args):
            await asyncio.sleep(0.01)
            return True
        self.ctx['try_deduct_balance_async'].side_effect = debit
        await asyncio.gather(view.accept.callback(interaction()), view.accept.callback(interaction()))
        self.ctx['try_deduct_balance_async'].assert_awaited_once()
        match.view.stop()
        view.stop()

    async def test_full_command_registration_without_connecting(self):
        source = (ROOT / 'bot.py').read_text(encoding='utf-8').split('MEMBERS_INTENT_FALLBACK_FLAG =')[0]
        namespace = {'__name__': 'offline_registration_test', '__file__': str(ROOT / 'bot.py')}
        with patch('dotenv.load_dotenv'), \
             patch.object(bot_core, 'configure_stdio_line_buffering'), \
             patch('bot_modules.db.get_db_connection', side_effect=AssertionError('unexpected DB access')):
            exec(compile(source, str(ROOT / 'bot.py'), 'exec'), namespace)
        registered = namespace['bot']
        self.assertGreater(len(list(registered.tree.walk_commands())), 30)
        await registered.close()

    async def test_admin_reversal_rejects_missing_transaction(self):
        conn = SqliteConnection()
        try:
            self.ctx.update(ALLOWED_HOST_IDS=[2], get_db_connection=lambda: conn)
            admin_commands.register_admin_commands(self.bot, self.ctx)
            inter = interaction()
            await self.bot.tree.get_command('admin_revert_tx').callback(inter, 999)
            self.assertIn('找不到該流水', inter.followup.send.call_args.args[0])
        finally:
            conn.db.close()

    async def test_admin_take_logs_actual_deduction(self):
        conn = SqliteConnection()
        conn.db.execute("INSERT INTO users (user_id,balance) VALUES ('3',50)")
        conn.commit()
        try:
            member = SimpleNamespace(id=3, mention='<@3>')
            self.ctx.update(ALLOWED_HOST_IDS=[2], get_db_connection=lambda: conn,
                resolve_slash_target=AsyncMock(return_value=(member, None)))
            admin_commands.register_admin_commands(self.bot, self.ctx)
            inter = interaction()
            inter.user.display_name = 'admin'
            await self.bot.tree.get_command('take').callback(inter, 100, member)
            self.assertEqual(conn.db.execute('SELECT balance FROM users').fetchone()[0], 0)
            self.assertEqual(conn.db.execute('SELECT amount FROM logs').fetchone()[0], -50)
        finally:
            conn.db.close()

    async def test_admin_reversal_is_once_and_does_not_confuse_tx1_with_tx10(self):
        conn = SqliteConnection()
        conn.db.execute("INSERT INTO users (user_id,balance) VALUES ('3',50)")
        conn.db.executemany('INSERT INTO logs (id,user_id,amount,reason) VALUES (?,?,?,?)',
            [(1, '3', -50, 'original'), (10, '3', 100, '管理員沖正 tx#10（原: unrelated）')])
        conn.commit()
        try:
            self.ctx.update(ALLOWED_HOST_IDS=[2], get_db_connection=lambda: conn)
            admin_commands.register_admin_commands(self.bot, self.ctx)
            callback = self.bot.tree.get_command('admin_revert_tx').callback
            await callback(interaction(), 1)
            await callback(interaction(), 1)
            self.assertEqual(conn.db.execute('SELECT balance FROM users').fetchone()[0], 100)
            self.assertEqual(conn.db.execute('SELECT COUNT(*) FROM logs WHERE reason LIKE ?', ('管理員沖正 tx#1（%',)).fetchone()[0], 1)
        finally:
            conn.db.close()

    async def test_old_duel_picker_cannot_play_in_next_round(self):
        duel.register_duel_commands(self.bot, self.ctx)
        invite = nested_class(self.bot, 'duel', 'EDuelInviteView')
        match_cls = inspect.getclosurevars(invite.accept.__wrapped__).nonlocals['EDuelMatch']
        match = match_cls(SimpleNamespace(id=1), SimpleNamespace(id=2), 100)
        match.round_in_game = 2
        await match.record_pick(interaction(), 2, 'citizen', (1, 1))
        self.assertEqual(match.picks, {})

    async def test_cop_hunt_success_and_failure_return_cleanly(self):
        self.ctx.update({name: value for name, value in vars(config).items() if name.isupper()})
        criminal = SimpleNamespace(id=3, mention='<@3>')
        conn = Mock()
        cursor = conn.cursor.return_value
        self.ctx.update(COP_HUNT_FEE=300000, BAIL_COST=100000, WANTED_BUYOUT_COST=300000,
            COP_HUNT_CAPTURE_BASE_PCT=35, COP_HUNT_CAPTURE_PER_STAR_PCT=5,
            GOOD_CITIZEN_CERT_COST=50000000, GOOD_CITIZEN_DESTROY_COST=500000000,
            get_db_connection=lambda: conn, resolve_slash_target=AsyncMock(return_value=(criminal, None)),
            lock_user_rows=Mock(), log_transaction_in_tx=Mock(),
            now_tw_naive=datetime.datetime.now, rob_history_total_from_raw=lambda _: (100, 1),
            get_last_five_robs_total=lambda _: (100, 1, []))
        wanted.register_wanted_commands(self.bot, self.ctx)
        for draw in (0.0, 0.99):
            cursor.fetchone.side_effect = [('cop', 1, 1000000), (1, 0, 500, 0, 1, None, 0)]
            inter = interaction()
            inter.user.mention = '<@2>'
            with patch('bot_modules.commands.wanted.random.random', return_value=draw):
                await self.bot.tree.get_command('cop_hunt').callback(inter, criminal)
            self.assertTrue(inter.followup.send.called)


class SqliteConnection:
    """Small MySQL-to-SQLite adapter for testing actual balance/rollback behavior."""
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.create_function('GREATEST', -1, max)
        self.db.execute('CREATE TABLE users (user_id TEXT PRIMARY KEY, balance INTEGER, total_games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, total_profit INTEGER DEFAULT 0, level INTEGER DEFAULT 1, last_hourly_claim TIMESTAMP, hourly_bank INTEGER DEFAULT 0)')
        self.db.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER, reason TEXT)')
        self.db.execute('CREATE TABLE casino_logs (user_id TEXT, amount INTEGER, reason TEXT, source_log_id INTEGER)')
        self.db.execute('CREATE TABLE russian_roulette_settlements (match_id TEXT, event_key TEXT, PRIMARY KEY(match_id,event_key))')
        self.db.execute('CREATE TABLE russian_roulette_active_matches (match_id TEXT PRIMARY KEY, phase TEXT, payload TEXT)')
        self.db.execute('CREATE TABLE russian_roulette_stats (user_id TEXT PRIMARY KEY, games INTEGER, wins INTEGER, profit INTEGER)')

    def cursor(self):
        return self

    def execute(self, sql, args=()):
        if 'ON DUPLICATE KEY UPDATE balance=COALESCE' in sql:
            sql = sql.split('ON DUPLICATE KEY')[0] + 'ON CONFLICT(user_id) DO UPDATE SET balance=COALESCE(balance,0)+?'
        if 'ON DUPLICATE KEY UPDATE games=games+1' in sql:
            sql = sql.split('ON DUPLICATE KEY')[0] + 'ON CONFLICT(user_id) DO UPDATE SET games=games+1, wins=wins+?, profit=profit+?'
        self.cur = self.db.execute(sql.replace('%s', '?').replace(' FOR UPDATE', '').replace('INSERT IGNORE', 'INSERT OR IGNORE'), args)
        self.rowcount = self.cur.rowcount
        self.lastrowid = self.cur.lastrowid

    def fetchone(self):
        return self.cur.fetchone()

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass  # db_conn returns the connection; retain it for assertions.


class DatabaseRegressions(unittest.TestCase):
    def setUp(self):
        self.conn = SqliteConnection()

    def tearDown(self):
        self.conn.db.close()

    def test_exp_crosses_former_milestones_without_extra_money_or_tables(self):
        self.conn.db.execute('ALTER TABLE users ADD COLUMN exp INTEGER DEFAULT 0')
        self.conn.db.execute("INSERT INTO users (user_id,balance) VALUES ('1',1234)")
        self.conn.db.commit()
        calculate = lambda exp: game_repo.calc_level_from_exp(100, exp)
        target_exp = game_repo.exp_required_for_level(100, 100)
        result = game_repo.add_user_exp(lambda: self.conn, calculate, 1, target_exp)
        self.assertEqual(result, (1, 100, target_exp))
        self.assertEqual(self.conn.db.execute('SELECT balance,exp,level FROM users').fetchone(),
                         (1234, target_exp, 100))
        self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM logs').fetchone()[0], 0)

    def log(self, c, uid, amount, reason):
        c.execute('INSERT INTO logs (user_id,amount,reason) VALUES (%s,%s,%s)', (str(uid), amount, reason))

    def test_credit_creates_new_user_and_adds_existing_balance(self):
        for credit in (economy_service.credit_user, economy_repo.credit_balance_with_log):
            credit(lambda: self.conn, self.log, 123, 100, 'redpacket')
        self.assertEqual(self.conn.db.execute('SELECT balance FROM users').fetchone()[0], 200)
        self.assertEqual(self.conn.db.execute('SELECT SUM(amount) FROM logs').fetchone()[0], 200)

    def test_credit_and_log_roll_back_together(self):
        with self.assertRaises(RuntimeError):
            economy_service.credit_user(lambda: self.conn, Mock(side_effect=RuntimeError('log failed')), 123, 100, 'redpacket')
        self.assertIsNone(self.conn.db.execute('SELECT * FROM users').fetchone())

    def test_inflation_query_releases_connection_before_beg_transaction(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.return_value = (0, None)
        active = False
        def connect():
            nonlocal active
            self.assertFalse(active)
            active = True
            return conn
        def close():
            nonlocal active
            active = False
        conn.close.side_effect = close
        def inflation():
            self.assertFalse(active, 'nested connection acquisition')
            return 1, 0, 0
        economy_repo.claim_beg_sync(Mock(), connect, datetime.datetime.now, inflation, Mock(), 1)

    def test_stale_lottery_snapshot_does_not_pay_closed_round_again(self):
        cursor = Mock()
        cursor.fetchall.return_value = [('2026-10-20', 1000, 2, None)]
        cursor.fetchone.return_value = (1000, 2, '123', datetime.datetime(2026, 10, 21))
        conn = Mock()
        conn.cursor.return_value = cursor
        with patch.object(lottery_repo, 'get_db_connection', create=True), \
             patch('bot_modules.db.get_db_connection', return_value=conn), \
             patch.object(lottery_repo, 'log_transaction_in_tx') as log:
            result = lottery_repo.finalize_due_rounds_sync(datetime.datetime(2026, 10, 22))
        self.assertEqual(result, [])
        log.assert_not_called()
        self.assertFalse(any('UPDATE users' in call.args[0] for call in cursor.execute.call_args_list))

    def test_failed_match_save_does_not_poison_busy_cache(self):
        with rr_match_repo._lock:
            rr_match_repo._user_active.clear()
        with patch('bot_modules.db.get_db_connection', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                rr_match_repo.save_match_sync(match_id='test', guild_id=1, channel_id=2,
                    message_id=3, mode='duel', phase='invite', payload={'participant_ids': [123]})
        self.assertIsNone(rr_match_repo.user_active_match_id(123))

    def test_roulette_payout_rollback_and_retry_are_atomic_and_idempotent(self):
        self.conn.db.executemany('INSERT INTO users (user_id,balance) VALUES (?,?)', [('1', 0), ('2', 0)])
        self.conn.db.execute("INSERT INTO russian_roulette_active_matches VALUES ('match','playing','{}')")
        self.conn.commit()
        args = dict(match_id='match', event_key='duel', credits={1: 200},
                    results={1: (True, 100), 2: (False, -100)}, reason='test',
                    payload={'participant_ids': [1, 2]}, finished=True)
        with patch('bot_modules.db.get_db_connection', return_value=self.conn):
            with patch.object(rr_match_repo, 'log_transaction_in_tx', side_effect=RuntimeError('logging failed')):
                with self.assertRaises(RuntimeError):
                    rr_match_repo.settle_event_sync(**args)
            self.assertEqual(self.conn.db.execute('SELECT balance FROM users WHERE user_id="1"').fetchone()[0], 0)
            self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM russian_roulette_settlements').fetchone()[0], 0)
            self.assertTrue(rr_match_repo.settle_event_sync(**args))
            self.assertFalse(rr_match_repo.settle_event_sync(**args))
        self.assertEqual(self.conn.db.execute('SELECT balance,total_games,wins,total_profit FROM users WHERE user_id="1"').fetchone(), (200, 1, 1, 100))
        self.assertEqual(self.conn.db.execute('SELECT games,wins,profit FROM russian_roulette_stats WHERE user_id="2"').fetchone(), (1, 0, -100))
        self.assertEqual(self.conn.db.execute('SELECT SUM(amount) FROM logs').fetchone()[0], 200)
        self.assertEqual(self.conn.db.execute('SELECT COUNT(*) FROM russian_roulette_active_matches').fetchone()[0], 0)

    def test_hourly_claim_caps_slots_and_cannot_pay_same_slots_twice(self):
        now = datetime.datetime(2026, 10, 22, 12, 0)
        self.conn.db.execute('INSERT INTO users (user_id,balance,level,last_hourly_claim) VALUES (?,?,?,?)',
            ('1', 0, 2, now - datetime.timedelta(hours=5)))
        self.conn.commit()
        args = (Mock(), lambda: self.conn, lambda: now, self.log, 100, 1)
        first = economy_repo.claim_hourly_reward(*args)
        second = economy_repo.claim_hourly_reward(*args)
        self.assertEqual(first['payout'], 2000)
        self.assertFalse(second['claimed'])
        self.assertEqual(self.conn.db.execute('SELECT balance FROM users').fetchone()[0], 2000)


if __name__ == '__main__':
    unittest.main()
