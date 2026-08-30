import datetime
import random
import typing

from bot_modules import config
from bot_modules.db import get_db_connection
from bot_modules.social_repo import lottery_day_key
from bot_modules.tx_ops import log_transaction_in_tx


def init_lottery_tables(cursor) -> None:
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS lottery_rounds (
            week_key VARCHAR(16) PRIMARY KEY,
            pool_amount BIGINT DEFAULT 0,
            ticket_count INT DEFAULT 0,
            winner_id VARCHAR(255) NULL,
            drawn_at TIMESTAMP NULL
        )"""
    )
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS lottery_entries (
            id INT AUTO_INCREMENT PRIMARY KEY,
            week_key VARCHAR(16) NOT NULL,
            user_id VARCHAR(255) NOT NULL,
            tickets INT NOT NULL DEFAULT 1,
            amount_paid BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_lottery_week_user (week_key, user_id)
        )"""
    )
    try:
        cursor.execute("CREATE INDEX idx_lottery_entries_week ON lottery_entries (week_key)")
    except Exception:
        pass


def _day_sort_key(day_key: str) -> typing.Tuple[int, int, int]:
    try:
        dt = datetime.datetime.strptime(str(day_key), "%Y-%m-%d")
        return dt.year, dt.month, dt.day
    except Exception:
        return 0, 0, 0


def finalize_due_rounds_sync(now: typing.Optional[datetime.datetime] = None) -> typing.List[typing.Dict[str, typing.Any]]:
    now = now or config.now_tw_naive()
    current_key = lottery_day_key(now)
    current_sort = _day_sort_key(current_key)
    results: typing.List[typing.Dict[str, typing.Any]] = []

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT week_key, pool_amount, ticket_count, winner_id FROM lottery_rounds "
        "WHERE drawn_at IS NULL ORDER BY week_key ASC"
    )
    pending = c.fetchall() or []
    for round_key, pool_amount, ticket_count, winner_id in pending:
        if winner_id:
            continue
        key_str = str(round_key)
        if "-" in key_str and "W" in key_str:
            continue
        if _day_sort_key(key_str) >= current_sort:
            continue
        tickets_total = int(ticket_count or 0)
        pool = int(pool_amount or 0)
        winner_uid = None
        if tickets_total > 0 and pool > 0:
            c.execute(
                "SELECT user_id, tickets FROM lottery_entries WHERE week_key=%s AND tickets > 0",
                (key_str,),
            )
            entries = c.fetchall() or []
            weighted: typing.List[str] = []
            for uid, tix in entries:
                weighted.extend([str(uid)] * max(1, int(tix or 0)))
            if weighted:
                winner_uid = random.choice(weighted)
                c.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (winner_uid,))
                if c.fetchone():
                    c.execute(
                        "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                        (pool, winner_uid),
                    )
                    log_transaction_in_tx(c, winner_uid, pool, f"日彩池開獎（{key_str}）")
        c.execute(
            "UPDATE lottery_rounds SET winner_id=%s, drawn_at=%s WHERE week_key=%s",
            (winner_uid, now, key_str),
        )
        conn.commit()
        results.append(
            {
                "day_key": key_str,
                "pool": pool,
                "tickets": tickets_total,
                "winner_id": winner_uid,
            }
        )
    conn.close()
    return results


def buy_lottery_tickets_sync(
    user_id: int,
    tickets: int,
    ticket_cost: int,
    max_tickets_per_buy: int,
) -> typing.Dict[str, typing.Any]:
    tickets = max(1, min(int(max_tickets_per_buy), int(tickets)))
    cost = tickets * int(ticket_cost)
    if cost <= 0:
        return {"ok": False, "reason": "invalid_cost"}

    now = config.now_tw_naive()
    finalize_due_rounds_sync(now)
    day_key = lottery_day_key(now)
    uid = str(user_id)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=%s FOR UPDATE", (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "reason": "not_found"}
    balance = int(row[0] or 0)
    if balance < cost:
        conn.close()
        return {"ok": False, "reason": "insufficient", "balance": balance, "cost": cost}

    c.execute(
        "INSERT INTO lottery_rounds (week_key, pool_amount, ticket_count) VALUES (%s, 0, 0) "
        "ON DUPLICATE KEY UPDATE week_key=week_key",
        (day_key,),
    )
    c.execute(
        "SELECT drawn_at FROM lottery_rounds WHERE week_key=%s FOR UPDATE",
        (day_key,),
    )
    round_row = c.fetchone()
    if round_row and round_row[0]:
        conn.close()
        return {"ok": False, "reason": "round_closed", "day_key": day_key}

    c.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (cost, uid))
    log_transaction_in_tx(c, uid, -cost, f"日彩池購票（{day_key} x{tickets}）")
    c.execute(
        "INSERT INTO lottery_entries (week_key, user_id, tickets, amount_paid) VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE tickets=tickets+%s, amount_paid=amount_paid+%s",
        (day_key, uid, tickets, cost, tickets, cost),
    )
    c.execute(
        "UPDATE lottery_rounds SET pool_amount=pool_amount+%s, ticket_count=ticket_count+%s WHERE week_key=%s",
        (cost, tickets, day_key),
    )
    c.execute("SELECT balance FROM users WHERE user_id=%s", (uid,))
    new_bal = int((c.fetchone() or [0])[0] or 0)
    c.execute("SELECT tickets FROM lottery_entries WHERE week_key=%s AND user_id=%s", (day_key, uid))
    my_tickets = int((c.fetchone() or [0])[0] or 0)
    c.execute("SELECT pool_amount, ticket_count FROM lottery_rounds WHERE week_key=%s", (day_key,))
    pool_row = c.fetchone() or (0, 0)
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "day_key": day_key,
        "tickets_bought": tickets,
        "my_tickets": my_tickets,
        "pool": int(pool_row[0] or 0),
        "total_tickets": int(pool_row[1] or 0),
        "cost": cost,
        "balance": new_bal,
    }


def fetch_lottery_status_sync(user_id: int) -> typing.Dict[str, typing.Any]:
    now = config.now_tw_naive()
    finalize_due_rounds_sync(now)
    day_key = lottery_day_key(now)
    uid = str(user_id)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT pool_amount, ticket_count, winner_id, drawn_at FROM lottery_rounds WHERE week_key=%s",
        (day_key,),
    )
    round_row = c.fetchone()
    pool, total_tickets, winner_id, drawn_at = (0, 0, None, None)
    if round_row:
        pool = int(round_row[0] or 0)
        total_tickets = int(round_row[1] or 0)
        winner_id = round_row[2]
        drawn_at = round_row[3]
    c.execute("SELECT tickets, amount_paid FROM lottery_entries WHERE week_key=%s AND user_id=%s", (day_key, uid))
    entry = c.fetchone()
    my_tickets = int((entry[0] if entry else 0) or 0)
    my_paid = int((entry[1] if entry else 0) or 0)
    conn.close()

    tomorrow = now.date() + datetime.timedelta(days=1)
    draw_dt = datetime.datetime.combine(tomorrow, datetime.time.min)

    return {
        "day_key": day_key,
        "pool": pool,
        "total_tickets": total_tickets,
        "my_tickets": my_tickets,
        "my_paid": my_paid,
        "winner_id": winner_id,
        "drawn_at": drawn_at,
        "draw_dt": draw_dt,
        "closed": bool(drawn_at),
    }
