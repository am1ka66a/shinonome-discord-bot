"""Durable configuration and game escrow. Never retry an unkeyed payment."""
import asyncio
import json
import logging
import random
import time
import uuid

from bot_modules.db import db_cursor
from bot_modules.tx_ops import lock_user_rows, log_transaction_in_tx


def init_state_tables(c):
    c.execute("""CREATE TABLE IF NOT EXISTS bot_settings (
        setting_key VARCHAR(64) PRIMARY KEY, value_json TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_game_sessions (
        session_id VARCHAR(96) PRIMARY KEY, kind VARCHAR(24) NOT NULL,
        owner_id VARCHAR(36) NOT NULL, phase VARCHAR(16) NOT NULL,
        escrow TEXT NOT NULL, payload TEXT NOT NULL, settlement TEXT NULL,
        expires_at BIGINT NOT NULL, INDEX recovery_phase (phase, expires_at))""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_game_events (
        session_id VARCHAR(96) NOT NULL, event_id VARCHAR(96) NOT NULL,
        PRIMARY KEY (session_id, event_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_message_audit (
        request_id VARCHAR(96) PRIMARY KEY, actor_id VARCHAR(255) NOT NULL,
        channel_id VARCHAR(255) NOT NULL, reply_id VARCHAR(255) NULL,
        message_id VARCHAR(255) NULL, status VARCHAR(24) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")


def load_settings():
    with db_cursor() as c:
        c.execute("SELECT setting_key, value_json FROM bot_settings")
        return {key: json.loads(value) for key, value in c.fetchall()}


def save_setting(key, value):
    with db_cursor(commit=True) as c:
        c.execute("SELECT setting_key FROM bot_settings WHERE setting_key=%s FOR UPDATE", (key,))
        if c.fetchone():
            c.execute("UPDATE bot_settings SET value_json=%s WHERE setting_key=%s", (json.dumps(value), key))
        else:
            c.execute("INSERT INTO bot_settings (setting_key,value_json) VALUES (%s,%s)", (key, json.dumps(value)))


class InsufficientFunds(ValueError):
    pass


class GameStore:
    def __init__(self):
        self.owner_id = str(uuid.uuid4())

    async def call(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self, method), *args, **kwargs)

    def _read(self, c, sid):
        c.execute("SELECT phase, escrow, payload, settlement, expires_at FROM bot_game_sessions WHERE session_id=%s FOR UPDATE", (sid,))
        row = c.fetchone()
        if row is None:
            raise ValueError("找不到遊戲存檔")
        return row[0], json.loads(row[1]), json.loads(row[2]), json.loads(row[3]) if row[3] else None, int(row[4])

    def debit(self, sid, kind, uid, amount, event, payload=None, seconds=600, reason=None):
        amount, uid = int(amount), str(uid)
        if amount <= 0:
            raise ValueError("注金必須大於零")
        try:
            with db_cursor(commit=True) as c:
                c.execute("INSERT IGNORE INTO bot_game_sessions (session_id,kind,owner_id,phase,escrow,payload,expires_at) VALUES (%s,%s,%s,'active','{}','{}',%s)",
                          (sid, kind, self.owner_id, int(time.time()) + seconds))
                phase, escrow, data, _, expires = self._read(c, sid)
                c.execute("SELECT 1 FROM bot_game_events WHERE session_id=%s AND event_id=%s", (sid, event))
                if c.fetchone():
                    return phase == "active" and expires > time.time()
                if phase != "active" or expires <= time.time():
                    return False
                lock_user_rows(c, [uid])
                c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s", (uid,))
                balance = c.fetchone()
                if balance is None or int(balance[0]) < amount:
                    raise InsufficientFunds()
                c.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (amount, uid))
                log_transaction_in_tx(c, uid, -amount, reason or f"{kind} 注金｜{sid}")
                escrow[uid] = escrow.get(uid, 0) + amount
                data.update(payload or {})
                c.execute("UPDATE bot_game_sessions SET escrow=%s,payload=%s,expires_at=%s WHERE session_id=%s",
                          (json.dumps(escrow), json.dumps(data), int(time.time()) + seconds, sid))
                c.execute("INSERT INTO bot_game_events (session_id,event_id) VALUES (%s,%s)", (sid, event))
            return True
        except InsufficientFunds:
            return False

    def checkpoint(self, sid, payload, seconds=600):
        with db_cursor(commit=True) as c:
            phase, _, _, _, expires = self._read(c, sid)
            if phase != "active" or expires <= time.time():
                raise ValueError("這場遊戲已關閉，請重新開局")
            c.execute("UPDATE bot_game_sessions SET payload=%s,expires_at=%s WHERE session_id=%s",
                      (json.dumps(payload), int(time.time()) + seconds, sid))

    def prepare(self, sid, credits, results=None, reason="遊戲結算", refund=False):
        with db_cursor(commit=True) as c:
            phase, escrow, _, intent, _ = self._read(c, sid)
            if phase == "active":
                intent = {"credits": escrow if refund else {str(k): int(v) for k, v in credits.items()},
                          "results": results or {}, "reason": reason}
                if any(v < 0 for v in intent["credits"].values()):
                    raise ValueError("入帳金額不得為負")
                c.execute("UPDATE bot_game_sessions SET phase='settling',settlement=%s WHERE session_id=%s", (json.dumps(intent), sid))

    def apply(self, sid):
        with db_cursor(commit=True) as c:
            phase, _, _, intent, _ = self._read(c, sid)
            if phase == "settled":
                return intent
            if phase != "settling" or intent is None:
                raise ValueError("結算尚未準備完成")
            ids = sorted(set(intent["credits"]) | set(intent["results"]))
            # Claimants/share accounts may not have played before. Create them
            # before deterministic row locking, in the same order for every payer.
            for uid in ids:
                c.execute("INSERT IGNORE INTO users (user_id,balance) VALUES (%s,0)", (uid,))
            lock_user_rows(c, ids)
            for uid in ids:
                amount = int(intent["credits"].get(uid, 0))
                if amount:
                    c.execute("UPDATE users SET balance=COALESCE(balance,0)+%s WHERE user_id=%s", (amount, uid))
                    reason = intent["reason"].get(uid, "遊戲結算") if isinstance(intent["reason"], dict) else intent["reason"]
                    if isinstance(reason, list):
                        if sum(int(part[0]) for part in reason) != amount:
                            raise ValueError("結算與流水金額不一致")
                        for delta, label in reason:
                            log_transaction_in_tx(c, uid, int(delta), label)
                    else:
                        log_transaction_in_tx(c, uid, amount, reason)
                result = intent["results"].get(uid)
                if result:
                    win, profit, push = result
                    c.execute("UPDATE users SET total_profit=total_profit+%s,total_games=total_games+%s,wins=wins+%s WHERE user_id=%s",
                              (profit, int(not push), int(win and not push), uid))
            c.execute("UPDATE bot_game_sessions SET phase='settled',escrow='{}' WHERE session_id=%s", (sid,))
            return intent

    async def finish(self, sid, credits, results=None, reason="遊戲結算", refund=False):
        await self.call("prepare", sid, credits, results, reason, refund)
        return await self.call("apply", sid)

    def claim(self, sid, uid):
        uid = str(uid)
        with db_cursor(commit=True) as c:
            phase, escrow, data, _, expires = self._read(c, sid)
            claimed = data.setdefault("claims", {})
            if uid in claimed:
                return claimed[uid], data
            if phase != "active" or expires <= time.time():
                raise ValueError("紅包已關閉")
            left = sum(escrow.values())
            count = int(data["count"]) - len(claimed)
            if count <= 0 or left <= 0:
                raise ValueError("紅包已搶完")
            amount = left if count == 1 else random.randint(1, max(1, min(left - count + 1, int(left * .4))))
            c.execute("INSERT IGNORE INTO users (user_id,balance) VALUES (%s,0)", (uid,))
            lock_user_rows(c, [uid])
            c.execute("UPDATE users SET balance=COALESCE(balance,0)+%s WHERE user_id=%s", (amount, uid))
            log_transaction_in_tx(c, uid, amount, f"搶紅包｜{sid}")
            creator = str(data["creator_id"])
            escrow[creator] -= amount
            claimed[uid] = amount
            c.execute("UPDATE bot_game_sessions SET escrow=%s,payload=%s WHERE session_id=%s", (json.dumps(escrow), json.dumps(data), sid))
            return amount, data

    def recover(self, startup=False):
        with db_cursor() as c:
            if startup:
                c.execute("SELECT session_id FROM bot_game_sessions WHERE phase='settling' OR (phase='active' AND (expires_at<=%s OR owner_id<>%s))",
                          (int(time.time()), self.owner_id))
            else:
                c.execute("SELECT session_id FROM bot_game_sessions WHERE phase='settling' OR (phase='active' AND expires_at<=%s)", (int(time.time()),))
            ids = [row[0] for row in c.fetchall()]
        recovered = 0
        for sid in ids:
            try:
                recovered += int(self.recover_one(sid, startup))
            except Exception:
                logging.getLogger(__name__).exception("遊戲存檔等待下次重試 session=%s", sid)
        return recovered

    def recover_one(self, sid, startup):
        with db_cursor(commit=True) as c:
            phase, escrow, _, _, expires = self._read(c, sid)
            c.execute("SELECT owner_id FROM bot_game_sessions WHERE session_id=%s", (sid,))
            owner = c.fetchone()[0]
            # Recheck under the row lock: a live callback may have extended
            # its deadline after the initial scan.
            if phase == "active":
                if expires > time.time() and not (startup and owner != self.owner_id):
                    return False
                intent = {"credits": escrow, "results": {}, "reason": "中斷／逾時遊戲退回剩餘注金"}
                c.execute("UPDATE bot_game_sessions SET phase='settling',settlement=%s WHERE session_id=%s", (json.dumps(intent), sid))
            elif phase != "settling":
                return False
        self.apply(sid)
        return True


def audit_message(request_id, actor_id, channel_id, reply_id, status, message_id=None):
    with db_cursor(commit=True) as c:
        if status == "sending":
            c.execute("INSERT INTO bot_message_audit (request_id,actor_id,channel_id,reply_id,status) VALUES (%s,%s,%s,%s,%s)",
                      (request_id, str(actor_id), str(channel_id), str(reply_id) if reply_id else None, status))
        else:
            c.execute("UPDATE bot_message_audit SET status=%s,message_id=%s WHERE request_id=%s", (status, str(message_id) if message_id else None, request_id))
