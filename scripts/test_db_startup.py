"""Schema setup must not recycle pooled connections on expected DDL errors."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import db


class DatabaseStartupTests(unittest.TestCase):
    def test_existing_schema_errors_use_one_direct_connection(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.fetchone.return_value = (1,)
        def execute(sql, *args):
            if sql.startswith("ALTER TABLE"):
                raise db.pymysql.OperationalError(1060, "Duplicate column")
            if sql.startswith("CREATE INDEX") or sql.startswith("CREATE UNIQUE INDEX"):
                raise db.pymysql.OperationalError(1061, "Duplicate key")
        cursor.execute.side_effect = execute
        with patch.object(db, "_get_mysql_pool", side_effect=AssertionError("DDL must not use pool")), \
                patch.object(db.pymysql, "connect", return_value=conn) as connect:
            db.init_db()
        connect.assert_called_once()
        ddl = [call.args[0] for call in cursor.execute.call_args_list
               if call.args[0].startswith("ALTER TABLE")]
        self.assertTrue(ddl)
        self.assertEqual(len(ddl), len(set(ddl)))
        self.assertFalse(any("milestone" in call.args[0].lower() for call in cursor.execute.call_args_list))
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    def test_socket_timeouts_apply_to_both_configuration_formats(self):
        for url in ("", "mysql://test:test@localhost/test"):
            with self.subTest(url=bool(url)), patch.dict(os.environ, {
                "MYSQL_URL": url, "MYSQL_READ_TIMEOUT": "17", "MYSQL_WRITE_TIMEOUT": "19"
            }, clear=True):
                kw = db._mysql_connect_kwargs()
                self.assertEqual(kw["read_timeout"], 17)
                self.assertEqual(kw["write_timeout"], 19)


if __name__ == "__main__":
    unittest.main()
