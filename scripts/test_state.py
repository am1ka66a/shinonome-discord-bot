"""Exercise real escrow SQL and rollback without Discord or production data."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot_modules import state_repo
from bot_modules.db import db_cursor
from test_regressions import SqliteConnection


class StateTests(unittest.TestCase):
    def setUp(self):
        self.conn = SqliteConnection()
        self.conn.db.executescript("""
            CREATE TABLE bot_game_sessions (session_id TEXT PRIMARY KEY, kind TEXT, owner_id TEXT,
                phase TEXT, escrow TEXT, payload TEXT, settlement TEXT, expires_at INTEGER);
            CREATE TABLE bot_game_events (session_id TEXT, event_id TEXT, PRIMARY KEY(session_id,event_id));
            CREATE TABLE bot_settings (setting_key TEXT PRIMARY KEY, value_json TEXT);
            CREATE TABLE bot_message_audit (request_id TEXT PRIMARY KEY,actor_id TEXT,channel_id TEXT,
                reply_id TEXT,message_id TEXT,status TEXT);
            INSERT INTO users (user_id,balance) VALUES ('1',1000),('2',1000);
        """)
        # The adapter preserves production commit/rollback behavior.
        self.conn.fetchall = lambda: self.conn.cur.fetchall()
        self.patch = patch.object(state_repo, "db_cursor", lambda **kw: db_cursor(connect=lambda: self.conn, **kw))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.conn.db.close)
        self.store = state_repo.GameStore()

    def balance(self, uid=1):
        return self.conn.db.execute("SELECT balance FROM users WHERE user_id=?", (str(uid),)).fetchone()[0]

    def phase(self, sid="bj:1"):
        return self.conn.db.execute("SELECT phase FROM bot_game_sessions WHERE session_id=?", (sid,)).fetchone()[0]

    def test_duplicate_open_and_additional_debit_are_once(self):
        for _ in range(2):
            self.assertTrue(self.store.debit("bj:1", "bj", 1, 100, "open"))
            self.assertTrue(self.store.debit("bj:1", "bj", 1, 100, "double"))
        self.assertEqual(self.balance(), 800)

    def test_insufficient_balance_rolls_back_session_and_event(self):
        self.assertFalse(self.store.debit("bj:1", "bj", 1, 2000, "open"))
        self.assertEqual(self.balance(), 1000)
        self.assertEqual(self.conn.db.execute("SELECT COUNT(*) FROM bot_game_sessions").fetchone()[0], 0)

    def test_failed_debit_log_rolls_back_money_and_save(self):
        with patch.object(state_repo, "log_transaction_in_tx", side_effect=RuntimeError("offline log failure")):
            with self.assertRaises(RuntimeError):
                self.store.debit("bj:1", "bj", 1, 100, "open")
        self.assertEqual(self.balance(), 1000)
        self.assertEqual(self.conn.db.execute("SELECT COUNT(*) FROM bot_game_events").fetchone()[0], 0)

    def test_failed_payout_then_retry_is_atomic_and_idempotent(self):
        self.store.debit("bj:1", "bj", 1, 100, "open")
        self.store.prepare("bj:1", {"1": 200}, {"1": (True, 100, False)})
        with patch.object(state_repo, "log_transaction_in_tx", side_effect=RuntimeError("offline failure")):
            with self.assertRaises(RuntimeError):
                self.store.apply("bj:1")
        self.assertEqual(self.balance(), 900)
        self.assertEqual(self.phase(), "settling")
        self.store.apply("bj:1")
        self.store.apply("bj:1")
        self.assertEqual(self.balance(), 1100)
        self.assertEqual(self.conn.db.execute("SELECT total_games,wins,total_profit FROM users WHERE user_id='1'").fetchone(), (1, 1, 100))

    def test_recovery_keeps_saved_result_instead_of_refunding(self):
        self.store.debit("bj:1", "bj", 1, 100, "open")
        self.store.prepare("bj:1", {"1": 200})
        state_repo.GameStore().recover(startup=True)
        self.assertEqual(self.balance(), 1100)
        self.assertEqual(self.phase(), "settled")

    def test_player_and_share_same_account_keep_separate_logs(self):
        self.store.debit("bj:1", "bj", 1, 100, "open")
        self.store.prepare("bj:1", {"1": 75}, reason={"1": [[50, "遊戲結算"], [25, "回收分潤"]]})
        self.store.apply("bj:1")
        self.store.apply("bj:1")
        rows = self.conn.db.execute("SELECT amount,reason FROM logs WHERE amount>0 ORDER BY id").fetchall()
        self.assertEqual(rows, [(50, "遊戲結算"), (25, "回收分潤")])
        self.assertEqual(self.balance(), 975)

    def test_restart_refunds_unfinished_game_once(self):
        self.store.debit("bj:1", "bj", 1, 100, "open")
        restarted = state_repo.GameStore()
        restarted.recover(startup=True)
        restarted.recover(startup=True)
        self.assertEqual(self.balance(), 1000)

    def test_live_games_are_not_refunded_by_regular_scan(self):
        self.store.debit("bj:1", "bj", 1, 100, "open")
        self.store.recover()
        self.assertEqual(self.balance(), 900)
        self.assertEqual(self.phase(), "active")

    def test_expired_game_refund_prevents_further_debits(self):
        self.store.debit("bj:1", "bj", 1, 100, "open")
        self.conn.db.execute("UPDATE bot_game_sessions SET expires_at=0")
        self.conn.commit()
        self.store.recover()
        self.assertFalse(self.store.debit("bj:1", "bj", 1, 100, "double"))
        self.assertEqual(self.balance(), 1000)

    def test_duel_refund_is_atomic_for_both_players(self):
        self.store.debit("duel:1", "duel", 1, 100, "a")
        self.store.debit("duel:1", "duel", 2, 100, "b")
        self.store.prepare("duel:1", {}, refund=True)
        real_log = state_repo.log_transaction_in_tx
        def fail_second(c, uid, amount, reason):
            if str(uid) == "2":
                raise RuntimeError("second refund failed")
            real_log(c, uid, amount, reason)
        with patch.object(state_repo, "log_transaction_in_tx", side_effect=fail_second), self.assertRaises(RuntimeError):
            self.store.apply("duel:1")
        self.assertEqual((self.balance(1), self.balance(2)), (900, 900))
        self.store.apply("duel:1")
        self.assertEqual((self.balance(1), self.balance(2)), (1000, 1000))

    def test_packet_claim_once_and_restart_refunds_only_remaining(self):
        self.store.debit("packet:1", "redpacket", 1, 100, "open", {"creator_id": 1, "count": 2, "claims": {}})
        amount, _ = self.store.claim("packet:1", 2)
        self.assertEqual(self.store.claim("packet:1", 2)[0], amount)
        state_repo.GameStore().recover(startup=True)
        self.assertEqual(self.balance(2), 1000 + amount)
        self.assertEqual(self.balance(1), 1000 - amount)
        with self.assertRaises(ValueError):
            self.store.claim("packet:1", 3)

    def test_failed_claim_can_retry_without_losing_a_share(self):
        self.store.debit("packet:1", "redpacket", 1, 100, "open", {"creator_id": 1, "count": 1, "claims": {}})
        with patch.object(state_repo, "log_transaction_in_tx", side_effect=RuntimeError("claim failed")), self.assertRaises(RuntimeError):
            self.store.claim("packet:1", 2)
        self.assertEqual(self.balance(2), 1000)
        self.assertEqual(self.store.claim("packet:1", 2)[0], 100)

    def test_settings_survive_new_runtime(self):
        state_repo.save_setting("feature:bj", False)
        state_repo.save_setting("casino_open", False)
        self.assertEqual(state_repo.load_settings(), {"feature:bj": False, "casino_open": False})
        state_repo.save_setting("feature:bj", True)
        self.assertTrue(state_repo.load_settings()["feature:bj"])

    def test_one_failed_recovery_does_not_block_other_payouts(self):
        for sid, uid in (("bj:1", 1), ("bj:2", 2)):
            self.store.debit(sid, "bj", uid, 100, "open")
            self.store.prepare(sid, {str(uid): 200})
        real_apply = self.store.apply
        def fail_one(sid):
            if sid == "bj:1":
                raise RuntimeError("temporary failure")
            return real_apply(sid)
        with patch.object(self.store, "apply", side_effect=fail_one), self.assertLogs("bot_modules.state_repo", level="ERROR"):
            self.assertEqual(self.store.recover(), 1)
        self.assertEqual((self.balance(1), self.balance(2)), (900, 1100))
        self.store.recover()
        self.assertEqual(self.balance(1), 1100)

    def test_message_audit_prevents_duplicate_request_and_records_message_id(self):
        state_repo.audit_message("say:1", 1, 2, 3, "sending")
        with self.assertRaises(Exception):
            state_repo.audit_message("say:1", 1, 2, 3, "sending")
        state_repo.audit_message("say:1", 1, 2, 3, "sent", 4)
        self.assertEqual(self.conn.db.execute("SELECT status,message_id FROM bot_message_audit").fetchone(), ("sent", "4"))


if __name__ == "__main__":
    unittest.main()
