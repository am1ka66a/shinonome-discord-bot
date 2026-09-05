import asyncio
import typing

from bot_modules.runtime import snapshot_cache
from bot_modules.user_repo import ensure_user_exists, fetch_record_rows, get_user_stats


async def db_to_thread(func: typing.Callable[..., typing.Any], *args, **kwargs):
    """把同步 DB helper 丟到 thread，避免阻塞 discord.py event loop。"""
    return await asyncio.to_thread(func, *args, **kwargs)


def build_async_wrappers(domain) -> typing.Dict[str, typing.Any]:
    """依 domain_sync 模組建立 async 包裝，供 bot.py 注入 register ctx。"""

    async def get_user_stats_async(user_id):
        return await db_to_thread(get_user_stats, user_id)

    async def fetch_record_rows_async(user_id, limit: int = 50):
        return await db_to_thread(fetch_record_rows, user_id, limit)

    async def fetch_casino_stats_rows_async():
        return await snapshot_cache.get_casino_stats_rows_cached()

    async def fetch_casino_share_stats_rows_async(days: int = 7):
        return await db_to_thread(domain.fetch_casino_share_stats_rows, days)

    async def claim_daily_reward_async(user_id, daily_reward: int = 100_000):
        return await db_to_thread(domain.claim_daily_reward, user_id, daily_reward)

    async def claim_hourly_reward_async(user_id, reward_per_slot: int = 1000):
        return await db_to_thread(domain.claim_hourly_reward, user_id, reward_per_slot)

    async def ensure_user_exists_async(user_id, default_balance=50000):
        return await db_to_thread(ensure_user_exists, user_id, default_balance)

    async def get_level_stats_async(user_id):
        return await db_to_thread(domain.get_level_stats, user_id)

    async def get_claimed_milestones_async(user_id):
        return await db_to_thread(domain.get_claimed_milestones, user_id)

    async def try_deduct_balance_async(user_id, amount, reason):
        return await db_to_thread(domain.try_deduct_balance, user_id, amount, reason)

    async def update_game_result_async(
        user_id, balance_delta, profit_delta, is_win, is_push=False, share_recovery=True
    ):
        return await db_to_thread(
            domain.update_game_result, user_id, balance_delta, profit_delta, is_win, is_push, share_recovery
        )

    async def add_user_exp_async(user_id, amount):
        return await db_to_thread(domain.add_user_exp, user_id, amount)

    async def credit_balance_with_log_async(user_id, amount, reason):
        return await db_to_thread(domain.credit_balance_with_log, user_id, amount, reason)

    async def settle_duel_payouts_with_log_async(challenger_id, opponent_id, a_amt, b_amt, s_a, s_b):
        return await db_to_thread(
            domain.settle_duel_payouts_with_log,
            challenger_id,
            opponent_id,
            a_amt,
            b_amt,
            s_a,
            s_b,
        )

    async def load_rob_context_async(robber_id, target_id):
        return await db_to_thread(domain.load_rob_context, robber_id, target_id)

    async def apply_rob_success_db_async(robber_id, target_id, now, success_rate_pct):
        return await db_to_thread(
            domain.apply_rob_success_db, robber_id, target_id, now, success_rate_pct
        )

    async def apply_rob_fail_db_async(robber_id, target_id, now):
        return await db_to_thread(domain.apply_rob_fail_db, robber_id, target_id, now)

    async def claim_beg_sync_async(user_id):
        return await db_to_thread(domain.claim_beg_sync, user_id)

    async def choose_role_sync_async(user_id, role, now):
        return await db_to_thread(domain.choose_role_sync, user_id, role, now)

    async def toggle_good_citizen_sync_async(user_id, now):
        return await db_to_thread(domain.toggle_good_citizen_sync, user_id, now)

    async def fetch_good_citizen_rows_sync_async():
        return await snapshot_cache.get_good_citizen_rows_cached()

    async def fetch_wanted_status_row_sync_async(user_id):
        return await snapshot_cache.get_wanted_status_cached(user_id)

    async def fetch_wanted_list_rows_sync_async():
        return await snapshot_cache.get_wanted_list_rows_cached()

    async def pay_bail_sync_async(user_id, now):
        return await db_to_thread(domain.pay_bail_sync, user_id, now)

    async def claim_rescue_sync_async(user_id):
        return await db_to_thread(domain.claim_rescue_sync, user_id)

    async def wanted_buyout_sync_async(user_id, now):
        return await db_to_thread(domain.wanted_buyout_sync, user_id, now)

    async def break_citizen_sync_async(attacker_id, target_id, now):
        return await db_to_thread(domain.break_citizen_sync, attacker_id, target_id, now)

    async def fetch_user_cooldowns_async(user_id):
        return await db_to_thread(domain.fetch_user_cooldowns_sync, user_id)

    async def fetch_user_profile_async(user_id):
        return await db_to_thread(domain.fetch_user_profile_sync, user_id)

    async def fetch_user_ranks_async(user_id):
        return await db_to_thread(domain.fetch_user_ranks_sync, user_id)

    async def fetch_compare_async(user_a, user_b):
        return await db_to_thread(domain.fetch_compare_sync, user_a, user_b)

    async def settle_coinflip_async(user_id, bet, picked_side):
        return await db_to_thread(domain.settle_coinflip_sync, user_id, bet, picked_side)

    async def buy_lottery_tickets_async(user_id, tickets):
        return await db_to_thread(domain.buy_lottery_tickets_sync, user_id, tickets)

    async def fetch_lottery_status_async(user_id):
        return await db_to_thread(domain.fetch_lottery_status_sync, user_id)

    async def finalize_due_lottery_rounds_async():
        return await db_to_thread(domain.finalize_due_lottery_rounds_sync)

    async def record_rr_result_async(user_id, *, is_win, profit_delta):
        return await db_to_thread(domain.record_rr_result_sync, user_id, is_win=is_win, profit_delta=profit_delta)

    async def fetch_rr_stats_async(user_id):
        return await db_to_thread(domain.fetch_rr_stats_sync, user_id)

    async def fetch_rr_leaderboard_async(limit: int = 10):
        return await db_to_thread(domain.fetch_rr_leaderboard_sync, limit)

    async def fetch_rr_rate_leaderboard_async(limit: int = 10):
        return await db_to_thread(domain.fetch_rr_rate_leaderboard_sync, limit)

    async def save_rr_match_async(
        *,
        match_id,
        guild_id,
        channel_id,
        message_id,
        mode,
        phase,
        payload,
    ):
        return await db_to_thread(
            domain.save_rr_match_sync,
            match_id=match_id,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            mode=mode,
            phase=phase,
            payload=payload,
        )

    async def delete_rr_match_async(match_id, payload=None):
        return await db_to_thread(domain.delete_rr_match_sync, match_id, payload)

    async def fetch_active_rr_matches_async():
        return await db_to_thread(domain.fetch_active_rr_matches_sync)

    async def add_milestone_guild_async(guild_id, added_by=None):
        return await db_to_thread(domain.add_milestone_guild_sync, guild_id, added_by)

    async def remove_milestone_guild_async(guild_id):
        return await db_to_thread(domain.remove_milestone_guild_sync, guild_id)

    async def list_milestone_guilds_async():
        return await db_to_thread(domain.list_milestone_guilds_sync)

    async def transfer_sync_async(sender_id, receiver_id, amount, note_text):
        return await db_to_thread(domain.transfer_sync, sender_id, receiver_id, amount, note_text)

    return {
        "db_to_thread": db_to_thread,
        "get_user_stats_async": get_user_stats_async,
        "fetch_record_rows_async": fetch_record_rows_async,
        "fetch_casino_stats_rows_async": fetch_casino_stats_rows_async,
        "fetch_casino_share_stats_rows_async": fetch_casino_share_stats_rows_async,
        "claim_daily_reward_async": claim_daily_reward_async,
        "claim_hourly_reward_async": claim_hourly_reward_async,
        "ensure_user_exists_async": ensure_user_exists_async,
        "get_level_stats_async": get_level_stats_async,
        "get_claimed_milestones_async": get_claimed_milestones_async,
        "try_deduct_balance_async": try_deduct_balance_async,
        "update_game_result_async": update_game_result_async,
        "add_user_exp_async": add_user_exp_async,
        "credit_balance_with_log_async": credit_balance_with_log_async,
        "settle_duel_payouts_with_log_async": settle_duel_payouts_with_log_async,
        "load_rob_context_async": load_rob_context_async,
        "apply_rob_success_db_async": apply_rob_success_db_async,
        "apply_rob_fail_db_async": apply_rob_fail_db_async,
        "claim_beg_sync_async": claim_beg_sync_async,
        "choose_role_sync_async": choose_role_sync_async,
        "toggle_good_citizen_sync_async": toggle_good_citizen_sync_async,
        "fetch_good_citizen_rows_sync_async": fetch_good_citizen_rows_sync_async,
        "fetch_wanted_status_row_sync_async": fetch_wanted_status_row_sync_async,
        "fetch_wanted_list_rows_sync_async": fetch_wanted_list_rows_sync_async,
        "pay_bail_sync_async": pay_bail_sync_async,
        "claim_rescue_sync_async": claim_rescue_sync_async,
        "wanted_buyout_sync_async": wanted_buyout_sync_async,
        "break_citizen_sync_async": break_citizen_sync_async,
        "fetch_user_cooldowns_async": fetch_user_cooldowns_async,
        "fetch_user_profile_async": fetch_user_profile_async,
        "fetch_user_ranks_async": fetch_user_ranks_async,
        "fetch_compare_async": fetch_compare_async,
        "settle_coinflip_async": settle_coinflip_async,
        "buy_lottery_tickets_async": buy_lottery_tickets_async,
        "fetch_lottery_status_async": fetch_lottery_status_async,
        "finalize_due_lottery_rounds_async": finalize_due_lottery_rounds_async,
        "record_rr_result_async": record_rr_result_async,
        "fetch_rr_stats_async": fetch_rr_stats_async,
        "fetch_rr_leaderboard_async": fetch_rr_leaderboard_async,
        "fetch_rr_rate_leaderboard_async": fetch_rr_rate_leaderboard_async,
        "save_rr_match_async": save_rr_match_async,
        "delete_rr_match_async": delete_rr_match_async,
        "fetch_active_rr_matches_async": fetch_active_rr_matches_async,
        "add_milestone_guild_async": add_milestone_guild_async,
        "remove_milestone_guild_async": remove_milestone_guild_async,
        "list_milestone_guilds_async": list_milestone_guilds_async,
        "transfer_sync_async": transfer_sync_async,
    }
