# bot.py — 組裝與啟動入口（業務邏輯在 bot_modules/）

import datetime
import os
import typing

import discord
from dotenv import load_dotenv

from bot_modules import config
from bot_modules import domain_sync
from bot_modules import level_rewards
from bot_modules import milestone_guild_repo
from bot_modules import discord_helpers
from bot_modules import discord_logging
from bot_modules import assembly
from bot_modules.async_bridge import build_async_wrappers
from bot_modules.bot_core import configure_stdio_line_buffering, create_shinonome_bot, register_on_ready, setup_bot_logging
from bot_modules.db import get_db_connection, init_db, log_transaction
from bot_modules.runtime.events import register_events
from bot_modules.runtime import snapshot_cache, relay, app_lock
from bot_modules.tx_ops import lock_user_rows, log_transaction_in_tx
from bot_modules.user_repo import (
    ensure_user_exists,
    fetch_balance_leaderboard_snapshot,
    fetch_casino_stats_rows,
    fetch_level_leaderboard_snapshot,
)

load_dotenv()
configure_stdio_line_buffering()
logger = setup_bot_logging()

# --- 執行期常數 ---
ALLOWED_HOST_IDS = config.ALLOWED_HOST_IDS
SIDE_BET_RATIO = config.SIDE_BET_RATIO
IS_EVENT_ACTIVE = True
MAX_LEVEL = config.MAX_LEVEL
DISCORD_MESSAGE_CAP = config.DISCORD_MESSAGE_CAP
EXP_COOLDOWN_SECONDS = config.EXP_COOLDOWN_SECONDS
CHAT_EXP_MULTIPLIER = config.CHAT_EXP_MULTIPLIER
RED_PACKET_MIN_SECONDS = config.RED_PACKET_MIN_SECONDS
ROB_COOLDOWN_SECONDS = config.ROB_COOLDOWN_SECONDS
ROB_VICTIM_PROTECT_SECONDS = config.ROB_VICTIM_PROTECT_SECONDS
ROB_BASE_SUCCESS_RATE = config.ROB_BASE_SUCCESS_RATE
COUNTER_ROB_BASE_SUCCESS_RATE = config.COUNTER_ROB_BASE_SUCCESS_RATE
CASINO_RECOVERY_SHARE_ENABLED = config.CASINO_RECOVERY_SHARE_ENABLED
CASINO_RECOVERY_SHARE_TARGET_ID = config.CASINO_RECOVERY_SHARE_TARGET_ID
CASINO_RECOVERY_SHARE_RATE = config.CASINO_RECOVERY_SHARE_RATE
FEATURE_TOGGLES: typing.Dict[str, bool] = {
    "rob": True,
    "bj": True,
    "duel": True,
    "redpacket": True,
    "coinflip": True,
    "lottery": True,
    "russian_roulette": True,
}
BICYCLE_COOLDOWN_SECONDS = config.BICYCLE_COOLDOWN_SECONDS
COINFLIP_MIN_BET = config.COINFLIP_MIN_BET
COINFLIP_MAX_BET = config.COINFLIP_MAX_BET
LOTTERY_TICKET_COST = config.LOTTERY_TICKET_COST
LOTTERY_MAX_TICKETS_PER_BUY = config.LOTTERY_MAX_TICKETS_PER_BUY
LOTTERY_DRAW_CHECK_SECONDS = config.LOTTERY_DRAW_CHECK_SECONDS
RUSSIAN_ROULETTE_CHAMBERS = config.RUSSIAN_ROULETTE_CHAMBERS
red_packet_seq_ref = [0]
MSG_DB_FLUSH_EVERY_SECONDS = config.MSG_DB_FLUSH_EVERY_SECONDS
MSG_DB_FLUSH_COUNT = config.MSG_DB_FLUSH_COUNT
LOG_RETENTION_DAYS = config.LOG_RETENTION_DAYS
LOG_PURGE_INTERVAL_SECONDS = config.LOG_PURGE_INTERVAL_SECONDS
TW_TZ = config.TW_TZ
MINECRAFT_DEATH_MESSAGES = config.MINECRAFT_DEATH_MESSAGES
MINECRAFT_ITEMS = config.MINECRAFT_ITEMS
_pending_msg_counts: typing.Dict[str, int] = {}
_last_msg_flush_ts: typing.Dict[str, float] = {}
_last_exp_award_ts: typing.Dict[str, float] = {}
DM_RELAY_CHANNEL_ID = int(os.getenv("DM_RELAY_CHANNEL_ID", "1500383156186906764"))
DM_RELAY_NOTIFY_USER_ID = int(os.getenv("DM_RELAY_NOTIFY_USER_ID", "531308526262550528"))
DELETE_LOG_CHANNEL_ID = int(os.getenv("DELETE_LOG_CHANNEL_ID", str(DM_RELAY_CHANNEL_ID)))

BAIL_COST = domain_sync.BAIL_COST
WANTED_BUYOUT_COST = domain_sync.WANTED_BUYOUT_COST
GOOD_CITIZEN_CERT_COST = domain_sync.GOOD_CITIZEN_CERT_COST
GOOD_CITIZEN_DESTROY_COST = domain_sync.GOOD_CITIZEN_DESTROY_COST
COP_HUNT_CAPTURE_BASE_PCT = domain_sync.COP_HUNT_CAPTURE_BASE_PCT
COP_HUNT_CAPTURE_PER_STAR_PCT = domain_sync.COP_HUNT_CAPTURE_PER_STAR_PCT
COP_HUNT_FEE = 300_000
LEVEL_MILE_TIERS = level_rewards.LEVEL_MILE_TIERS
LEVEL_MILESTONE_COINS = level_rewards.LEVEL_MILESTONE_COINS

# --- async / sync 橋接 ---
_async = build_async_wrappers(domain_sync)
globals().update(_async)

get_level_stats = domain_sync.get_level_stats
calc_level_from_exp = domain_sync.calc_level_from_exp
exp_required_for_level = domain_sync.exp_required_for_level
build_exp_progress_bar = domain_sync.build_exp_progress_bar
roll_gamble_exp_from_bet = domain_sync.roll_gamble_exp_from_bet
load_rob_context = domain_sync.load_rob_context
apply_rob_success_db = domain_sync.apply_rob_success_db
apply_rob_fail_db = domain_sync.apply_rob_fail_db
rob_history_total_from_raw = domain_sync.rob_history_total_from_raw
get_last_five_robs_total = domain_sync.get_last_five_robs_total
fetch_wanted_list_rows_sync = domain_sync.fetch_wanted_list_rows_sync
fetch_good_citizen_rows_sync = domain_sync.fetch_good_citizen_rows_sync
fetch_wanted_status_row_sync = domain_sync.fetch_wanted_status_row_sync
purge_old_logs_sync = domain_sync.purge_old_logs_sync
award_vc_rewards_sync = domain_sync.award_vc_rewards_sync
process_on_message_activity_sync = domain_sync.process_on_message_activity_sync
parse_tw_datetime = domain_sync.parse_tw_datetime
tw_naive_to_discord_ts = domain_sync.tw_naive_to_discord_ts

resolve_slash_target = discord_helpers.resolve_slash_target
interaction_defer_if_needed = discord_helpers.interaction_defer_if_needed
interaction_send = discord_helpers.interaction_send
_split_discord_message_chunks = discord_helpers.split_discord_message_chunks
_chunk_text_lines = discord_helpers.chunk_text_lines


def get_is_event_active() -> bool:
    return bool(IS_EVENT_ACTIVE)


def set_is_event_active(value: bool) -> None:
    global IS_EVENT_ACTIVE
    IS_EVENT_ACTIVE = bool(value)


def now_tw_naive() -> datetime.datetime:
    return domain_sync.now_tw_naive()


def is_host():
    return discord_helpers.make_host_check(ALLOWED_HOST_IDS)


async def process_level_ups(
    member: typing.Union[discord.Member, discord.User],
    old_lv: int,
    new_lv: int,
    guild_id: typing.Optional[int] = None,
):
    await level_rewards.process_level_ups(
        member,
        old_lv,
        new_lv,
        guild_id=guild_id,
        try_claim_milestone=domain_sync.try_claim_milestone,
        is_milestone_guild_allowed=milestone_guild_repo.is_milestone_guild_allowed,
    )


def get_share_enabled() -> bool:
    return bool(domain_sync.CASINO_RECOVERY_SHARE_ENABLED)


def set_share_enabled(value: bool) -> None:
    domain_sync.CASINO_RECOVERY_SHARE_ENABLED = bool(value)


# --- Bot 實例與 runtime ---
bot = create_shinonome_bot(
    logger=logger,
    allowed_host_ids=ALLOWED_HOST_IDS,
    dm_relay_channel_id=DM_RELAY_CHANNEL_ID,
    chunk_text_lines=_chunk_text_lines,
    discord_message_cap=DISCORD_MESSAGE_CAP,
)

_snapshot_cache_tasks = snapshot_cache.register_snapshot_cache(
    bot,
    {
        "logger": logger,
        "db_to_thread": db_to_thread,
        "fetch_balance_leaderboard_snapshot": fetch_balance_leaderboard_snapshot,
        "fetch_level_leaderboard_snapshot": fetch_level_leaderboard_snapshot,
        "fetch_casino_stats_rows": fetch_casino_stats_rows,
        "fetch_wanted_list_rows_sync": fetch_wanted_list_rows_sync,
        "fetch_good_citizen_rows_sync": fetch_good_citizen_rows_sync,
        "fetch_wanted_status_row_sync": fetch_wanted_status_row_sync,
    },
)

app_lock.register_app_command_lock(bot, logger)

_relay_handlers = relay.register_relay(
    bot,
    {
        "logger": logger,
        "DM_RELAY_CHANNEL_ID": DM_RELAY_CHANNEL_ID,
        "DM_RELAY_NOTIFY_USER_ID": DM_RELAY_NOTIFY_USER_ID,
        "DISCORD_MESSAGE_CAP": DISCORD_MESSAGE_CAP,
        "split_discord_message_chunks": _split_discord_message_chunks,
    },
)

_event_tasks = register_events(
    bot,
    {
        "logger": logger,
        "now_tw_naive": now_tw_naive,
        "db_to_thread": db_to_thread,
        "award_vc_rewards_sync": award_vc_rewards_sync,
        "purge_old_logs_sync": purge_old_logs_sync,
        "relay_dm_to_staff_channel": _relay_handlers["relay_dm_to_staff_channel"],
        "relay_staff_reply_to_dm_user": _relay_handlers["relay_staff_reply_to_dm_user"],
        "relay_user_message_to_staff_channel": _relay_handlers["relay_user_message_to_staff_channel"],
        "process_on_message_activity_sync": process_on_message_activity_sync,
        "process_level_ups": process_level_ups,
        "cleanup_local_caches": snapshot_cache.cleanup_local_caches,
        "DM_RELAY_CHANNEL_ID": DM_RELAY_CHANNEL_ID,
        "DELETE_LOG_CHANNEL_ID": DELETE_LOG_CHANNEL_ID,
        "MSG_DB_FLUSH_EVERY_SECONDS": MSG_DB_FLUSH_EVERY_SECONDS,
        "MSG_DB_FLUSH_COUNT": MSG_DB_FLUSH_COUNT,
        "EXP_COOLDOWN_SECONDS": EXP_COOLDOWN_SECONDS,
        "CHAT_EXP_MULTIPLIER": CHAT_EXP_MULTIPLIER,
        "LOG_RETENTION_DAYS": LOG_RETENTION_DAYS,
        "LOG_PURGE_INTERVAL_SECONDS": LOG_PURGE_INTERVAL_SECONDS,
        "LEVEL_MILE_TIERS": LEVEL_MILE_TIERS,
        "_pending_msg_counts": _pending_msg_counts,
        "_last_msg_flush_ts": _last_msg_flush_ts,
        "_last_exp_award_ts": _last_exp_award_ts,
        "finalize_due_lottery_rounds_async": finalize_due_lottery_rounds_async,
        "LOTTERY_DRAW_CHECK_SECONDS": LOTTERY_DRAW_CHECK_SECONDS,
    },
)

register_on_ready(
    bot,
    logger=logger,
    dm_relay_channel_id=DM_RELAY_CHANNEL_ID,
    init_db=init_db,
    discord_log_register=discord_logging.register_discord_log_handler,
    event_tasks=_event_tasks,
    snapshot_cache_tasks=_snapshot_cache_tasks,
)

assembly.register_all_commands(
    bot,
    {
        "ALLOWED_HOST_IDS": ALLOWED_HOST_IDS,
        "MAX_LEVEL": MAX_LEVEL,
        "LEVEL_MILE_TIERS": LEVEL_MILE_TIERS,
        "LEVEL_MILESTONE_COINS": LEVEL_MILESTONE_COINS,
        "FEATURE_TOGGLES": FEATURE_TOGGLES,
        "RED_PACKET_MIN_SECONDS": RED_PACKET_MIN_SECONDS,
        "red_packet_seq_ref": red_packet_seq_ref,
        "SIDE_BET_RATIO": SIDE_BET_RATIO,
        "ROB_COOLDOWN_SECONDS": ROB_COOLDOWN_SECONDS,
        "ROB_VICTIM_PROTECT_SECONDS": ROB_VICTIM_PROTECT_SECONDS,
        "ROB_BASE_SUCCESS_RATE": ROB_BASE_SUCCESS_RATE,
        "COP_HUNT_FEE": COP_HUNT_FEE,
        "COP_HUNT_CAPTURE_BASE_PCT": COP_HUNT_CAPTURE_BASE_PCT,
        "COP_HUNT_CAPTURE_PER_STAR_PCT": COP_HUNT_CAPTURE_PER_STAR_PCT,
        "BAIL_COST": BAIL_COST,
        "WANTED_BUYOUT_COST": WANTED_BUYOUT_COST,
        "GOOD_CITIZEN_CERT_COST": GOOD_CITIZEN_CERT_COST,
        "GOOD_CITIZEN_DESTROY_COST": GOOD_CITIZEN_DESTROY_COST,
        "COUNTER_ROB_BASE_SUCCESS_RATE": COUNTER_ROB_BASE_SUCCESS_RATE,
        "CASINO_RECOVERY_SHARE_ENABLED": CASINO_RECOVERY_SHARE_ENABLED,
        "CASINO_RECOVERY_SHARE_RATE": CASINO_RECOVERY_SHARE_RATE,
        "CASINO_RECOVERY_SHARE_TARGET_ID": CASINO_RECOVERY_SHARE_TARGET_ID,
        "MINECRAFT_DEATH_MESSAGES": MINECRAFT_DEATH_MESSAGES,
        "MINECRAFT_ITEMS": MINECRAFT_ITEMS,
        "BICYCLE_COOLDOWN_SECONDS": BICYCLE_COOLDOWN_SECONDS,
        "COINFLIP_MIN_BET": COINFLIP_MIN_BET,
        "COINFLIP_MAX_BET": COINFLIP_MAX_BET,
        "LOTTERY_TICKET_COST": LOTTERY_TICKET_COST,
        "LOTTERY_MAX_TICKETS_PER_BUY": LOTTERY_MAX_TICKETS_PER_BUY,
        "RUSSIAN_ROULETTE_CHAMBERS": RUSSIAN_ROULETTE_CHAMBERS,
        "TW_TZ": TW_TZ,
        "resolve_slash_target": resolve_slash_target,
        "ensure_user_exists": ensure_user_exists,
        "ensure_user_exists_async": ensure_user_exists_async,
        "interaction_send": interaction_send,
        "interaction_defer_if_needed": interaction_defer_if_needed,
        "get_level_stats": get_level_stats,
        "exp_required_for_level": exp_required_for_level,
        "process_level_ups": process_level_ups,
        "get_db_connection": get_db_connection,
        "logger": logger,
        "log_transaction": log_transaction,
        "credit_balance_with_log_async": credit_balance_with_log_async,
        "try_deduct_balance_async": try_deduct_balance_async,
        "calc_level_from_exp": calc_level_from_exp,
        "get_is_event_active": get_is_event_active,
        "set_is_event_active": set_is_event_active,
        "get_share_enabled": get_share_enabled,
        "set_share_enabled": set_share_enabled,
        "claim_daily_reward_async": claim_daily_reward_async,
        "claim_hourly_reward_async": claim_hourly_reward_async,
        "claim_beg_sync_async": claim_beg_sync_async,
        "claim_rescue_sync_async": claim_rescue_sync_async,
        "get_user_stats_async": get_user_stats_async,
        "get_level_stats_async": get_level_stats_async,
        "get_claimed_milestones_async": get_claimed_milestones_async,
        "build_exp_progress_bar": build_exp_progress_bar,
        "transfer_sync_async": transfer_sync_async,
        "fetch_record_rows_async": fetch_record_rows_async,
        "now_tw_naive": now_tw_naive,
        "fetch_casino_share_stats_rows_async": fetch_casino_share_stats_rows_async,
        "load_rob_context_async": load_rob_context_async,
        "apply_rob_success_db_async": apply_rob_success_db_async,
        "apply_rob_fail_db_async": apply_rob_fail_db_async,
        "db_to_thread": db_to_thread,
        "choose_role_sync_async": choose_role_sync_async,
        "tw_naive_to_discord_ts": tw_naive_to_discord_ts,
        "wanted_buyout_sync_async": wanted_buyout_sync_async,
        "toggle_good_citizen_sync_async": toggle_good_citizen_sync_async,
        "fetch_good_citizen_rows_sync_async": fetch_good_citizen_rows_sync_async,
        "break_citizen_sync_async": break_citizen_sync_async,
        "fetch_wanted_status_row_sync_async": fetch_wanted_status_row_sync_async,
        "fetch_wanted_list_rows_sync_async": fetch_wanted_list_rows_sync_async,
        "rob_history_total_from_raw": rob_history_total_from_raw,
        "get_last_five_robs_total": get_last_five_robs_total,
        "pay_bail_sync_async": pay_bail_sync_async,
        "lock_user_rows": lock_user_rows,
        "log_transaction_in_tx": log_transaction_in_tx,
        "update_game_result_async": update_game_result_async,
        "add_user_exp_async": add_user_exp_async,
        "roll_gamble_exp_from_bet": roll_gamble_exp_from_bet,
        "settle_duel_payouts_with_log_async": settle_duel_payouts_with_log_async,
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
        "add_milestone_guild_async": add_milestone_guild_async,
        "remove_milestone_guild_async": remove_milestone_guild_async,
        "list_milestone_guilds_async": list_milestone_guilds_async,
    },
)

bot.run(os.getenv("DISCORD_TOKEN"))
