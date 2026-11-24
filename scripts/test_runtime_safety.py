import asyncio
import logging
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules.discord_logging import DiscordLogHandler
from bot_modules.runtime.instance_lease import InstanceLease, LeaseBusyError, LEASE_IDLE_TIMEOUT_SECONDS
from bot_modules.bot_core import create_shinonome_bot
import discord
from discord.ext import commands


class LeaseTests(unittest.TestCase):
    def test_lock_is_held_by_one_session_without_reconnect(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.side_effect = [(1,), (42,), (42,), (None,)]
        lease = InstanceLease(connect=Mock(return_value=conn))
        lease.acquire()
        conn.cursor.return_value.execute.assert_any_call("SET SESSION wait_timeout=%s", (LEASE_IDLE_TIMEOUT_SECONDS,))
        lease.verify()
        conn.ping.assert_called_once_with(reconnect=False)
        with self.assertRaises(RuntimeError):
            lease.verify()
        lease.close()
        conn.close.assert_called_once()

    def test_second_instance_cannot_start_and_closes_its_connection(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.return_value = (0,)
        lease = InstanceLease(connect=Mock(return_value=conn))
        with self.assertRaisesRegex(RuntimeError, "拒絕重複"):
            lease.acquire()
        conn.close.assert_called_once()
        self.assertIsNone(lease.connection)

    def test_timeout_cannot_reconnect_or_reacquire_the_session_lock(self):
        import pymysql
        conn = Mock()
        connect = Mock(return_value=conn)
        conn.cursor.return_value.fetchone.side_effect = [(1,), (42,)]
        lease = InstanceLease(connect=connect)
        lease.acquire()
        conn.ping.side_effect = pymysql.OperationalError(2013, 'timed out')
        with self.assertRaises(pymysql.OperationalError):
            lease.verify()
        conn.ping.assert_called_once_with(reconnect=False)
        connect.assert_called_once()
        lease.close()


class LeaseStartupRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_outage_then_busy_lock_recovers_using_fresh_connections(self):
        import pymysql
        busy, healthy = Mock(), Mock()
        busy.cursor.return_value.fetchone.return_value = (0,)
        healthy.cursor.return_value.fetchone.side_effect = [(1,), (42,)]
        connect = Mock(side_effect=[pymysql.OperationalError(2003, 'connection timed out'), busy, healthy])
        lease, logger = InstanceLease(connect=connect), Mock()
        self.addCleanup(lease.close)
        with patch('bot_modules.runtime.instance_lease.asyncio.sleep', new=AsyncMock()) as sleep:
            await lease.acquire_with_retry(logger)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [5, 10])
        self.assertEqual(connect.call_count, 3)
        busy.close.assert_called_once()
        healthy.close.assert_not_called()
        self.assertIs(lease.connection, healthy)
        logger.warning.assert_called_once()
        logger.info.assert_called_once()

    async def test_long_lock_wait_exceeds_platform_retry_budget_without_crashing(self):
        lease, logger = InstanceLease(), Mock()
        with patch.object(lease, 'acquire', side_effect=[LeaseBusyError('busy')]*15+[None]) as acquire, \
                patch('bot_modules.runtime.instance_lease.asyncio.sleep', new=AsyncMock()) as sleep:
            await lease.acquire_with_retry(logger)
        self.assertEqual(acquire.call_count, 16)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [5,10,20]+[30]*12)
        logger.warning.assert_called_once()

    async def test_authentication_and_unknown_lock_result_are_not_retried(self):
        import pymysql
        for error in (pymysql.OperationalError(1045, 'access denied'), RuntimeError('unknown lock result')):
            with self.subTest(error=type(error).__name__):
                lease = InstanceLease()
                with patch.object(lease, 'acquire', side_effect=error) as acquire, \
                        patch('bot_modules.runtime.instance_lease.asyncio.sleep', new=AsyncMock()) as sleep:
                    with self.assertRaises(type(error)):
                        await lease.acquire_with_retry(Mock())
                acquire.assert_called_once()
                sleep.assert_not_awaited()

    async def test_cancelling_an_inflight_acquire_cleans_up_a_late_lock(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        conn = Mock()
        conn.cursor.return_value.fetchone.side_effect = [(1,), (42,)]
        def delayed_connect(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test did not release worker')
            return conn
        lease = InstanceLease(connect=delayed_connect)
        task = asyncio.create_task(lease.acquire_with_retry(Mock()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        conn.close.assert_called_once()
        self.assertIsNone(lease.connection)

    async def test_cancel_while_sleeping_stops_retries_immediately(self):
        lease, sleeping = InstanceLease(), asyncio.Event()
        async def wait(_):
            sleeping.set()
            await asyncio.Future()
        with patch.object(lease, 'acquire', side_effect=LeaseBusyError('healthy owner')) as acquire, \
                patch('bot_modules.runtime.instance_lease.asyncio.sleep', new=wait):
            task = asyncio.create_task(lease.acquire_with_retry(Mock()))
            await asyncio.wait_for(sleeping.wait(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        acquire.assert_called_once()
        self.assertIsNone(lease.connection)


def offline_bot():
    return create_shinonome_bot(logger=Mock(), allowed_host_ids=[], dm_relay_channel_id=0,
        chunk_text_lines=lambda lines: lines, discord_message_cap=2000, intents=discord.Intents.none())


class LeaseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_failure_clears_readiness_and_closes_before_start_raises(self):
        import pymysql
        bot = offline_bot()
        bot._db_ready_event = asyncio.Event()
        bot._db_ready_event.set()
        failure = pymysql.OperationalError(2013, 'timed out')
        bot.instance_lease = Mock(verify=Mock(side_effect=failure))
        self.addAsyncCleanup(bot.close)

        async def connected(client, token, *, reconnect):
            client.lease_task = asyncio.create_task(client.watch_instance_lease())
            await client.lease_task

        with patch.object(commands.Bot, 'start', connected), patch('bot_modules.bot_core.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, '自動重啟') as caught:
                await bot.start('offline-test-token')
        self.assertIs(caught.exception.__cause__, failure)
        self.assertTrue(bot.is_closed())
        self.assertFalse(bot._db_ready_event.is_set())
        bot.instance_lease.close.assert_called_once()

    async def test_lease_is_released_even_when_discord_cleanup_fails(self):
        bot = offline_bot()
        bot.instance_lease = Mock()
        with patch.object(commands.Bot, 'close', new=AsyncMock(side_effect=RuntimeError('cleanup failed'))):
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                await bot.close()
        bot.instance_lease.close.assert_called_once()
        await bot.close()


class LeaseProcessExitTests(unittest.TestCase):
    def test_real_run_exits_nonzero_for_watchdog_failure_but_zero_for_normal_stop(self):
        # Exercise discord.py's actual run()/async context cleanup in a child
        # process. No login, network connection or production token is involved.
        script = textwrap.dedent('''
            import asyncio, sys
            from unittest.mock import AsyncMock, Mock, patch
            import discord, pymysql
            from discord.ext import commands
            from bot_modules.bot_core import create_shinonome_bot
            mode = sys.argv[1]
            bot = create_shinonome_bot(logger=Mock(), allowed_host_ids=[], dm_relay_channel_id=0,
                chunk_text_lines=lambda x:x, discord_message_cap=2000, intents=discord.Intents.none())
            async def connected(client, token, *, reconnect):
                if mode == 'normal':
                    await client.close()
                    return
                error = pymysql.OperationalError(2013, 'timed out') if mode == 'timeout' else RuntimeError('lock owned elsewhere')
                client.instance_lease = Mock(verify=Mock(side_effect=error))
                client._db_ready_event = asyncio.Event()
                client._db_ready_event.set()
                with patch('bot_modules.bot_core.asyncio.sleep', new=AsyncMock()):
                    client.lease_task = asyncio.create_task(client.watch_instance_lease())
                    await client.lease_task
                assert not client._db_ready_event.is_set()
                assert client.is_closed()
            with patch.object(commands.Bot, 'start', connected), patch('pymysql.connect', side_effect=AssertionError('no database')):
                bot.run('offline-test-token', log_handler=None)
        ''')
        for mode in ('timeout', 'ownership', 'normal'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', script, mode],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                    encoding='utf-8', env=dict(os.environ, PYTHONUTF8='1'), timeout=20)
                if mode == 'normal':
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn('機器人執行權檢查失敗，需要自動重啟', result.stderr)


class DiscordLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicates_are_batched_and_queue_is_bounded(self):
        client = SimpleNamespace(loop=asyncio.get_running_loop())
        handler = DiscordLogHandler(client, 123, capacity=3, interval=0.01)
        handler._send = AsyncMock()
        self.addCleanup(handler.close)
        repeated = logging.LogRecord("bot", logging.ERROR, "", 0, "database failed", (), None)
        for _ in range(1000):
            handler.emit(repeated)
        for i in range(10):
            handler.emit(logging.LogRecord("bot", logging.WARNING, "", 0, "error %s", (i,), None))
        self.assertLessEqual(len(handler.pending), 3)
        for _ in range(50):
            if handler._send.await_count:
                break
            await asyncio.sleep(0.01)
        text = handler._send.await_args.args[0]
        self.assertIn("1000", text)
        self.assertIn("database failed", text)
        self.assertIn("容量限制", text)
        self.assertLessEqual(len(text), 1900)
        self.assertEqual(handler._send.await_count, 1)
