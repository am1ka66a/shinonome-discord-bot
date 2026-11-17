"""Audited maintenance operations; never reset an account with unsettled games."""
import contextlib
import json

from bot_modules.db import db_cursor
from bot_modules.tx_ops import log_transaction_in_tx


def casino_open(c):
    # Admissions and resets use the same row, so closing the casino is a DB barrier.
    c.execute("SELECT value_json FROM bot_settings WHERE setting_key='casino_open' FOR UPDATE")
    row = c.fetchone()
    return True if row is None else bool(json.loads(row[0]))


@contextlib.contextmanager
def active_game_action(bot):
    bot._active_game_actions = getattr(bot, "_active_game_actions", 0) + 1
    try:
        yield
    finally:
        bot._active_game_actions -= 1


def require_idle(bot):
    if getattr(bot, "_active_mutating_commands", {}) or getattr(bot, "_active_game_actions", 0):
        raise ValueError("仍有指令或遊戲按鈕正在處理，請稍後再確認。")


def preview_reset():
    with db_cursor() as c:
        if casino_open(c):
            raise ValueError("請先使用 /lock 關閉賭場，再執行重置。")
        c.execute("SELECT COUNT(*), COALESCE(SUM(balance),0) FROM users")
        count, balance = c.fetchone()
        return int(count), int(balance)


def reset_balances(request_id, actor_id, target_balance, reset_stats=False):
    if target_balance not in (0, 50000):
        raise ValueError("不支援的重置金額")
    with db_cursor(commit=True) as c:
        opened = casino_open(c)
        c.execute("SELECT result_json FROM bot_admin_operations WHERE request_id=%s FOR UPDATE", (str(request_id),))
        prior = c.fetchone()
        if prior:
            return json.loads(prior[0])
        if opened:
            raise ValueError("賭場已重新開啟，取消重置。")
        c.execute("SELECT session_id FROM bot_game_sessions WHERE phase IN ('active','settling') LIMIT 1 FOR UPDATE")
        if c.fetchone():
            raise ValueError("仍有遊戲／紅包或待結算款項，請完成或等待退款後再重置。")
        c.execute("SELECT match_id FROM russian_roulette_active_matches LIMIT 1 FOR UPDATE")
        if c.fetchone():
            raise ValueError("仍有輪盤遊戲，請先完成該場遊戲。")
        c.execute("SELECT user_id, COALESCE(balance,0), rescue_count, total_games, wins, total_profit FROM users ORDER BY user_id FOR UPDATE")
        rows = c.fetchall()
        delta_total = 0
        for uid, before, rescue, games, wins, profit in rows:
            snapshot = dict(balance=int(before), rescue_count=rescue, total_games=games, wins=wins, total_profit=profit)
            c.execute("INSERT INTO bot_admin_snapshots (request_id,user_id,snapshot_json) VALUES (%s,%s,%s)",
                      (str(request_id), str(uid), json.dumps(snapshot)))
            delta = target_balance - int(before)
            delta_total += delta
            if reset_stats:
                c.execute("UPDATE users SET balance=%s,rescue_count=0,total_games=0,wins=0,total_profit=0 WHERE user_id=%s",
                          (target_balance, str(uid)))
            else:
                c.execute("UPDATE users SET balance=%s WHERE user_id=%s", (target_balance, str(uid)))
            log_transaction_in_tx(c, uid, delta, f"管理員重置｜actor={actor_id}｜op={request_id}")
        result = dict(accounts=len(rows), delta=delta_total, target_balance=target_balance)
        c.execute("INSERT INTO bot_admin_operations (request_id,actor_id,action,result_json) VALUES (%s,%s,%s,%s)",
                  (str(request_id), str(actor_id), "reset_default" if reset_stats else "reset_zero", json.dumps(result)))
        return result
