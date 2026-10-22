"""Recent successful traffic replaces redundant MySQL health-check packets."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import db


class MySQLHealthcheckTests(unittest.TestCase):
    def connection(self, verified=100, opened=True):
        conn = object.__new__(db._RecentMySQLConnection)
        conn._sock = object() if opened else None
        conn._last_verified_query = verified
        self.addCleanup(setattr, conn, "_sock", None)
        return conn

    def test_recent_success_skips_ping_but_idle_connection_is_checked(self):
        conn = self.connection()
        with patch.object(db.time, "monotonic", return_value=105), \
                patch.object(db.pymysql.connections.Connection, "ping") as ping:
            conn.ping()
            ping.assert_not_called()
        with patch.object(db.time, "monotonic", return_value=130), \
                patch.object(db.pymysql.connections.Connection, "ping") as ping:
            conn.ping(reconnect=False)
            ping.assert_called_once_with(reconnect=False)

    def test_query_failure_never_counts_as_healthy(self):
        conn = self.connection()
        with patch.object(db.pymysql.connections.Connection, "query",
                          side_effect=db.pymysql.OperationalError(2013, "lost connection")):
            with self.assertRaises(db.pymysql.OperationalError):
                conn.query("SELECT 1")
        self.assertIsNone(conn._last_verified_query)
        with patch.object(db.pymysql.connections.Connection, "ping") as ping:
            conn.ping()
            ping.assert_called_once()

    def test_successful_query_refreshes_health_and_closed_socket_is_checked(self):
        conn = self.connection(opened=False)
        with patch.object(db.time, "monotonic", return_value=110), \
                patch.object(db.pymysql.connections.Connection, "query", return_value=1):
            self.assertEqual(conn.query("SELECT 1"), 1)
        self.assertEqual(conn._last_verified_query, 110)
        with patch.object(db.time, "monotonic", return_value=111), \
                patch.object(db.pymysql.connections.Connection, "ping") as ping:
            conn.ping()
            ping.assert_called_once()


if __name__ == "__main__":
    unittest.main()
