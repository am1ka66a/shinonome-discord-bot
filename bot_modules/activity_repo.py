import datetime
import typing

from bot_modules.db import db_cursor

# 各函式保留 get_db_connection 參數以維持既有呼叫端簽名，連線改由 db_cursor 管理。


def purge_old_logs_sync(get_db_connection, retention_days: int) -> int:
    """刪除 logs 超過 retention_days 的資料，回傳刪除筆數。"""
    if retention_days <= 0:
        return 0
    with db_cursor(commit=True, connect=get_db_connection) as c:
        c.execute(
            "DELETE FROM logs WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (retention_days,),
        )
        removed = int(c.rowcount or 0)
    return removed


def award_vc_rewards_sync(
    get_db_connection,
    log_transaction_in_tx,
    user_ids: typing.Sequence[str],
    now: datetime.datetime,
) -> int:
    """依輸入 user_ids 發放語音獎勵（每 30 分鐘一次），回傳本輪發放人數。"""
    if not user_ids:
        return 0
    awarded: typing.List[str] = []
    with db_cursor(commit=True, connect=get_db_connection) as c:
        for user_id in user_ids:
            c.execute("SELECT last_vc_reward FROM activity_stats WHERE user_id=%s", (user_id,))
            row = c.fetchone()
            if row and row[0] is not None and (now - row[0]).total_seconds() < 1800:
                continue
            c.execute(
                "INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500",
                (user_id,),
            )
            c.execute(
                "INSERT INTO activity_stats (user_id, last_vc_reward) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_vc_reward=%s",
                (user_id, now, now),
            )
            log_transaction_in_tx(c, user_id, 500, "語音通話獎勵 (10min)")
            awarded.append(user_id)
    return len(awarded)


def process_on_message_activity_sync(
    get_db_connection,
    log_transaction_in_tx,
    ensure_user_exists,
    add_user_exp,
    user_id: str,
    pending_count: int,
    now: datetime.datetime,
    exp_due: bool,
    exp_gain: int,
) -> typing.Dict[str, typing.Any]:
    """處理聊天訊息累積、EXP 發放與聊天活躍獎勵。"""
    exp_awarded = False
    old_level = None
    new_level = None
    msg_rewarded = False
    # 這兩個呼叫各自會取得連線；若在本函式持有連線時才呼叫，併發訊息會每人佔住兩條，
    # 連線池（上限 16）可能被佔滿而互相等待，所以先在無連線狀態下處理完 EXP。
    if exp_due:
        ensure_user_exists(user_id, 50000)
        exp_result = add_user_exp(user_id, exp_gain)
        if exp_result:
            old_level, new_level = int(exp_result[0] or 1), int(exp_result[1] or 1)
        exp_awarded = True
    with db_cursor(commit=True, connect=get_db_connection) as c:
        c.execute(
            "INSERT INTO activity_stats (user_id, msg_count) VALUES (%s, %s) ON DUPLICATE KEY UPDATE msg_count=msg_count+%s",
            (user_id, pending_count, pending_count),
        )
        c.execute("SELECT msg_count, last_msg_reward FROM activity_stats WHERE user_id=%s", (user_id,))
        row = c.fetchone()

        if exp_due:
            c.execute("UPDATE activity_stats SET last_exp_reward=%s WHERE user_id=%s", (now, user_id))

        if row and int(row[0] or 0) >= 10:
            if row[1] is None or (now - row[1]).total_seconds() >= 1800:
                c.execute(
                    "INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500",
                    (user_id,),
                )
                c.execute(
                    "UPDATE activity_stats SET msg_count=0, last_msg_reward=%s WHERE user_id=%s",
                    (now, user_id),
                )
                log_transaction_in_tx(c, user_id, 500, "聊天活躍獎勵 (10句)")
                msg_rewarded = True
    return {
        "exp_awarded": exp_awarded,
        "old_level": old_level,
        "new_level": new_level,
        "msg_rewarded": msg_rewarded,
    }
