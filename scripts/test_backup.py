import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules.backup import restore_test_database


class RestoreGuardTests(unittest.TestCase):
    def test_remote_database_is_rejected_before_any_sql(self):
        conn = Mock(host="production.example")
        with self.assertRaises(RuntimeError):
            restore_test_database(conn, "unused")
        conn.cursor.assert_not_called()

    def test_production_name_is_rejected_even_on_localhost(self):
        conn = Mock(host="127.0.0.1")
        conn.cursor.return_value.fetchone.return_value = ("production",)
        with self.assertRaises(RuntimeError):
            restore_test_database(conn, "unused")
        calls = conn.cursor.return_value.execute.call_args_list
        self.assertEqual([x.args[0] for x in calls], ["SELECT DATABASE()"])
