import datetime
import typing

from bot_modules import activity_repo
from bot_modules import config
from bot_modules import economy_repo
from bot_modules import economy_service
from bot_modules import game_repo
from bot_modules import lottery_repo
from bot_modules import milestone_guild_repo
from bot_modules import rr_repo
from bot_modules import rob_repo
from bot_modules import social_repo
from bot_modules import wanted_repo
from bot_modules.db import get_db_connection
from bot_modules.tx_ops import get_locked_user_balance, lock_user_rows, log_transaction_in_tx
from bot_modules.user_repo import ensure_user_exists, fetch_casino_share_stats_rows, get_user_stats

MAX_LEVEL = config.MAX_LEVEL
TW_TZ = config.TW_TZ
GAMBLE_EXP_MIN = config.GAMBLE_EXP_MIN
GAMBLE_EXP_MAX = config.GAMBLE_EXP_MAX
COUNTER_ROB_BASE_SUCCESS_RATE = config.COUNTER_ROB_BASE_SUCCESS_RATE
ROB_STEAL_CAP = config.ROB_STEAL_CAP
ROB_FAIL_PENALTY_CAP = config.ROB_FAIL_PENALTY_CAP
CASINO_RECOVERY_SHARE_ENABLED = config.CASINO_RECOVERY_SHARE_ENABLED
CASINO_RECOVERY_SHARE_TARGET_ID = config.CASINO_RECOVERY_SHARE_TARGET_ID
CASINO_RECOVERY_SHARE_RATE = config.CASINO_RECOVERY_SHARE_RATE
CASINO_RECOVERY_SHARE_REASON_PREFIX = config.CASINO_RECOVERY_SHARE_REASON_PREFIX

BAIL_COST = 100_000
WANTED_BUYOUT_COST = 300_000
WANTED_BUYOUT_COOLDOWN_SECONDS = 86400
COP_HUNT_CAPTURE_BASE_PCT = 35
COP_HUNT_CAPTURE_PER_STAR_PCT = 5
ROLE_CHANGE_COOLDOWN_SECONDS = 86400
GOOD_CITIZEN_CERT_COST = 50_000_000
GOOD_CITIZEN_CERT_COOLDOWN_SECONDS = 86400
GOOD_CITIZEN_DESTROY_COST = 500_000_000
GOOD_CITIZEN_BROKEN_LOCK_DAYS = 10


def now_tw_naive() -> datetime.datetime:
    return config.now_tw_naive()


def apply_casino_recovery_share(recovered_amount: int, source: str) -> int:
    """
    將「實際回收金額」按比例切給分潤帳號，回傳實際分潤額。
    recovered_amount 需為正整數；本函式不更動玩家結算邏輯。
    """
    if not CASINO_RECOVERY_SHARE_ENABLED:
        return 0
    if not CASINO_RECOVERY_SHARE_TARGET_ID:
        return 0
    if CASINO_RECOVERY_SHARE_RATE <= 0:
        return 0
    recovered_amount = int(recovered_amount or 0)
    if recovered_amount <= 0:
        return 0
    share_amount = int(recovered_amount * CASINO_RECOVERY_SHARE_RATE)
    if share_amount <= 0:
        return 0
    credit_balance_with_log(
        CASINO_RECOVERY_SHARE_TARGET_ID,
        share_amount,
        f"{CASINO_RECOVERY_SHARE_REASON_PREFIX}｜{source}",
    )
    return share_amount

def _user_role_value(raw) -> str:
    """將 users.role 轉成與程式一致的鍵（cop/criminal/civilian），避免大小寫／空白／bytes 造成比對失敗。"""
    if raw is None:
        return "civilian"
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    s = str(raw).strip().lower()
    return s if s else "civilian"


# DB 交易規範（重要）：
# 1) 涉及金流/冷卻判斷時，先 lock_user_rows(...) 或 SELECT ... FOR UPDATE，再做判斷與更新。
# 2) 同一筆交易中的資金變化，優先使用 log_transaction_in_tx(...) 同交易寫流水，避免資金與流水分離。
# 3) 多人互動（轉帳、追捕、反搶等）一律固定順序上鎖，避免死鎖。


def get_inflation_multiplier():
    """依全服流通量計算通膨倍率，回傳 (multiplier, circulation, avg_balance)。"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(balance), 0), COUNT(*) FROM users")
    row = c.fetchone()
    conn.close()
    circulation = int((row[0] if row else 0) or 0)
    user_count = int((row[1] if row else 0) or 0)
    avg_balance = circulation / user_count if user_count > 0 else 50000.0
    # 以全服流通量作為主軸（不是單人平均）
    base_circulation = 5_000_000.0
    multiplier = circulation / base_circulation
    # 低風險範圍：避免獎勵暴衝或過低
    multiplier = max(0.5, min(5.0, multiplier))
    return multiplier, circulation, avg_balance


# 假釋金（出獄一次）
BAIL_COST = 100_000
# 搶匪付費消除通緝（全部星數與本輪追捕狀態）
WANTED_BUYOUT_COST = 300_000
WANTED_BUYOUT_COOLDOWN_SECONDS = 86400  # 24 小時內不可再次買斷
# 警察每次 /cop_hunt 追捕前須支付（成敗皆扣）
COP_HUNT_FEE = 300_000
# 追捕成功率：clamp(5~95, 基底 + 通緝星 × 每星加成 + 等級差)；1★ 基準 = 40%
COP_HUNT_CAPTURE_BASE_PCT = 35
COP_HUNT_CAPTURE_PER_STAR_PCT = 5
# 陣營轉職冷卻（24 小時）
ROLE_CHANGE_COOLDOWN_SECONDS = 86400
GOOD_CITIZEN_CERT_COST = 50_000_000
GOOD_CITIZEN_CERT_COOLDOWN_SECONDS = 86400
GOOD_CITIZEN_DESTROY_COST = 500_000_000
GOOD_CITIZEN_BROKEN_LOCK_DAYS = 10


def append_rob_history_on_cursor(c, user_id: int, steal_amount: int) -> None:
    return rob_repo.append_rob_history_on_cursor(c, user_id, steal_amount, now_tw_naive)


def get_last_five_robs_total(user_id: typing.Union[int, str]) -> typing.Tuple[int, int, typing.List[typing.Any]]:
    return rob_repo.get_last_five_robs_total(get_db_connection, user_id)


def rob_history_total_from_raw(raw: typing.Any) -> typing.Tuple[int, int]:
    return rob_repo.rob_history_total_from_raw(raw)


def clear_rob_history(user_id: typing.Union[int, str]) -> None:
    return rob_repo.clear_rob_history(get_db_connection, user_id)


def load_rob_context(robber_id: int, target_id: int) -> typing.Dict[str, typing.Any]:
    return rob_repo.load_rob_context(
        get_db_connection,
        lock_user_rows,
        _user_role_value,
        robber_id,
        target_id,
    )


def apply_rob_success_db(
    robber_id: int,
    target_id: int,
    now: datetime.datetime,
    success_rate_pct: int,
) -> typing.Dict[str, typing.Any]:
    return rob_repo.apply_rob_success_db(
        get_db_connection,
        lock_user_rows,
        now_tw_naive,
        _user_role_value,
        log_transaction_in_tx,
        COUNTER_ROB_BASE_SUCCESS_RATE,
        BAIL_COST,
        ROB_STEAL_CAP,
        COP_HUNT_CAPTURE_BASE_PCT,
        COP_HUNT_CAPTURE_PER_STAR_PCT,
        robber_id,
        target_id,
        now,
        success_rate_pct,
    )


def apply_rob_fail_db(
    robber_id: int,
    target_id: int,
    now: datetime.datetime,
) -> typing.Dict[str, typing.Any]:
    return rob_repo.apply_rob_fail_db(
        get_db_connection,
        lock_user_rows,
        ROB_FAIL_PENALTY_CAP,
        robber_id,
        target_id,
        now,
    )


def claim_beg_sync(user_id: int) -> typing.Dict[str, typing.Any]:
    return economy_repo.claim_beg_sync(
        ensure_user_exists,
        get_db_connection,
        now_tw_naive,
        get_inflation_multiplier,
        log_transaction_in_tx,
        user_id,
    )


def choose_role_sync(user_id: int, role: str, now: datetime.datetime) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(user_id, 50000)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(role,'civilian'), COALESCE(wanted_stars,0), last_role_change,
                  COALESCE(good_citizen_cert_active,0)
           FROM users WHERE user_id=%s FOR UPDATE""",
        (str(user_id),),
    )
    row = c.fetchone()
    old_role = (row[0] or "civilian") if row else "civilian"
    wanted_now = int(row[1] or 0) if row else 0
    last_role_change = row[2] if row else None
    cert_active = int(row[3] or 0) if row else 0
    if role != old_role and cert_active:
        conn.close()
        return {"ok": False, "reason": "cert_active"}
    if role != old_role and last_role_change is not None:
        elapsed = (now - last_role_change).total_seconds()
        if elapsed < ROLE_CHANGE_COOLDOWN_SECONDS:
            conn.close()
            next_dt = last_role_change + datetime.timedelta(seconds=ROLE_CHANGE_COOLDOWN_SECONDS)
            return {"ok": False, "reason": "cooldown", "next_dt": next_dt}
    if role == "civilian" and old_role == "civilian":
        conn.close()
        return {"ok": False, "reason": "already_civilian"}
    if old_role == "criminal" and wanted_now > 0 and role in ("cop", "civilian"):
        conn.close()
        return {"ok": False, "reason": "wanted_block", "wanted_now": wanted_now}
    if role in ("cop", "civilian"):
        c.execute(
            "UPDATE users SET role=%s, wanted_stars=0, wanted_hunted_count=0, last_five_robs=NULL, last_role_change=%s WHERE user_id=%s",
            (role, now, str(user_id)),
        )
    else:
        c.execute("UPDATE users SET role=%s, last_role_change=%s WHERE user_id=%s", (role, now, str(user_id)))
    conn.commit()
    conn.close()
    return {"ok": True, "old_role": old_role}


def toggle_good_citizen_sync(user_id: int, now: datetime.datetime) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(user_id, 50000)
    uid = str(user_id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(role,'civilian'), COALESCE(balance,0),
                  COALESCE(good_citizen_cert_active,0), last_good_citizen_cert_action,
                  good_citizen_cert_broken_until
           FROM users WHERE user_id=%s FOR UPDATE""",
        (uid,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "reason": "not_found"}
    role_raw, bal_raw, cert_active_raw, last_action, broken_until = row
    role_now = _user_role_value(role_raw)
    bal = int(bal_raw or 0)
    cert_active = int(cert_active_raw or 0)
    if role_now != "civilian":
        conn.close()
        return {"ok": False, "reason": "not_civilian"}
    if cert_active == 0 and broken_until is not None and now < broken_until:
        conn.close()
        return {"ok": False, "reason": "broken_lock", "until": broken_until}
    if last_action is not None:
        elapsed = (now - last_action).total_seconds()
        if elapsed < GOOD_CITIZEN_CERT_COOLDOWN_SECONDS:
            conn.close()
            next_dt = last_action + datetime.timedelta(seconds=GOOD_CITIZEN_CERT_COOLDOWN_SECONDS)
            return {"ok": False, "reason": "cooldown", "next_dt": next_dt}
    if bal < GOOD_CITIZEN_CERT_COST:
        conn.close()
        return {"ok": False, "reason": "insufficient", "balance": bal}
    next_active = 0 if cert_active else 1
    reason = "啟用良民證（防搶）" if next_active else "解除良民證（取消防搶）"
    c.execute(
        """UPDATE users
           SET balance=balance-%s,
               good_citizen_cert_active=%s,
               last_good_citizen_cert_action=%s
           WHERE user_id=%s AND balance >= %s""",
        (GOOD_CITIZEN_CERT_COST, next_active, now, uid, GOOD_CITIZEN_CERT_COST),
    )
    if c.rowcount == 0:
        conn.close()
        return {"ok": False, "reason": "deduct_failed"}
    log_transaction_in_tx(c, user_id, -GOOD_CITIZEN_CERT_COST, reason)
    conn.commit()
    conn.close()
    return {"ok": True, "next_active": next_active, "new_balance": bal - GOOD_CITIZEN_CERT_COST}


def fetch_good_citizen_rows_sync() -> typing.List[typing.Tuple[typing.Any, typing.Any, typing.Any]]:
    return wanted_repo.fetch_good_citizen_rows_sync(get_db_connection)


def fetch_wanted_status_row_sync(user_id: int):
    return wanted_repo.fetch_wanted_status_row_sync(
        ensure_user_exists,
        get_db_connection,
        user_id,
    )


def fetch_wanted_list_rows_sync():
    return wanted_repo.fetch_wanted_list_rows_sync(get_db_connection)


def pay_bail_sync(user_id: int, now: datetime.datetime) -> typing.Dict[str, typing.Any]:
    return wanted_repo.pay_bail_sync(
        ensure_user_exists,
        get_db_connection,
        log_transaction_in_tx,
        BAIL_COST,
        user_id,
        now,
    )


def claim_rescue_sync(user_id: int) -> typing.Dict[str, typing.Any]:
    return economy_repo.claim_rescue_sync(
        ensure_user_exists,
        get_db_connection,
        now_tw_naive,
        get_inflation_multiplier,
        log_transaction_in_tx,
        user_id,
    )


def wanted_buyout_sync(user_id: int, now: datetime.datetime) -> typing.Dict[str, typing.Any]:
    return wanted_repo.wanted_buyout_sync(
        ensure_user_exists,
        get_db_connection,
        _user_role_value,
        log_transaction_in_tx,
        WANTED_BUYOUT_COST,
        WANTED_BUYOUT_COOLDOWN_SECONDS,
        user_id,
        now,
    )


def break_citizen_sync(attacker_id: int, target_id: int, now: datetime.datetime) -> typing.Dict[str, typing.Any]:
    return wanted_repo.break_citizen_sync(
        ensure_user_exists,
        get_db_connection,
        lock_user_rows,
        get_locked_user_balance,
        log_transaction_in_tx,
        GOOD_CITIZEN_DESTROY_COST,
        GOOD_CITIZEN_BROKEN_LOCK_DAYS,
        attacker_id,
        target_id,
        now,
    )


def transfer_sync(sender_id: int, receiver_id: int, amount: int, note_text: str) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(sender_id, 50000)
    ensure_user_exists(receiver_id, 0)
    if note_text:
        out_reason = f"轉帳給 {receiver_id}（備註: {note_text}）"
        in_reason = f"收到 {sender_id} 的轉帳（備註: {note_text}）"
    else:
        out_reason = f"轉帳給 {receiver_id}"
        in_reason = f"收到 {sender_id} 的轉帳"
    return economy_service.transfer_users(
        get_db_connection,
        lock_user_rows,
        get_locked_user_balance,
        log_transaction_in_tx,
        sender_id,
        receiver_id,
        amount,
        out_reason,
        in_reason,
    )


def try_deduct_balance(user_id, amount, reason):
    return economy_service.debit_user(
        get_db_connection,
        get_locked_user_balance,
        log_transaction_in_tx,
        user_id,
        amount,
        reason,
    )


def credit_balance_with_log(user_id, amount, reason):
    return economy_service.credit_user(
        get_db_connection,
        log_transaction_in_tx,
        user_id,
        amount,
        reason,
    )


def settle_duel_payouts_with_log(challenger_id, opponent_id, a_amt, b_amt, s_a, s_b):
    """E 卡結算：先入帳，再寫各自分配紀錄。"""
    conn = get_db_connection()
    cur = conn.cursor()
    lock_user_rows(cur, [challenger_id, opponent_id])
    if a_amt > 0:
        cur.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (a_amt, str(challenger_id)),
        )
    if b_amt > 0:
        cur.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (b_amt, str(opponent_id)),
        )
    if a_amt > 0:
        log_transaction_in_tx(cur, challenger_id, a_amt, f"E卡決鬥分配（積分 {s_a}:{s_b}）")
    if b_amt > 0:
        log_transaction_in_tx(cur, opponent_id, b_amt, f"E卡決鬥分配（積分 {s_a}:{s_b}）")
    conn.commit()
    conn.close()

def roll_gamble_exp_from_bet(main_bet: int) -> int:
    return game_repo.roll_gamble_exp_from_bet(GAMBLE_EXP_MIN, GAMBLE_EXP_MAX, main_bet)


def update_game_result(user_id, balance_delta, profit_delta, is_win, is_push=False):
    return game_repo.update_game_result(
        get_db_connection,
        log_transaction_in_tx,
        apply_casino_recovery_share,
        CASINO_RECOVERY_SHARE_ENABLED,
        CASINO_RECOVERY_SHARE_RATE,
        CASINO_RECOVERY_SHARE_TARGET_ID,
        user_id,
        balance_delta,
        profit_delta,
        is_win,
        is_push,
    )

def exp_for_next_level(level):
    return game_repo.exp_for_next_level(MAX_LEVEL, level)

def calc_level_from_exp(exp):
    return game_repo.calc_level_from_exp(MAX_LEVEL, exp)

def exp_required_for_level(target_level: int) -> int:
    return game_repo.exp_required_for_level(MAX_LEVEL, target_level)

def get_level_stats(user_id):
    return game_repo.get_level_stats(get_db_connection, user_id)

def add_user_exp(user_id, amount):
    return game_repo.add_user_exp(get_db_connection, calc_level_from_exp, user_id, amount)

def get_claimed_milestones(user_id) -> set:
    return game_repo.get_claimed_milestones(get_db_connection, user_id)

def try_claim_milestone(user_id, milestone, coin_amount) -> int:
    return game_repo.try_claim_milestone(get_db_connection, log_transaction_in_tx, user_id, milestone, coin_amount)

def build_exp_progress_bar(cur: int, need: int, width: int = 12) -> str:
    return game_repo.build_exp_progress_bar(cur, need, width)


def refresh_hourly_bank(user_id):
    return economy_repo.refresh_hourly_bank(
        get_db_connection,
        now_tw_naive,
        MAX_LEVEL,
        user_id,
    )

def payout_hourly_bank(user_id, bank, reward_per_slot):
    return economy_repo.payout_hourly_bank(
        get_db_connection,
        log_transaction_in_tx,
        user_id,
        bank,
        reward_per_slot,
    )


def claim_daily_reward(user_id, daily_reward: int = 100_000):
    return economy_repo.claim_daily_reward(
        ensure_user_exists,
        now_tw_naive,
        TW_TZ,
        get_db_connection,
        log_transaction_in_tx,
        user_id,
        daily_reward,
    )


def claim_hourly_reward(user_id, reward_per_slot: int = 1000):
    ensure_user_exists(user_id, 50000)
    bank_info = refresh_hourly_bank(user_id)
    if not bank_info:
        return {"ok": False}

    level_num = int(bank_info["level"])
    bank = int(bank_info["bank"])
    if bank <= 0:
        sec = int(bank_info["next_in_seconds"])
        mins = max(1, int(sec // 60))
        return {"ok": True, "claimed": False, "level": level_num, "mins": mins}

    payout = payout_hourly_bank(user_id, bank, reward_per_slot)
    stats = get_user_stats(user_id)
    return {
        "ok": True,
        "claimed": True,
        "level": level_num,
        "bank": bank,
        "reward_per_slot": reward_per_slot,
        "payout": int(payout),
        "balance": int((stats[0] if stats else 0) or 0),
    }


def purge_old_logs_sync(retention_days: int) -> int:
    return activity_repo.purge_old_logs_sync(get_db_connection, retention_days)


def award_vc_rewards_sync(user_ids: typing.Sequence[str], now: datetime.datetime) -> int:
    return activity_repo.award_vc_rewards_sync(
        get_db_connection,
        log_transaction_in_tx,
        user_ids,
        now,
    )


def process_on_message_activity_sync(
    user_id: str,
    pending_count: int,
    now: datetime.datetime,
    exp_due: bool,
    exp_gain: int,
) -> typing.Dict[str, typing.Any]:
    return activity_repo.process_on_message_activity_sync(
        get_db_connection,
        log_transaction_in_tx,
        ensure_user_exists,
        add_user_exp,
        user_id,
        pending_count,
        now,
        exp_due,
        exp_gain,
    )


def parse_tw_datetime(text):
    # 接受格式: YYYY-MM-DD HH:MM (台灣時間 UTC+8)
    dt = datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TW_TZ).replace(tzinfo=None)

def tw_naive_to_discord_ts(dt):
    if not dt:
        return None
    return int(dt.replace(tzinfo=TW_TZ).timestamp())


def fetch_user_cooldowns_sync(user_id: int):
    return social_repo.fetch_user_cooldowns_sync(user_id)


def fetch_user_profile_sync(user_id: int):
    return social_repo.fetch_user_profile_sync(user_id)


def fetch_user_ranks_sync(user_id: int):
    return social_repo.fetch_user_ranks_sync(user_id)


def fetch_compare_sync(user_a: int, user_b: int):
    return social_repo.fetch_compare_sync(user_a, user_b)


def settle_coinflip_sync(user_id: int, bet: int, picked_side: str):
    return game_repo.settle_coinflip_sync(
        get_db_connection,
        log_transaction_in_tx,
        apply_casino_recovery_share,
        CASINO_RECOVERY_SHARE_ENABLED,
        CASINO_RECOVERY_SHARE_RATE,
        CASINO_RECOVERY_SHARE_TARGET_ID,
        user_id,
        bet,
        picked_side,
        config.COINFLIP_MIN_BET,
        config.COINFLIP_MAX_BET,
    )


def buy_lottery_tickets_sync(user_id: int, tickets: int):
    return lottery_repo.buy_lottery_tickets_sync(
        user_id,
        tickets,
        config.LOTTERY_TICKET_COST,
        config.LOTTERY_MAX_TICKETS_PER_BUY,
    )


def fetch_lottery_status_sync(user_id: int):
    return lottery_repo.fetch_lottery_status_sync(user_id)


def finalize_due_lottery_rounds_sync():
    return lottery_repo.finalize_due_rounds_sync()


def record_rr_result_sync(user_id: int, *, is_win: bool, profit_delta: int):
    return rr_repo.record_rr_result_sync(user_id, is_win=is_win, profit_delta=profit_delta)


def fetch_rr_stats_sync(user_id: int):
    return rr_repo.fetch_rr_stats_sync(user_id)


def fetch_rr_leaderboard_sync(limit: int = 10):
    return rr_repo.fetch_rr_leaderboard_sync(limit=limit)


def add_milestone_guild_sync(guild_id: int, added_by: typing.Optional[int] = None):
    return milestone_guild_repo.add_milestone_guild_sync(guild_id, added_by)


def remove_milestone_guild_sync(guild_id: int):
    return milestone_guild_repo.remove_milestone_guild_sync(guild_id)


def list_milestone_guilds_sync():
    return milestone_guild_repo.list_milestone_guilds_sync()
