import datetime
import random
import typing


def try_deduct_balance(get_db_connection, get_locked_user_balance, log_transaction_in_tx, user_id, amount, reason):
    if amount <= 0:
        return True
    conn = get_db_connection()
    c = conn.cursor()
    bal = get_locked_user_balance(c, user_id)
    if bal < amount:
        conn.close()
        return False
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s",
        (amount, str(user_id)),
    )
    log_transaction_in_tx(c, user_id, -amount, reason)
    conn.commit()
    conn.close()
    return True


def credit_balance_with_log(get_db_connection, log_transaction_in_tx, user_id, amount, reason):
    if amount <= 0:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
    c.fetchone()
    c.execute(
        "UPDATE users SET balance=balance+%s WHERE user_id=%s",
        (amount, str(user_id)),
    )
    log_transaction_in_tx(c, user_id, amount, reason)
    conn.commit()
    conn.close()


def claim_beg_sync(
    ensure_user_exists,
    get_db_connection,
    now_tw_naive_func,
    get_inflation_multiplier_func,
    log_transaction_in_tx,
    user_id: int,
) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(user_id, 50000)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, last_beg FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
    row = c.fetchone()
    now = now_tw_naive_func()
    if row and row[1] and (now - row[1]).total_seconds() < 120:
        conn.close()
        remain = 120 - int((now - row[1]).total_seconds())
        return {"ok": False, "reason": "cooldown", "remain_sec": max(1, remain)}
    inflation_mult, _, _ = get_inflation_multiplier_func()
    base_earn = random.randint(100, 600)
    earn = max(50, int(base_earn * inflation_mult))
    fail = random.random() < 0.3
    if fail:
        c.execute("UPDATE users SET last_beg=%s WHERE user_id=%s", (now, str(user_id)))
        conn.commit()
        conn.close()
        return {"ok": True, "earned": 0, "fail": True}
    c.execute("UPDATE users SET balance=balance+%s, last_beg=%s WHERE user_id=%s", (earn, now, str(user_id)))
    log_transaction_in_tx(c, user_id, earn, "乞討所得")
    conn.commit()
    conn.close()
    return {"ok": True, "earned": earn, "fail": False}


def claim_rescue_sync(
    ensure_user_exists,
    get_db_connection,
    now_tw_naive_func,
    get_inflation_multiplier_func,
    log_transaction_in_tx,
    user_id: int,
) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(user_id, 50000)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, last_rescue, rescue_count FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "reason": "not_found"}
    balance = int(row[0] or 0)
    if balance > 0:
        conn.close()
        return {"ok": False, "reason": "not_bankrupt", "balance": balance}
    rescue_count = int(row[2] or 0)
    if rescue_count >= 10:
        conn.close()
        return {"ok": False, "reason": "limit_reached"}
    now = now_tw_naive_func()
    if row[1] and (now - row[1]).total_seconds() < 3600:
        rem = 3600 - (now - row[1]).total_seconds()
        conn.close()
        return {"ok": False, "reason": "cooldown", "remain_sec": max(1, int(rem))}
    inflation_mult, _, _ = get_inflation_multiplier_func()
    rescue_reward = max(500, min(50000, int(1000 * inflation_mult)))
    c.execute(
        "UPDATE users SET balance=balance+%s, last_rescue=%s, rescue_count=rescue_count+1 WHERE user_id=%s",
        (rescue_reward, now, str(user_id)),
    )
    log_transaction_in_tx(c, user_id, rescue_reward, "賭狗破產救濟")
    conn.commit()
    conn.close()
    return {"ok": True, "reward": rescue_reward, "claim_no": rescue_count + 1}


def refresh_hourly_bank(get_db_connection, now_tw_naive_func, max_level: int, user_id):
    now = now_tw_naive_func()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT level, last_hourly_claim, hourly_bank FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    level = max(1, min(max_level, int(row[0] or 1)))
    last_claim = row[1]
    bank = int(row[2] or 0)
    if last_claim is None:
        c.execute("UPDATE users SET last_hourly_claim=%s WHERE user_id=%s", (now, str(user_id)))
        conn.commit()
        conn.close()
        return {"level": level, "bank": bank, "next_in_seconds": 3600}

    elapsed_hours = int((now - last_claim).total_seconds() // 3600)
    if elapsed_hours > 0:
        bank = min(level, bank + elapsed_hours)
        last_claim = last_claim + datetime.timedelta(hours=elapsed_hours)
        c.execute("UPDATE users SET hourly_bank=%s, last_hourly_claim=%s WHERE user_id=%s", (bank, last_claim, str(user_id)))
        conn.commit()
    next_in_seconds = max(0, 3600 - int((now - last_claim).total_seconds()))
    conn.close()
    return {"level": level, "bank": bank, "next_in_seconds": next_in_seconds}


def payout_hourly_bank(get_db_connection, log_transaction_in_tx, user_id, bank, reward_per_slot):
    if bank <= 0:
        return 0
    payout = int(bank * reward_per_slot)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance=balance+%s, hourly_bank=0 WHERE user_id=%s", (payout, str(user_id)))
    log_transaction_in_tx(c, user_id, payout, "每小時簽到")
    conn.commit()
    conn.close()
    return payout


def claim_daily_reward(
    ensure_user_exists,
    now_tw_naive_func,
    tw_tz,
    get_db_connection,
    log_transaction_in_tx,
    user_id,
    daily_reward: int = 100_000,
):
    ensure_user_exists(user_id, 50000)
    today_tw = now_tw_naive_func().date()
    tomorrow_tw = today_tw + datetime.timedelta(days=1)
    next_claim_dt = datetime.datetime.combine(tomorrow_tw, datetime.time.min, tzinfo=tw_tz)
    next_ts = int(next_claim_dt.timestamp())

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
    c.fetchone()
    c.execute("SELECT last_claim FROM daily_claims WHERE user_id=%s FOR UPDATE", (str(user_id),))
    row = c.fetchone()
    if row and row[0] == today_tw:
        conn.close()
        return {"claimed": False, "next_ts": next_ts}

    c.execute(
        "INSERT INTO daily_claims (user_id, last_claim) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_claim=%s",
        (str(user_id), today_tw, today_tw),
    )
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (daily_reward, str(user_id)))
    log_transaction_in_tx(c, user_id, daily_reward, "每日簽到")
    conn.commit()
    c.execute("SELECT balance FROM users WHERE user_id=%s", (str(user_id),))
    bal_row = c.fetchone()
    conn.close()
    new_balance = int(((bal_row[0] if bal_row else 0) or 0))
    return {
        "claimed": True,
        "reward": daily_reward,
        "balance": new_balance,
        "next_ts": next_ts,
    }
