import asyncio
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules.discord_logging import DiscordLogHandler
from bot_modules.runtime.instance_lease import InstanceLease


class LeaseTests(unittest.TestCase):
    def test_lock_is_held_by_one_session_without_reconnect(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.side_effect = [(1,), (42,), (42,), (None,)]
        lease = InstanceLease(connect=Mock(return_value=conn))
        lease.acquire()
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
