import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_local


class LocalPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "requirements.txt").write_text("discord.py==2.3.2\n", encoding="utf-8")
        self.patchers = [patch.object(run_local, "ROOT", self.root),
                         patch.object(run_local.sys, "version_info", (3, 11, 17)),
                         patch.object(run_local.importlib.metadata, "version", return_value="2.3.2"),
                         patch.dict(os.environ, {}, clear=True)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def env(self, host="localhost", token="not-a-real-token"):
        (self.root / ".env").write_text(f"DISCORD_TOKEN={token}\nMYSQLHOST={host}\nMYSQLPORT=3306\nMYSQLUSER=test\nMYSQLPASSWORD=not-a-real-password\nMYSQLDATABASE=test\n", encoding="utf-8")

    def test_missing_secret_blocks_start_without_printing_credentials(self):
        self.env(token="")
        with self.assertRaises(ValueError) as caught:
            run_local.preflight()
        self.assertIn("DISCORD_TOKEN", str(caught.exception))
        self.assertNotIn("not-a-real-password", str(caught.exception))

    def test_internal_railway_host_is_rejected_on_local_machine(self):
        self.env(host="mysql.railway.internal")
        with self.assertRaises(ValueError):
            run_local.preflight()

    def test_wrong_dependency_version_is_rejected(self):
        self.env()
        with patch.object(run_local.importlib.metadata, "version", return_value="2.7.1"), self.assertRaises(ValueError):
            run_local.preflight()

    def test_check_mode_never_connects_to_database(self):
        self.env()
        with patch.object(run_local.sys, "argv", ["run_local.py", "--check"]), \
                patch("bot_modules.db.get_db_connection", side_effect=AssertionError("must not connect")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_local.main(), 0)


if __name__ == "__main__":
    unittest.main()
