import typing
import random


def roll_gamble_exp_from_bet(gamble_exp_min: int, gamble_exp_max: int, main_bet: int) -> int:
    base = random.randint(int(gamble_exp_min), int(gamble_exp_max))
    bonus = min(max(int(main_bet), 0) // 2500, 25)
    return base + bonus


def exp_for_next_level(max_level: int, level):
    lv = max(1, min(int(max_level), int(level)))
    return 60 + lv * 25 + int((lv ** 1.6) * 8)


def calc_level_from_exp(max_level: int, exp):
    level = 1
    remaining = max(0, int(exp))
    while level < int(max_level):
        need = exp_for_next_level(max_level, level)
        if remaining < need:
            break
        remaining -= need
        level += 1
    return level, remaining, (0 if level >= int(max_level) else exp_for_next_level(max_level, level))


def exp_required_for_level(max_level: int, target_level: int) -> int:
    lv = max(1, min(int(max_level), int(target_level)))
    total = 0
    for cur in range(1, lv):
        total += exp_for_next_level(max_level, cur)
    return total


def get_level_stats(get_db_connection, user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT exp, level FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row


def add_user_exp(get_db_connection, calc_level_from_exp_func, user_id, amount):
    if amount <= 0:
        return None
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT exp, level FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    old_exp, old_level = int(row[0] or 0), int(row[1] or 1)
    new_exp = old_exp + int(amount)
    new_level, _, _ = calc_level_from_exp_func(new_exp)
    if new_level != old_level:
        c.execute("UPDATE users SET exp=%s, level=%s WHERE user_id=%s", (new_exp, new_level, str(user_id)))
    else:
        c.execute("UPDATE users SET exp=%s WHERE user_id=%s", (new_exp, str(user_id)))
    conn.commit()
    conn.close()
    return old_level, new_level, new_exp


def get_claimed_milestones(get_db_connection, user_id) -> set:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT milestone FROM level_milestone_claims WHERE user_id=%s", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return {int(r[0]) for r in rows} if rows else set()


def try_claim_milestone(get_db_connection, log_transaction_in_tx, user_id, milestone, coin_amount) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM level_milestone_claims WHERE user_id=%s AND milestone=%s", (str(user_id), milestone))
    if c.fetchone():
        conn.close()
        return -1
    c.execute("INSERT INTO level_milestone_claims (user_id, milestone) VALUES (%s, %s)", (str(user_id), milestone))
    added = 0
    if coin_amount and coin_amount > 0:
        c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (coin_amount, str(user_id)))
        log_transaction_in_tx(c, user_id, coin_amount, f"等級里程碑 Lv.{milestone}")
        added = coin_amount
    conn.commit()
    conn.close()
    return added


def build_exp_progress_bar(cur: int, need: int, width: int = 12) -> str:
    if need <= 0:
        return "▓" * width + " 100%"
    ratio = min(1.0, max(0.0, cur / need))
    filled = int(round(ratio * width))
    return "▓" * filled + "░" * (width - filled) + f"  {ratio * 100:.0f}%"


def update_game_result(
    get_db_connection,
    log_transaction_in_tx,
    apply_casino_recovery_share_func,
    share_enabled: bool,
    share_rate: float,
    share_target_id: str,
    user_id,
    balance_delta,
    profit_delta,
    is_win,
    is_push=False,
):
    conn = get_db_connection()
    c = conn.cursor()
    win_int = 1 if is_win else 0
    if is_push:
        c.execute(
            "UPDATE users SET balance=GREATEST(0, balance+%s), total_profit=total_profit+%s WHERE user_id=%s",
            (balance_delta, profit_delta, str(user_id)),
        )
    else:
        c.execute(
            "UPDATE users SET balance=GREATEST(0, balance+%s), total_profit=total_profit+%s, total_games=total_games+1, wins=wins+%s WHERE user_id=%s",
            (balance_delta, profit_delta, win_int, str(user_id)),
        )
    if balance_delta != 0:
        log_transaction_in_tx(c, user_id, balance_delta, "21點遊戲結算")
    conn.commit()
    conn.close()

    if (
        bool(share_enabled)
        and float(share_rate) > 0
        and profit_delta < 0
        and str(share_target_id or "").strip()
    ):
        recovered_amount = int(-profit_delta)
        apply_casino_recovery_share_func(recovered_amount, "21點回收切割")
