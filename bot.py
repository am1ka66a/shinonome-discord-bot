# ==============================================================================
# 【一】匯入套件與環境變數
# 載入 discord.py、pymysql 等依賴，並以 python-dotenv 讀取 .env
# （DISCORD_TOKEN、MYSQL_URL、轉接頻道與等級相關設定）。
# ==============================================================================

import asyncio
import datetime
import io
import json
import logging
import math
import os
import random
import re
import sys
import time
import typing

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from bot_modules import config
from bot_modules.admin_commands import register_admin_commands
from bot_modules.db import (
    get_db_connection,
    init_db,
    log_casino_transaction,
    log_transaction,
)
from bot_modules.user_repo import (
    ensure_user_exists,
    fetch_casino_share_stats_rows,
    fetch_casino_stats_rows,
    fetch_record_rows,
    get_user_stats,
    is_blacklisted,
)
from bot_modules.tx_ops import (
    get_locked_user_balance,
    lock_user_rows,
    log_transaction_in_tx,
)
from bot_modules import rob_repo
from bot_modules import economy_repo
from bot_modules import economy_service
from bot_modules import wanted_repo
from bot_modules import game_repo
from bot_modules.runtime import register_events
from bot_modules.commands import register_duel_commands, register_blackjack_commands

load_dotenv()

# 容器環境（Railway 等）若未使用 python -u，預設 stdout 會緩衝，部署日誌像「卡住」
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass

# ------------------------------------------------------------------------------
# bot.py 索引（2026-05 重構後）
#
# 【核心定位】
# - bot.py 目前是「組裝與註冊入口」：保留全域狀態、共用工具、主要玩家指令與啟動流程
# - 高複雜度模組已抽離到 bot_modules 子資料夾
#
# 【A】基礎與全域
# - 【一】匯入套件
# - 【二】日誌系統
# - 【三】常數／執行期狀態／轉接對照
#
# 【B】共用工具與資料操作
# - 【四】Discord 日誌轉發 Handler
# - 【五】等級里程碑通知
# - 【六】Slash 共用解析與互動回覆工具
# - 【七】Repo 包裝與交易輔助（透過 bot_modules）
#
# 【C】Bot 核心與 Relay
# - 【十】Bot intents / setup_hook / bot 實例
# - 【十一】私訊與群組 @ 的轉接流程
#
# 【D】互動 UI（保留）
# - 【八】本檔保留的共用互動 View（如紅包、翻頁）
# - Blackjack / Duel 已抽到：
#   - bot_modules/commands/blackjack.py
#   - bot_modules/commands/duel.py
#
# 【E】事件模組（抽離）
# - 【十二】on_message / on_message_delete / vc 獎勵 / logs retention
# - 實作位置：bot_modules/runtime/events.py
# - 本檔只做 register 與 task 啟動
#
# 【F】玩家指令與管理註冊
# - 【十三】經濟、警匪、排行榜等玩家 Slash（本檔）
# - 管理指令註冊：bot_modules/admin_commands.py
# - 遊戲指令註冊：bot_modules/commands/*
#
# 【G】進入點
# - 【十五】集中註冊（admin/events/blackjack/duel）後 bot.run
# ------------------------------------------------------------------------------

# ==============================================================================
# 【二】日誌系統
# 設定主程式 Logger（主控台、可選 log 檔）；啟動後可選擇再掛「Discord 頻道轉發」
#（見【四】），把重要 log 非同步貼到指定文字頻道方便遠端查看。
# ==============================================================================


def setup_bot_logging() -> logging.Logger:
    """主程序日誌：主控台必出；若設定 BOT_LOG_FILE 或 LOG_FILE 則同步寫入檔案。"""
    name = "shinonome_bot"
    log = logging.getLogger(name)
    if log.handlers:
        return log
    level_raw = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_raw, logging.INFO)
    log.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Railway/容器常為 UTC，統一以台灣時間（UTC+8）輸出日誌時間，避免誤判。
    _log_tz = datetime.timezone(datetime.timedelta(hours=8))
    fmt.converter = lambda ts: datetime.datetime.fromtimestamp(ts, _log_tz).timetuple()
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    log.propagate = False
    log_path = (os.getenv("BOT_LOG_FILE") or os.getenv("LOG_FILE") or "").strip()
    if log_path:
        try:
            log_dir = os.path.dirname(log_path)
            if log_dir and not os.path.isdir(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setLevel(level)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as e:
            log.warning("無法建立日誌檔 %s: %s", log_path, e)
    return log


logger = setup_bot_logging()

# ==============================================================================
# 【三】全域常數、執行期狀態、轉接用對照與靜態資料
# 含：管理員 ID、賭場／等級／搶紅包、訊息與 EXP 冷卻、
# 私訊轉接頻道與「轉發訊息 ID -> 原訊中繼」表格、Minecraft 死法／物品詞庫（JSON）。
# ==============================================================================
ALLOWED_HOST_IDS = config.ALLOWED_HOST_IDS
SIDE_BET_RATIO = config.SIDE_BET_RATIO
IS_EVENT_ACTIVE = True                   # 賭場狀態
MAX_LEVEL = config.MAX_LEVEL
# Discord 單則訊息字元上限（與 API 一致）
DISCORD_MESSAGE_CAP = config.DISCORD_MESSAGE_CAP
EXP_COOLDOWN_SECONDS = config.EXP_COOLDOWN_SECONDS
# 發話經驗 = random(12,20) × 此倍數（冷卻不變）
CHAT_EXP_MULTIPLIER = config.CHAT_EXP_MULTIPLIER
# 每完成一局 21 點結算時加發的隨機 EXP（見 roll_gamble_exp_from_bet）
GAMBLE_EXP_MIN = config.GAMBLE_EXP_MIN
GAMBLE_EXP_MAX = config.GAMBLE_EXP_MAX
RED_PACKET_MIN_SECONDS = config.RED_PACKET_MIN_SECONDS
ROB_COOLDOWN_SECONDS = config.ROB_COOLDOWN_SECONDS
ROB_VICTIM_PROTECT_SECONDS = config.ROB_VICTIM_PROTECT_SECONDS
# /rob 專用：基礎成功率；每 1 級差距 ±1%，再 clamp 至 5%～95%
ROB_BASE_SUCCESS_RATE = config.ROB_BASE_SUCCESS_RATE
# 平民 `/counter_rob` 加倍搶回專用基礎機率（與 `/rob` 分開）。
COUNTER_ROB_BASE_SUCCESS_RATE = config.COUNTER_ROB_BASE_SUCCESS_RATE
ROB_STEAL_CAP = config.ROB_STEAL_CAP
ROB_FAIL_PENALTY_CAP = config.ROB_FAIL_PENALTY_CAP
# 賭場回收分潤：僅在「實際回收」時切割，不在下注當下抽成
CASINO_RECOVERY_SHARE_ENABLED = config.CASINO_RECOVERY_SHARE_ENABLED
CASINO_RECOVERY_SHARE_TARGET_ID = config.CASINO_RECOVERY_SHARE_TARGET_ID
CASINO_RECOVERY_SHARE_RATE = config.CASINO_RECOVERY_SHARE_RATE
CASINO_RECOVERY_SHARE_REASON_PREFIX = config.CASINO_RECOVERY_SHARE_REASON_PREFIX
# 可線上切換的功能開關（不需重啟）
FEATURE_TOGGLES: typing.Dict[str, bool] = {
    "rob": True,
    "bj": True,
    "duel": True,
    "redpacket": True,
}
BICYCLE_COOLDOWN_SECONDS = config.BICYCLE_COOLDOWN_SECONDS
red_packet_seq = 0
MSG_DB_FLUSH_EVERY_SECONDS = config.MSG_DB_FLUSH_EVERY_SECONDS
MSG_DB_FLUSH_COUNT = config.MSG_DB_FLUSH_COUNT
# logs 流水表：只保留最近 N 天，排程定期刪除更早資料（與 MySQL session 時區一致）
LOG_RETENTION_DAYS = config.LOG_RETENTION_DAYS
LOG_PURGE_INTERVAL_SECONDS = config.LOG_PURGE_INTERVAL_SECONDS
# 台灣時間 (UTC+8)；與 get_db_connection 的 MySQL session time_zone 一致
TW_TZ = config.TW_TZ

# 新用戶預設起始金（ensure_user_exists 預設）
DEFAULT_STARTUP_BALANCE = config.DEFAULT_STARTUP_BALANCE
REASON_USER_INITIAL_BALANCE = config.REASON_USER_INITIAL_BALANCE


def now_tw_naive() -> datetime.datetime:
    """目前台灣本地時間（naive datetime）。"""
    return config.now_tw_naive()


MINECRAFT_DEATH_MESSAGES = config.MINECRAFT_DEATH_MESSAGES
MINECRAFT_ITEMS = config.MINECRAFT_ITEMS
_pending_msg_counts: typing.Dict[str, int] = {}
_last_msg_flush_ts: typing.Dict[str, float] = {}
_last_exp_award_ts: typing.Dict[str, float] = {}

# 私訊 ↔ 管理頻道雙向轉接（使用者 DM -> 頻道；管理員「回覆」轉發訊息 -> 私訊使用者）
DM_RELAY_CHANNEL_ID = int(os.getenv("DM_RELAY_CHANNEL_ID", "1500383156186906764"))
# 轉發到頻道時 @ 通知對象（預設此 Discord ID，可用 DM_RELAY_NOTIFY_USER_ID 覆寫）
DM_RELAY_NOTIFY_USER_ID = int(os.getenv("DM_RELAY_NOTIFY_USER_ID", "531308526262550528"))
# 訊息刪除追蹤頻道（0 表示停用；未設時沿用 DM 轉接頻道）
DELETE_LOG_CHANNEL_ID = int(os.getenv("DELETE_LOG_CHANNEL_ID", str(DM_RELAY_CHANNEL_ID)))
# 轉發訊息 ID -> (原 user_id, 是否走 DM 回覆, guild_id, channel_id, message_id)
# 私訊轉發：(..., True, 0, 0, 0)；群組 @：(..., False, 原訊 guild/channel/message)
RelayForwardMeta = typing.Tuple[int, bool, int, int, int]
_relay_forward_meta: typing.Dict[int, RelayForwardMeta] = {}

_discord_log_handler_installed = False


def get_is_event_active() -> bool:
    return bool(IS_EVENT_ACTIVE)


def set_is_event_active(value: bool) -> None:
    global IS_EVENT_ACTIVE
    IS_EVENT_ACTIVE = bool(value)


def get_share_enabled() -> bool:
    return bool(CASINO_RECOVERY_SHARE_ENABLED)


def set_share_enabled(value: bool) -> None:
    global CASINO_RECOVERY_SHARE_ENABLED
    CASINO_RECOVERY_SHARE_ENABLED = bool(value)

# ==============================================================================
# 【四】Discord 頻道日誌轉發
# 自訂 logging.Handler，把 Python logging 的內容用非同步方式貼到指定文字頻道
#（與轉接頻道可相同或分開，由 LOG_DISCORD_CHANNEL_ID 控制）。
# ==============================================================================


class DiscordLogHandler(logging.Handler):
    """將 logging 轉成非同步發送到 Discord 文字頻道（避免阻塞 logging）。"""

    def __init__(self, client: commands.Bot, channel_id: int):
        super().__init__()
        self.client = client
        self.channel_id = channel_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if len(msg) > 1900:
                msg = msg[:1900] + "…"
            loop = self.client.loop
            if loop is None or not loop.is_running():
                return
            fut = asyncio.run_coroutine_threadsafe(self._send(msg), loop)

            def _discard_future_result(f: asyncio.Future) -> None:
                try:
                    f.result()
                except Exception:
                    pass

            fut.add_done_callback(_discard_future_result)
        except Exception:
            self.handleError(record)

    async def _send(self, text: str) -> None:
        try:
            ch = self.client.get_channel(self.channel_id)
            if ch is None:
                ch = await self.client.fetch_channel(self.channel_id)
            if not isinstance(ch, discord.abc.Messageable):
                return
            chunk = text[:1990]
            await ch.send(f"```\n{chunk}\n```")
        except Exception:
            pass


def register_discord_log_handler(client: commands.Bot) -> None:
    """啟動後掛上 Discord 頻道日誌；預設與私訊轉接同一頻道，可用 LOG_DISCORD_CHANNEL_ID 覆寫。"""
    global _discord_log_handler_installed
    if _discord_log_handler_installed:
        return
    raw = (os.getenv("LOG_DISCORD_CHANNEL_ID") or "").strip()
    ch_id = int(raw) if raw else DM_RELAY_CHANNEL_ID
    if not ch_id:
        return
    try:
        h = DiscordLogHandler(client, ch_id)
        h.setLevel(logger.level)
        h.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(h)
        _discord_log_handler_installed = True
        logger.info("已啟用 Discord 頻道日誌（頻道 ID %s）", ch_id)
    except Exception as e:
        logger.warning("無法註冊 Discord 日誌 handler: %s", e)

# ==============================================================================
# 【五】等級里程碑（獎勵幣、私訊文案、身分組）
# 在首次達到 Lv.20/40/60/80/100 時觸發獎勵與可選身分組；伺服器與各階 role ID
# 由 LEVEL_MILESTONE_GUILD_ID、LEVEL_ROLE_ID_* 等環境變數決定。
# ==============================================================================
LEVEL_MILE_TIERS: typing.Tuple[int, ...] = (20, 40, 60, 80, 100)
LEVEL_MILESTONE_COINS: typing.Dict[int, int] = {
    20: 500_000,
    40: 1_000_000,
    60: 2_000_000,
    80: 8_000_000,
    100: 20_000_000,
}

# 各階首次解鎖時的私訊短句；每則都帶「奈音」群梗，玩笑偏開，可再自行改
LEVEL_MILESTONE_FLAVOR: typing.Dict[int, str] = {
    20: "恭喜你升上20等。請繼續努力，當個好賭狗。",
    40: "恭喜你從賭狗進化成了奈音的狗，到了這裡請當個好狗狗，多催更奈音的女裝。",
    60: "你好閒，水群水到了60等，請繼續浪費時間在DC上面，多多賭博有助身心健康。",
    80: "到了這一階，你時間真的很多，到了這個等級請買一張2330供奉給am1ka，撫慰他的辛勞。",
    100: "封頂。你這傻逼能滿等也是個奇蹟= =。",
}

def level_milestone_guild_id() -> typing.Optional[int]:
    """要自動上身分組的目標 Discord 伺服器 ID；未設則不會在任何伺服器加身分組。"""
    raw = (os.getenv("LEVEL_MILESTONE_GUILD_ID", "") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

def level_auto_role_id(milestone: int) -> typing.Optional[int]:
    """.env 設 LEVEL_ROLE_ID_20=數字 等；僅在 `LEVEL_MILESTONE_GUILD_ID` 相符的伺服器內、首次到達該等級時加身分組。"""
    raw = (os.getenv(f"LEVEL_ROLE_ID_{milestone}", "") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

# ==============================================================================
# 【六】指令權限與 Slash 共用工具
# 前綴指令的 hosts 檢查、訊息字元切分、解析使用者 ID／提及、以及 Slash 中依「成員
# 或手填 ID」解析目標成員或 User（搶劫／轉帳／後台等情境共用）。
# ==============================================================================


def is_host():
    def predicate(ctx):
        return ctx.author.id in ALLOWED_HOST_IDS

    return commands.check(predicate)


def _split_discord_message_chunks(
    text: str, limit: int = DISCORD_MESSAGE_CAP
) -> typing.List[str]:
    """依字元長度切分，供多則訊息送出（一般文字／DM）。"""
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def parse_discord_user_id(raw: typing.Optional[str]) -> typing.Optional[int]:
    """解析純數字 ID 或 <@...> 提及。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.fullmatch(r"<@!?(\d{17,20})>", s)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d{17,20})", s)
    if m:
        return int(m.group(1))
    return None


async def resolve_slash_target(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member],
    user_id: typing.Optional[str],
    *,
    required: bool = True,
    in_guild_only: bool = False,
) -> typing.Tuple[typing.Optional[typing.Union[discord.Member, discord.User]], typing.Optional[str]]:
    """
    優先使用選取的 member；否則解析 user_id。
    in_guild_only=True 時必須為本伺服器成員（搶劫／轉帳等）；False 時若不在伺服器則改以 fetch_user（後台／黑名單等）。
    """
    if member is not None:
        return member, None
    uid = parse_discord_user_id(user_id)
    if uid is None:
        if not required:
            return None, None
        return None, "請選擇成員，或在「使用者 ID」填寫 17～19 位數字（亦可貼 `<@...>` 提及）。"
    guild = interaction.guild
    client = interaction.client
    if guild is not None:
        cached = guild.get_member(uid)
        if cached is not None:
            return cached, None
        try:
            m = await guild.fetch_member(uid)
            return m, None
        except discord.NotFound:
            if in_guild_only:
                return None, "找不到此成員（請確認對方仍在這個伺服器）。"
            try:
                u = await client.fetch_user(uid)
                return u, None
            except discord.NotFound:
                return None, "找不到此 Discord 使用者。"
            except discord.HTTPException as e:
                return None, f"無法查詢使用者：{e}"
        except discord.HTTPException as e:
            return None, f"無法查詢成員：{e}"
    if in_guild_only:
        return None, "請在伺服器頻道使用此指令。"
    try:
        u = await client.fetch_user(uid)
        return u, None
    except discord.NotFound:
        return None, "找不到此 Discord 使用者。"
    except discord.HTTPException as e:
        return None, f"無法查詢使用者：{e}"


async def interaction_defer_if_needed(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = False,
    thinking: bool = True,
) -> None:
    """先 ACK slash interaction，避免同步 DB 或外部 API 慢時觸發 3 秒逾時。"""
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
    except discord.InteractionResponded:
        pass


async def interaction_send(
    interaction: discord.Interaction,
    *args,
    **kwargs,
):
    """已 defer 的互動走 followup；未回應的互動走原本 response。"""
    if interaction.response.is_done():
        kwargs.setdefault("wait", True)
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)


async def db_to_thread(func: typing.Callable[..., typing.Any], *args, **kwargs):
    """把同步 DB helper 丟到 thread，避免阻塞 discord.py event loop。"""
    return await asyncio.to_thread(func, *args, **kwargs)


async def get_user_stats_async(user_id):
    return await db_to_thread(get_user_stats, user_id)


async def fetch_record_rows_async(user_id, limit: int = 50):
    return await db_to_thread(fetch_record_rows, user_id, limit)


async def fetch_casino_stats_rows_async():
    return await get_casino_stats_rows_cached()


async def fetch_casino_share_stats_rows_async(days: int = 7):
    return await db_to_thread(fetch_casino_share_stats_rows, days)


async def claim_daily_reward_async(user_id, daily_reward: int = 100_000):
    return await db_to_thread(claim_daily_reward, user_id, daily_reward)


async def claim_hourly_reward_async(user_id, reward_per_slot: int = 1000):
    return await db_to_thread(claim_hourly_reward, user_id, reward_per_slot)


async def ensure_user_exists_async(user_id, default_balance=50000):
    return await db_to_thread(ensure_user_exists, user_id, default_balance)


async def get_level_stats_async(user_id):
    return await db_to_thread(get_level_stats, user_id)


async def get_claimed_milestones_async(user_id):
    return await db_to_thread(get_claimed_milestones, user_id)


async def try_deduct_balance_async(user_id, amount, reason):
    return await db_to_thread(try_deduct_balance, user_id, amount, reason)


async def update_game_result_async(user_id, balance_delta, profit_delta, is_win, is_push=False):
    return await db_to_thread(update_game_result, user_id, balance_delta, profit_delta, is_win, is_push)


async def add_user_exp_async(user_id, amount):
    return await db_to_thread(add_user_exp, user_id, amount)


async def credit_balance_with_log_async(user_id, amount, reason):
    return await db_to_thread(credit_balance_with_log, user_id, amount, reason)


async def settle_duel_payouts_with_log_async(challenger_id, opponent_id, a_amt, b_amt, s_a, s_b):
    return await db_to_thread(
        settle_duel_payouts_with_log,
        challenger_id,
        opponent_id,
        a_amt,
        b_amt,
        s_a,
        s_b,
    )


async def load_rob_context_async(robber_id, target_id):
    return await db_to_thread(load_rob_context, robber_id, target_id)


async def apply_rob_success_db_async(robber_id, target_id, now, success_rate_pct):
    return await db_to_thread(
        apply_rob_success_db,
        robber_id,
        target_id,
        now,
        success_rate_pct,
    )


async def apply_rob_fail_db_async(robber_id, target_id, now):
    return await db_to_thread(
        apply_rob_fail_db,
        robber_id,
        target_id,
        now,
    )


async def claim_beg_sync_async(user_id):
    return await db_to_thread(claim_beg_sync, user_id)


async def choose_role_sync_async(user_id, role, now):
    return await db_to_thread(choose_role_sync, user_id, role, now)


async def toggle_good_citizen_sync_async(user_id, now):
    return await db_to_thread(toggle_good_citizen_sync, user_id, now)


async def fetch_good_citizen_rows_sync_async():
    return await get_good_citizen_rows_cached()


async def fetch_wanted_status_row_sync_async(user_id):
    return await get_wanted_status_cached(user_id)


async def fetch_wanted_list_rows_sync_async():
    return await get_wanted_list_rows_cached()


async def pay_bail_sync_async(user_id, now):
    return await db_to_thread(pay_bail_sync, user_id, now)


async def claim_rescue_sync_async(user_id):
    return await db_to_thread(claim_rescue_sync, user_id)


async def wanted_buyout_sync_async(user_id, now):
    return await db_to_thread(wanted_buyout_sync, user_id, now)


async def break_citizen_sync_async(attacker_id, target_id, now):
    return await db_to_thread(break_citizen_sync, attacker_id, target_id, now)


async def transfer_sync_async(sender_id, receiver_id, amount, note_text):
    return await db_to_thread(transfer_sync, sender_id, receiver_id, amount, note_text)


async def fetch_balance_leaderboard_core_async(user_id):
    rows = await get_balance_leaderboard_rows_cached()
    uid = str(user_id)
    my_bal = 0
    global_rank = len(rows) + 1
    for idx, (rid, bal) in enumerate(rows):
        if str(rid) == uid:
            my_bal = int(bal or 0)
            global_rank = idx + 1
            break
    pool = rows[:LEADERBOARD_POOL]
    richer = [(r[0],) for r in rows[: max(0, min(global_rank - 1, LEADERBOARD_RANK_SCAN))]]
    top10 = rows[:10]
    return my_bal, pool, richer, top10, global_rank


async def fetch_level_leaderboard_core_async(user_id):
    rows = await get_level_leaderboard_rows_cached()
    uid = str(user_id)
    my_level, my_exp = 1, 0
    global_rank = len(rows) + 1
    for idx, (rid, lv, exp) in enumerate(rows):
        if str(rid) == uid:
            my_level, my_exp = int(lv or 1), int(exp or 0)
            global_rank = idx + 1
            break
    pool = rows[:LEADERBOARD_POOL]
    richer_lv = [(r[0],) for r in rows[: max(0, min(global_rank - 1, LEADERBOARD_RANK_SCAN))]]
    top10 = rows[:10]
    return my_level, my_exp, pool, richer_lv, top10, global_rank


# ··············································································
# [C · 資料與持久化]
# ··············································································

# ==============================================================================
# 【七】MySQL 與核心業務邏輯
# 連線與資料表初始化；使用者餘額／交易／黑名單／通膨；二十一點與統計；等級與 EXP、
# 里程碑領獎、時薪銀行；排行榜取樣輔助等。
# （二十一點「牌面」介面邏輯在【八】【九】）
# ==============================================================================


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


def fetch_balance_leaderboard_snapshot():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC")
    rows = c.fetchall()
    conn.close()
    return rows


def fetch_level_leaderboard_snapshot():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, level, exp FROM users ORDER BY level DESC, exp DESC")
    rows = c.fetchall()
    conn.close()
    return rows


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

async def process_level_ups(member: typing.Union[discord.Member, discord.User], old_lv: int, new_lv: int):
    if new_lv <= old_lv or getattr(member, "bot", False):
        return
    crossed = [m for m in LEVEL_MILE_TIERS if old_lv < m <= new_lv]
    if not crossed:
        return
    intro = f"從 Lv.{old_lv} 升至 Lv.{new_lv}，**首次**通過本階檻。"
    embed = discord.Embed(title="🎉 等級階段解鎖", description=intro, color=0x57F287)
    reward_lines = []
    flavor_paras: typing.List[str] = []
    for m in crossed:
        coin_amt = LEVEL_MILESTONE_COINS.get(m, 0)
        got = try_claim_milestone(member.id, m, coin_amt)
        # 里程碑內容（致詞 / 幣）僅在首次達成時顯示；但身分組可獨立嘗試補發。
        if got >= 0:
            fl = LEVEL_MILESTONE_FLAVOR.get(m)
            if fl:
                flavor_paras.append(f"**【Lv.{m}】** {fl}")
            if got > 0:
                reward_lines.append(f"🎁 Lv.{m}：+**{got:,}** 東雲幣")
        rid = level_auto_role_id(m)
        g_limit = level_milestone_guild_id()
        if (
            rid
            and isinstance(member, discord.Member)
            and member.guild
            and g_limit is not None
            and member.guild.id == g_limit
        ):
            role = member.guild.get_role(rid)
            if role:
                try:
                    await member.add_roles(role, reason=f"首次達到 Lv.{m} 解鎖（{member.guild.name}）")
                    reward_lines.append(f"🎭 已授予身分組 {role.name}")
                except discord.Forbidden:
                    reward_lines.append(f"⚠️ 無法加上身分組「{role.name}」：請確認 Bot 有**管理身分組**，且 Bot 的**位階**高於該身分組。")
                except discord.HTTPException:
                    reward_lines.append("⚠️ 授予身分組時發生錯誤，稍後可請管理員手動補上。")
            else:
                reward_lines.append(f"⚠️ 找不到 Lv.{m} 對應身分組（ID: {rid}），請確認此 ID 屬於目前伺服器。")
    if not flavor_paras and not reward_lines:
        return
    if flavor_paras:
        embed.add_field(
            name="階段致詞",
            value="\n\n".join(flavor_paras)[:3800],
            inline=False,
        )
    if reward_lines:
        embed.add_field(name="本次獎勵", value="\n".join(reward_lines)[:1000], inline=False)
    try:
        await member.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass

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
    """刪除 logs 超過 retention_days 的資料，回傳刪除筆數。"""
    if retention_days <= 0:
        return 0
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "DELETE FROM logs WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
        (retention_days,),
    )
    removed = int(c.rowcount or 0)
    conn.commit()
    conn.close()
    return removed


def award_vc_rewards_sync(user_ids: typing.Sequence[str], now: datetime.datetime) -> int:
    """依輸入 user_ids 發放語音獎勵（每 30 分鐘一次），回傳本輪發放人數。"""
    if not user_ids:
        return 0
    conn = get_db_connection()
    c = conn.cursor()
    awarded: typing.List[str] = []
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
    conn.commit()
    conn.close()
    return len(awarded)


def process_on_message_activity_sync(
    user_id: str,
    pending_count: int,
    now: datetime.datetime,
    exp_due: bool,
    exp_gain: int,
) -> typing.Dict[str, typing.Any]:
    """處理聊天訊息累積、EXP 發放與聊天活躍獎勵。"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO activity_stats (user_id, msg_count) VALUES (%s, %s) ON DUPLICATE KEY UPDATE msg_count=msg_count+%s",
        (user_id, pending_count, pending_count),
    )
    c.execute("SELECT msg_count, last_msg_reward FROM activity_stats WHERE user_id=%s", (user_id,))
    row = c.fetchone()

    exp_awarded = False
    old_level = None
    new_level = None
    if exp_due:
        ensure_user_exists(user_id, 50000)
        exp_result = add_user_exp(user_id, exp_gain)
        if exp_result:
            old_level, new_level = int(exp_result[0] or 1), int(exp_result[1] or 1)
        c.execute("UPDATE activity_stats SET last_exp_reward=%s WHERE user_id=%s", (now, user_id))
        exp_awarded = True

    msg_rewarded = False
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
    conn.commit()
    conn.close()
    return {
        "exp_awarded": exp_awarded,
        "old_level": old_level,
        "new_level": new_level,
        "msg_rewarded": msg_rewarded,
    }


def parse_tw_datetime(text):
    # 接受格式: YYYY-MM-DD HH:MM (台灣時間 UTC+8)
    dt = datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TW_TZ).replace(tzinfo=None)

def tw_naive_to_discord_ts(dt):
    if not dt:
        return None
    return int(dt.replace(tzinfo=TW_TZ).timestamp())

# ··············································································
# [D · 互動 UI]
# ··············································································

# ==============================================================================
# 【八】遊戲 UI 與互動 View
# 二十一點與 E 卡決鬥已抽離到 bot_modules；此段保留其餘互動 View（如紅包、翻頁）。
# ==============================================================================


# 二十一點 UI／流程已抽離到 bot_modules/commands/blackjack.py

class RedPacketView(discord.ui.View):
    def __init__(self, creator_id, total_amount, count):
        super().__init__(timeout=120)
        global red_packet_seq
        red_packet_seq += 1
        self.packet_id = red_packet_seq
        self.creator_id = creator_id
        self.total_amount = total_amount
        self.count = count
        self.left_amount = total_amount
        self.left_count = count
        self.claimed_users = set()
        self.claim_results = []
        self._claim_lock = asyncio.Lock()

    def summary_text(self):
        claimed = self.count - self.left_count
        return (
            f"🧧 紅包編號 #{self.packet_id}\n"
            f"總金額：`{self.total_amount}` | 份數：`{self.count}`\n"
            f"已搶：`{claimed}` 人 | 剩餘金額：`{self.left_amount}`"
        )

    def winners_text(self):
        if not self.claim_results:
            return "尚未有人搶到紅包。"
        lines = [f"{i+1}. <@{uid}>：`{amt}`" for i, (uid, amt) in enumerate(self.claim_results)]
        return "🎉 搶到紅包名單：\n" + "\n".join(lines)

    @discord.ui.button(label="搶紅包", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction_defer_if_needed(interaction, ephemeral=True, thinking=False)
        async with self._claim_lock:
            if interaction.user.bot:
                return await interaction_send(interaction, "機器人不能搶紅包", ephemeral=True)
            if interaction.user.id in self.claimed_users:
                return await interaction_send(interaction, "你已經搶過這包了", ephemeral=True)
            if self.left_count <= 0 or self.left_amount <= 0:
                return await interaction_send(interaction, "紅包已搶完", ephemeral=True)

            if self.left_count == 1:
                amount = self.left_amount
            else:
                max_pick = self.left_amount - (self.left_count - 1)
                # 非最後一位：單次最多可拿「剩餘金額」的 40%
                non_last_cap = max(1, int(self.left_amount * 0.4))
                capped_max_pick = max(1, min(max_pick, non_last_cap))
                amount = random.randint(1, capped_max_pick)
            self.left_amount -= amount
            self.left_count -= 1
            self.claimed_users.add(interaction.user.id)
            self.claim_results.append((interaction.user.id, amount))

            await credit_balance_with_log_async(interaction.user.id, amount, f"搶紅包 #{self.packet_id}")

            if self.left_count <= 0 or self.left_amount <= 0:
                for child in self.children:
                    child.disabled = True
                try:
                    await interaction.message.edit(
                        content=self.summary_text() + "\n✅ 紅包已被搶完！\n" + self.winners_text(),
                        view=self,
                    )
                except Exception:
                    logger.exception("RedPacketView.claim 結算更新失敗 packet_id=%s", self.packet_id)
                await interaction_send(interaction, f"🎉 你搶到 `{amount}` 東雲幣！", ephemeral=True)
                return
            try:
                await interaction.message.edit(content=self.summary_text(), view=self)
            except Exception:
                logger.exception("RedPacketView.claim 更新摘要失敗 packet_id=%s", self.packet_id)
            await interaction_send(interaction, f"🎉 你搶到 `{amount}` 東雲幣！", ephemeral=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, "message") and self.message:
                await self.message.edit(
                    content=self.summary_text() + "\n⌛ 紅包已逾時關閉。\n" + self.winners_text(),
                    view=self
                )
        except Exception:
            logger.exception("RedPacketView.on_timeout 更新訊息失敗 packet_id=%s", self.packet_id)

class LinePagerView(discord.ui.View):
    def __init__(self, owner_id, title, lines, page_size=10, start_page=1, color=0x2b2d31, footer_prefix=""):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.title = title
        self.lines = lines
        self.page_size = max(1, page_size)
        self.total_pages = max(1, (len(lines) + self.page_size - 1) // self.page_size)
        self.page = max(1, min(start_page, self.total_pages))
        self.color = color
        self.footer_prefix = footer_prefix
        self.message = None
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= self.total_pages

    def build_embed(self):
        start = (self.page - 1) * self.page_size
        end = start + self.page_size
        body = "\n".join(self.lines[start:end]) or "無資料"
        embed = discord.Embed(title=self.title, description=body, color=self.color)
        footer = f"第 {self.page}/{self.total_pages} 頁"
        if self.footer_prefix:
            footer += f" | {self.footer_prefix}"
        embed.set_footer(text=footer)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("這不是你的翻頁面板。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

# ··············································································
# [E · Bot 核心與私訊／頻道轉接]
# ··············································································

# ==============================================================================
# 【十】Intents、開發者診斷指令、Bot 子類別與實例
# 啟用訊息內容與私訊相關意圖；註冊僅限開發者的 /dev_list_guilds；自訂 setup_hook
# 以決定該指令要 guild 註冊或全域；最後建立全域 bot 物件供事件與 Slash 掛載。
# ==============================================================================

intents = discord.Intents.default()
intents.message_content = True
if hasattr(intents, "dm_messages"):
    intents.dm_messages = True


@app_commands.command(
    name="dev_list_guilds",
    description="[開發者] 列出機器人所在的所有伺服器（名稱、成員數、ID）",
)
async def dev_list_guilds_command(interaction: discord.Interaction):
    if interaction.user.id not in ALLOWED_HOST_IDS:
        return await interaction.response.send_message("❌ 僅限開發者使用。", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines: typing.List[str] = []
    for g in sorted(interaction.client.guilds, key=lambda x: (x.name or "").lower()):
        name = g.name or "(無名稱)"
        name_safe = discord.utils.escape_markdown(name)
        try:
            mc = g.member_count
        except Exception:
            mc = None
        if mc is None:
            mc = len(g.members)
        lines.append(
            f"• **{name_safe}** — 成員 `{mc:,}` ｜ ID `{g.id}`"
        )
    if not lines:
        return await interaction.followup.send("目前沒有任何伺服器。", ephemeral=True)
    header = f"**服務中伺服器**（共 `{len(interaction.client.guilds)}` 個）\n"
    parts = _chunk_text_lines(lines)
    first_combined = header + parts[0]
    if len(first_combined) <= DISCORD_MESSAGE_CAP:
        await interaction.followup.send(first_combined, ephemeral=True)
        for p in parts[1:]:
            await interaction.followup.send(p, ephemeral=True)
    else:
        await interaction.followup.send(header.rstrip(), ephemeral=True)
        for p in parts:
            await interaction.followup.send(p, ephemeral=True)
    logger.info(
        "dev_list_guilds: user=%s listed %s guilds",
        interaction.user.id,
        len(interaction.client.guilds),
    )


class ShinonomeBot(commands.Bot):
    async def setup_hook(self) -> None:
        env_override = (os.getenv("DEV_RELAY_GUILD_ID") or "").strip()
        gid: typing.Optional[int] = int(env_override) if env_override else None
        if gid is None:
            try:
                ch = await self.fetch_channel(DM_RELAY_CHANNEL_ID)
                if ch and getattr(ch, "guild", None):
                    gid = ch.guild.id
            except Exception as e:
                logger.warning("無法自動取得轉接頻道所在伺服器（dev_list_guilds 可能以全域註冊）: %s", e)
        try:
            if gid:
                self.tree.add_command(dev_list_guilds_command, guild=discord.Object(id=gid))
                logger.info("dev_list_guilds 僅註冊於伺服器 %s（其他伺服器不會出現此指令）", gid)
            else:
                self.tree.add_command(dev_list_guilds_command)
                logger.warning(
                    "dev_list_guilds 以全域註冊。若要隱藏，請設 DEV_RELAY_GUILD_ID 或確認 bot 能讀取 DM_RELAY_CHANNEL_ID"
                )
        except Exception as e:
            logger.exception("註冊 dev_list_guilds 失敗，改為全域: %s", e)
            self.tree.add_command(dev_list_guilds_command)


bot = ShinonomeBot(command_prefix="!", intents=intents)

# 同一使用者的 Slash 指令互斥鎖：查詢類放行；會改狀態/金流的指令才互斥。
READ_ONLY_APP_COMMANDS: typing.Set[str] = {
    "help",
    "balance",
    "level",
    "record",
    "leaderboard",
    "lvleaderboard",
    "casino_stats",
    "share_stats",
    "wanted_status",
    "wanted_list",
    "good_citizen_list",
    "admin_user_flags",
    "admin_logs",
    "dev_list_guilds",
}
_active_app_command_locks: typing.Dict[int, typing.Tuple[str, float]] = {}
_active_app_command_lock_guard = asyncio.Lock()
APP_COMMAND_LOCK_TIMEOUT_SECONDS = 180.0


@bot.tree.interaction_check
async def enforce_single_active_app_command(interaction: discord.Interaction) -> bool:
    user = getattr(interaction, "user", None)
    if user is None:
        return True
    command_name = getattr(getattr(interaction, "command", None), "name", "") or ""
    if command_name in READ_ONLY_APP_COMMANDS:
        return True
    uid = int(user.id)
    now_ts = time.time()
    lock_group = "mutating"
    async with _active_app_command_lock_guard:
        active = _active_app_command_locks.get(uid)
        started_ts = active[1] if active else None
        if started_ts is not None and (now_ts - started_ts) < APP_COMMAND_LOCK_TIMEOUT_SECONDS:
            await interaction.response.send_message(
                "⏳ 你有一個會變更資料的指令仍在執行中，請稍候再試。",
                ephemeral=True,
            )
            return False
        _active_app_command_locks[uid] = (lock_group, now_ts)
    return True


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command):
    user = getattr(interaction, "user", None)
    if user is None:
        return
    async with _active_app_command_lock_guard:
        _active_app_command_locks.pop(int(user.id), None)


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    user = getattr(interaction, "user", None)
    if user is not None:
        async with _active_app_command_lock_guard:
            _active_app_command_locks.pop(int(user.id), None)
    # 保留既有錯誤輸出，避免吞掉實際問題。
    logger.exception("app command 錯誤: %s", error)

# ==============================================================================
# 【十一】私訊／群組 @ ↔ 管理頻道轉接（Relay）
# 將使用者私訊或群組 @ 機器人轉到固定管理頻道；工作人員在該頻道回覆時，依來源
# 改以「私訊使用者」或「回到原頻道以機器人代發」回應（不洗版、不私訊群組案）。
# ==============================================================================


async def _resolve_relay_from_staff_reply(
    channel: discord.abc.GuildChannel, message: discord.Message
) -> typing.Optional[RelayForwardMeta]:
    """沿回覆鏈找到機器人轉發訊息，回傳完整 relay meta。"""
    ref_id = message.reference.message_id if message.reference and message.reference.message_id else None
    seen: typing.Set[int] = set()
    while ref_id and ref_id not in seen:
        seen.add(ref_id)
        meta = _relay_forward_meta.get(ref_id)
        if meta is not None:
            return meta
        try:
            ref_msg = await channel.fetch_message(ref_id)
        except Exception:
            return None
        parsed = _relay_meta_from_forward_message(ref_msg)
        if parsed is not None:
            _relay_forward_meta[ref_id] = parsed
            return parsed
        if ref_msg.reference and ref_msg.reference.message_id:
            ref_id = ref_msg.reference.message_id
        else:
            return None
    return None


def _relay_meta_from_forward_message(ref_msg: discord.Message) -> typing.Optional[RelayForwardMeta]:
    """從已發送到轉接頻道的 embed 反解析 relay meta（供重啟後快取遺失時使用）。"""
    if bot.user is None or ref_msg.author.id != bot.user.id:
        return None
    if not ref_msg.embeds:
        return None

    emb = ref_msg.embeds[0]
    title = (emb.title or "").strip()
    if not title:
        return None
    allow_private_reply = "私訊轉發" in title

    field_map = {str(f.name): str(f.value) for f in emb.fields}
    sender_text = field_map.get("發送者", "")
    if not sender_text:
        return None

    m_uid = re.search(r"ID\s*`(\d{15,20})`", sender_text)
    if not m_uid:
        m_uid = re.search(r"<@!?(\d{15,20})>", sender_text)
    if not m_uid:
        return None
    target_user_id = int(m_uid.group(1))

    guild_id = 0
    channel_id = 0
    message_id = 0
    if not allow_private_reply:
        loc_text = field_map.get("頻道／原訊", "")
        m_jump = re.search(r"/channels/(\d{15,20})/(\d{15,20})/(\d{15,20})", loc_text)
        if m_jump:
            guild_id = int(m_jump.group(1))
            channel_id = int(m_jump.group(2))
            message_id = int(m_jump.group(3))
        else:
            # 群組回覆至少要拿到原頻道與原訊息 ID 才能代發。
            return None

    return (target_user_id, allow_private_reply, guild_id, channel_id, message_id)


async def relay_user_message_to_staff_channel(message: discord.Message, *, is_dm: bool) -> None:
    """私訊或群組 @ 機器人 -> 轉發到管理頻道。
    管理員回覆：私訊轉發 -> DM；群組 @ 轉發 -> 在原頻道以機器人代發（不 DM）。"""
    ch = bot.get_channel(DM_RELAY_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(DM_RELAY_CHANNEL_ID)
        except Exception as e:
            logger.warning("找不到私訊轉接頻道 %s: %s", DM_RELAY_CHANNEL_ID, e)
            return
    if not isinstance(ch, discord.TextChannel):
        logger.warning("DM_RELAY_CHANNEL_ID 不是文字頻道")
        return
    author = message.author
    text = (message.content or "").strip()
    title = "📩 私訊轉發" if is_dm else "📣 群組 @ 機器人"
    emb = discord.Embed(title=title, color=0x5865F2, timestamp=datetime.datetime.now(datetime.timezone.utc))
    emb.set_author(name=str(author), icon_url=author.display_avatar.url)
    emb.add_field(name="發送者", value=f"<@{author.id}> ｜ ID `{author.id}`", inline=False)
    if not is_dm and message.guild:
        gname = discord.utils.escape_markdown(message.guild.name or "")
        emb.add_field(name="伺服器", value=f"{gname}（`{message.guild.id}`）", inline=False)
        emb.add_field(
            name="頻道／原訊",
            value=f"{message.channel.mention}\n[前往原訊息]({message.jump_url})",
            inline=False,
        )
    emb.description = text[:4096] if text else "（無文字內容）"
    att_urls = [a.url for a in message.attachments[:10]]
    if att_urls:
        emb.add_field(name="附件連結", value="\n".join(att_urls)[:1024], inline=False)
    sticker_names = [str(s.name) for s in message.stickers][:5]
    if sticker_names:
        emb.add_field(name="貼圖", value=", ".join(sticker_names)[:1024], inline=False)
    notify_id = DM_RELAY_NOTIFY_USER_ID
    sent = await ch.send(
        content=f"<@{notify_id}>",
        embed=emb,
        allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=notify_id)]),
    )
    if is_dm:
        _relay_forward_meta[sent.id] = (author.id, True, 0, 0, 0)
    elif message.guild:
        _relay_forward_meta[sent.id] = (
            author.id,
            False,
            message.guild.id,
            message.channel.id,
            message.id,
        )
    else:
        _relay_forward_meta[sent.id] = (author.id, False, 0, 0, 0)


async def _post_staff_reply_to_guild_channel(
    target_user_id: int,
    _guild_id: int,
    channel_id: int,
    original_message_id: int,
    staff_msg: discord.Message,
) -> None:
    """在原伺服器頻道以機器人身分回覆使用者原訊息（不發私訊）。"""
    text = (staff_msg.content or "").strip()
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            raise RuntimeError(f"無法取得頻道：{e}") from e
    if not isinstance(ch, discord.abc.Messageable):
        raise RuntimeError("目標不是可發言頻道")

    ref_msg: typing.Optional[discord.Message] = None
    try:
        ref_msg = await ch.fetch_message(original_message_id)
    except discord.NotFound:
        ref_msg = None

    files_first: typing.List[discord.File] = []
    for att in staff_msg.attachments:
        try:
            data = await att.read()
            files_first.append(discord.File(io.BytesIO(data), filename=att.filename or "attachment"))
        except Exception:
            continue

    if not text and not files_first:
        raise RuntimeError("沒有可發送的文字或附件")

    parts: typing.List[str] = _split_discord_message_chunks(text) if text else [""]

    am = discord.AllowedMentions(users=[discord.Object(id=target_user_id)])
    first_chunk = True
    for idx, part in enumerate(parts):
        use_ref = ref_msg if (first_chunk and ref_msg is not None) else None
        fs = files_first if idx == 0 else []
        if part:
            if first_chunk and ref_msg is None:
                prefix = f"<@{target_user_id}> "
                room = max(0, DISCORD_MESSAGE_CAP - len(prefix))
                body = prefix + part[:room]
            else:
                body = part[:DISCORD_MESSAGE_CAP]
            kwargs: typing.Dict[str, typing.Any] = {"content": body, "allowed_mentions": am}
            if use_ref is not None:
                kwargs["reference"] = use_ref
            if fs:
                kwargs["files"] = fs
            await ch.send(**kwargs)
        elif idx == 0 and fs:
            kwargs = {"allowed_mentions": am}
            if ref_msg is not None:
                kwargs["reference"] = ref_msg
                kwargs["files"] = fs
                await ch.send(**kwargs)
            else:
                await ch.send(
                    content=f"<@{target_user_id}>（附件）",
                    files=fs,
                    allowed_mentions=am,
                )
        first_chunk = False


async def relay_dm_to_staff_channel(message: discord.Message) -> None:
    """使用者私訊機器人 -> 轉發到管理頻道。"""
    await relay_user_message_to_staff_channel(message, is_dm=True)


async def relay_staff_reply_to_dm_user(message: discord.Message) -> bool:
    """管理員在轉接頻道回覆轉發：私訊轉發 -> DM 對方；群組 @ 轉發 -> 在原頻道以機器人代發回覆（不私訊）。"""
    if message.channel.id != DM_RELAY_CHANNEL_ID:
        return False
    resolved = await _resolve_relay_from_staff_reply(message.channel, message)
    if resolved is None:
        return False
    uid, allow_private_reply, og_gid, og_cid, og_mid = resolved
    text = (message.content or "").strip()
    if not text and not message.attachments:
        try:
            await message.add_reaction("❔")
        except Exception:
            pass
        return True

    if not allow_private_reply:
        if not og_cid or not og_mid:
            try:
                await message.reply("❌ 找不到原訊息位置，無法代發到群組。", mention_author=False)
            except Exception:
                pass
            return True
        try:
            await _post_staff_reply_to_guild_channel(uid, og_gid, og_cid, og_mid, message)
        except Exception as e:
            logger.exception("代發群組回覆失敗: %s", e)
            try:
                await message.reply(f"❌ 無法在原頻道代發：{e}", mention_author=False)
            except Exception:
                pass
            return True
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        return True

    try:
        user = await bot.fetch_user(uid)
        for chunk in _split_discord_message_chunks(text):
            await user.send(chunk)
        for att in message.attachments:
            try:
                data = await att.read()
                await user.send(file=discord.File(io.BytesIO(data), filename=att.filename or "file"))
            except Exception:
                await user.send(att.url)
    except discord.Forbidden:
        try:
            await message.reply("❌ 無法私訊該使用者（可能已關閉與機器人的私訊）。", mention_author=False)
        except Exception:
            pass
    except Exception as e:
        logger.exception("轉發管理員回覆到私訊失敗: %s", e)
        try:
            await message.reply(f"❌ 發送私訊失敗：{e}", mention_author=False)
        except Exception:
            pass
    else:
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
    return True


# ··············································································
# [F · 事件迴圈]
# ··············································································

# ==============================================================================
# 【十二】事件迴圈：啟動、語音掛機獎勵、一般訊息
# on_ready：DB 初始化、Slash 同步、掛 Discord 日誌、排程語音通道定期發獎、logs 過期清理。
# on_message：私訊轉接、轉接頻道內工作人員回覆、群組 @ 轉發、聊天句數與 EXP 等。
# ==============================================================================

_event_tasks = register_events(
    bot,
    {
        "logger": logger,
        "now_tw_naive": now_tw_naive,
        "db_to_thread": db_to_thread,
        "award_vc_rewards_sync": award_vc_rewards_sync,
        "purge_old_logs_sync": purge_old_logs_sync,
        "relay_dm_to_staff_channel": relay_dm_to_staff_channel,
        "relay_staff_reply_to_dm_user": relay_staff_reply_to_dm_user,
        "relay_user_message_to_staff_channel": relay_user_message_to_staff_channel,
        "process_on_message_activity_sync": process_on_message_activity_sync,
        "process_level_ups": process_level_ups,
        "cleanup_local_caches": (lambda: cleanup_local_caches()),
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
    },
)


@bot.event
async def on_ready():
    register_discord_log_handler(bot)
    try:
        init_db()
        logger.info("資料庫初始化完成")
    except Exception as e:
        logger.exception("init_db 失敗: %s", e)
    try:
        synced = await bot.tree.sync()
        logger.info("Slash 指令同步完成: %s 個", len(synced))
    except Exception as e:
        logger.exception("Slash 同步失敗: %s", e)
    # 在每個伺服器做 guild sync，讓新指令幾乎即時可用
    for guild in bot.guilds:
        try:
            gsynced = await bot.tree.sync(guild=guild)
            logger.info("Guild 同步完成 %s: %s 個指令", guild.id, len(gsynced))
        except Exception as e:
            logger.exception("Guild 同步失敗 %s: %s", guild.id, e)
    bot.loop.create_task(_event_tasks["vc_reward_task"]())
    bot.loop.create_task(_event_tasks["logs_retention_task"]())
    bot.loop.create_task(_event_tasks["cache_cleanup_task"]())
    bot.loop.create_task(emit_cache_metrics_log_task())
    bot.loop.create_task(refresh_leaderboard_snapshots_task())
    bot.loop.create_task(refresh_casino_stats_snapshot_task())
    logger.info("機器人已啟動: %s（伺服器數 %s）", bot.user, len(bot.guilds))

# ··············································································
# [G · 玩家 Slash 指令]
# ··············································································

# ==============================================================================
# 【十三】Slash 指令：經濟、小遊戲、轉帳、排行榜、管理公告等
# 含每日／每小時簽到、乞討搶劫救濟、21 點、餘額與等級查詢、轉帳、紅包、
# /say、戰報、賭場統計、排行榜（不含「僅主機」後台）。
# ==============================================================================


@bot.tree.command(name="help", description="機器人指令總覽（一般玩家）")
async def help_slash(interaction: discord.Interaction):
    """東雲幣、賭場、通緝／警察、等級等 Slash 說明（不含主機／管理員專用指令）。"""
    emb = discord.Embed(
        title="📖 東雲機器人指令說明",
        description="以下為**一般玩家**常用指令；管理／主機專用請見伺服公告或管理員。",
        color=0x5865F2,
    )
    _cop_hunt_pct_1star = min(
        95,
        COP_HUNT_CAPTURE_BASE_PCT + COP_HUNT_CAPTURE_PER_STAR_PCT,
    )
    emb.add_field(
        name="💰 日常與經濟",
        value=(
            "`/daily` — 每日簽到領幣\n"
            "`/hourly` — 每小時簽到（依等級累積）\n"
            "`/beg` — 乞討\n"
            f"`/rob` — 搶劫（**僅搶匪**；約 **{int(round(ROB_BASE_SUCCESS_RATE * 100))}%** 基礎成功率、每級差 ±1%；成功累積通緝）\n"
            "`/rescue` — 破產救濟（餘額 0 時）\n"
            "`/transfer` — 轉帳給其他玩家\n"
            "`/redpacket` — 發紅包\n"
            "`/record` — 最近帳務紀錄（翻頁）\n"
            "`/balance` — 餘額與戰績"
        ),
        inline=False,
    )
    emb.add_field(
        name="🃏 賭場與等級",
        value=(
            "`/bj` — 二十一點\n"
            "`/duel` — E 卡決鬥（兩大局；第二大局交換陣營；奴贏王 +3、其餘決勝 +1；依積分分配彩池）\n"
            "`/level` — 等級與 EXP\n"
            "`/leaderboard` — 餘額榜前 10\n"
            "`/lvleaderboard` — 等級榜前 10\n"
            "`/casino_stats` — 經濟總金流統計"
        ),
        inline=False,
    )
    emb.add_field(
        name="🚔 通緝與警察",
        value=(
            "`/role_choose` — 選警察／搶匪／平民\n"
            "`/wanted_status` — 自己的通緝、監獄、搶劫紀錄\n"
            "`/wanted_list` — 目前通緝名單與可否追捕\n"
            f"`/good_citizen` — [平民] 付 `{GOOD_CITIZEN_CERT_COST:,}` 啟用防搶；再付同額解除（啟用/解除皆 24h 冷卻）\n"
            "`/good_citizen_list` — 查看目前良民證持有者\n"
            f"`/break_citizen` — 摧毀目標良民證（花費 `{GOOD_CITIZEN_DESTROY_COST:,}`，目標 10 天禁用）\n"
            f"`/cop_hunt` — 警察追捕（僅警察；每次 **`{COP_HUNT_FEE:,}`** 幣、成敗皆扣）。"
            f"成功率 **1★ 約 {_cop_hunt_pct_1star}%** 起，通緝每多 **1** 星 **+{COP_HUNT_CAPTURE_PER_STAR_PCT}%**，並受等級差影響（每級 ±1%，保底 **5%**、上限 **95%**）\n"
            f"`/wanted_buyout` — [搶匪] 付 `{WANTED_BUYOUT_COST:,}` 消除全部通緝星並**清空最近搶劫紀錄**（**24 小時**冷卻）\n"
            f"`/counter_rob` — 已改為平民被搶後**自動反制結算**（約 **{int(round(COUNTER_ROB_BASE_SUCCESS_RATE * 100))}%** 基礎、級差 ±1%）\n"
            f"`/bail` — 入獄繳 **基礎 `{BAIL_COST:,}` + 累計假釋欠款** 出獄"
        ),
        inline=False,
    )
    emb.add_field(
        name="🎮 其他",
        value="`/kill` — Minecraft 風格隨機死法（需選本群成員）",
        inline=False,
    )
    emb.set_footer(text="私訊轉接、群組 @ 機器人可聯繫管理員｜管理員請用 /adminhelp（僅主機）")
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="daily", description="每日簽到領取 100,000 東雲幣")
async def daily(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    result = await claim_daily_reward_async(interaction.user.id)
    if not result["claimed"]:
        ts = result["next_ts"]
        return await interaction_send(
            interaction,
            f"⚠️ 你今天已經簽到過囉！下次簽到時間：<t:{ts}:F> (<t:{ts}:R>)",
            ephemeral=True,
        )

    daily_reward = result["reward"]
    new_bal = result["balance"]
    ts = result["next_ts"]
    embed = discord.Embed(title="✅ 每日簽到成功", color=discord.Color.green())
    embed.add_field(name="獲得", value=f"`{daily_reward:,}` 東雲幣", inline=False)
    embed.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
    embed.add_field(name="下次可領取", value=f"<t:{ts}:f>（<t:{ts}:R>）", inline=False)
    embed.set_footer(text=f"簽到者：{interaction.user.display_name}")
    await interaction_send(interaction, embed=embed)

@bot.tree.command(name="hourly", description="每小時簽到（可依等級累積）")
async def hourly(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    result = await claim_hourly_reward_async(interaction.user.id)
    if not result.get("ok"):
        return await interaction_send(interaction, "資料初始化失敗", ephemeral=True)
    level_num = result["level"]
    if not result["claimed"]:
        mins = result["mins"]
        return await interaction_send(
            interaction,
            f"⏳ 目前尚無可領時段。下次可累積約 `{mins}` 分鐘後。\n"
            f"你目前 Lv.{level_num}，最多可累積 `{level_num}` 小時。",
            ephemeral=True
        )
    bank = result["bank"]
    reward_per_slot = result["reward_per_slot"]
    payout = result["payout"]
    new_bal = result["balance"]
    embed = discord.Embed(title="✅ 每小時簽到成功", color=discord.Color.green())
    embed.add_field(name="累積時段", value=f"`{bank}` 小時（上限 `{level_num}`）", inline=False)
    embed.add_field(name="每小時獎勵", value=f"`{reward_per_slot:,}` 東雲幣", inline=False)
    embed.add_field(name="本次獲得", value=f"`{payout:,}` 東雲幣", inline=False)
    embed.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
    embed.set_footer(text=f"領取者：{interaction.user.display_name}")
    await interaction_send(interaction, embed=embed)

@bot.tree.command(name="beg", description="街頭乞討")
async def beg(interaction: discord.Interaction):
    result = await claim_beg_sync_async(interaction.user.id)
    if not result.get("ok"):
        return await interaction.response.send_message("太快了", ephemeral=True)
    if result.get("fail"):
        return await interaction.response.send_message("沒人鳥你 乞丐")
    earn = int(result.get("earned") or 0)
    return await interaction.response.send_message(f"你獲得了{earn}東雲幣!錢給你啦 乞丐!")

@bot.tree.command(name="rob", description="搶劫其他玩家（僅搶匪；高風險高報酬）")
@app_commands.describe(member="要搶劫的對象（選人）", user_id="或填使用者 ID／貼提及")
async def rob(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    if not FEATURE_TOGGLES.get("rob", True):
        return await interaction.response.send_message("⛔ `/rob` 目前暫時關閉中。", ephemeral=True)
    m_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=True
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if not isinstance(m_user, discord.Member):
        return await interaction.response.send_message("搶劫目標必須是此伺服器成員。", ephemeral=True)
    member = m_user
    if member.bot:
        return await interaction.response.send_message("不能搶劫機器人。", ephemeral=True)
    if member.id == interaction.user.id:
        return await interaction.response.send_message("你不能搶劫自己。", ephemeral=True)

    await ensure_user_exists_async(interaction.user.id, 50000)
    await ensure_user_exists_async(member.id, 0)

    ctx = await load_rob_context_async(interaction.user.id, member.id)
    if ctx["in_prison"]:
        return await interaction.response.send_message("🔒 你在監獄裡無法搶劫。", ephemeral=True)
    if ctx["robber_role"] != "criminal":
        return await interaction.response.send_message(
            "❌ 只有**搶匪**可以搶劫。請先用 `/role_choose` 選擇搶匪（criminal）。",
            ephemeral=True,
        )
    robber_balance = int(ctx["robber_balance"])
    last_rob = ctx["last_rob"]
    robber_level = int(ctx["robber_level"])
    target_balance = int(ctx["target_balance"])
    target_level = int(ctx["target_level"])
    target_last_robbed = ctx["target_last_robbed"]
    target_good_cert = int(ctx["target_good_cert"])
    now = now_tw_naive()

    if last_rob and (now - last_rob).total_seconds() < ROB_COOLDOWN_SECONDS:
        remain = ROB_COOLDOWN_SECONDS - int((now - last_rob).total_seconds())
        mins = max(1, remain // 60)
        return await interaction.response.send_message(f"⏳ 你剛搶過，請再等 `{mins}` 分鐘。", ephemeral=True)

    if target_balance < 50000:
        return await interaction.response.send_message("對方太窮了，沒有東西可以搶。", ephemeral=True)
    if robber_balance < 50000:
        return await interaction.response.send_message("你的餘額低於 50,000，無法發起搶劫。", ephemeral=True)
    if target_good_cert:
        return await interaction.response.send_message(
            "🪪 對方已啟用良民證，無法被搶劫。",
            ephemeral=True,
        )
    if target_last_robbed and (now - target_last_robbed).total_seconds() < ROB_VICTIM_PROTECT_SECONDS:
        remain = ROB_VICTIM_PROTECT_SECONDS - int((now - target_last_robbed).total_seconds())
        mins = max(1, remain // 60)
        return await interaction.response.send_message(
            f"對方目前有保護，請 `{mins}` 分鐘後再試。",
            ephemeral=True,
        )

    await interaction_defer_if_needed(interaction)

    # /rob 基礎成功率 ROB_BASE_SUCCESS_RATE；每差 1 等調整 1%
    level_gap = robber_level - target_level
    success_rate = ROB_BASE_SUCCESS_RATE + (level_gap * 0.01)
    success_rate = max(0.05, min(0.95, success_rate))
    success_rate_pct = int(round(success_rate * 100))
    success = random.random() < success_rate
    robber_name = interaction.user.display_name
    victim_name = member.display_name

    if success:
        success_result = await apply_rob_success_db_async(
            interaction.user.id,
            member.id,
            now,
            success_rate_pct,
        )
        if not success_result.get("ok"):
            return await interaction_send(interaction, "對方及時把錢藏好了，這次搶劫失敗。", ephemeral=True)
        steal_amount = int(success_result["steal_amount"])
        wanted_info = success_result["wanted_info"]
        counter_note = success_result.get("counter_note", "")
        await db_to_thread(log_transaction, interaction.user.id, steal_amount, f"搶劫成功（目標:{member.id}）")
        await db_to_thread(log_transaction, member.id, -steal_amount, f"被搶劫（搶匪:{interaction.user.id}）")
        return await interaction_send(
            interaction,
            f"{robber_name}搶了{victim_name}`{steal_amount:,}`東雲幣!!（本次成功率約 {success_rate_pct}%）{wanted_info}{counter_note}"
        )

    fail_result = await apply_rob_fail_db_async(interaction.user.id, member.id, now)
    fail_penalty = int(fail_result["fail_penalty"])
    deducted = bool(fail_result["deducted"])
    if deducted:
        await db_to_thread(log_transaction, interaction.user.id, -fail_penalty, f"搶劫失敗反噬（目標:{member.id}）")
        await db_to_thread(log_transaction, member.id, fail_penalty, f"反制搶劫獲賠（搶匪:{interaction.user.id}）")
        return await interaction_send(
            interaction,
            f"{robber_name}失手了! 反而被{victim_name}搶了`{fail_penalty:,}`東雲幣!（本次成功率約 {success_rate_pct}%）"
        )
    return await interaction_send(
        interaction,
        f"{robber_name}失手了! 反而被{victim_name}搶了`{fail_penalty:,}`東雲幣!（本次成功率約 {success_rate_pct}%）"
    )


@bot.tree.command(name="role_choose", description="切換陣營：警察／搶匪／平民（24 小時冷卻）")
@app_commands.describe(role="要切換的陣營")
@app_commands.choices(
    role=[
        app_commands.Choice(name="警察", value="cop"),
        app_commands.Choice(name="搶匪", value="criminal"),
        app_commands.Choice(name="平民", value="civilian"),
    ]
)
async def role_choose_slash(interaction: discord.Interaction, role: str):
    if role not in ("cop", "criminal", "civilian"):
        return await interaction.response.send_message(
            "❌ 請從選單選擇 **警察**、**搶匪** 或 **平民**。",
            ephemeral=True,
        )
    now = now_tw_naive()
    role_result = await choose_role_sync_async(interaction.user.id, role, now)
    if not role_result.get("ok"):
        reason = role_result.get("reason")
        if reason == "cert_active":
            return await interaction.response.send_message(
                "❌ 你目前已啟用良民證，無法切換身分。請先使用 `/good_citizen` 解除後再轉職。",
                ephemeral=True,
            )
        if reason == "cooldown":
            ts = tw_naive_to_discord_ts(role_result["next_dt"])
            return await interaction.response.send_message(
                f"⏳ 轉職冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再切換陣營。",
                ephemeral=True,
            )
        if reason == "already_civilian":
            return await interaction.response.send_message("ℹ️ 你目前已是**平民**。", ephemeral=True)
        if reason == "wanted_block":
            wanted_now = int(role_result.get("wanted_now") or 0)
            return await interaction.response.send_message(
                f"❌ 搶匪轉為警察或平民須 **通緝 0 星**（目前 {wanted_now} 星）。請先透過追捕／入獄等流程歸零後再切換。",
                ephemeral=True,
            )
        return await interaction.response.send_message("❌ 轉職失敗，請稍後再試。", ephemeral=True)
    old_role = role_result.get("old_role", "civilian")

    role_name = (
        "🚔 警察"
        if role == "cop"
        else ("🔪 搶匪" if role == "criminal" else "👤 平民")
    )
    old_role_name = (
        "🚔 警察"
        if old_role == "cop"
        else ("🔪 搶匪" if old_role == "criminal" else "👤 平民")
    )
    emb = discord.Embed(
        title="✅ 角色選擇成功",
        description=f"從 {old_role_name} 切換為 {role_name}",
        color=0x57F287,
    )
    if role == "cop":
        emb.add_field(
            name="🚔 警察",
            value=(
                "• 使用 `/cop_hunt` 選擇通緝犯並嘗試逮捕\n"
                f"• 每次追捕會先扣 `{COP_HUNT_FEE:,}`（成敗皆扣）\n"
                f"• 成功率：通緝星級每星 +{COP_HUNT_CAPTURE_PER_STAR_PCT}%，並受雙方等級差影響（每級 ±1%，保底 5%、上限 95%）\n"
                "• 成功可獲得對方最近五次搶劫總額獎金（另依規則沒收）\n"
                "• 每次切換陣營皆有 24 小時冷卻"
            ),
            inline=False,
        )
    elif role == "criminal":
        emb.add_field(
            name="🔪 搶匪",
            value=(
                "• `/rob` 搶劫成功會累積通緝星（最高 5）\n"
                "• 通緝星級越高，且你等級越低於警察時，遭追捕成功率越高\n"
                "• 入獄後可用 `/bail`：基礎假釋金 + 累計欠款（沒收／反搶不足皆會併入）\n"
                "• 轉回警察或平民前，通緝必須先歸零"
            ),
            inline=False,
        )
    else:
        emb.add_field(
            name="👤 平民",
            value=(
                "• 不再以警察／搶匪身分參與通緝與追捕\n"
                "• 可用 `/good_citizen` 啟用／解除良民證（需付費，且有 24 小時冷卻）\n"
                "• 可隨時再用 `/role_choose` 重新選擇陣營（24 小時冷卻）"
            ),
            inline=False,
        )
    await interaction_send(interaction, embed=emb)


@bot.tree.command(
    name="cop_hunt",
    description=f"警察追捕通緝犯（每次須付 {COP_HUNT_FEE:,} 東雲幣；成功可獲贓款、對方入獄）",
)
@app_commands.describe(
    member="通緝犯（選人）",
    user_id="或填使用者 ID／貼提及",
)
async def cop_hunt_slash(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    criminal_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=False
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await interaction_defer_if_needed(interaction)

    await ensure_user_exists_async(interaction.user.id, 50000)
    await ensure_user_exists_async(criminal_user.id, 0)
    criminal_id = str(criminal_user.id)
    cop_id = str(interaction.user.id)

    conn = get_db_connection()
    c = conn.cursor()
    # 固定順序鎖定警察/罪犯兩列，避免多人同時追捕造成讀寫交錯
    lock_user_rows(c, [cop_id, criminal_id])

    c.execute(
        "SELECT COALESCE(role,'civilian'), COALESCE(level,1), COALESCE(balance,0) "
        "FROM users WHERE user_id=%s FOR UPDATE",
        (cop_id,),
    )
    cop_row = c.fetchone()
    if not cop_row or cop_row[0] != "cop":
        conn.close()
        return await interaction_send(
            interaction,
            "❌ 只有**警察**可以追捕。請先用 `/role_choose` 選擇警察。",
            ephemeral=True,
        )
    cop_level = int(cop_row[1] or 1)
    cop_balance = int(cop_row[2] or 0)

    c.execute(
        "SELECT COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0), COALESCE(balance,0), "
        "COALESCE(in_prison,0), COALESCE(level,1), last_five_robs, COALESCE(bail_debt,0) "
        "FROM users WHERE user_id=%s FOR UPDATE",
        (criminal_id,),
    )
    criminal_row = c.fetchone()
    if not criminal_row:
        conn.close()
        return await interaction_send(interaction, "❌ 找不到該玩家資料。", ephemeral=True)

    wanted_stars = int(criminal_row[0] or 0)
    hunted_count = int(criminal_row[1] or 0)
    criminal_balance = int(criminal_row[2] or 0)
    in_prison = int(criminal_row[3] or 0)
    criminal_level = int(criminal_row[4] or 1)
    criminal_last_five_raw = criminal_row[5]
    bail_debt_before = int(criminal_row[6] or 0)

    if in_prison:
        conn.close()
        return await interaction_send(
            interaction,
            f"ℹ️ {criminal_user.mention} 已在監獄中，無法追捕。",
            ephemeral=True,
        )
    if wanted_stars <= 0:
        conn.close()
        return await interaction_send(
            interaction,
            f"ℹ️ {criminal_user.mention} 目前沒有通緝度。",
            ephemeral=True,
        )
    if criminal_id == cop_id:
        conn.close()
        return await interaction_send(interaction, "❌ 不能追捕自己。", ephemeral=True)

    can_hunt = hunted_count == 0
    if wanted_stars <= 4:
        hunt_rule = f"{wanted_stars}★：本星級僅能追捕一次（失敗或成功後需再升星或滿星規則）。"
    else:
        hunt_rule = "5★：每次搶劫成功後可追捕一次（本輪若已追捕過則需等對方再搶劫成功）。"

    if not can_hunt:
        conn.close()
        return await interaction_send(
            interaction,
            f"❌ 目前無法追捕。\n{hunt_rule}",
            ephemeral=True,
        )

    if cop_balance < COP_HUNT_FEE:
        conn.close()
        return await interaction_send(
            interaction,
            f"❌ 每次追捕須支付 **`{COP_HUNT_FEE:,}`** 東雲幣，你的餘額不足。",
            ephemeral=True,
        )
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s",
        (COP_HUNT_FEE, cop_id),
    )

    capture_chance_raw = (
        COP_HUNT_CAPTURE_BASE_PCT
        + wanted_stars * COP_HUNT_CAPTURE_PER_STAR_PCT
        + (cop_level - criminal_level)
    )
    capture_chance = max(5, min(95, capture_chance_raw))
    is_caught = random.random() * 100.0 < float(capture_chance)
    now = now_tw_naive()

    c.execute(
        "INSERT INTO wanted_log (criminal_id, cop_id, wanted_stars, caught) VALUES (%s, %s, %s, %s)",
        (criminal_id, cop_id, wanted_stars, 1 if is_caught else 0),
    )
    log_transaction_in_tx(c, cop_id, -COP_HUNT_FEE, "追捕行動費用")

    if is_caught:
        last_five_total, rob_count = rob_history_total_from_raw(criminal_last_five_raw)
        rob_history: typing.List[typing.Any] = []
        if criminal_last_five_raw:
            try:
                parsed = json.loads(criminal_last_five_raw)
                if isinstance(parsed, list):
                    rob_history = parsed
            except Exception:
                rob_history = []
        cop_reward = int(last_five_total)
        confiscated_base = int(last_five_total * 0.6)
        confiscated_amount = min(confiscated_base, criminal_balance)
        conf_shortfall = max(0, confiscated_base - confiscated_amount)
        remaining_bal = max(0, criminal_balance - confiscated_amount)
        bail_debt_after = bail_debt_before + conf_shortfall
        total_bail_needed = BAIL_COST + bail_debt_after

        c.execute(
            """UPDATE users SET in_prison=1, prison_start=%s,
               balance=GREATEST(0, balance-%s), arrest_count=arrest_count+1,
               wanted_stars=0, wanted_hunted_count=0, last_five_robs=NULL,
               bail_debt=COALESCE(bail_debt,0)+%s
               WHERE user_id=%s""",
            (now, confiscated_amount, conf_shortfall, criminal_id),
        )
        c.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (cop_reward, cop_id),
        )
        log_transaction_in_tx(c, criminal_id, -confiscated_amount, f"被警察逮捕沒收 {confiscated_amount:,}")
        log_transaction_in_tx(c, cop_id, cop_reward, f"逮捕通緝犯 {criminal_user.id} 贓款")
        c.execute(
            """INSERT INTO prison_records
               (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, bail_cost, arrested_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, BAIL_COST, now),
        )
        conn.commit()
        conn.close()

        rob_detail = ""
        if rob_history:
            rob_detail = "\n**最近搶劫紀錄：**\n"
            for i, rob in enumerate(rob_history, 1):
                if isinstance(rob, dict):
                    rob_detail += f"{i}. `{int(rob.get('amount',0)):,}` 幣（{rob.get('time','')}）\n"

        emb = discord.Embed(
            title="✅ 追捕成功",
            description=f"🚔 {interaction.user.mention} 逮捕了 🔪 {criminal_user.mention}",
            color=0x57F287,
        )
        emb.add_field(name="通緝星級", value="⭐" * wanted_stars, inline=True)
        emb.add_field(name="追捕成功率（本輪）", value=f"`{capture_chance}%`", inline=True)
        emb.add_field(
            name="追捕費用（已扣）",
            value=f"`{COP_HUNT_FEE:,}` 東雲幣",
            inline=True,
        )
        emb.add_field(
            name="警察獲得（最近五次搶劫成功總額）",
            value=f"`{cop_reward:,}` 東雲幣（{rob_count} 筆）",
            inline=False,
        )
        emb.add_field(
            name="沒收（近五次贓款總和 60%）",
            value=f"應沒收 `{confiscated_base:,}`｜實扣 `{confiscated_amount:,}` 東雲幣",
            inline=False,
        )
        if conf_shortfall > 0:
            emb.add_field(
                name="未沒收差額（併入假釋債務）",
                value=f"`{conf_shortfall:,}` 東雲幣",
                inline=True,
            )
        emb.add_field(name="罪犯剩餘餘額", value=f"`{remaining_bal:,}` 東雲幣", inline=True)
        if rob_detail:
            emb.add_field(name="搶劫紀錄", value=rob_detail[:1000], inline=False)
        _debt_txt = ""
        if bail_debt_after > 0:
            _debt_txt = f"累計假釋欠款：`{bail_debt_after:,}` 幣"
            if conf_shortfall > 0:
                _debt_txt += f"（本次未沒收 `{conf_shortfall:,}`）"
            _debt_txt += "。\n"
        if bail_debt_after > 0:
            _out_txt = (
                f"出獄請繳：基礎 `{BAIL_COST:,}` + 欠款 `{bail_debt_after:,}` "
                f"= **合計 `{total_bail_needed:,}`** 幣（`/bail`）"
            )
        else:
            _out_txt = f"出獄請繳：`{BAIL_COST:,}` 幣（`/bail`）"
        emb.add_field(
            name="入獄／出獄",
            value="通緝歸零、搶劫紀錄清空。\n" + _debt_txt + _out_txt,
            inline=False,
        )
        await interaction_send(interaction, embed=emb)
        return

    c.execute(
        "UPDATE users SET wanted_hunted_count=1 WHERE user_id=%s",
        (criminal_id,),
    )
    conn.commit()
    conn.close()

    last_five_total, rob_count, rob_history = get_last_five_robs_total(criminal_id)
    rob_detail = ""
    if rob_history:
        rob_detail = "\n**最近搶劫紀錄：**\n"
        for i, rob in enumerate(rob_history, 1):
            if isinstance(rob, dict):
                rob_detail += f"{i}. `{int(rob.get('amount',0)):,}` 幣\n"

    emb = discord.Embed(
        title="❌ 追捕失敗",
        description=f"🔪 {criminal_user.mention} 逃過了 🚔 {interaction.user.mention} 的追捕",
        color=0xED4245,
    )
    emb.add_field(name="通緝星級", value="⭐" * wanted_stars, inline=True)
    emb.add_field(name="本次追捕成功率", value=f"`{capture_chance}%`", inline=True)
    emb.add_field(
        name="追捕費用（已扣）",
        value=f"`{COP_HUNT_FEE:,}` 東雲幣",
        inline=True,
    )
    emb.add_field(
        name="若成功可獲（最近五次搶劫總額）",
        value=f"`{last_five_total:,}` 東雲幣（{rob_count} 筆）",
        inline=False,
    )
    if rob_detail:
        emb.add_field(name="搶劫紀錄", value=rob_detail[:1000], inline=False)
    emb.add_field(name="規則", value=hunt_rule, inline=False)
    if wanted_stars >= 5:
        emb.set_footer(text="對方若再次搶劫成功，你可再追捕一次。")
    else:
        emb.set_footer(text="對方通緝升星後，你可再嘗試追捕。")
    await interaction_send(interaction, embed=emb)


@bot.tree.command(
    name="wanted_buyout",
    description=f"[搶匪] 支付 {WANTED_BUYOUT_COST:,} 東雲幣消除通緝並清空最近搶劫紀錄（24 小時僅能一次）",
)
async def wanted_buyout_slash(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
    # 先以個人可見 defer，避免冷卻/錯誤提示被公開。
    await interaction_defer_if_needed(interaction, ephemeral=True)
    now = now_tw_naive()
    result = await wanted_buyout_sync_async(interaction.user.id, now)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_found":
            return await interaction_send(interaction, "找不到帳號資料。", ephemeral=True)
        if reason == "not_criminal":
            return await interaction_send(interaction, "❌ 僅**搶匪**可使用此指令。", ephemeral=True)
        if reason == "in_prison":
            return await interaction_send(interaction, "❌ 你在監獄中，無法消除通緝。", ephemeral=True)
        if reason == "no_stars":
            return await interaction_send(interaction, "ℹ️ 你目前沒有通緝星。", ephemeral=True)
        if reason == "insufficient":
            bal = int(result.get("balance") or 0)
            return await interaction_send(
                interaction,
                f"❌ 需要 `{WANTED_BUYOUT_COST:,}` 東雲幣，你的餘額不足（目前 `{bal:,}`）。",
                ephemeral=True,
            )
        if reason == "cooldown":
            ts = tw_naive_to_discord_ts(result["next_dt"])
            return await interaction_send(
                interaction,
                f"⏳ 通緝買斷冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再使用。",
                ephemeral=True,
            )
        return await interaction_send(interaction, "扣款失敗（餘額不足）。", ephemeral=True)
    new_bal = int(result["new_balance"])
    stars_was = int(result["stars_was"])
    cost = int(WANTED_BUYOUT_COST)
    emb = discord.Embed(
        title="✅ 通緝買斷成功（頻道公告）",
        description=(
            f"{interaction.user.mention} 支付 **`{cost:,}`** 東雲幣，"
            f"原通緝 **{stars_was}** 星已消除，追捕計數已歸零，**最近搶劫紀錄已清空**。"
        ),
        color=0x57F287,
    )
    emb.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
    _am = discord.AllowedMentions(users=[discord.Object(id=interaction.user.id)])
    await interaction_send(interaction, embed=emb, ephemeral=False, allowed_mentions=_am)


@bot.tree.command(name="good_citizen", description="良民證：支付 5,000 萬啟用防搶；再支付 5,000 萬解除（兩者皆 24h 冷卻）")
async def good_citizen_slash(interaction: discord.Interaction):
    now = now_tw_naive()
    result = await toggle_good_citizen_sync_async(interaction.user.id, now)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_found":
            return await interaction.response.send_message("找不到帳號資料。", ephemeral=True)
        if reason == "not_civilian":
            return await interaction.response.send_message(
                "❌ 良民證僅限 **平民** 使用；請先用 `/role_choose` 切換為平民。",
                ephemeral=True,
            )
        if reason == "broken_lock":
            ts = tw_naive_to_discord_ts(result["until"])
            return await interaction.response.send_message(
                f"❌ 你的良民證已被摧毀，需等到 <t:{ts}:F>（<t:{ts}:R>）後才能再次啟用。",
                ephemeral=True,
            )
        if reason == "cooldown":
            ts = tw_naive_to_discord_ts(result["next_dt"])
            return await interaction.response.send_message(
                f"⏳ 良民證冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再操作。",
                ephemeral=True,
            )
        if reason == "insufficient":
            bal = int(result.get("balance") or 0)
            return await interaction.response.send_message(
                f"❌ 需要 `{GOOD_CITIZEN_CERT_COST:,}` 東雲幣，你的餘額不足（目前 `{bal:,}`）。",
                ephemeral=True,
            )
        if reason == "deduct_failed":
            return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
        return await interaction.response.send_message("❌ 良民證操作失敗，請稍後再試。", ephemeral=True)

    next_active = int(result["next_active"])
    new_bal = int(result["new_balance"])
    title = "✅ 良民證已啟用" if next_active else "✅ 良民證已解除"
    status_txt = "已啟用（不可被搶劫）" if next_active else "已解除（可被搶劫）"
    emb = discord.Embed(title=title, color=0x57F287 if next_active else 0xFEE75C)
    emb.add_field(name="本次花費", value=f"`{GOOD_CITIZEN_CERT_COST:,}` 東雲幣", inline=False)
    emb.add_field(name="目前狀態", value=status_txt, inline=False)
    emb.add_field(name="目前餘額", value=f"`{new_bal:,}` 東雲幣", inline=False)
    emb.set_footer(text="啟用與解除皆有 24 小時冷卻")
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="good_citizen_list", description="查看目前啟用良民證的玩家清單")
async def good_citizen_list_slash(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
    rows = await fetch_good_citizen_rows_sync_async()
    if not rows:
        return await interaction.response.send_message("目前沒有啟用良民證的玩家。", ephemeral=True)
    guild = interaction.guild
    lines: typing.List[str] = []
    for uid_str, bal_raw, _last_action in rows:
        try:
            mid = int(uid_str)
        except (TypeError, ValueError):
            continue
        mem = guild.get_member(mid)
        if mem is None:
            try:
                mem = await guild.fetch_member(mid)
            except Exception:
                mem = None
        disp = mem.display_name if mem else "未知成員"
        disp_safe = discord.utils.escape_markdown(disp)
        bal = int(bal_raw or 0)
        lines.append(f"• {disp_safe}（<@{mid}>）｜餘額 `{bal:,}`")
    if not lines:
        return await interaction.response.send_message("目前沒有啟用良民證的玩家。", ephemeral=True)
    emb = discord.Embed(
        title="🪪 良民證持有者名單",
        description="\n".join(lines)[:3900],
        color=0x57F287,
    )
    emb.set_footer(text=f"共 {len(lines)} 人")
    await interaction.response.send_message(embed=emb, ephemeral=False)


@bot.tree.command(name="break_citizen", description="摧毀目標良民證（花費 5 億；目標 10 天內無法再取得）")
@app_commands.describe(member="目標玩家（選人）", user_id="或填使用者 ID／貼提及")
async def break_citizen_slash(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    target_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=False
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if target_user.id == interaction.user.id:
        return await interaction.response.send_message("❌ 不能對自己使用。", ephemeral=True)
    now = now_tw_naive()
    result = await break_citizen_sync_async(interaction.user.id, target_user.id, now)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "insufficient":
            attacker_bal = int(result.get("balance") or 0)
            return await interaction.response.send_message(
                f"❌ 需要 `{GOOD_CITIZEN_DESTROY_COST:,}` 東雲幣，你的餘額不足（目前 `{attacker_bal:,}`）。",
                ephemeral=True,
            )
        if reason == "target_not_found":
            return await interaction.response.send_message("找不到目標資料。", ephemeral=True)
        if reason == "target_not_active":
            target_broken_until = result.get("target_broken_until")
            if target_broken_until and now < target_broken_until:
                ts = tw_naive_to_discord_ts(target_broken_until)
                return await interaction.response.send_message(
                    f"ℹ️ 目標目前未啟用良民證，且已被封鎖至 <t:{ts}:F>（<t:{ts}:R>）。",
                    ephemeral=True,
                )
            return await interaction.response.send_message("ℹ️ 目標目前沒有啟用良民證。", ephemeral=True)
        return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
    broken_until = result["broken_until"]
    ts = tw_naive_to_discord_ts(broken_until)
    emb = discord.Embed(
        title="💥 良民證已摧毀",
        description=(
            f"{interaction.user.mention} 花費 **`{GOOD_CITIZEN_DESTROY_COST:,}`** 東雲幣，"
            f"摧毀了 {target_user.mention} 的良民證。"
        ),
        color=0xED4245,
    )
    emb.add_field(
        name="封鎖時間",
        value=f"目標於 <t:{ts}:F>（<t:{ts}:R>）前無法再啟用良民證",
        inline=False,
    )
    _am = discord.AllowedMentions(users=[discord.Object(id=interaction.user.id), discord.Object(id=target_user.id)])
    await interaction.response.send_message(embed=emb, ephemeral=False, allowed_mentions=_am)


@bot.tree.command(name="wanted_status", description="查看自己的陣營、通緝、監獄狀態與最近搶劫紀錄")
async def wanted_status_slash(interaction: discord.Interaction):
    row = await fetch_wanted_status_row_sync_async(interaction.user.id)
    if not row:
        return await interaction.response.send_message("找不到資料。", ephemeral=True)
    role, stars, hunted, in_pr, raw_hist, arrests, rev_pend, rev_amt, bail_debt_u, cert_active = row
    role_s = role or "civilian"
    stars_i = int(stars or 0)
    hunted_i = int(hunted or 0)
    in_pr_i = int(in_pr or 0)
    arrests_i = int(arrests or 0)

    role_disp = {"cop": "🚔 警察", "criminal": "🔪 搶匪"}.get(role_s, "👤 平民")
    emb = discord.Embed(title="📋 通緝／監獄狀態", color=0x5865F2)
    emb.add_field(name="陣營", value=role_disp, inline=True)
    emb.add_field(name="通緝星", value=("⭐" * stars_i + "☆" * (5 - stars_i)) if stars_i <= 5 else str(stars_i), inline=True)
    emb.add_field(name="本輪可追捕", value="否（已嘗試）" if hunted_i else "是", inline=True)
    emb.add_field(name="監獄", value="🔒 在押" if in_pr_i else "否", inline=True)
    emb.add_field(name="累計被捕次數", value=str(arrests_i), inline=True)
    if role_s == "civilian":
        emb.add_field(
            name="良民證",
            value="🪪 已啟用（不可被搶）" if int(cert_active or 0) else "未啟用（可被搶）",
            inline=True,
        )
    if int(rev_pend or 0) and (role or "civilian") == "civilian":
        emb.add_field(
            name="加倍搶回",
            value=(
                "已改為**自動反制**：平民被搶成功後會立即自動判定，不需手動輸入指令。"
            ),
            inline=False,
        )

    hist_lines = ""
    if raw_hist:
        try:
            h = json.loads(raw_hist)
            if isinstance(h, list) and h:
                for i, item in enumerate(h[-5:], 1):
                    if isinstance(item, dict):
                        hist_lines += f"{i}. `{int(item.get('amount',0)):,}` — {item.get('time','')}\n"
        except Exception:
            hist_lines = "（紀錄格式異常）"
    emb.add_field(
        name="最近搶劫成功紀錄（最多五筆）",
        value=hist_lines[:1000] if hist_lines else "（無）",
        inline=False,
    )
    _bd = int(bail_debt_u or 0)
    _total_out = BAIL_COST + _bd
    emb.set_footer(
        text=(
            f"出獄須繳：基礎 `{BAIL_COST:,}`"
            + (f" + 欠款 `{_bd:,}` = 合計 `{_total_out:,}`" if _bd else "")
            + " 幣｜/bail"
        )
    )
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="wanted_list", description="列出目前通緝中玩家（不含 0 星），並顯示可否被追捕")
async def wanted_list_slash(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
    rows = await fetch_wanted_list_rows_sync_async()
    if not rows:
        return await interaction.response.send_message(
            "目前沒有通緝中的玩家（僅顯示通緝星 1～5 星）。",
            ephemeral=True,
        )
    guild = interaction.guild
    lines: typing.List[str] = []
    for uid_str, stars, hunted, in_pr, raw_hist in rows:
        stars_i = int(stars or 0)
        if stars_i <= 0:
            continue
        hunted_i = int(hunted or 0)
        in_pr_i = int(in_pr or 0)
        try:
            mid = int(uid_str)
        except (TypeError, ValueError):
            continue
        mem = guild.get_member(mid)
        if mem is None:
            try:
                mem = await guild.fetch_member(mid)
            except Exception:
                mem = None
        disp = mem.display_name if mem else "未知成員"
        disp_safe = discord.utils.escape_markdown(disp)
        star_s = "⭐" * min(stars_i, 5)
        bounty, bounty_count = rob_history_total_from_raw(raw_hist)
        bounty_txt = f"`{bounty:,}` 東雲幣（{bounty_count} 筆）"
        if in_pr_i:
            hunt_txt = "在獄中（無法被追捕）"
        elif hunted_i:
            hunt_txt = "本輪已追捕（待升星或搶匪再搶成功後才可再追）"
        else:
            hunt_txt = "**可追捕**"
        lines.append(f"• {disp_safe}（<@{mid}>）｜{star_s}｜可獲獎金 {bounty_txt}｜{hunt_txt}")
    if not lines:
        return await interaction.response.send_message(
            "目前沒有通緝中的玩家（僅顯示通緝星 1～5 星）。",
            ephemeral=True,
        )
    body = "\n".join(lines)[:3900]
    emb = discord.Embed(
        title="📣 通緝名單",
        description=body,
        color=0xED4245,
    )
    emb.set_footer(text="警察請用 /cop_hunt 指定對象｜0 星不會出現在此清單")
    await interaction.response.send_message(embed=emb, ephemeral=False)


@bot.tree.command(
    name="counter_rob",
    description="（相容保留）平民反制已改為被搶成功後自動結算",
)
async def counter_rob_slash(interaction: discord.Interaction):
    return await interaction.response.send_message(
        "ℹ️ 平民反制已改為被搶成功後**自動觸發**，不需手動使用 `/counter_rob`。",
        ephemeral=True,
    )


@bot.tree.command(name="bail", description=f"繳納假釋金（基礎 {BAIL_COST:,} + 累計欠款）出獄")
async def bail_slash(interaction: discord.Interaction):
    now = now_tw_naive()
    result = await pay_bail_sync_async(interaction.user.id, now)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_in_prison":
            return await interaction.response.send_message("你不在監獄裡。", ephemeral=True)
        if reason == "insufficient":
            debt = int(result.get("debt") or 0)
            total_bail = int(result.get("total_bail") or BAIL_COST)
            return await interaction.response.send_message(
                f"假釋須繳 **基礎 `{BAIL_COST:,}`**"
                + (f" + **欠款 `{debt:,}`**" if debt else "")
                + f" = **合計 `{total_bail:,}`** 東雲幣，你的餘額不足。",
                ephemeral=True,
            )
        return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
    debt = int(result["debt"])
    total_bail = int(result["total_bail"])
    await interaction.response.send_message(
        f"✅ 已繳納 **`{total_bail:,}`** 東雲幣（基礎 `{BAIL_COST:,}`"
        + (f" + 清償欠款 `{debt:,}`" if debt else "")
        + "），你已出獄。",
        ephemeral=True,
    )


@bot.tree.command(name="rescue", description="破產救濟計畫，餘額為 0 元時可領 1,000 (每人限領 10 次)")
async def rescue(interaction: discord.Interaction):
    result = await claim_rescue_sync_async(interaction.user.id)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_bankrupt":
            bal = int(result.get("balance") or 0)
            return await interaction.response.send_message(
                f"💰 還沒破產（餘額: {bal}），請這位賭狗先去賭到傾家蕩產！完全歸零時再來領。",
                ephemeral=True,
            )
        if reason == "limit_reached":
            return await interaction.response.send_message(
                "🚫 抱歉，你的救濟次數已達 10 次上限。這輩子不能再領了，賭鬼！",
                ephemeral=True,
            )
        if reason == "cooldown":
            rem = int(result.get("remain_sec") or 0)
            return await interaction.response.send_message(f"🕒 銀行還不想給你錢！請再等 `{int(rem//60)}` 分鐘。", ephemeral=True)
        return await interaction.response.send_message("暫時無法領取救濟，請稍後再試。", ephemeral=True)
    rescue_reward = int(result["reward"])
    claim_no = int(result["claim_no"])
    embed = discord.Embed(title="✅ 破產救濟發放", color=discord.Color.green())
    embed.add_field(name="獲得", value=f"`{rescue_reward:,}` 東雲幣", inline=False)
    embed.add_field(name="累計次數", value=f"`{claim_no}/10`", inline=False)
    embed.set_footer(text="請謹慎下注，避免再次破產")
    await interaction.response.send_message(embed=embed)

# /bj 已抽離到 bot_modules/commands/blackjack.py

@bot.tree.command(name="balance", description="查詢個人的戰績與餘額")
@app_commands.describe(member="要查詢的成員（選填）", user_id="或填對方使用者 ID（選填）")
async def balance(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    resolved, err = await resolve_slash_target(
        interaction, member, user_id, required=False, in_guild_only=False
    )
    if err:
        return await interaction_send(interaction, err, ephemeral=True)
    await interaction_defer_if_needed(interaction)
    target: typing.Union[discord.Member, discord.User] = resolved or interaction.user
    if target.id == interaction.user.id:
        await ensure_user_exists_async(target.id, 50000)
    else:
        await ensure_user_exists_async(target.id, 0)
    stats = await get_user_stats_async(target.id)
    bal, total, wins, t_prof = stats
    wr = (wins/total*100) if total > 0 else 0
    embed = discord.Embed(title="📊 帳戶統計", color=0x2b2d31)
    av = target.display_avatar.url if getattr(target, "display_avatar", None) else None
    embed.set_author(name=target.display_name, icon_url=av)
    embed.add_field(name="目前餘額", value=f"`{bal:,}` 東雲幣", inline=False)
    embed.add_field(name="總遊玩局數", value=f"`{total:,}` 局", inline=False)
    embed.add_field(name="勝利場次", value=f"`{wins:,}` 場", inline=False)
    embed.add_field(name="勝率", value=f"`{wr:.1f}%`", inline=False)
    embed.add_field(name="歷史總盈虧", value=f"`{t_prof:,}` 東雲幣", inline=False)
    await interaction_send(interaction, embed=embed)

@bot.tree.command(name="level", description="查詢等級與經驗值")
@app_commands.describe(member="要查詢的成員（選填）", user_id="或填對方使用者 ID（選填）")
async def level(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    resolved, err = await resolve_slash_target(
        interaction, member, user_id, required=False, in_guild_only=False
    )
    if err:
        return await interaction_send(interaction, err, ephemeral=True)
    await interaction_defer_if_needed(interaction)
    target: typing.Union[discord.Member, discord.User] = resolved or interaction.user
    if target.id == interaction.user.id:
        await ensure_user_exists_async(target.id, 50000)
    else:
        await ensure_user_exists_async(target.id, 0)
    lv_row = await get_level_stats_async(target.id)
    exp = int(lv_row[0] or 0)
    level_num = int(lv_row[1] or 1)
    calc_lv, cur_progress, next_need = calc_level_from_exp(exp)
    level_num = max(level_num, calc_lv)
    claimed = await get_claimed_milestones_async(target.id)
    bar = build_exp_progress_bar(cur_progress, next_need) if level_num < MAX_LEVEL else "▓" * 12 + "  100%"
    need_more = (next_need - cur_progress) if level_num < MAX_LEVEL and next_need > 0 else 0
    ms_lines = []
    for m, coins in sorted(LEVEL_MILESTONE_COINS.items()):
        if m in claimed:
            ms_lines.append(f"**Lv.{m}** — ✅ 已領 {coins:,} 幣")
        elif level_num < m:
            ms_lines.append(f"**Lv.{m}** — 💰 達到 Lv.{m} 領 {coins:,} 幣")
        else:
            ms_lines.append(f"**Lv.{m}** — 已達此級（獎勵僅在**升級當下首次跨過**時發放，不事後補發）")
    emb = discord.Embed(title="🏅 等級與經驗", color=0x2b2d31)
    av = target.display_avatar.url if getattr(target, "display_avatar", None) else None
    emb.set_author(name=target.display_name, icon_url=av)
    if level_num >= MAX_LEVEL:
        emb.description = f"{target.mention} **Lv.{level_num}**（已滿級）\n✨ 總 EXP：`{exp:,}`"
    else:
        emb.description = (
            f"{target.mention} **Lv.{level_num}**\n"
            f"✨ 總 EXP：`{exp:,}`\n"
            f"📈 本級累積 **{cur_progress:,} / {next_need:,}** EXP，尚餘 **{need_more:,}** EXP 可升 Lv.{level_num + 1}\n"
            f"`{bar}`"
        )
    emb.add_field(
        name="階段里程碑 Lv.20／40／60／80／100",
        value="\n".join(ms_lines) or "未設定",
        inline=False,
    )
    await interaction_send(interaction, embed=emb)

@bot.tree.command(name="transfer", description="轉帳給其他玩家")
@app_commands.describe(
    member="收款人（選人，選填若已填 user_id）",
    user_id="或填收款人使用者 ID",
    amount="轉帳金額",
    note="備註（選填）",
)
async def transfer(
    interaction: discord.Interaction,
    amount: int,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
    note: str = "",
):
    m_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=True
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if not isinstance(m_user, discord.Member):
        return await interaction.response.send_message("轉帳對象必須是此伺服器成員。", ephemeral=True)
    member = m_user
    if amount <= 0:
        return await interaction.response.send_message("金額必須大於 0", ephemeral=True)
    if member.bot:
        return await interaction.response.send_message("不能轉帳給機器人", ephemeral=True)
    if member.id == interaction.user.id:
        return await interaction.response.send_message("不能轉帳給自己", ephemeral=True)
    note_text = (note or "").strip()
    if len(note_text) > 100:
        note_text = note_text[:100]
    result = await transfer_sync_async(interaction.user.id, member.id, amount, note_text)
    if not result.get("ok"):
        return await interaction.response.send_message("餘額不足，無法轉帳", ephemeral=True)
    sender_after = int(result["sender_after"])
    receiver_after = int(result["receiver_after"])

    now_text = now_tw_naive().strftime("%Y/%m/%d %H:%M:%S")

    embed = discord.Embed(
        title="✅ 轉帳成功",
        color=discord.Color.green()
    )
    embed.add_field(
        name="匯款方",
        value=(
            f"{interaction.user.mention}\n"
            f"轉帳後餘額：`{sender_after:,}` 東雲幣"
        ),
        inline=False
    )
    embed.add_field(
        name="收款方",
        value=(
            f"{member.mention}\n"
            f"收款後餘額：`{receiver_after:,}` 東雲幣"
        ),
        inline=False
    )
    embed.add_field(
        name="轉帳金額",
        value=f"`{amount:,}` 東雲幣",
        inline=False
    )
    embed.add_field(
        name="轉帳備註",
        value=note_text if note_text else "（無）",
        inline=False
    )
    embed.set_footer(text=f"交易時間：{now_text}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="redpacket", description="發送紅包！")
@app_commands.describe(total_amount="紅包總金額", count="份數", seconds="有效秒數(最少10秒)")
async def redpacket(interaction: discord.Interaction, total_amount: int, count: int, seconds: int = 60):
    if not FEATURE_TOGGLES.get("redpacket", True):
        return await interaction.response.send_message("⛔ `/redpacket` 目前暫時關閉中。", ephemeral=True)
    await ensure_user_exists_async(interaction.user.id, 50000)
    if total_amount < count or total_amount <= 0:
        return await interaction.response.send_message("總金額需大於 0，且至少要能每包 1 元。", ephemeral=True)
    if count < 1 or count > 100:
        return await interaction.response.send_message("份數需介於 1 到 100。", ephemeral=True)
    if not await try_deduct_balance_async(interaction.user.id, total_amount, "發送紅包扣款"):
        return await interaction.response.send_message("餘額不足，無法發紅包", ephemeral=True)
    timeout_seconds = max(RED_PACKET_MIN_SECONDS, seconds)
    view = RedPacketView(interaction.user.id, total_amount, count)
    view.timeout = timeout_seconds
    await interaction.response.send_message(
        f"{interaction.user.mention} 發了一個紅包！\n{view.summary_text()}\n"
        f"⏰ 有效時間：`{timeout_seconds}` 秒",
        view=view
    )
    try:
        view.message = await interaction.original_response()
    except Exception:
        logger.exception("redpacket: 無法取得 original_response 供 timeout 編輯使用")

# E 卡決鬥指令／流程已抽離到 bot_modules/commands/duel.py


@bot.tree.command(name="kill", description="在目前頻道送出 Minecraft 風格隨機死法")
@app_commands.describe(target="目標（選人）", user_id="或填使用者 ID／貼提及")
async def kill(
    interaction: discord.Interaction,
    target: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    m_user, err = await resolve_slash_target(
        interaction, target, user_id, required=True, in_guild_only=True
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if not isinstance(m_user, discord.Member):
        return await interaction.response.send_message("目標必須是此伺服器成員。", ephemeral=True)
    target = m_user
    template = random.choice(MINECRAFT_DEATH_MESSAGES)
    item = random.choice(MINECRAFT_ITEMS)
    target_text = target.mention
    msg = (
        template.format(target=target_text)
        .replace("<死者>", target_text)
        .replace("死者", target_text)
        .replace("擊殺者", interaction.user.mention)
        .replace("击杀者", interaction.user.mention)
        .replace("物品", item)
    )
    await interaction.response.send_message(msg)


@bot.tree.command(name="bicycle", description="嘗試偷走奈音的腳踏車")
async def bicycle_slash(interaction: discord.Interaction):
    thief_id = str(interaction.user.id)
    target_id = "1027248561177509919"
    steal_amount = 100
    now = now_tw_naive()
    await ensure_user_exists_async(interaction.user.id, 50000)
    await ensure_user_exists_async(int(target_id), 0)

    conn = get_db_connection()
    c = conn.cursor()
    lock_user_rows(c, [thief_id, target_id])
    c.execute("SELECT last_bicycle FROM users WHERE user_id=%s", (thief_id,))
    row = c.fetchone()
    last_bicycle = row[0] if row else None
    if last_bicycle and (now - last_bicycle).total_seconds() < BICYCLE_COOLDOWN_SECONDS:
        conn.close()
        remain = BICYCLE_COOLDOWN_SECONDS - int((now - last_bicycle).total_seconds())
        ts = int((now + datetime.timedelta(seconds=remain)).replace(tzinfo=TW_TZ).timestamp())
        return await interaction.response.send_message(
            f"⏳ /bicycle 冷卻中，請於 <t:{ts}:R> 再試。",
            ephemeral=True,
        )

    # 99% 成功
    success = random.random() < 0.99
    if not success:
        c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
        conn.commit()
        conn.close()
        return await interaction.response.send_message("小黑龜再練練 連個腳踏車都偷不走")

    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (steal_amount, target_id, steal_amount),
    )
    if c.rowcount > 0:
        c.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (steal_amount, thief_id),
        )
        c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
        log_transaction_in_tx(c, target_id, -steal_amount, f"腳踏車被偷（偷車者:{thief_id}）")
        log_transaction_in_tx(c, thief_id, steal_amount, f"偷走奈音腳踏車（目標:{target_id}）")
        conn.commit()
        conn.close()
        return await interaction.response.send_message("你成功偷走了奈音的腳踏車!")

    c.execute("UPDATE users SET last_bicycle=%s WHERE user_id=%s", (now, thief_id))
    conn.commit()
    conn.close()
    return await interaction.response.send_message("奈音身上沒錢了! 沒有腳踏車能偷!")

@bot.tree.command(name="say", description="[管理員] 指定機器人對特定頻道發送內容")
@app_commands.describe(text="你要機器人說什麼？", channel="指定發送到哪個頻道？(選填)")
@app_commands.default_permissions(manage_messages=True)
async def say_slash(interaction: discord.Interaction, text: str, channel: discord.TextChannel = None):
    if interaction.user.id not in ALLOWED_HOST_IDS:
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    target_channel = channel or interaction.channel
    await target_channel.send(text)
    await interaction.response.send_message(f"✅ 訊息已發送到 {target_channel.mention}！", ephemeral=True)

@bot.tree.command(name="record", description="最近紀錄（最多 50 筆，10 筆一頁）")
@app_commands.describe(member="要查詢的玩家（選填）", user_id="或填對方使用者 ID（選填）", page="頁碼（每頁 10 筆）")
async def record_cmd(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
    page: int = 1,
):
    resolved, err = await resolve_slash_target(
        interaction, member, user_id, required=False, in_guild_only=False
    )
    if err:
        return await interaction_send(interaction, err, ephemeral=True)
    await interaction_defer_if_needed(interaction, ephemeral=True)
    target: typing.Union[discord.Member, discord.User] = resolved or interaction.user
    rows = await fetch_record_rows_async(target.id, 50)
    if not rows:
        return await interaction_send(interaction, "無紀錄", ephemeral=True)

    page_size = 10
    page = max(1, int(page))
    all_lines = []
    for i, r in enumerate(rows):
        time_text = r[2].strftime('%m/%d %H:%M') if r[2] else "N/A"
        all_lines.append(f"{i+1}. [{time_text}] {r[1]}: `{r[0]}`")
    view = LinePagerView(
        owner_id=interaction.user.id,
        title=f"📒 {target.display_name} 的最近紀錄",
        lines=all_lines,
        page_size=page_size,
        start_page=page,
        footer_prefix=f"共 {len(rows)} 筆（最多顯示 50）"
    )
    sent = await interaction_send(interaction, embed=view.build_embed(), view=view)
    try:
        view.message = sent or await interaction.original_response()
    except Exception:
        logger.exception("record: 無法取得 original_response 供翻頁更新使用")


async def _is_user_in_guild(guild: discord.Guild, user_id: int) -> bool:
    """以快取或 API 確認使用者是否仍在該伺服器。"""
    key = (int(guild.id), int(user_id))
    now_ts = time.time()
    cached = _guild_member_cache.get(key)
    if cached and (now_ts - cached[0]) < GUILD_MEMBER_CACHE_SECONDS:
        async with _metrics_lock:
            _metrics_counters["guild_member_hit"] += 1
        return bool(cached[1])
    if guild.get_member(user_id):
        _guild_member_cache[key] = (now_ts, True)
        async with _metrics_lock:
            _metrics_counters["guild_member_miss"] += 1
        return True
    async with _guild_member_cache_lock:
        now_ts = time.time()
        cached = _guild_member_cache.get(key)
        if cached and (now_ts - cached[0]) < GUILD_MEMBER_CACHE_SECONDS:
            async with _metrics_lock:
                _metrics_counters["guild_member_hit"] += 1
            return bool(cached[1])
        try:
            await guild.fetch_member(user_id)
            result = True
        except discord.NotFound:
            result = False
        except Exception:
            result = False
        _guild_member_cache[key] = (now_ts, result)
        async with _metrics_lock:
            _metrics_counters["guild_member_miss"] += 1
        return result


LEADERBOARD_POOL = 400
LEADERBOARD_RANK_SCAN = 800
LEADERBOARD_CACHE_SECONDS = 30.0
LEADERBOARD_SNAPSHOT_SECONDS = 30.0
WANTED_CACHE_SECONDS = 10.0
GOOD_CITIZEN_CACHE_SECONDS = 15.0
GUILD_MEMBER_CACHE_SECONDS = 20.0
CASINO_STATS_CACHE_SECONDS = 60.0
CASINO_STATS_SNAPSHOT_SECONDS = 60.0
METRICS_LOG_INTERVAL_SECONDS = 120
_lb_balance_cache: typing.Optional[typing.Tuple[float, typing.List[typing.Tuple]]] = None
_lb_level_cache: typing.Optional[typing.Tuple[float, typing.List[typing.Tuple]]] = None
_lb_cache_lock = asyncio.Lock()
_casino_stats_cache: typing.Optional[typing.Tuple[float, typing.Tuple[int, int, int]]] = None
_casino_stats_cache_lock = asyncio.Lock()
_leaderboard_snapshot_ready = asyncio.Event()
_casino_stats_snapshot_ready = asyncio.Event()
_guild_member_cache: typing.Dict[typing.Tuple[int, int], typing.Tuple[float, bool]] = {}
_guild_member_cache_lock = asyncio.Lock()
_wanted_list_cache: typing.Optional[typing.Tuple[float, typing.List[typing.Tuple]]] = None
_good_citizen_cache: typing.Optional[typing.Tuple[float, typing.List[typing.Tuple]]] = None
_wanted_status_cache: typing.Dict[str, typing.Tuple[float, typing.Any]] = {}
_wanted_cache_lock = asyncio.Lock()
_metrics_counters: typing.Dict[str, int] = {
    "wanted_status_hit": 0,
    "wanted_status_miss": 0,
    "wanted_list_hit": 0,
    "wanted_list_miss": 0,
    "good_citizen_hit": 0,
    "good_citizen_miss": 0,
    "guild_member_hit": 0,
    "guild_member_miss": 0,
}
_metrics_lock = asyncio.Lock()


async def get_balance_leaderboard_rows_cached() -> typing.List[typing.Tuple]:
    global _lb_balance_cache
    now_ts = time.time()
    cached = _lb_balance_cache
    if cached:
        return cached[1]
    if not _leaderboard_snapshot_ready.is_set():
        try:
            await asyncio.wait_for(_leaderboard_snapshot_ready.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        cached = _lb_balance_cache
        if cached:
            return cached[1]
    async with _lb_cache_lock:
        now_ts = time.time()
        cached = _lb_balance_cache
        if cached:
            return cached[1]
        rows = await db_to_thread(fetch_balance_leaderboard_snapshot)
        _lb_balance_cache = (now_ts, rows)
        _leaderboard_snapshot_ready.set()
        return rows


async def get_level_leaderboard_rows_cached() -> typing.List[typing.Tuple]:
    global _lb_level_cache
    now_ts = time.time()
    cached = _lb_level_cache
    if cached:
        return cached[1]
    if not _leaderboard_snapshot_ready.is_set():
        try:
            await asyncio.wait_for(_leaderboard_snapshot_ready.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        cached = _lb_level_cache
        if cached:
            return cached[1]
    async with _lb_cache_lock:
        now_ts = time.time()
        cached = _lb_level_cache
        if cached:
            return cached[1]
        rows = await db_to_thread(fetch_level_leaderboard_snapshot)
        _lb_level_cache = (now_ts, rows)
        if _lb_balance_cache is not None:
            _leaderboard_snapshot_ready.set()
        return rows


async def get_casino_stats_rows_cached() -> typing.Tuple[int, int, int]:
    global _casino_stats_cache
    now_ts = time.time()
    cached = _casino_stats_cache
    if cached:
        return cached[1]
    if not _casino_stats_snapshot_ready.is_set():
        try:
            await asyncio.wait_for(_casino_stats_snapshot_ready.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        cached = _casino_stats_cache
        if cached:
            return cached[1]
    async with _casino_stats_cache_lock:
        now_ts = time.time()
        cached = _casino_stats_cache
        if cached:
            return cached[1]
        rows = await db_to_thread(fetch_casino_stats_rows)
        _casino_stats_cache = (now_ts, rows)
        _casino_stats_snapshot_ready.set()
        return rows


async def refresh_leaderboard_snapshots_task():
    """背景更新排行榜，讓 /leaderboard 與 /lvleaderboard 只讀記憶體快照。"""
    global _lb_balance_cache, _lb_level_cache
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now_ts = time.time()
            balance_rows, level_rows = await asyncio.gather(
                db_to_thread(fetch_balance_leaderboard_snapshot),
                db_to_thread(fetch_level_leaderboard_snapshot),
            )
            async with _lb_cache_lock:
                _lb_balance_cache = (now_ts, balance_rows)
                _lb_level_cache = (now_ts, level_rows)
                _leaderboard_snapshot_ready.set()
        except Exception as e:
            logger.exception("刷新排行榜背景快照失敗: %s", e)
        await asyncio.sleep(LEADERBOARD_SNAPSHOT_SECONDS)


async def refresh_casino_stats_snapshot_task():
    """背景更新經濟總金流統計，避免查詢指令直接掃大表。"""
    global _casino_stats_cache
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            rows = await db_to_thread(fetch_casino_stats_rows)
            async with _casino_stats_cache_lock:
                _casino_stats_cache = (time.time(), rows)
                _casino_stats_snapshot_ready.set()
        except Exception as e:
            logger.exception("刷新 casino_stats 背景快照失敗: %s", e)
        await asyncio.sleep(CASINO_STATS_SNAPSHOT_SECONDS)


async def get_wanted_list_rows_cached() -> typing.List[typing.Tuple]:
    global _wanted_list_cache
    now_ts = time.time()
    cached = _wanted_list_cache
    if cached and (now_ts - cached[0]) < WANTED_CACHE_SECONDS:
        async with _metrics_lock:
            _metrics_counters["wanted_list_hit"] += 1
        return cached[1]
    async with _wanted_cache_lock:
        now_ts = time.time()
        cached = _wanted_list_cache
        if cached and (now_ts - cached[0]) < WANTED_CACHE_SECONDS:
            async with _metrics_lock:
                _metrics_counters["wanted_list_hit"] += 1
            return cached[1]
        rows = await db_to_thread(fetch_wanted_list_rows_sync)
        _wanted_list_cache = (now_ts, rows)
        async with _metrics_lock:
            _metrics_counters["wanted_list_miss"] += 1
        return rows


async def get_good_citizen_rows_cached() -> typing.List[typing.Tuple]:
    global _good_citizen_cache
    now_ts = time.time()
    cached = _good_citizen_cache
    if cached and (now_ts - cached[0]) < GOOD_CITIZEN_CACHE_SECONDS:
        async with _metrics_lock:
            _metrics_counters["good_citizen_hit"] += 1
        return cached[1]
    async with _wanted_cache_lock:
        now_ts = time.time()
        cached = _good_citizen_cache
        if cached and (now_ts - cached[0]) < GOOD_CITIZEN_CACHE_SECONDS:
            async with _metrics_lock:
                _metrics_counters["good_citizen_hit"] += 1
            return cached[1]
        rows = await db_to_thread(fetch_good_citizen_rows_sync)
        _good_citizen_cache = (now_ts, rows)
        async with _metrics_lock:
            _metrics_counters["good_citizen_miss"] += 1
        return rows


async def get_wanted_status_cached(user_id: int):
    key = str(user_id)
    now_ts = time.time()
    cached = _wanted_status_cache.get(key)
    if cached and (now_ts - cached[0]) < WANTED_CACHE_SECONDS:
        async with _metrics_lock:
            _metrics_counters["wanted_status_hit"] += 1
        return cached[1]
    async with _wanted_cache_lock:
        now_ts = time.time()
        cached = _wanted_status_cache.get(key)
        if cached and (now_ts - cached[0]) < WANTED_CACHE_SECONDS:
            async with _metrics_lock:
                _metrics_counters["wanted_status_hit"] += 1
            return cached[1]
        row = await db_to_thread(fetch_wanted_status_row_sync, user_id)
        _wanted_status_cache[key] = (now_ts, row)
        async with _metrics_lock:
            _metrics_counters["wanted_status_miss"] += 1
        return row


def cleanup_local_caches() -> None:
    """清理過期快取，避免無界成長。"""
    now_ts = time.time()
    for store, ttl in (
        (_guild_member_cache, GUILD_MEMBER_CACHE_SECONDS),
        (_wanted_status_cache, WANTED_CACHE_SECONDS),
    ):
        expired_keys = [k for k, (ts, _v) in store.items() if (now_ts - ts) >= ttl]
        for k in expired_keys:
            store.pop(k, None)


async def emit_cache_metrics_log_task():
    """每段時間輸出快取命中率，方便觀察優化成效。"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(METRICS_LOG_INTERVAL_SECONDS)
        async with _metrics_lock:
            snap = dict(_metrics_counters)
            for k in _metrics_counters.keys():
                _metrics_counters[k] = 0

        def _ratio(hit_key: str, miss_key: str) -> str:
            hit = int(snap.get(hit_key, 0))
            miss = int(snap.get(miss_key, 0))
            total = hit + miss
            if total <= 0:
                return "n/a"
            return f"{(hit * 100.0 / total):.1f}% ({hit}/{total})"

        logger.info(
            "cache-metrics 120s | wanted_status=%s | wanted_list=%s | good_citizen=%s | guild_member=%s",
            _ratio("wanted_status_hit", "wanted_status_miss"),
            _ratio("wanted_list_hit", "wanted_list_miss"),
            _ratio("good_citizen_hit", "good_citizen_miss"),
            _ratio("guild_member_hit", "guild_member_miss"),
        )


@bot.tree.command(name="leaderboard", description="前 10 名")
async def leaderboard(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    await ensure_user_exists_async(interaction.user.id, 50000)
    my_bal, pool, richer, top10, global_rank = await fetch_balance_leaderboard_core_async(interaction.user.id)
    snapshot_age = time.time() - _lb_balance_cache[0] if _lb_balance_cache else None
    data = top10
    my_rank = global_rank
    title = "🏆 排行榜（全站）"
    note = "\n\n※ 已改為全站榜單，不再限制當前伺服器成員。"

    lines = [f"{i+1}. <@{uid}>: {int(bal):,}" for i, (uid, bal) in enumerate(data)]
    msg = "\n".join(lines) if lines else "（尚無符合條件的成員）"
    msg += f"\n\n📍 你的目前名次：**#{my_rank}**（餘額 `{my_bal:,}`）{note}"
    emb = discord.Embed(title=title, description=msg)
    if snapshot_age is not None:
        emb.set_footer(text=f"背景快照約 {int(snapshot_age)} 秒前更新")
    await interaction_send(interaction, embed=emb)

@bot.tree.command(name="casino_stats", description="查看經濟總金流統計（回收率/總發幣量/流通量）")
async def casino_stats(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    total_issued, total_recovered, circulation = await get_casino_stats_rows_cached()
    snapshot_age = time.time() - _casino_stats_cache[0] if _casino_stats_cache else None

    recovery_rate = (total_recovered / total_issued * 100) if total_issued > 0 else 0.0
    net_issued = total_issued - total_recovered
    embed = discord.Embed(title="🏦 經濟總金流統計", color=0x2b2d31)
    embed.add_field(name="金錢回收率", value=f"`{recovery_rate:.2f}%`", inline=False)
    embed.add_field(name="總發幣量", value=f"`{total_issued:,}` 東雲幣", inline=False)
    embed.add_field(name="總回收量", value=f"`{total_recovered:,}` 東雲幣", inline=False)
    embed.add_field(name="淨發行量", value=f"`{net_issued:,}` 東雲幣", inline=False)
    embed.add_field(name="目前流通量", value=f"`{circulation:,}` 東雲幣", inline=False)
    footer = "計算基準：casino_logs（logs 全期鏡像總帳）與 users.balance"
    if snapshot_age is not None:
        footer += f"｜背景快照約 {int(snapshot_age)} 秒前更新"
    embed.set_footer(text=footer)
    await interaction_send(interaction, embed=embed)


@bot.tree.command(name="share_stats", description="查看賭場回收分潤統計（管理）")
@app_commands.describe(days="近幾天統計（預設 7 天）")
async def share_stats(interaction: discord.Interaction, days: int = 7):
    if interaction.user.id not in ALLOWED_HOST_IDS:
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    await interaction_defer_if_needed(interaction, ephemeral=True)
    total, recent, by_reason = await fetch_casino_share_stats_rows_async(days)
    embed = discord.Embed(title="📊 賭場回收分潤統計", color=0x5865F2)
    embed.add_field(name="分潤功能", value="啟用" if CASINO_RECOVERY_SHARE_ENABLED else "停用", inline=True)
    embed.add_field(name="分潤比例", value=f"`{CASINO_RECOVERY_SHARE_RATE * 100:.2f}%`", inline=True)
    embed.add_field(name="分潤目標", value=f"<@{CASINO_RECOVERY_SHARE_TARGET_ID}>", inline=True)
    embed.add_field(name="累計分潤總額", value=f"`{total:,}` 東雲幣", inline=False)
    embed.add_field(name=f"近 {max(1, int(days))} 天分潤", value=f"`{recent:,}` 東雲幣", inline=False)
    if by_reason:
        lines: typing.List[str] = []
        for reason, amt in by_reason:
            amt_i = int(amt or 0)
            rs = str(reason or "未知來源")
            lines.append(f"• {rs}：`{amt_i:,}`")
        embed.add_field(name="來源明細（累計）", value="\n".join(lines)[:1024], inline=False)
    await interaction_send(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="lvleaderboard", description="等級排行榜前 10 名")
async def lvleaderboard(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    await ensure_user_exists_async(interaction.user.id, 50000)
    my_level, my_exp, pool, richer_lv, top10, global_rank = await fetch_level_leaderboard_core_async(interaction.user.id)
    snapshot_age = time.time() - _lb_level_cache[0] if _lb_level_cache else None
    data = top10
    my_rank = global_rank
    title = "🧠 Lv 排行榜（全站）"
    note = "\n\n※ 已改為全站榜單，不再限制當前伺服器成員。"

    if not data:
        return await interaction_send(interaction, "目前沒有符合條件的等級資料。", ephemeral=True)

    msg = "\n".join(
        [f"{i+1}. <@{uid}>: Lv.{int(lv)} | EXP {int(exp):,}" for i, (uid, lv, exp) in enumerate(data)]
    )
    msg += f"\n\n📍 你的目前名次：**#{my_rank}**（Lv.{my_level} | EXP {my_exp:,}）{note}"
    emb = discord.Embed(title=title, description=msg)
    if snapshot_age is not None:
        emb.set_footer(text=f"背景快照約 {int(snapshot_age)} 秒前更新")
    await interaction_send(interaction, embed=emb)

# 相關指令區段已精簡

register_admin_commands(
    bot,
    {
        "ALLOWED_HOST_IDS": ALLOWED_HOST_IDS,
        "MAX_LEVEL": MAX_LEVEL,
        "LEVEL_MILE_TIERS": LEVEL_MILE_TIERS,
        "FEATURE_TOGGLES": FEATURE_TOGGLES,
        "resolve_slash_target": resolve_slash_target,
        "ensure_user_exists": ensure_user_exists,
        "ensure_user_exists_async": ensure_user_exists_async,
        "get_level_stats": get_level_stats,
        "exp_required_for_level": exp_required_for_level,
        "process_level_ups": process_level_ups,
        "get_db_connection": get_db_connection,
        "logger": logger,
        "log_transaction": log_transaction,
        "credit_balance_with_log_async": credit_balance_with_log_async,
        "try_deduct_balance_async": try_deduct_balance_async,
        "calc_level_from_exp": calc_level_from_exp,
        "TW_TZ": TW_TZ,
        "get_is_event_active": get_is_event_active,
        "set_is_event_active": set_is_event_active,
        "get_share_enabled": get_share_enabled,
        "set_share_enabled": set_share_enabled,
    },
)

register_blackjack_commands(
    bot,
    {
        "logger": logger,
        "FEATURE_TOGGLES": FEATURE_TOGGLES,
        "get_is_event_active": get_is_event_active,
        "SIDE_BET_RATIO": SIDE_BET_RATIO,
        "LEVEL_MILE_TIERS": LEVEL_MILE_TIERS,
        "ensure_user_exists_async": ensure_user_exists_async,
        "get_user_stats_async": get_user_stats_async,
        "try_deduct_balance_async": try_deduct_balance_async,
        "update_game_result_async": update_game_result_async,
        "add_user_exp_async": add_user_exp_async,
        "process_level_ups": process_level_ups,
        "roll_gamble_exp_from_bet": roll_gamble_exp_from_bet,
        "interaction_send": interaction_send,
        "interaction_defer_if_needed": interaction_defer_if_needed,
    },
)

register_duel_commands(
    bot,
    {
        "interaction_send": interaction_send,
        "interaction_defer_if_needed": interaction_defer_if_needed,
        "ensure_user_exists_async": ensure_user_exists_async,
        "try_deduct_balance_async": try_deduct_balance_async,
        "credit_balance_with_log_async": credit_balance_with_log_async,
        "settle_duel_payouts_with_log_async": settle_duel_payouts_with_log_async,
        "FEATURE_TOGGLES": FEATURE_TOGGLES,
    },
)

# ··············································································
# [H · 主機後台與程式進入點]
# ··············································································

# ==============================================================================
# 【十五】Slash：主機後台、長文分行工具與程式進入點
# 限 ALLOWED_HOST_IDS 的 /give、/ban、全服重置等；_chunk_text_lines 供多則訊息列表
# 排版；最後以 DISCORD_TOKEN 啟動 bot。
# ==============================================================================


def _chunk_text_lines(lines: typing.List[str], max_len: int = 1900) -> typing.List[str]:
    chunks: typing.List[str] = []
    buf: typing.List[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if buf else 0)
        if buf and size + add > max_len:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            if buf:
                size += 1
            buf.append(line)
            size += len(line)
    if buf:
        chunks.append("\n".join(buf))
    return chunks



bot.run(os.getenv('DISCORD_TOKEN'))