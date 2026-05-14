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
from urllib.parse import urlparse

import discord
import pymysql
import threading
from dbutils.pooled_db import PooledDB
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# 容器環境（Railway 等）若未使用 python -u，預設 stdout 會緩衝，部署日誌像「卡住」
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass

# ------------------------------------------------------------------------------
# bot.py 大區索引（細節仍見各段「【數字】」標題）
#
#   [A · 基礎]           【一】匯入 【二】日誌 【三】常數／靜態資料／轉接表
#   [B · Discord 工具]   【四】Logging Handler 【五】等級里程碑 【六】Slash 共用工具
#   [C · 資料與持久化]   【七】MySQL／連線池／init_db／使用者與錦標賽資料存取
#   [D · 二十一點與 UI]  【八】牌組與結算 【九】Modal／View／按鈕／翻頁
#   [E · Bot 與轉接]     【十】Intents／Bot 【十一】私訊與頻道 Relay
#   [F · 事件迴圈]       【十二】on_ready／語音獎勵／logs 清理／聊天／刪除訊息紀錄
#   [G · 玩家 Slash]     【十三】經濟與一般 【十四】錦標賽
#   [H · 主機與進入點]   【十五】/give 等與 bot.run
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
ALLOWED_HOST_IDS = [
    531308526262550528,
    600177596088582185,
    1027248561177509919,
    1309551323682701444,
]  # ⚠️ 填入你的 Discord ID
SIDE_BET_RATIO = 0.5                     # 側注上限 (主注的 50%)
IS_EVENT_ACTIVE = True                   # 賭場狀態
MAX_LEVEL = 100
# Discord 單則訊息字元上限（與 API 一致）
DISCORD_MESSAGE_CAP = 2000
EXP_COOLDOWN_SECONDS = 45
# 發話經驗 = random(12,20) × 此倍數（冷卻不變）
CHAT_EXP_MULTIPLIER = 3
# 每完成一局 21 點結算時加發的隨機 EXP（見 roll_gamble_exp_from_bet）
GAMBLE_EXP_MIN = 12
GAMBLE_EXP_MAX = 38
RED_PACKET_MIN_SECONDS = 10
ROB_COOLDOWN_SECONDS = 60
ROB_VICTIM_PROTECT_SECONDS = 3600
# /rob 專用：基礎成功率；每 1 級差距 ±1%，再 clamp 至 5%～95%
ROB_BASE_SUCCESS_RATE = 0.60
# 平民 `/counter_rob` 加倍搶回專用基礎機率（與 `/rob` 分開）。
COUNTER_ROB_BASE_SUCCESS_RATE = 0.30
red_packet_seq = 0
MSG_DB_FLUSH_EVERY_SECONDS = 8
MSG_DB_FLUSH_COUNT = 3
# logs 流水表：只保留最近 N 天，排程定期刪除更早資料（與 MySQL session 時區一致）
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "14"))
LOG_PURGE_INTERVAL_SECONDS = int(os.getenv("LOG_PURGE_INTERVAL_SECONDS", str(24 * 3600)))
# 台灣時間 (UTC+8)；與 get_db_connection 的 MySQL session time_zone 一致
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 新用戶預設起始金（ensure_user_exists 預設）
DEFAULT_STARTUP_BALANCE = 50_000
REASON_USER_INITIAL_BALANCE = "帳號建立初始資金"


def now_tw_naive() -> datetime.datetime:
    """目前台灣本地時間（naive datetime）。"""
    return datetime.datetime.now(TW_TZ).replace(tzinfo=None)


MINECRAFT_DEATH_MESSAGES_PATH = os.path.join(
    os.path.dirname(__file__),
    "data",
    "minecraft_death_messages_zh_tw.json",
)
MINECRAFT_ITEMS_PATH = os.path.join(
    os.path.dirname(__file__),
    "data",
    "minecraft_items_zh_tw.json",
)
DEFAULT_MINECRAFT_DEATH_MESSAGES = [
    "{target} 死了",
    "{target} 在嘗試與地形理論辯論時失敗了",
    "{target} 以為自己能扛住這一下",
]

def load_minecraft_death_messages() -> typing.List[str]:
    try:
        with open(MINECRAFT_DEATH_MESSAGES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        msgs = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(msgs, list):
            return DEFAULT_MINECRAFT_DEATH_MESSAGES[:]
        cleaned = [str(x).strip() for x in msgs if isinstance(x, str) and x.strip()]
        return cleaned if cleaned else DEFAULT_MINECRAFT_DEATH_MESSAGES[:]
    except Exception as e:
        print(f"⚠️ 載入 Minecraft 死法 JSON 失敗: {e}")
        return DEFAULT_MINECRAFT_DEATH_MESSAGES[:]

MINECRAFT_DEATH_MESSAGES = load_minecraft_death_messages()
DEFAULT_MINECRAFT_ITEMS = [
    "鑽石劍", "下界合金劍", "弓", "弩", "三叉戟", "鐵斧", "鑽石斧", "終界水晶",
    "TNT 炸藥", "火焰彈", "烈焰棒", "不死圖騰", "附魔金蘋果", "終界珍珠", "地獄石",
    "黑曜石", "床", "熔岩桶", "水桶", "鐵砧", "盾牌", "雪球", "雞蛋", "釣魚竿",
    "鵝卵石", "鑽石鎬", "下界之星", "煙火火箭", "歌萊果", "苦力怕頭顱"
]

def load_minecraft_items() -> typing.List[str]:
    try:
        with open(MINECRAFT_ITEMS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return DEFAULT_MINECRAFT_ITEMS[:]
        cleaned = [str(x).strip() for x in items if isinstance(x, str) and x.strip()]
        return cleaned if cleaned else DEFAULT_MINECRAFT_ITEMS[:]
    except Exception as e:
        print(f"⚠️ 載入 Minecraft 物品 JSON 失敗: {e}")
        return DEFAULT_MINECRAFT_ITEMS[:]

MINECRAFT_ITEMS = load_minecraft_items()
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


async def ensure_user_exists_async(user_id, startup_balance=DEFAULT_STARTUP_BALANCE):
    return await db_to_thread(ensure_user_exists, user_id, startup_balance)


async def get_user_stats_async(user_id):
    return await db_to_thread(get_user_stats, user_id)


async def fetch_record_rows_async(user_id, limit: int = 50):
    return await db_to_thread(fetch_record_rows, user_id, limit)


async def fetch_casino_stats_rows_async():
    return await db_to_thread(fetch_casino_stats_rows)


async def claim_daily_reward_async(user_id, daily_reward: int = 100_000):
    return await db_to_thread(claim_daily_reward, user_id, daily_reward)


async def claim_hourly_reward_async(user_id, reward_per_slot: int = 1000):
    return await db_to_thread(claim_hourly_reward, user_id, reward_per_slot)


async def get_level_stats_async(user_id):
    return await db_to_thread(get_level_stats, user_id)


async def get_claimed_milestones_async(user_id):
    return await db_to_thread(get_claimed_milestones, user_id)


async def try_deduct_balance_async(user_id, amount, reason):
    return await db_to_thread(try_deduct_balance, user_id, amount, reason)


# ··············································································
# [C · 資料與持久化]
# ··············································································

# ==============================================================================
# 【七】MySQL 與核心業務邏輯
# 連線與資料表初始化；使用者餘額／交易／黑名單／通膨；二十一點與統計；等級與 EXP、
# 里程碑領獎、時薪銀行；錦標賽資料結構與晉級；排行榜取樣輔助等。
# （二十一點「牌面」介面邏輯在【八】【九】）
# ==============================================================================

_mysql_pool: typing.Optional[PooledDB] = None
_mysql_pool_lock = threading.Lock()


def _mysql_connect_kwargs() -> dict:
    """供連線池建立時傳入 pymysql.connect 的參數。"""
    mysql_url = os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL")
    if mysql_url:
        parsed = urlparse(mysql_url)
        if parsed.scheme.startswith("mysql"):
            return {
                "host": parsed.hostname,
                "port": parsed.port or 3306,
                "user": parsed.username,
                "password": parsed.password,
                "database": (parsed.path or "/").lstrip("/"),
                "charset": "utf8mb4",
                "init_command": "SET time_zone = '+08:00'",
            }
    return {
        "host": os.getenv("MYSQLHOST") or os.getenv("DB_HOST"),
        "port": int(os.getenv("MYSQLPORT") or os.getenv("DB_PORT", 3306)),
        "user": os.getenv("MYSQLUSER") or os.getenv("DB_USER"),
        "password": os.getenv("MYSQLPASSWORD") or os.getenv("DB_PASS"),
        "database": os.getenv("MYSQLDATABASE") or os.getenv("DB_NAME"),
        "charset": "utf8mb4",
        "init_command": "SET time_zone = '+08:00'",
    }


def _get_mysql_pool() -> PooledDB:
    global _mysql_pool
    if _mysql_pool is None:
        with _mysql_pool_lock:
            if _mysql_pool is None:
                kw = _mysql_connect_kwargs()
                _mysql_pool = PooledDB(
                    creator=pymysql,
                    mincached=int(os.getenv("MYSQL_POOL_MINCACHED", "1")),
                    maxcached=int(os.getenv("MYSQL_POOL_MAXCACHED", "8")),
                    maxconnections=int(os.getenv("MYSQL_POOL_MAX", "16")),
                    blocking=True,
                    ping=1,
                    **kw,
                )
    return _mysql_pool


def get_db_connection():
    """從連線池取得連線；用完請 commit／rollback 並 close（會歸還池中）。"""
    return _get_mysql_pool().connection()


def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id VARCHAR(255) PRIMARY KEY, balance BIGINT, rescue_count INT DEFAULT 0,
                  total_games INT DEFAULT 0, wins INT DEFAULT 0, total_profit BIGINT DEFAULT 0,
                  last_work TIMESTAMP NULL, last_beg TIMESTAMP NULL, last_rescue TIMESTAMP NULL, last_rob TIMESTAMP NULL, last_robbed TIMESTAMP NULL,
                  exp BIGINT DEFAULT 0, level INT DEFAULT 1,
                  last_hourly_claim TIMESTAMP NULL, hourly_bank INT DEFAULT 0,
                  good_citizen_cert_active TINYINT(1) DEFAULT 0, last_good_citizen_cert_action TIMESTAMP NULL,
                  good_citizen_cert_broken_until TIMESTAMP NULL)''')
    # 確保現有表也有新欄位 (Migration)
    try: c.execute("ALTER TABLE users ADD COLUMN last_work TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_beg TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_rescue TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_rob TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_robbed TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN exp BIGINT DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN level INT DEFAULT 1")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN last_hourly_claim TIMESTAMP NULL")
    except: pass
    try: c.execute("ALTER TABLE users ADD COLUMN hourly_bank INT DEFAULT 0")
    except: pass
    # 警察／搶匪／通緝／監獄
    try:
        c.execute("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'civilian'")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN wanted_stars INT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN wanted_hunted_count INT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_five_robs TEXT NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN in_prison TINYINT(1) DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN prison_start TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN arrest_count INT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN revenge_pending TINYINT(1) DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN revenge_robber_id VARCHAR(255) NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN revenge_amount BIGINT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_wanted_buyout TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_role_change TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN bail_debt BIGINT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN good_citizen_cert_active TINYINT(1) DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_good_citizen_cert_action TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN good_citizen_cert_broken_until TIMESTAMP NULL")
    except Exception:
        pass

    c.execute(
        """CREATE TABLE IF NOT EXISTS wanted_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        criminal_id VARCHAR(255) NOT NULL,
        cop_id VARCHAR(255) NOT NULL,
        wanted_stars INT NOT NULL,
        caught TINYINT(1) DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS prison_records (
        id INT AUTO_INCREMENT PRIMARY KEY,
        criminal_id VARCHAR(255) NOT NULL,
        cop_id VARCHAR(255) NOT NULL,
        wanted_stars INT NOT NULL,
        confiscated_amount BIGINT NOT NULL,
        cop_reward BIGINT NOT NULL,
        bail_cost BIGINT DEFAULT 100000,
        arrested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        released_at TIMESTAMP NULL
        )"""
    )

    c.execute('''CREATE TABLE IF NOT EXISTS activity_stats 
                 (user_id VARCHAR(255) PRIMARY KEY, msg_count INT DEFAULT 0, 
                  last_msg_reward TIMESTAMP NULL, last_vc_reward TIMESTAMP NULL,
                  last_exp_reward TIMESTAMP NULL)''')
    try: c.execute("ALTER TABLE activity_stats ADD COLUMN last_exp_reward TIMESTAMP NULL")
    except: pass

    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (user_id VARCHAR(255) PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_claims (user_id VARCHAR(255) PRIMARY KEY, last_claim DATE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (id INT AUTO_INCREMENT PRIMARY KEY, user_id VARCHAR(255), amount BIGINT, reason VARCHAR(255), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    try: c.execute("CREATE INDEX idx_logs_user_created ON logs (user_id, created_at)")
    except: pass
    # 經濟總帳鏡像（對應 logs 每一筆；不受 logs_retention_task 清理）
    c.execute(
        '''CREATE TABLE IF NOT EXISTS casino_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id VARCHAR(255),
            amount BIGINT,
            reason VARCHAR(255),
            source_log_id INT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )'''
    )
    try:
        c.execute("ALTER TABLE casino_logs ADD COLUMN source_log_id INT NULL")
    except Exception:
        pass
    try: c.execute("CREATE UNIQUE INDEX uq_casino_logs_source_log_id ON casino_logs (source_log_id)")
    except Exception:
        pass
    try: c.execute("CREATE INDEX idx_casino_logs_user_created ON casino_logs (user_id, created_at)")
    except Exception:
        pass
    # 一次性／增量對齊：以 logs.id 去重，將既有流水完整鏡像到 casino_logs（全期總金流）
    try:
        c.execute("SELECT COUNT(*) FROM casino_logs WHERE source_log_id IS NOT NULL")
        _mapped_row = c.fetchone()
        _mapped_cnt = int((_mapped_row[0] if _mapped_row else 0) or 0)
        if _mapped_cnt == 0:
            c.execute("SELECT COUNT(*) FROM casino_logs")
            _cl_any_row = c.fetchone()
            _cl_any_cnt = int((_cl_any_row[0] if _cl_any_row else 0) or 0)
            if _cl_any_cnt > 0:
                try:
                    c.execute("TRUNCATE TABLE casino_logs")
                except Exception:
                    c.execute("DELETE FROM casino_logs")
            c.execute(
                "INSERT INTO casino_logs (user_id, amount, reason, source_log_id, created_at) "
                "SELECT user_id, amount, reason, id, created_at FROM logs"
            )
        else:
            c.execute(
                "INSERT INTO casino_logs (user_id, amount, reason, source_log_id, created_at) "
                "SELECT l.user_id, l.amount, l.reason, l.id, l.created_at FROM logs l "
                "LEFT JOIN casino_logs c ON c.source_log_id = l.id "
                "WHERE c.source_log_id IS NULL"
            )
    except Exception:
        pass
    try: c.execute("CREATE INDEX idx_users_level_exp ON users (level, exp)")
    except: pass
    c.execute('''CREATE TABLE IF NOT EXISTS tournament_players (
                 player_game_id VARCHAR(255) PRIMARY KEY,
                 player_discord_id VARCHAR(255) NULL,
                 deck_name VARCHAR(255) NOT NULL,
                 deck_image_url TEXT,
                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                 )''')
    try: c.execute("ALTER TABLE tournament_players ADD COLUMN player_discord_id VARCHAR(255) NULL")
    except: pass
    try: c.execute("CREATE UNIQUE INDEX uq_tournament_players_discord_id ON tournament_players (player_discord_id)")
    except: pass
    c.execute('''CREATE TABLE IF NOT EXISTS tournament_config (
                 id INT PRIMARY KEY,
                 reg_start TIMESTAMP NULL,
                 reg_end TIMESTAMP NULL
                 )''')
    c.execute("INSERT IGNORE INTO tournament_config (id, reg_start, reg_end) VALUES (1, NULL, NULL)")
    c.execute('''CREATE TABLE IF NOT EXISTS tournament_meta (
                 id INT PRIMARY KEY,
                 status VARCHAR(32) DEFAULT 'idle',
                 total_rounds INT DEFAULT 0,
                 current_round INT DEFAULT 0,
                 champion_player_id VARCHAR(255) NULL,
                 started_at TIMESTAMP NULL
                 )''')
    c.execute("INSERT IGNORE INTO tournament_meta (id, status, total_rounds, current_round, champion_player_id, started_at) VALUES (1, 'idle', 0, 0, NULL, NULL)")
    c.execute('''CREATE TABLE IF NOT EXISTS tournament_matches (
                 round_no INT NOT NULL,
                 match_no INT NOT NULL,
                 p1_player_id VARCHAR(255) NULL,
                 p2_player_id VARCHAR(255) NULL,
                 p1_score INT NULL,
                 p2_score INT NULL,
                 p1_confirmed TINYINT(1) DEFAULT 0,
                 p2_confirmed TINYINT(1) DEFAULT 0,
                 winner_player_id VARCHAR(255) NULL,
                 status VARCHAR(32) DEFAULT 'pending',
                 reported_by VARCHAR(255) NULL,
                 reported_at TIMESTAMP NULL,
                 updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                 PRIMARY KEY (round_no, match_no)
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS level_milestone_claims (
                 user_id VARCHAR(255) NOT NULL,
                 milestone INT NOT NULL,
                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (user_id, milestone)
                 )''')
    # 清除舊版里程碑（10/25/50），改為 20/40/60/80/100 後不再使用
    try:
        c.execute("DELETE FROM level_milestone_claims WHERE milestone IN (10, 25, 50)")
    except Exception:
        pass
    conn.commit()
    conn.close()

def log_transaction(user_id, amount, reason):
    if amount == 0:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO logs (user_id, amount, reason) VALUES (%s, %s, %s)",
        (str(user_id), amount, reason),
    )
    new_log_id = c.lastrowid
    c.execute(
        "INSERT INTO casino_logs (user_id, amount, reason, source_log_id) VALUES (%s, %s, %s, %s)",
        (str(user_id), amount, reason, new_log_id),
    )
    conn.commit()
    conn.close()


def log_casino_transaction(user_id, amount, reason, source_log_id=None, created_at=None):
    """logs 鏡像列（給 /casino_stats 全期總金流；不受一般 logs 清理影響）。"""
    if amount == 0:
        return
    conn = get_db_connection()
    c = conn.cursor()
    if created_at is not None:
        c.execute(
            "INSERT INTO casino_logs (user_id, amount, reason, source_log_id, created_at) VALUES (%s, %s, %s, %s, %s)",
            (str(user_id), amount, reason, source_log_id, created_at),
        )
    else:
        c.execute(
            "INSERT INTO casino_logs (user_id, amount, reason, source_log_id) VALUES (%s, %s, %s, %s)",
            (str(user_id), amount, reason, source_log_id),
        )
    conn.commit()
    conn.close()

def is_blacklisted(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE user_id=%s", (str(user_id),))
    res = c.fetchone()
    conn.close()
    return res is not None

def get_user_stats(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, total_games, wins, total_profit FROM users WHERE user_id=%s", (str(user_id),))
    res = c.fetchone()
    conn.close()
    return res


def fetch_record_rows(user_id, limit: int = 50):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT amount, reason, created_at FROM logs WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (str(user_id), int(limit)),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def fetch_casino_stats_rows():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) FROM casino_logs")
    issued_row = c.fetchone()
    c.execute("SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) FROM casino_logs")
    recovered_row = c.fetchone()
    c.execute("SELECT COALESCE(SUM(balance), 0) FROM users")
    circulation_row = c.fetchone()
    conn.close()
    return (
        int((issued_row[0] if issued_row else 0) or 0),
        int((recovered_row[0] if recovered_row else 0) or 0),
        int((circulation_row[0] if circulation_row else 0) or 0),
    )

def ensure_user_exists(user_id, startup_balance=DEFAULT_STARTUP_BALANCE):
    uid = str(user_id)
    bal = int(startup_balance)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT IGNORE INTO users (user_id, balance) VALUES (%s, %s)",
        (uid, bal),
    )
    inserted = c.rowcount > 0
    conn.commit()
    conn.close()
    if inserted and bal != 0:
        log_transaction(uid, bal, REASON_USER_INITIAL_BALANCE)


def _user_role_value(raw) -> str:
    """將 users.role 轉成與程式一致的鍵（cop/criminal/civilian），避免大小寫／空白／bytes 造成比對失敗。"""
    if raw is None:
        return "civilian"
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    s = str(raw).strip().lower()
    return s if s else "civilian"


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
GOOD_CITIZEN_CERT_COST = 5_000_000
GOOD_CITIZEN_CERT_COOLDOWN_SECONDS = 86400
GOOD_CITIZEN_DESTROY_COST = 50_000_000
GOOD_CITIZEN_BROKEN_LOCK_DAYS = 10


def append_rob_history_on_cursor(c, user_id: int, steal_amount: int) -> None:
    """在同一 DB cursor／交易中更新搶匪最近五次成功搶劫紀錄（JSON 陣列）。"""
    uid_str = str(user_id)
    c.execute("SELECT last_five_robs FROM users WHERE user_id=%s", (uid_str,))
    row = c.fetchone()
    history: typing.List[typing.Any] = []
    raw = row[0] if row else None
    if raw:
        try:
            parsed = json.loads(raw)
            history = parsed if isinstance(parsed, list) else []
        except Exception:
            history = []
    history.append(
        {
            "amount": int(steal_amount),
            "time": now_tw_naive().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    history = history[-5:]
    c.execute(
        "UPDATE users SET last_five_robs=%s WHERE user_id=%s",
        (json.dumps(history, ensure_ascii=False), uid_str),
    )


def get_last_five_robs_total(user_id: typing.Union[int, str]) -> typing.Tuple[int, int, typing.List[typing.Any]]:
    """回傳 (五次內搶劫總額, 筆數, 明細列表)。"""
    uid = str(user_id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT last_five_robs FROM users WHERE user_id=%s", (uid,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return 0, 0, []
    try:
        history = json.loads(row[0])
    except Exception:
        return 0, 0, []
    if not isinstance(history, list):
        return 0, 0, []
    total = sum(int(item.get("amount", 0) or 0) for item in history if isinstance(item, dict))
    return total, len(history), history


def rob_history_total_from_raw(raw: typing.Any) -> typing.Tuple[int, int]:
    if not raw:
        return 0, 0
    try:
        history = json.loads(raw)
    except Exception:
        return 0, 0
    if not isinstance(history, list):
        return 0, 0
    total = sum(int(item.get("amount", 0) or 0) for item in history if isinstance(item, dict))
    return total, len(history)


def clear_rob_history(user_id: typing.Union[int, str]) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET last_five_robs=NULL WHERE user_id=%s", (str(user_id),))
    conn.commit()
    conn.close()


def try_deduct_balance(user_id, amount, reason):
    if amount <= 0:
        return True
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (amount, str(user_id), amount)
    )
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    if ok:
        log_transaction(user_id, -amount, reason)
    return ok

def roll_gamble_exp_from_bet(main_bet: int) -> int:
    """完成一局 21 點時發放的隨機 EXP；主注越高可略增上限（仍為隨機區間）。"""
    base = random.randint(GAMBLE_EXP_MIN, GAMBLE_EXP_MAX)
    bonus = min(max(int(main_bet), 0) // 2500, 25)
    return base + bonus


def update_game_result(user_id, balance_delta, profit_delta, is_win, is_push=False):
    conn = get_db_connection()
    c = conn.cursor()
    win_int = 1 if is_win else 0
    if is_push:
        c.execute("UPDATE users SET balance=GREATEST(0, balance+%s), total_profit=total_profit+%s WHERE user_id=%s",
                  (balance_delta, profit_delta, str(user_id)))
    else:
        c.execute("UPDATE users SET balance=GREATEST(0, balance+%s), total_profit=total_profit+%s, total_games=total_games+1, wins=wins+%s WHERE user_id=%s",
                  (balance_delta, profit_delta, win_int, str(user_id)))
    conn.commit()
    conn.close()
    if balance_delta != 0:
        log_transaction(user_id, balance_delta, "21點遊戲結算")

def exp_for_next_level(level):
    lv = max(1, min(MAX_LEVEL, level))
    return 60 + lv * 25 + int((lv ** 1.6) * 8)

def calc_level_from_exp(exp):
    level = 1
    remaining = max(0, int(exp))
    while level < MAX_LEVEL:
        need = exp_for_next_level(level)
        if remaining < need:
            break
        remaining -= need
        level += 1
    return level, remaining, (0 if level >= MAX_LEVEL else exp_for_next_level(level))

def exp_required_for_level(target_level: int) -> int:
    """回傳達到指定等級所需的總 EXP（例如 Lv.1 -> 0 EXP）。"""
    lv = max(1, min(MAX_LEVEL, int(target_level)))
    total = 0
    for cur in range(1, lv):
        total += exp_for_next_level(cur)
    return total

def get_level_stats(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT exp, level FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row

def add_user_exp(user_id, amount):
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
    new_level, _, _ = calc_level_from_exp(new_exp)
    if new_level != old_level:
        c.execute("UPDATE users SET exp=%s, level=%s WHERE user_id=%s", (new_exp, new_level, str(user_id)))
    else:
        c.execute("UPDATE users SET exp=%s WHERE user_id=%s", (new_exp, str(user_id)))
    conn.commit()
    conn.close()
    return old_level, new_level, new_exp

def get_claimed_milestones(user_id) -> set:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT milestone FROM level_milestone_claims WHERE user_id=%s", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return {int(r[0]) for r in rows} if rows else set()

def try_claim_milestone(user_id, milestone, coin_amount) -> int:
    """
    首次記錄該里程碑：寫入 level_milestone_claims，可選加幣。
    已領過回傳 -1；本輪新領則回傳入帳金額（0 表示僅解鎖里程碑，無幣）。
    """
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
        log_transaction(user_id, coin_amount, f"等級里程碑 Lv.{milestone}")
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
    now = now_tw_naive()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT level, last_hourly_claim, hourly_bank FROM users WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    level = max(1, min(MAX_LEVEL, int(row[0] or 1)))
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

def payout_hourly_bank(user_id, bank, reward_per_slot):
    if bank <= 0:
        return 0
    payout = int(bank * reward_per_slot)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance=balance+%s, hourly_bank=0 WHERE user_id=%s", (payout, str(user_id)))
    conn.commit()
    conn.close()
    log_transaction(user_id, payout, "每小時簽到")
    return payout


def claim_daily_reward(user_id, daily_reward: int = 100_000):
    ensure_user_exists(user_id, 50000)
    today_tw = now_tw_naive().date()
    tomorrow_tw = today_tw + datetime.timedelta(days=1)
    next_claim_dt = datetime.datetime.combine(tomorrow_tw, datetime.time.min, tzinfo=TW_TZ)
    next_ts = int(next_claim_dt.timestamp())

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT last_claim FROM daily_claims WHERE user_id=%s", (str(user_id),))
    row = c.fetchone()
    if row and row[0] == today_tw:
        conn.close()
        return {"claimed": False, "next_ts": next_ts}

    c.execute(
        "INSERT INTO daily_claims (user_id, last_claim) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_claim=%s",
        (str(user_id), today_tw, today_tw),
    )
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (daily_reward, str(user_id)))
    conn.commit()
    conn.close()
    log_transaction(user_id, daily_reward, "每日簽到")
    stats = get_user_stats(user_id)
    return {
        "claimed": True,
        "reward": daily_reward,
        "balance": int((stats[0] if stats else 0) or 0),
        "next_ts": next_ts,
    }


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


def get_tournament_window():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT reg_start, reg_end FROM tournament_config WHERE id=1")
    row = c.fetchone()
    conn.close()
    if not row:
        return None, None
    return row[0], row[1]

def set_tournament_window(reg_start, reg_end):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE tournament_config SET reg_start=%s, reg_end=%s WHERE id=1", (reg_start, reg_end))
    conn.commit()
    conn.close()

def parse_tw_datetime(text):
    # 接受格式: YYYY-MM-DD HH:MM (台灣時間 UTC+8)
    dt = datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TW_TZ).replace(tzinfo=None)

def tw_naive_to_discord_ts(dt):
    if not dt:
        return None
    return int(dt.replace(tzinfo=TW_TZ).timestamp())

def _build_tournament_bracket_lines(matches, total_rounds):
    grouped = {}
    for row in matches:
        grouped.setdefault(row["round_no"], []).append(row)
    lines = []
    for rnd in range(1, total_rounds + 1):
        lines.append(f"**R{rnd}**")
        rows = sorted(grouped.get(rnd, []), key=lambda x: x["match_no"])
        if not rows:
            lines.append("（尚未建立）")
            continue
        for m in rows:
            p1 = m["p1_player_id"] or "TBD"
            p2 = m["p2_player_id"] or "TBD"
            status = m["status"] or "pending"
            if status == "completed" and m["winner_player_id"]:
                lines.append(f"M{m['match_no']}: `{p1}` vs `{p2}` → ✅ `{m['winner_player_id']}`")
            elif m["p1_score"] is not None and m["p2_score"] is not None:
                lines.append(f"M{m['match_no']}: `{p1}` vs `{p2}` | 比分 {m['p1_score']}:{m['p2_score']} (待確認)")
            else:
                lines.append(f"M{m['match_no']}: `{p1}` vs `{p2}`")
    return lines

def _advance_winner(conn, round_no, match_no, winner_player_id, total_rounds):
    if round_no >= total_rounds:
        c = conn.cursor()
        c.execute(
            "UPDATE tournament_meta SET status='finished', champion_player_id=%s WHERE id=1",
            (winner_player_id,)
        )
        return
    next_round = round_no + 1
    next_match_no = ((match_no - 1) // 2) + 1
    put_on_p1 = (match_no % 2 == 1)
    c = conn.cursor()
    if put_on_p1:
        c.execute(
            "UPDATE tournament_matches SET p1_player_id=%s WHERE round_no=%s AND match_no=%s",
            (winner_player_id, next_round, next_match_no)
        )
    else:
        c.execute(
            "UPDATE tournament_matches SET p2_player_id=%s WHERE round_no=%s AND match_no=%s",
            (winner_player_id, next_round, next_match_no)
        )
    c.execute(
        "SELECT p1_player_id, p2_player_id, status FROM tournament_matches WHERE round_no=%s AND match_no=%s",
        (next_round, next_match_no)
    )
    nxt = c.fetchone()
    if not nxt:
        return
    n_p1, n_p2, n_status = nxt
    if n_status == "completed":
        return
    # 若下一輪遇到輪空，直接自動晉級，避免卡關。
    if n_p1 and not n_p2:
        c.execute(
            "UPDATE tournament_matches SET winner_player_id=%s, status='completed', p1_score=2, p2_score=0, p1_confirmed=1, p2_confirmed=1 WHERE round_no=%s AND match_no=%s",
            (n_p1, next_round, next_match_no)
        )
        _advance_winner(conn, next_round, next_match_no, n_p1, total_rounds)
    elif n_p2 and not n_p1:
        c.execute(
            "UPDATE tournament_matches SET winner_player_id=%s, status='completed', p1_score=0, p2_score=2, p1_confirmed=1, p2_confirmed=1 WHERE round_no=%s AND match_no=%s",
            (n_p2, next_round, next_match_no)
        )
        _advance_winner(conn, next_round, next_match_no, n_p2, total_rounds)

def _clear_downstream_from_match(conn, round_no, match_no, total_rounds):
    if round_no >= total_rounds:
        return
    next_round = round_no + 1
    next_match_no = ((match_no - 1) // 2) + 1
    clear_p1 = (match_no % 2 == 1)
    c = conn.cursor()
    if clear_p1:
        c.execute(
            "UPDATE tournament_matches SET p1_player_id=NULL WHERE round_no=%s AND match_no=%s",
            (next_round, next_match_no)
        )
    else:
        c.execute(
            "UPDATE tournament_matches SET p2_player_id=NULL WHERE round_no=%s AND match_no=%s",
            (next_round, next_match_no)
        )
    c.execute(
        "UPDATE tournament_matches SET p1_score=NULL, p2_score=NULL, p1_confirmed=0, p2_confirmed=0, winner_player_id=NULL, status='pending', reported_by=NULL, reported_at=NULL WHERE round_no=%s AND match_no=%s",
        (next_round, next_match_no)
    )
    _clear_downstream_from_match(conn, next_round, next_match_no, total_rounds)

def _refresh_champion_if_single_left(conn):
    c = conn.cursor()
    c.execute("SELECT status FROM tournament_meta WHERE id=1")
    meta_row = c.fetchone()
    if not meta_row:
        return
    status = meta_row[0] or "idle"
    if status != "running":
        return
    c.execute("SELECT p1_player_id, p2_player_id FROM tournament_matches WHERE status <> 'completed'")
    rows = c.fetchall()
    alive = set()
    for p1, p2 in rows:
        if p1:
            alive.add(p1)
        if p2:
            alive.add(p2)
    if len(alive) == 1:
        champion = next(iter(alive))
        c.execute("UPDATE tournament_meta SET status='finished', champion_player_id=%s WHERE id=1", (champion,))

# ··············································································
# [D · 二十一點與 UI]
# ··············································································

# ==============================================================================
# 【八】二十一點：牌組、算分、旁注與牌局訊息更新
# 多副牌洗牌、手牌點數（含 A）、對子／21+3 旁注結算、以及嵌入式訊息編輯流程。
# ==============================================================================


def get_deck(num_decks=6):
    suits = ['♥️', '♦️', '♣️', '♠️']
    ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    return [{'rank': r, 'suit': s} for s in suits for r in ranks] * num_decks

def card_to_emoji(card, guild_id=None) -> str:
    return f"**[{card['rank']} {card['suit']}]**"

def card_back_emoji(guild_id=None) -> str:
    return "**[??]**"

async def _send_game(channel, gv: 'BlackjackGame', interaction: discord.Interaction = None, message_obj: discord.Message = None, view=None, 
                     done=False, res="", profit=0, animating=False, extra_msg="") -> discord.Message:
    embed = gv.build_embed(done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg, guild_id=channel.guild.id if channel.guild else None)
    current_view = view if view is not None else gv

    if interaction:
        if interaction.response.is_done():
            return await interaction.edit_original_response(embed=embed, view=current_view, attachments=[])
        else:
            await interaction.response.edit_message(embed=embed, view=current_view, attachments=[])
            return await interaction.original_response()
    elif message_obj:
        return await message_obj.edit(embed=embed, view=current_view, attachments=[])
    return await channel.send(embed=embed, view=current_view)

def calculate_score(hand):
    score, aces = 0, 0
    values = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':10,'Q':10,'K':10,'A':11}
    for c in hand:
        score += values[c['rank']]
        if c['rank'] == 'A': aces += 1
    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score

def check_sidebets(player_hand, dealer_up, p_bet, s_bet):
    res_msg, total_p = "", 0
    if p_bet > 0:
        c1, c2 = player_hand[0], player_hand[1]
        if c1['rank'] == c2['rank']:
            if c1['suit'] == c2['suit']: mult, m = 30, "同花對子"
            else: mult, m = 5, "混合對子"
            total_p += p_bet * mult
            res_msg += f"🧧 {m}！+{p_bet*mult} "
        else:
            total_p -= p_bet
            res_msg += f"🧧 對子未中 -{p_bet} "
    if s_bet > 0:
        cards = [player_hand[0], player_hand[1], dealer_up]
        suits = [c['suit'] for c in cards]
        rv = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':11,'Q':12,'K':13,'A':14}
        v = sorted([rv[c['rank']] for c in cards])
        if v == [2,3,14]: v = [1,2,3]
        is_flush    = len(set(suits)) == 1
        is_straight = (v[2]-v[1] == 1 and v[1]-v[0] == 1)
        is_triplet  = len(set([c['rank'] for c in cards])) == 1
        if is_flush and is_triplet: mult, m = 50, "同花三條"
        elif is_flush and is_straight: mult, m = 25, "同花順"
        elif is_triplet: mult, m = 25, "三條"
        elif is_straight: mult, m = 10, "順子"
        elif is_flush: mult, m = 5, "同花"
        else: mult, m = -1, "未中"
        
        if mult > 0:
            total_p += s_bet * mult
            res_msg += f"🎯 21+3 {m}！+{s_bet*mult} "
        else:
            total_p -= s_bet
            res_msg += f"🎯 21+3 未中 -{s_bet} "
    return total_p, res_msg

# ==============================================================================
# 【九】Discord 互動介面（UI）
# 自訂下注 Modal、二十一點主 View 與相關按鈕、全下確認、新局、紅包搶領、多行文字 Embed 翻頁等。
# ==============================================================================


class BetModal(discord.ui.Modal, title='自訂下注金額'):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.b_input = discord.ui.TextInput(label='主注 (最低 100)', default=str(view.base_bet), required=True)
        self.p_input = discord.ui.TextInput(label='對子旁注', default=str(view.p_bet), required=False)
        self.s_input = discord.ui.TextInput(label='21+3旁注', default=str(view.s_bet), required=False)
        self.add_item(self.b_input)
        self.add_item(self.p_input)
        self.add_item(self.s_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            b = int(self.b_input.value)
            p = int(self.p_input.value or 0)
            s = int(self.s_input.value or 0)
            if b < 100 or p < 0 or s < 0: raise ValueError
        except ValueError:
            return await interaction.response.send_message("請輸入有效正整數 (主注最低 100)", ephemeral=True)
        max_side = int(b * SIDE_BET_RATIO)
        if p + s > max_side:
            return await interaction.response.send_message(f"旁注總和 ({p+s}) 不能超過主注的 {int(SIDE_BET_RATIO*100)}% ({max_side})", ephemeral=True)
        ensure_user_exists(self.view.user.id, 50000)
        stats = get_user_stats(self.view.user.id)
        if stats[0] < (b + p + s): return await interaction.response.send_message(f"餘額不足！你目前有 {stats[0]} 東雲幣", ephemeral=True)
        self.view.base_bet = b
        self.view.max_side = max_side
        self.view.p_bet = p
        self.view.s_bet = s
        await interaction.response.edit_message(embed=self.view.build_embed(), view=self.view)

class SetupView(discord.ui.View):
    def __init__(self, user, base_bet, p_bet=0, s_bet=0):
        super().__init__(timeout=90)
        self.user, self.base_bet = user, base_bet
        self.p_bet, self.s_bet = p_bet, s_bet
        self.max_side = int(base_bet * SIDE_BET_RATIO)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("這不是你的牌局！", ephemeral=True)
            return False
        now = asyncio.get_running_loop().time()
        if hasattr(self, "last_action") and now - self.last_action < 2.0:
            await interaction.response.send_message("⚠️ 操作太快了！按鈕有 2 秒冷卻時間。", ephemeral=True)
            return False
        self.last_action = now
        return True

    def build_embed(self, err=""):
        ensure_user_exists(self.user.id, 50000)
        stats = get_user_stats(self.user.id)
        embed = discord.Embed(title="🃏 21點 — 下注設定", color=0x2b2d31)
        err_prefix = f"❌ {err}\n" if err else ""
        embed.description = f"{err_prefix}主注：`{self.base_bet}`\n旁注剩餘額度：**`{self.max_side - (self.p_bet + self.s_bet)}`**\n你的餘額：`{stats[0]}`"
        embed.add_field(name="🧧 對子旁注", value=f"下注金額：`{self.p_bet}`\n**同花對子**: 30倍\n**混合對子**: 5倍", inline=True)
        embed.add_field(name="🎯 21+3旁注", value=f"下注金額：`{self.s_bet}`\n**同花三條**: 50倍\n**同花順**: 25倍\n**三條**: 25倍\n**順子**: 10倍\n**同花**: 5倍", inline=True)
        return embed

    @discord.ui.button(label="開始遊戲 (再來一局)", style=discord.ButtonStyle.success)
    async def start(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        ensure_user_exists(self.user.id, 50000)
        stats = get_user_stats(self.user.id)
        total_cost = self.base_bet + self.p_bet + self.s_bet
        if not try_deduct_balance(self.user.id, total_cost, "21點開局扣款"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop()
        gv = BlackjackGame(self.user, self.base_bet, self.p_bet, self.s_bet, upfront_cost=total_cost)
        msg = await _send_game(inter.channel, gv, interaction=inter)
        if msg is not None:
            await gv.check_auto_bj(msg)
        else:
            logger.error("21點 SetupView.start：_send_game 未回傳訊息，略過自動 BJ 結算 user=%s", inter.user.id)

    @discord.ui.button(label="自訂下注金額", style=discord.ButtonStyle.primary)
    async def custom_bet(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.send_modal(BetModal(self))

class BlackjackGame(discord.ui.View):
    def __init__(self, user, bet, p_bet, s_bet, upfront_cost=0):
        super().__init__(timeout=90)
        self.user, self.bet, self.p_bet, self.s_bet = user, bet, p_bet, s_bet
        self.total_deducted = upfront_cost
        self.hand_bets = [bet]
        self.deck = get_deck()
        random.shuffle(self.deck)
        self.hands = [[self.deck.pop(), self.deck.pop()]]
        self.d_hand = [self.deck.pop(), self.deck.pop()]
        self.current_hand = 0
        self.hand_results = [None]
        self.side_p, self.side_m = check_sidebets(self.hands[0], self.d_hand[0], p_bet, s_bet)
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("這不是你的牌局！", ephemeral=True)
            return False
        now = asyncio.get_running_loop().time()
        if hasattr(self, "last_action") and now - self.last_action < 1.0:
            await interaction.response.send_message("⚠️ 操作太快了！請慢慢點擊。", ephemeral=True)
            return False
        self.last_action = now
        return True

    async def _edit(self, message=None, extra_msg="", interaction: discord.Interaction = None, done=False, res="", profit=0, animating=False):
        try:
            if interaction:
                await _send_game(interaction.channel, self, interaction=interaction, done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg)
            elif message:
                await _send_game(message.channel, self, message_obj=message, done=done, res=res, profit=profit, animating=animating, extra_msg=extra_msg)
        except Exception as e: print(f"❌ 渲染錯誤: {e}")

    @property
    def p_hand(self): return self.hands[self.current_hand]

    def update_buttons(self):
        values = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':10,'Q':10,'K':10,'A':11}
        can_split = len(self.hands) == 1 and len(self.p_hand) == 2 and values[self.p_hand[0]['rank']] == values[self.p_hand[1]['rank']]
        can_double = len(self.p_hand) == 2
        for c in self.children:
            if c.label == "分牌":
                c.disabled = not can_split
            elif c.label == "雙倍":
                c.disabled = not can_double
            elif c.label == "投降":
                c.disabled = len(self.p_hand) > 2 or len(self.hands) > 1
            elif c.label == "要牌":
                c.disabled = calculate_score(self.p_hand) > 21

    def build_embed(self, done=False, res="", profit=0, animating=False, extra_msg="", guild_id=None):
        stats = get_user_stats(self.user.id)
        if stats: bal, total, wins, t_prof = stats
        else: bal, total, wins, t_prof = 0, 0, 0, 0
        wr = (wins/total*100) if total>0 else 0
        embed = discord.Embed(title="🃏 21點大賽", color=0x2b2d31)
        main_ui = f"💰 餘額：{bal} | 🏆 勝場：{wins} | 🎲 總局數：{total} | 📈 勝率：{wr:.1f}% | 💸 總盈虧：{t_prof}\n"
        if extra_msg: main_ui += f"**{extra_msg}**\n"
        for i, hand in enumerate(self.hands):
            indicator = "👉 " if i == self.current_hand and not done else ""
            title_text = f"{indicator}👤 {self.user.display_name} 的手牌"
            if len(self.hands) > 1: title_text += f" (第 {i+1} 手)"
            p_cards = ' '.join([card_to_emoji(c, guild_id) for c in hand])
            main_ui += f"### {title_text}\n### {p_cards} (點數: **{calculate_score(hand)}**)\n"
        if done or animating:
            d_cards = ' '.join([card_to_emoji(c, guild_id) for c in self.d_hand])
            main_ui += f"### 🤖 莊家手牌\n### {d_cards} (點數: **{calculate_score(self.d_hand)}**)\n"
            if done:
                total_profit = profit + self.side_p
                res_line = f"### 🏆 {res}\n{self.side_m}\n"
                if total_profit > 0: res_line += f"📈 總盈虧：`+{total_profit}` | 💰 餘額：`{bal}`\n"
                elif total_profit < 0: res_line += f"📉 總盈虧：`{total_profit}` | 💰 餘額：`{bal}`\n"
                else: res_line += f"➖ 無輸贏 | 💰 餘額：`{bal}`\n"
                main_ui += res_line
        else:
            main_ui += f"### 🤖 莊家手牌\n### {card_to_emoji(self.d_hand[0], guild_id)} {card_back_emoji(guild_id)} (點數: **❓**)\n"
        embed.description = main_ui
        return embed

    async def check_auto_bj(self, message):
        if len(self.p_hand) == 2 and calculate_score(self.p_hand) == 21:
            await asyncio.sleep(1.5)
            try:
                await self.advance_hand(message_obj=message)
            except Exception:
                logger.exception("21點 check_auto_bj 自動結算失敗 user=%s", self.user.id)

    async def end(self, res, prof, win=False, is_push=False, message_obj=None, interaction=None, exp_gain=0, exp_detail=""):
        if getattr(self, '_game_over', False): return
        self._game_over = True
        
        total_p = prof + getattr(self, 'side_p', 0)
        settlement_credit = self.total_deducted + total_p
        update_game_result(self.user.id, settlement_credit, total_p, win, is_push)

        if exp_gain > 0:
            ensure_user_exists(self.user.id, 50000)
            exp_result = add_user_exp(self.user.id, exp_gain)
            if exp_result and exp_result[1] > exp_result[0]:
                old_lv, new_lv = exp_result[0], exp_result[1]
                if any(old_lv < m <= new_lv for m in LEVEL_MILE_TIERS):
                    asyncio.create_task(process_level_ups(self.user, old_lv, new_lv))
            res = f"{res}\n✨ 經驗值 `+{exp_gain}`"
        else:
            res = f"{res}\n🧊 本局失利，不獲得 EXP"
        if exp_detail:
            res = f"{res}\n{exp_detail}"

        for c in self.children: c.disabled = True
        stats = get_user_stats(self.user.id)
        nv  = NewGameView(self.user, self.bet, self.p_bet, self.s_bet, stats[0] if stats else 0)
        await _send_game(message_obj.channel if message_obj else interaction.channel, self, 
                         interaction=interaction, message_obj=message_obj, view=nv, 
                         done=True, res=res, profit=prof)

    async def advance_hand(self, message_obj=None, interaction=None):
        if getattr(self, '_game_over', False): return
        if self.current_hand < len(self.hands) - 1:
            self.current_hand += 1
            self.update_buttons()
            await self._edit(message=message_obj, interaction=interaction, extra_msg=f"👉 換第 {self.current_hand+1} 手牌")
            if len(self.p_hand) == 2 and calculate_score(self.p_hand) == 21:
                await asyncio.sleep(1.5)
                await self.advance_hand(message_obj=message_obj, interaction=interaction)
        else:
            await self.resolve_dealer(message_obj=message_obj, interaction=interaction)

    async def resolve_dealer(self, message_obj=None, interaction=None):
        if getattr(self, '_game_over', False): return
        need_dealer = any(hand is None for hand in self.hand_results)
        for c in self.children: c.disabled = True
        await self._edit(message=message_obj, interaction=interaction, animating=True)
        if need_dealer:
            await asyncio.sleep(1.2)
            while calculate_score(self.d_hand) < 17 and len(self.d_hand) < 5:
                self.d_hand.append(self.deck.pop())
                await self._edit(message=message_obj, interaction=None, animating=True)
                await asyncio.sleep(1.2)
        total_prof, final_res_texts = 0, []
        total_exp_gain = 0
        exp_detail_texts = []
        ds = calculate_score(self.d_hand)
        dealer_bj = len(self.d_hand) == 2 and ds == 21
        dealer_5_card = len(self.d_hand) == 5 and ds <= 21
        for i, hand in enumerate(self.hands):
            if self.hand_results[i] is not None:
                r, p, w = self.hand_results[i]
                final_res_texts.append(f"第 {i+1} 手: {r}" if len(self.hands)>1 else r)
                hand_profit = p
                total_prof += hand_profit
                hand_exp_base = roll_gamble_exp_from_bet(self.hand_bets[i])
                if hand_profit > 0:
                    hand_exp_award = hand_exp_base
                elif hand_profit == 0:
                    hand_exp_award = max(1, hand_exp_base // 2)
                else:
                    hand_exp_award = 0
                total_exp_gain += hand_exp_award
                if len(self.hands) > 1:
                    exp_detail_texts.append(f"第 {i+1} 手 EXP `+{hand_exp_award}`")
                continue
            ps = calculate_score(hand)
            player_bj, player_5_card = (len(hand) == 2 and ps == 21), (len(hand) == 5 and ps <= 21)
            if player_5_card and dealer_5_card:
                final_res_texts.append("🤝 雙方皆過五關！平手")
                hand_profit = 0
            elif player_5_card:
                final_res_texts.append("🐉 你過五關啦！爽贏 2.5 倍！")
                hand_profit = int(self.hand_bets[i] * 2.5)
            elif dealer_5_card:
                final_res_texts.append("🐉 老子過五關啦！你這低能兒～")
                hand_profit = -self.hand_bets[i]
            elif player_bj and dealer_bj:
                final_res_texts.append("🤝 雙方皆為 BlackJack！平手")
                hand_profit = 0
            elif player_bj:
                final_res_texts.append("🌟 BlackJack！1.5倍賠率！")
                hand_profit = int(self.hand_bets[i] * 1.5)
            elif dealer_bj:
                final_res_texts.append("💀 莊家 BlackJack！你輸啦～雜魚～")
                hand_profit = -self.hand_bets[i]
            elif ds > 21 or ps > ds:
                final_res_texts.append("🎉 這次算你贏啦，腦殘！")
                hand_profit = self.hand_bets[i]
            elif ps < ds:
                final_res_texts.append("💀 你輸啦～雜魚～")
                hand_profit = -self.hand_bets[i]
            else:
                final_res_texts.append("🤝 就這點技術阿腦殘？")
                hand_profit = 0
            total_prof += hand_profit
            hand_exp_base = roll_gamble_exp_from_bet(self.hand_bets[i])
            if hand_profit > 0:
                hand_exp_award = hand_exp_base
            elif hand_profit == 0:
                hand_exp_award = max(1, hand_exp_base // 2)
            else:
                hand_exp_award = 0
            total_exp_gain += hand_exp_award
            if len(self.hands) > 1:
                exp_detail_texts.append(f"第 {i+1} 手 EXP `+{hand_exp_award}`")
        final_msg = "\n".join(final_res_texts)
        total_combined = total_prof + getattr(self, 'side_p', 0)
        exp_detail = ""
        if len(self.hands) > 1 and exp_detail_texts:
            exp_detail = "🧮 分牌 EXP 明細\n" + "\n".join(exp_detail_texts)
        await self.end(
            final_msg,
            total_prof,
            total_combined > 0,
            total_combined == 0,
            message_obj=message_obj,
            interaction=interaction,
            exp_gain=total_exp_gain,
            exp_detail=exp_detail,
        )

    @discord.ui.button(label="要牌", style=discord.ButtonStyle.success)
    async def hit(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        self.p_hand.append(self.deck.pop())
        self.update_buttons() 
        ps = calculate_score(self.p_hand)
        if ps > 21 or len(self.p_hand) == 5:
            if ps > 21: self.hand_results[self.current_hand] = ("爆牌輸了", -self.hand_bets[self.current_hand], False)
            await self.advance_hand(interaction=inter, message_obj=inter.message)
        else: await self._edit(interaction=inter)

    @discord.ui.button(label="停牌", style=discord.ButtonStyle.danger)
    async def stand(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer(); await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="投降", style=discord.ButtonStyle.secondary)
    async def surrender(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer(); self.hand_results[self.current_hand] = ("這樣就投降了嗎，雜魚～", -(self.hand_bets[self.current_hand]//2), False)
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="雙倍", style=discord.ButtonStyle.primary)
    async def double_down(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        extra_cost = self.hand_bets[self.current_hand]
        if not try_deduct_balance(self.user.id, extra_cost, "21點雙倍加注"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.total_deducted += extra_cost
        self.hand_bets[self.current_hand] *= 2
        self.p_hand.append(self.deck.pop())
        if calculate_score(self.p_hand) > 21: self.hand_results[self.current_hand] = ("你爆牌囉～小丑～", -self.hand_bets[self.current_hand], False)
        await self.advance_hand(interaction=inter, message_obj=inter.message)

    @discord.ui.button(label="分牌", style=discord.ButtonStyle.primary)
    async def split(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        if not try_deduct_balance(self.user.id, self.bet, "21點分牌加注"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.total_deducted += self.bet
        self.is_split, c1, c2 = True, self.hands[0][0], self.hands[0][1]
        self.hands, self.hand_results, self.hand_bets = [[c1, self.deck.pop()], [c2, self.deck.pop()]], [None, None], [self.bet, self.bet]
        self.update_buttons(); await self._edit(interaction=inter, extra_msg="✌️ 你選擇了分牌！")
        if calculate_score(self.p_hand) == 21: await asyncio.sleep(1.5); await self.advance_hand(interaction=None, message_obj=inter.message)

class ConfirmAllInView(discord.ui.View):
    def __init__(self, user, parent_msg):
        super().__init__(timeout=30)
        self.user, self.parent_msg = user, parent_msg
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id: return False
        return True
    @discord.ui.button(label="確定 All In！", style=discord.ButtonStyle.danger)
    async def confirm(self, inter, btn):
        stats = get_user_stats(self.user.id)
        if not stats or stats[0] < 100: return await inter.response.send_message("去乞討吧雜魚", ephemeral=True)
        self.stop(); await inter.response.edit_message(content="🔥 All In 已確認！正在為你開牌...", view=None)
        try: await self.parent_msg.delete()
        except: pass
        gv = BlackjackGame(self.user, stats[0], 0, 0)
        msg = await _send_game(inter.channel, gv)
        if msg is not None:
            await gv.check_auto_bj(msg)

class NewGameView(discord.ui.View):
    def __init__(self, user, last_bet, last_p_bet, last_s_bet, current_bal):
        super().__init__(timeout=90)
        self.user, self.last_bet, self.last_p_bet, self.last_s_bet, self.current_bal = user, last_bet, last_p_bet, last_s_bet, current_bal
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id: return False
        return True
    @discord.ui.button(label="再來一局", style=discord.ButtonStyle.success)
    async def again(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        total_cost = self.last_bet + self.last_p_bet + self.last_s_bet
        if not try_deduct_balance(self.user.id, total_cost, "21點開局扣款"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop()
        gv = BlackjackGame(self.user, self.last_bet, self.last_p_bet, self.last_s_bet, upfront_cost=total_cost)
        msg = await _send_game(inter.channel, gv, interaction=inter)
        if msg is not None:
            await gv.check_auto_bj(msg)
        else:
            logger.error("21點 NewGameView.again：_send_game 未回傳訊息，略過自動 BJ 結算 user=%s", inter.user.id)
    @discord.ui.button(label="雙倍再局 (Double)", style=discord.ButtonStyle.primary)
    async def double_again(self, inter, btn):
        if inter.user.id != self.user.id: return
        await inter.response.defer()
        new_bet = self.last_bet * 2
        total_cost = new_bet + self.last_p_bet + self.last_s_bet
        if not try_deduct_balance(self.user.id, total_cost, "21點開局扣款"):
            return await inter.followup.send("餘額不足", ephemeral=True)
        self.stop()
        gv = BlackjackGame(self.user, new_bet, self.last_p_bet, self.last_s_bet, upfront_cost=total_cost)
        msg = await _send_game(inter.channel, gv, interaction=inter)
        if msg is not None:
            await gv.check_auto_bj(msg)
        else:
            logger.error("21點 NewGameView.double_again：_send_game 未回傳訊息，略過自動 BJ 結算 user=%s", inter.user.id)
    @discord.ui.button(label="修改下注", style=discord.ButtonStyle.secondary)
    async def modify_bet(self, inter, btn):
        self.stop(); await inter.response.defer()
        try: await inter.message.delete()
        except: pass
        setup = SetupView(self.user, self.last_bet, self.last_p_bet, self.last_s_bet)
        await inter.channel.send(embed=setup.build_embed(), view=setup)
    @discord.ui.button(label="All In (全押)", style=discord.ButtonStyle.danger)
    async def all_in(self, inter, btn):
        cv = ConfirmAllInView(self.user, inter.message)
        await inter.response.send_message("⚠️ 警告：要全押嗎雜魚？", view=cv, ephemeral=True)

def build_random_splits(total_amount, count):
    remaining = total_amount
    amounts = []
    for i in range(count - 1):
        max_pick = remaining - (count - i - 1)
        pick = random.randint(1, max_pick)
        amounts.append(pick)
        remaining -= pick
    amounts.append(remaining)
    random.shuffle(amounts)
    return amounts

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
        if interaction.user.bot:
            return await interaction.response.send_message("機器人不能搶紅包", ephemeral=True)
        if interaction.user.id in self.claimed_users:
            return await interaction.response.send_message("你已經搶過這包了", ephemeral=True)
        if self.left_count <= 0 or self.left_amount <= 0:
            return await interaction.response.send_message("紅包已搶完", ephemeral=True)

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

        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (user_id, balance) VALUES (%s, %s) ON DUPLICATE KEY UPDATE balance=balance+%s",
            (str(interaction.user.id), amount, amount)
        )
        conn.commit()
        conn.close()
        log_transaction(interaction.user.id, amount, f"搶紅包 #{self.packet_id}")

        if self.left_count <= 0 or self.left_amount <= 0:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=self.summary_text() + "\n✅ 紅包已被搶完！\n" + self.winners_text(),
                view=self
            )
            return
        await interaction.response.edit_message(content=self.summary_text(), view=self)
        await interaction.followup.send(f"🎉 你搶到 `{amount}` 東雲幣！", ephemeral=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, "message") and self.message:
                await self.message.edit(
                    content=self.summary_text() + "\n⌛ 紅包已逾時關閉。\n" + self.winners_text(),
                    view=self
                )
        except:
            pass

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
    bot.loop.create_task(vc_reward_task())
    bot.loop.create_task(logs_retention_task())
    logger.info("機器人已啟動: %s（伺服器數 %s）", bot.user, len(bot.guilds))


async def logs_retention_task():
    """依 LOG_RETENTION_DAYS 刪除過期帳務流水（預設至少保留 14 天內）。"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if LOG_RETENTION_DAYS <= 0:
                await asyncio.sleep(LOG_PURGE_INTERVAL_SECONDS)
                continue
            conn = get_db_connection()
            c = conn.cursor()
            c.execute(
                "DELETE FROM logs WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
                (LOG_RETENTION_DAYS,),
            )
            removed = c.rowcount if c.rowcount is not None else 0
            conn.commit()
            conn.close()
            if removed:
                logger.info(
                    "logs 保留最近 %s 天：已刪除 %s 筆過期紀錄",
                    LOG_RETENTION_DAYS,
                    removed,
                )
        except Exception as e:
            logger.exception("logs 定期清理失敗: %s", e)
        await asyncio.sleep(LOG_PURGE_INTERVAL_SECONDS)


async def vc_reward_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(600)
        now = now_tw_naive()
        conn = get_db_connection()
        c = conn.cursor()
        awarded_users: typing.Set[str] = set()
        for guild in bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot or member.voice.self_deaf or member.voice.deaf:
                        continue
                    user_id = str(member.id)
                    if user_id in awarded_users:
                        continue
                    c.execute("SELECT last_vc_reward FROM activity_stats WHERE user_id=%s", (user_id,))
                    row = c.fetchone()
                    if not row or row[0] is None or (now - row[0]).total_seconds() >= 1800:
                        c.execute(
                            "INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500",
                            (user_id,),
                        )
                        c.execute(
                            "INSERT INTO activity_stats (user_id, last_vc_reward) VALUES (%s, %s) ON DUPLICATE KEY UPDATE last_vc_reward=%s",
                            (user_id, now, now),
                        )
                        log_transaction(user_id, 500, "語音通話獎勵 (10min)")
                        awarded_users.add(user_id)
        conn.commit()
        conn.close()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    try:
        # 私訊 -> 管理伺服器指定頻道
        if message.guild is None:
            await relay_dm_to_staff_channel(message)
            return
        # 管理頻道：回覆轉發訊息 -> DM 或原頻道代發（略過 Webhook）
        if message.channel.id == DM_RELAY_CHANNEL_ID:
            if not message.webhook_id and await relay_staff_reply_to_dm_user(message):
                return
        # 任一伺服器頻道 @ 機器人（略過轉接頻道）-> 轉發後可依類型回覆
        if (
            message.guild
            and message.channel.id != DM_RELAY_CHANNEL_ID
            and bot.user is not None
            and bot.user in message.mentions
        ):
            await relay_user_message_to_staff_channel(message, is_dm=False)
        if not message.guild:
            return
        user_id = str(message.author.id)
        now = now_tw_naive()
        now_ts = time.time()

        _pending_msg_counts[user_id] = _pending_msg_counts.get(user_id, 0) + 1
        pending = _pending_msg_counts[user_id]
        last_flush = _last_msg_flush_ts.get(user_id, 0.0)
        should_flush = pending >= MSG_DB_FLUSH_COUNT or (now_ts - last_flush) >= MSG_DB_FLUSH_EVERY_SECONDS

        last_exp_ts = _last_exp_award_ts.get(user_id, 0.0)
        exp_due = (now_ts - last_exp_ts) >= EXP_COOLDOWN_SECONDS

        if not should_flush and not exp_due:
            return

        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO activity_stats (user_id, msg_count) VALUES (%s, %s) ON DUPLICATE KEY UPDATE msg_count=msg_count+%s",
            (user_id, pending, pending)
        )
        _pending_msg_counts[user_id] = 0
        _last_msg_flush_ts[user_id] = now_ts

        c.execute("SELECT msg_count, last_msg_reward FROM activity_stats WHERE user_id=%s", (user_id,))
        row = c.fetchone()

        if exp_due:
            exp_gain = random.randint(12, 20) * CHAT_EXP_MULTIPLIER
            ensure_user_exists(message.author.id, 50000)
            exp_result = add_user_exp(user_id, exp_gain)
            if exp_result and exp_result[1] > exp_result[0]:
                o, n = exp_result[0], exp_result[1]
                if any(o < m <= n for m in LEVEL_MILE_TIERS):
                    asyncio.create_task(process_level_ups(message.author, o, n))
            c.execute("UPDATE activity_stats SET last_exp_reward=%s WHERE user_id=%s", (now, user_id))
            _last_exp_award_ts[user_id] = now_ts

        if row and row[0] >= 10:
            if row[1] is None or (now - row[1]).total_seconds() >= 1800:
                c.execute("INSERT INTO users (user_id, balance) VALUES (%s, 500) ON DUPLICATE KEY UPDATE balance=balance+500", (user_id,))
                c.execute("UPDATE activity_stats SET msg_count=0, last_msg_reward=%s WHERE user_id=%s", (now, user_id))
                log_transaction(user_id, 500, "聊天活躍獎勵 (10句)")

        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("on_message 錯誤: %s", e)
    finally:
        await bot.process_commands(message)


DELETE_LOG_EMBED_COLOR = 0x5865F2
# Discord 單則訊息最多 10 個 embed；圖片預覽用滿後其餘再發後續訊息
DELETE_LOG_MAX_EMBEDS_PER_MESSAGE = 10

_IMAGE_ATTACHMENT_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")
_VIDEO_ATTACHMENT_SUFFIX = (".mp4", ".webm", ".mov", ".mkv")


def _classify_attachment(a: discord.Attachment) -> str:
    """回傳 image / video / file（供刪除訊息紀錄預覽用）。"""
    ct = (getattr(a, "content_type", None) or "").lower()
    fn = (getattr(a, "filename", "") or "").lower()
    if ct.startswith("image/") or any(fn.endswith(s) for s in _IMAGE_ATTACHMENT_SUFFIX):
        return "image"
    if ct.startswith("video/") or any(fn.endswith(s) for s in _VIDEO_ATTACHMENT_SUFFIX):
        return "video"
    return "file"


def _delete_log_image_embed(url: str) -> discord.Embed:
    """僅含大圖預覽的嵌入（與主紀錄 embed 同色）。"""
    e = discord.Embed(color=DELETE_LOG_EMBED_COLOR)
    e.set_image(url=url)
    return e


def _chunk_plain_url_lines(urls: typing.Sequence[str], limit: int = 1950) -> typing.List[str]:
    """將多個 URL 切成多段訊息文字（供影片連結預覽），避免超過 Discord 上限。"""
    chunks: typing.List[str] = []
    buf: typing.List[str] = []
    size = 0
    for u in urls:
        add = len(u) + (1 if buf else 0)
        if buf and size + add > limit:
            chunks.append("\n".join(buf))
            buf = [u]
            size = len(u)
        else:
            if buf:
                size += 1
            buf.append(u)
            size += len(u)
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def _send_delete_log_image_overflow(ch: discord.TextChannel, urls: typing.Sequence[str]) -> None:
    """第一則已塞滿 10 個 embed 時，其餘圖片改為每則訊息最多 10 張預覽。"""
    if not urls:
        return
    for i in range(0, len(urls), DELETE_LOG_MAX_EMBEDS_PER_MESSAGE):
        part = urls[i : i + DELETE_LOG_MAX_EMBEDS_PER_MESSAGE]
        await ch.send(embeds=[_delete_log_image_embed(u) for u in part])


async def _is_message_deleted_by_bot(guild: discord.Guild, message: discord.Message) -> bool:
    """若可判定此刪除由機器人執行，回傳 True（用於刪除紀錄忽略機器人刪除）。"""
    if guild is None:
        return False
    try:
        me = guild.me
        if me and not me.guild_permissions.view_audit_log:
            return False
    except Exception:
        return False
    try:
        async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.message_delete):
            target = entry.target
            if not isinstance(target, (discord.Member, discord.User)):
                continue
            if message.author and target.id != message.author.id:
                continue
            if entry.extra and getattr(entry.extra, "channel", None):
                if entry.extra.channel.id != message.channel.id:
                    continue
            try:
                age = (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds()
                if age > 10:
                    continue
            except Exception:
                pass
            actor = entry.user
            if actor is not None and getattr(actor, "bot", False):
                return True
            return False
    except Exception:
        return False
    return False


@bot.event
async def on_message_delete(message: discord.Message):
    """追蹤伺服器訊息刪除，在紀錄頻道備份原文與附件連結。"""
    try:
        if DELETE_LOG_CHANNEL_ID <= 0:
            return
        if message.guild is None:
            return
        if await _is_message_deleted_by_bot(message.guild, message):
            return
        author = message.author
        if author is not None and author.bot:
            return
        log_ch = bot.get_channel(DELETE_LOG_CHANNEL_ID)
        if log_ch is None:
            try:
                log_ch = await bot.fetch_channel(DELETE_LOG_CHANNEL_ID)
            except Exception:
                return
        if not isinstance(log_ch, discord.TextChannel):
            return

        content = (message.content or "").strip()
        if not content:
            content = "*（無文字內容）*"

        guild = message.guild
        icon = None
        try:
            if guild.icon:
                icon = str(guild.icon.url)
        except Exception:
            icon = None

        if author is None:
            author_line = "發送者：`（訊息未在快取中，無法還原發送者）`"
        else:
            author_line = f"{author.mention} · `{author.id}`"

        try:
            ch_label = message.channel.mention
        except Exception:
            ch_label = f"<#{getattr(message.channel, 'id', 0)}>"

        desc_lines = [
            author_line,
            f"{ch_label} · {guild.name}",
        ]
        if message.created_at:
            desc_lines.append(f"原訊息時間：<t:{int(message.created_at.timestamp())}:F>")
        emb = discord.Embed(
            title="訊息已刪除",
            description="\n".join(desc_lines),
            color=DELETE_LOG_EMBED_COLOR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        if icon and icon.startswith("http"):
            emb.set_author(name="刪除紀錄", icon_url=icon)
        else:
            emb.set_author(name="刪除紀錄")
        emb.add_field(name="原文", value=content[:1024], inline=False)

        embeds_out: typing.List[discord.Embed] = [emb]
        overflow_image_urls: typing.List[str] = []
        images: typing.List[typing.Tuple[discord.Attachment, str]] = []
        videos: typing.List[typing.Tuple[discord.Attachment, str]] = []
        other_files: typing.List[typing.Tuple[discord.Attachment, str]] = []

        if message.attachments:
            for a in message.attachments:
                try:
                    url = a.url
                except Exception:
                    continue
                if not url:
                    continue
                kind = _classify_attachment(a)
                if kind == "image":
                    images.append((a, url))
                elif kind == "video":
                    videos.append((a, url))
                else:
                    other_files.append((a, url))

            if images:
                urls_only = [u for _, u in images]
                emb.set_image(url=urls_only[0])
                idx = 1
                while idx < len(urls_only) and len(embeds_out) < DELETE_LOG_MAX_EMBEDS_PER_MESSAGE:
                    embeds_out.append(_delete_log_image_embed(urls_only[idx]))
                    idx += 1
                overflow_image_urls = list(urls_only[idx:])

            if other_files:
                link_lines = []
                for i, (a, url) in enumerate(other_files, start=1):
                    name = getattr(a, "filename", None) or f"附件{i}"
                    link_lines.append(f"[`{name}`]({url})")
                emb.add_field(name="附件（檔案）", value="\n".join(link_lines)[:1024], inline=False)

        emb.set_footer(text=f"訊息 ID · {message.id}")

        video_urls = [u for _, u in videos]
        video_chunks = _chunk_plain_url_lines(video_urls) if video_urls else []

        send_kw: typing.Dict[str, typing.Any] = {"embeds": embeds_out}
        if video_chunks:
            send_kw["content"] = video_chunks[0]
        await log_ch.send(**send_kw)

        for vc in video_chunks[1:]:
            await log_ch.send(content=vc)

        if overflow_image_urls:
            await _send_delete_log_image_overflow(log_ch, overflow_image_urls)
    except Exception as e:
        logger.exception("on_message_delete 錯誤: %s", e)

# ··············································································
# [G · 玩家 Slash 指令]
# ··············································································

# ==============================================================================
# 【十三】Slash 指令：經濟、小遊戲、轉帳、排行榜、管理公告等
# 含每日／每小時簽到、乞討搶劫救濟、21 點、餘額與等級查詢、轉帳、紅包、
# /say、戰報、賭場統計、排行榜（不含錦標賽專區與「僅主機」後台）。
# ==============================================================================


@bot.tree.command(name="help", description="機器人指令總覽（一般玩家）")
async def help_slash(interaction: discord.Interaction):
    """東雲幣、賭場、通緝／警察、等級與錦標賽等 Slash 說明（不含主機／管理員專用指令）。"""
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
            f"`/counter_rob` — 平民被搶**成功**後限一次（約 **{int(round(COUNTER_ROB_BASE_SUCCESS_RATE * 100))}%** 基礎、級差 ±1%）；**成功領滿加倍**；搶匪扣款後**餘額 0 入獄**否則不入獄；不足額記假釋債\n"
            f"`/bail` — 入獄繳 **基礎 `{BAIL_COST:,}` + 累計假釋欠款** 出獄"
        ),
        inline=False,
    )
    emb.add_field(
        name="🏆 錦標賽（玩家）",
        value=(
            "`/tournament_register` — 報名與卡組\n"
            "`/tournament_update_deck` — 更新卡組\n"
            "`/tournament_list` — 報名名單（翻頁）\n"
            "`/tournament_window_show` — 報名時間窗\n"
            "`/tournament_bracket` — 賽程表\n"
            "`/tournament_submit_score` — 提交比分\n"
            "`/tournament_confirm_score` — 確認比分"
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
    ensure_user_exists(interaction.user.id, 50000)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, last_beg FROM users WHERE user_id=%s", (str(interaction.user.id),))
    row = c.fetchone()
    now = now_tw_naive()
    if row[1] and (now - row[1]).total_seconds() < 120:
        conn.close()
        return await interaction.response.send_message("太快了", ephemeral=True)
    inflation_mult, _, _ = get_inflation_multiplier()
    base_earn = random.randint(100, 600)
    earn = max(50, int(base_earn * inflation_mult))
    if random.random() < 0.3:
        c.execute("UPDATE users SET last_beg=%s WHERE user_id=%s", (now, str(interaction.user.id)))
        await interaction.response.send_message("沒人鳥你 乞丐")
    else:
        c.execute("UPDATE users SET balance=balance+%s, last_beg=%s WHERE user_id=%s", (earn, now, str(interaction.user.id)))
        log_transaction(interaction.user.id, earn, "乞討所得")
        await interaction.response.send_message(f"你獲得了{earn}東雲幣!錢給你啦 乞丐!")
    conn.commit()
    conn.close()

@bot.tree.command(name="rob", description="搶劫其他玩家（僅搶匪；高風險高報酬）")
@app_commands.describe(member="要搶劫的對象（選人）", user_id="或填使用者 ID／貼提及")
async def rob(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
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

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(in_prison,0) FROM users WHERE user_id=%s",
        (str(interaction.user.id),),
    )
    _pr = c.fetchone()
    if _pr and int(_pr[0] or 0):
        conn.close()
        return await interaction.response.send_message("🔒 你在監獄裡無法搶劫。", ephemeral=True)

    c.execute("SELECT role FROM users WHERE user_id=%s", (str(interaction.user.id),))
    _role_row = c.fetchone()
    robber_role = _user_role_value(_role_row[0] if _role_row else None)
    if robber_role != "criminal":
        conn.close()
        return await interaction.response.send_message(
            "❌ 只有**搶匪**可以搶劫。請先用 `/role_choose` 選擇搶匪（criminal）。",
            ephemeral=True,
        )

    c.execute("SELECT balance, last_rob, level FROM users WHERE user_id=%s", (str(interaction.user.id),))
    robber_row = c.fetchone()
    c.execute(
        "SELECT balance, level, last_robbed, COALESCE(good_citizen_cert_active,0) FROM users WHERE user_id=%s",
        (str(member.id),),
    )
    target_row = c.fetchone()

    robber_balance = int((robber_row[0] if robber_row else 0) or 0)
    last_rob = robber_row[1] if robber_row else None
    robber_level = int((robber_row[2] if robber_row else 1) or 1)
    target_balance = int((target_row[0] if target_row else 0) or 0)
    target_level = int((target_row[1] if target_row else 1) or 1)
    target_last_robbed = target_row[2] if target_row else None
    target_good_cert = int((target_row[3] if target_row else 0) or 0)
    now = now_tw_naive()

    if last_rob and (now - last_rob).total_seconds() < ROB_COOLDOWN_SECONDS:
        remain = ROB_COOLDOWN_SECONDS - int((now - last_rob).total_seconds())
        mins = max(1, remain // 60)
        conn.close()
        return await interaction.response.send_message(f"⏳ 你剛搶過，請再等 `{mins}` 分鐘。", ephemeral=True)

    if target_balance < 50000:
        conn.close()
        return await interaction.response.send_message("對方太窮了，沒有東西可以搶。", ephemeral=True)
    if robber_balance < 50000:
        conn.close()
        return await interaction.response.send_message("你的餘額低於 50,000，無法發起搶劫。", ephemeral=True)
    if target_good_cert:
        conn.close()
        return await interaction.response.send_message(
            "🪪 對方已啟用良民證，無法被搶劫。",
            ephemeral=True,
        )
    if target_last_robbed and (now - target_last_robbed).total_seconds() < ROB_VICTIM_PROTECT_SECONDS:
        remain = ROB_VICTIM_PROTECT_SECONDS - int((now - target_last_robbed).total_seconds())
        mins = max(1, remain // 60)
        conn.close()
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
    c.execute("UPDATE users SET last_rob=%s WHERE user_id=%s", (now, str(interaction.user.id)))
    robber_name = interaction.user.display_name
    victim_name = member.display_name

    if success:
        steal_amount = int(max(1, min(target_balance * random.uniform(0.10, 0.25), 1_000_000)))
        c.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
            (steal_amount, str(member.id), steal_amount)
        )
        if c.rowcount == 0:
            conn.commit()
            conn.close()
            return await interaction_send(interaction, "對方及時把錢藏好了，這次搶劫失敗。", ephemeral=True)
        c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (steal_amount, str(interaction.user.id)))
        c.execute("UPDATE users SET last_robbed=%s WHERE user_id=%s", (now, str(member.id)))
        append_rob_history_on_cursor(c, interaction.user.id, steal_amount)

        wanted_info = ""
        # 進入搶劫成功者必為搶匪（見上方角色檢查）；不再依第二次 SELECT role 判斷（避免 DB 回傳格式導致略過通緝）
        c.execute(
            "SELECT COALESCE(wanted_stars,0) FROM users WHERE user_id=%s",
            (str(interaction.user.id),),
        )
        _ws = c.fetchone()
        current_wanted = int(_ws[0] or 0) if _ws else 0

        if current_wanted < 5:
            c.execute(
                "UPDATE users SET wanted_stars=LEAST(5, COALESCE(wanted_stars,0)+1), wanted_hunted_count=0 WHERE user_id=%s",
                (str(interaction.user.id),),
            )
            c.execute("SELECT COALESCE(wanted_stars,0) FROM users WHERE user_id=%s", (str(interaction.user.id),))
            _nw = c.fetchone()
            new_wanted = int(_nw[0] or 0) if _nw else 0
            stars_display = "⭐" * new_wanted + "☆" * (5 - new_wanted)
            if new_wanted == 5:
                wanted_info = (
                    f"\n🔴 **達到最高通緝！** {stars_display}\n"
                    f"⚠️ 每次搶劫成功後警察可追捕一次。"
                )
            else:
                cap = min(
                    95,
                    COP_HUNT_CAPTURE_BASE_PCT + new_wanted * COP_HUNT_CAPTURE_PER_STAR_PCT,
                )
                wanted_info = (
                    f"\n⚠️ **通緝等級提升** → {stars_display}（{new_wanted}/5）\n"
                    f"🚔 追捕成功率基準約：**{cap}%**（實際另受警匪等級差影響，每級 ±1%）"
                )
        else:
            c.execute(
                "UPDATE users SET wanted_hunted_count=0 WHERE user_id=%s",
                (str(interaction.user.id),),
            )
            _cap5 = min(
                95,
                COP_HUNT_CAPTURE_BASE_PCT + 5 * COP_HUNT_CAPTURE_PER_STAR_PCT,
            )
            wanted_info = (
                f"\n🔴 **滿星通緝中** ⭐⭐⭐⭐⭐\n"
                f"🚔 每次搶劫成功後警察可追捕一次（基準約 **`{_cap5}%`**，實際另受警匪等級差影響，每級 ±1%）。"
            )

        revenge_hint = ""
        c.execute(
            "SELECT COALESCE(role,'civilian') FROM users WHERE user_id=%s",
            (str(member.id),),
        )
        vrole_row = c.fetchone()
        victim_role = _user_role_value(vrole_row[0] if vrole_row else None)
        if victim_role == "civilian":
            c.execute(
                "UPDATE users SET revenge_pending=1, revenge_robber_id=%s, revenge_amount=%s WHERE user_id=%s",
                (str(interaction.user.id), steal_amount, str(member.id)),
            )
            revenge_hint = (
                f"\n\n💢 {member.mention} 身為**平民**被搶成功，獲得 **一次**機會：使用 `/counter_rob` "
                f"可依每級差距 ±1% 公式（與搶劫相同結構，基礎成功率較低），嘗試從搶匪處**加倍搶回**（至多 `{(steal_amount * 2):,}` 幣，實際以搶匪餘額為準）。"
            )

        conn.commit()
        conn.close()
        await db_to_thread(log_transaction, interaction.user.id, steal_amount, f"搶劫成功（目標:{member.id}）")
        await db_to_thread(log_transaction, member.id, -steal_amount, f"被搶劫（搶匪:{interaction.user.id}）")
        return await interaction_send(
            interaction,
            f"{robber_name}搶了{victim_name}`{steal_amount:,}`東雲幣!!（本次成功率約 {success_rate_pct}%）{wanted_info}{revenge_hint}"
        )

    fail_penalty = int(max(1, min(robber_balance * random.uniform(0.15, 0.45), 1_000_000)))
    if fail_penalty > 0:
        c.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
            (fail_penalty, str(interaction.user.id), fail_penalty)
        )
        deducted = c.rowcount > 0
        if deducted:
            c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (fail_penalty, str(member.id)))
    else:
        deducted = False
    conn.commit()
    conn.close()
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
    ensure_user_exists(interaction.user.id, 50000)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(role,'civilian'), COALESCE(wanted_stars,0), last_role_change,
                  COALESCE(good_citizen_cert_active,0)
           FROM users WHERE user_id=%s""",
        (str(interaction.user.id),),
    )
    row = c.fetchone()
    old_role = (row[0] or "civilian") if row else "civilian"
    wanted_now = int(row[1] or 0) if row else 0
    last_role_change = row[2] if row else None
    cert_active = int(row[3] or 0) if row else 0
    now = now_tw_naive()
    if role != old_role and cert_active:
        conn.close()
        return await interaction.response.send_message(
            "❌ 你目前已啟用良民證，無法切換身分。請先使用 `/good_citizen` 解除後再轉職。",
            ephemeral=True,
        )
    if role != old_role and last_role_change is not None:
        elapsed = (now - last_role_change).total_seconds()
        if elapsed < ROLE_CHANGE_COOLDOWN_SECONDS:
            next_dt = last_role_change + datetime.timedelta(seconds=ROLE_CHANGE_COOLDOWN_SECONDS)
            ts = tw_naive_to_discord_ts(next_dt)
            conn.close()
            return await interaction.response.send_message(
                f"⏳ 轉職冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再切換陣營。",
                ephemeral=True,
            )
    if role == "civilian" and old_role == "civilian":
        conn.close()
        return await interaction.response.send_message("ℹ️ 你目前已是**平民**。", ephemeral=True)
    if old_role == "criminal" and wanted_now > 0 and role in ("cop", "civilian"):
        conn.close()
        return await interaction.response.send_message(
            f"❌ 搶匪轉為警察或平民須 **通緝 0 星**（目前 {wanted_now} 星）。請先透過追捕／入獄等流程歸零後再切換。",
            ephemeral=True,
        )
    if role in ("cop", "civilian"):
        c.execute(
            "UPDATE users SET role=%s, wanted_stars=0, wanted_hunted_count=0, last_five_robs=NULL, last_role_change=%s WHERE user_id=%s",
            (role, now, str(interaction.user.id)),
        )
    else:
        c.execute("UPDATE users SET role=%s, last_role_change=%s WHERE user_id=%s", (role, now, str(interaction.user.id)))
    conn.commit()
    conn.close()

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

    ensure_user_exists(interaction.user.id, 50000)
    ensure_user_exists(criminal_user.id, 0)
    criminal_id = str(criminal_user.id)
    cop_id = str(interaction.user.id)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(role,'civilian'), COALESCE(level,1) FROM users WHERE user_id=%s", (cop_id,))
    cop_row = c.fetchone()
    if not cop_row or cop_row[0] != "cop":
        conn.close()
        return await interaction.response.send_message(
            "❌ 只有**警察**可以追捕。請先用 `/role_choose` 選擇警察。",
            ephemeral=True,
        )
    cop_level = int(cop_row[1] or 1)

    c.execute(
        "SELECT COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0), COALESCE(balance,0), COALESCE(in_prison,0), COALESCE(level,1) FROM users WHERE user_id=%s",
        (criminal_id,),
    )
    criminal_row = c.fetchone()
    if not criminal_row:
        conn.close()
        return await interaction.response.send_message("❌ 找不到該玩家資料。", ephemeral=True)

    wanted_stars = int(criminal_row[0] or 0)
    hunted_count = int(criminal_row[1] or 0)
    criminal_balance = int(criminal_row[2] or 0)
    in_prison = int(criminal_row[3] or 0)
    criminal_level = int(criminal_row[4] or 1)

    if in_prison:
        conn.close()
        return await interaction.response.send_message(
            f"ℹ️ {criminal_user.mention} 已在監獄中，無法追捕。",
            ephemeral=True,
        )
    if wanted_stars <= 0:
        conn.close()
        return await interaction.response.send_message(
            f"ℹ️ {criminal_user.mention} 目前沒有通緝度。",
            ephemeral=True,
        )
    if criminal_id == cop_id:
        conn.close()
        return await interaction.response.send_message("❌ 不能追捕自己。", ephemeral=True)

    can_hunt = hunted_count == 0
    if wanted_stars <= 4:
        hunt_rule = f"{wanted_stars}★：本星級僅能追捕一次（失敗或成功後需再升星或滿星規則）。"
    else:
        hunt_rule = "5★：每次搶劫成功後可追捕一次（本輪若已追捕過則需等對方再搶劫成功）。"

    if not can_hunt:
        conn.close()
        return await interaction.response.send_message(
            f"❌ 目前無法追捕。\n{hunt_rule}",
            ephemeral=True,
        )

    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (COP_HUNT_FEE, cop_id, COP_HUNT_FEE),
    )
    if c.rowcount == 0:
        conn.close()
        return await interaction.response.send_message(
            f"❌ 每次追捕須支付 **`{COP_HUNT_FEE:,}`** 東雲幣，你的餘額不足。",
            ephemeral=True,
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

    if is_caught:
        last_five_total, rob_count, rob_history = get_last_five_robs_total(criminal_id)
        cop_reward = int(last_five_total)
        c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s", (criminal_id,))
        bal_row = c.fetchone()
        criminal_balance = int(bal_row[0] or 0) if bal_row else 0
        confiscated_base = int(last_five_total * 0.6)
        confiscated_amount = min(confiscated_base, criminal_balance)
        conf_shortfall = max(0, confiscated_base - confiscated_amount)
        remaining_bal = max(0, criminal_balance - confiscated_amount)

        c.execute("SELECT COALESCE(bail_debt, 0) FROM users WHERE user_id=%s", (criminal_id,))
        _bd_row = c.fetchone()
        bail_debt_before = int((_bd_row[0] if _bd_row else 0) or 0)
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
        c.execute(
            """INSERT INTO prison_records
               (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, bail_cost, arrested_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, BAIL_COST, now),
        )
        conn.commit()
        conn.close()

        log_transaction(cop_id, -COP_HUNT_FEE, "追捕行動費用")
        log_transaction(criminal_id, -confiscated_amount, f"被警察逮捕沒收 {confiscated_amount:,}")
        log_transaction(cop_id, cop_reward, f"逮捕通緝犯 {criminal_user.id} 贓款")

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
        await interaction.response.send_message(embed=emb)
        return

    c.execute(
        "UPDATE users SET wanted_hunted_count=1 WHERE user_id=%s",
        (criminal_id,),
    )
    conn.commit()
    conn.close()

    log_transaction(cop_id, -COP_HUNT_FEE, "追捕行動費用")

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
    await interaction.response.send_message(embed=emb)


@bot.tree.command(
    name="wanted_buyout",
    description=f"[搶匪] 支付 {WANTED_BUYOUT_COST:,} 東雲幣消除通緝並清空最近搶劫紀錄（24 小時僅能一次）",
)
async def wanted_buyout_slash(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
    ensure_user_exists(interaction.user.id, 50000)
    uid = str(interaction.user.id)
    cost = WANTED_BUYOUT_COST
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT role, COALESCE(wanted_stars,0), COALESCE(balance,0), COALESCE(in_prison,0), last_wanted_buyout FROM users WHERE user_id=%s",
        (uid,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return await interaction.response.send_message("找不到帳號資料。", ephemeral=True)
    role = _user_role_value(row[0])
    stars = int(row[1] or 0)
    bal = int(row[2] or 0)
    in_pr = int(row[3] or 0)
    last_buyout = row[4]

    if role != "criminal":
        conn.close()
        return await interaction.response.send_message("❌ 僅**搶匪**可使用此指令。", ephemeral=True)
    if in_pr:
        conn.close()
        return await interaction.response.send_message("❌ 你在監獄中，無法消除通緝。", ephemeral=True)
    if stars <= 0:
        conn.close()
        return await interaction.response.send_message("ℹ️ 你目前沒有通緝星。", ephemeral=True)
    if bal < cost:
        conn.close()
        return await interaction.response.send_message(
            f"❌ 需要 `{cost:,}` 東雲幣，你的餘額不足（目前 `{bal:,}`）。",
            ephemeral=True,
        )

    now = now_tw_naive()
    if last_buyout is not None:
        elapsed = (now - last_buyout).total_seconds()
        if elapsed < WANTED_BUYOUT_COOLDOWN_SECONDS:
            next_dt = last_buyout + datetime.timedelta(seconds=WANTED_BUYOUT_COOLDOWN_SECONDS)
            ts = tw_naive_to_discord_ts(next_dt)
            conn.close()
            return await interaction.response.send_message(
                f"⏳ 通緝買斷冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再使用。",
                ephemeral=True,
            )

    c.execute(
        """UPDATE users SET balance=balance-%s,
           wanted_stars=0, wanted_hunted_count=0, last_five_robs=NULL, last_wanted_buyout=%s
           WHERE user_id=%s AND balance >= %s""",
        (cost, now, uid, cost),
    )
    if c.rowcount == 0:
        conn.close()
        return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
    conn.commit()
    conn.close()
    log_transaction(interaction.user.id, -cost, "通緝買斷（消除通緝星）")
    new_bal = get_user_stats(interaction.user.id)[0]
    stars_was = stars
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
    await interaction.response.send_message(embed=emb, ephemeral=False, allowed_mentions=_am)


@bot.tree.command(name="good_citizen", description="良民證：支付 500 萬啟用防搶；再支付 500 萬解除（兩者皆 24h 冷卻）")
async def good_citizen_slash(interaction: discord.Interaction):
    ensure_user_exists(interaction.user.id, 50000)
    uid = str(interaction.user.id)
    now = now_tw_naive()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(role,'civilian'), COALESCE(balance,0),
                  COALESCE(good_citizen_cert_active,0), last_good_citizen_cert_action,
                  good_citizen_cert_broken_until
           FROM users WHERE user_id=%s""",
        (uid,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return await interaction.response.send_message("找不到帳號資料。", ephemeral=True)
    role_raw, bal_raw, cert_active_raw, last_action, broken_until = row
    role_now = _user_role_value(role_raw)
    bal = int(bal_raw or 0)
    cert_active = int(cert_active_raw or 0)
    if role_now != "civilian":
        conn.close()
        return await interaction.response.send_message(
            "❌ 良民證僅限 **平民** 使用；請先用 `/role_choose` 切換為平民。",
            ephemeral=True,
        )
    if cert_active == 0 and broken_until is not None and now < broken_until:
        ts = tw_naive_to_discord_ts(broken_until)
        conn.close()
        return await interaction.response.send_message(
            f"❌ 你的良民證已被摧毀，需等到 <t:{ts}:F>（<t:{ts}:R>）後才能再次啟用。",
            ephemeral=True,
        )
    if last_action is not None:
        elapsed = (now - last_action).total_seconds()
        if elapsed < GOOD_CITIZEN_CERT_COOLDOWN_SECONDS:
            next_dt = last_action + datetime.timedelta(seconds=GOOD_CITIZEN_CERT_COOLDOWN_SECONDS)
            ts = tw_naive_to_discord_ts(next_dt)
            conn.close()
            return await interaction.response.send_message(
                f"⏳ 良民證冷卻中，下次可於 <t:{ts}:F>（<t:{ts}:R>）再操作。",
                ephemeral=True,
            )
    if bal < GOOD_CITIZEN_CERT_COST:
        conn.close()
        return await interaction.response.send_message(
            f"❌ 需要 `{GOOD_CITIZEN_CERT_COST:,}` 東雲幣，你的餘額不足（目前 `{bal:,}`）。",
            ephemeral=True,
        )

    next_active = 0 if cert_active else 1
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
        return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
    conn.commit()
    conn.close()

    reason = "啟用良民證（防搶）" if next_active else "解除良民證（取消防搶）"
    log_transaction(interaction.user.id, -GOOD_CITIZEN_CERT_COST, reason)
    new_bal = get_user_stats(interaction.user.id)[0]
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
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT user_id, COALESCE(balance,0), last_good_citizen_cert_action
           FROM users
           WHERE COALESCE(good_citizen_cert_active,0)=1
           ORDER BY last_good_citizen_cert_action DESC, user_id ASC
           LIMIT 100"""
    )
    rows = c.fetchall()
    conn.close()
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


@bot.tree.command(name="break_citizen", description="摧毀目標良民證（花費 5,000 萬；目標 10 天內無法再取得）")
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
    ensure_user_exists(interaction.user.id, 50000)
    ensure_user_exists(target_user.id, 0)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(balance,0) FROM users WHERE user_id=%s",
        (str(interaction.user.id),),
    )
    atk_row = c.fetchone()
    attacker_bal = int((atk_row[0] if atk_row else 0) or 0)
    if attacker_bal < GOOD_CITIZEN_DESTROY_COST:
        conn.close()
        return await interaction.response.send_message(
            f"❌ 需要 `{GOOD_CITIZEN_DESTROY_COST:,}` 東雲幣，你的餘額不足（目前 `{attacker_bal:,}`）。",
            ephemeral=True,
        )

    c.execute(
        """SELECT COALESCE(good_citizen_cert_active,0), good_citizen_cert_broken_until
           FROM users WHERE user_id=%s""",
        (str(target_user.id),),
    )
    t_row = c.fetchone()
    if not t_row:
        conn.close()
        return await interaction.response.send_message("找不到目標資料。", ephemeral=True)
    target_active = int(t_row[0] or 0)
    target_broken_until = t_row[1]
    now = now_tw_naive()
    if target_active != 1:
        conn.close()
        if target_broken_until and now < target_broken_until:
            ts = tw_naive_to_discord_ts(target_broken_until)
            return await interaction.response.send_message(
                f"ℹ️ 目標目前未啟用良民證，且已被封鎖至 <t:{ts}:F>（<t:{ts}:R>）。",
                ephemeral=True,
            )
        return await interaction.response.send_message("ℹ️ 目標目前沒有啟用良民證。", ephemeral=True)

    broken_until = now + datetime.timedelta(days=GOOD_CITIZEN_BROKEN_LOCK_DAYS)
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (GOOD_CITIZEN_DESTROY_COST, str(interaction.user.id), GOOD_CITIZEN_DESTROY_COST),
    )
    if c.rowcount == 0:
        conn.close()
        return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
    c.execute(
        """UPDATE users
           SET good_citizen_cert_active=0,
               good_citizen_cert_broken_until=%s,
               last_good_citizen_cert_action=%s
           WHERE user_id=%s""",
        (broken_until, now, str(target_user.id)),
    )
    conn.commit()
    conn.close()
    log_transaction(interaction.user.id, -GOOD_CITIZEN_DESTROY_COST, f"摧毀良民證（目標:{target_user.id}）")
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
    ensure_user_exists(interaction.user.id, 50000)
    uid = str(interaction.user.id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(role,'civilian'), COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0),
                  COALESCE(in_prison,0), last_five_robs, COALESCE(arrest_count,0),
                  COALESCE(revenge_pending,0), COALESCE(revenge_amount,0), COALESCE(bail_debt,0),
                  COALESCE(good_citizen_cert_active,0)
           FROM users WHERE user_id=%s""",
        (uid,),
    )
    row = c.fetchone()
    conn.close()
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
                f"可使用 `/counter_rob` 一次；成功可領 **`{(int(rev_amt or 0) * 2):,}`**（加倍）；"
                "搶匪扣完若**餘額 0** 會**入獄**，否則不入獄；不足額記假釋債務。"
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
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT user_id, COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0),
                  COALESCE(in_prison,0), last_five_robs
           FROM users WHERE wanted_stars > 0
           ORDER BY wanted_stars DESC, user_id ASC LIMIT 50"""
    )
    rows = c.fetchall()
    conn.close()
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
    description="平民被搶成功後限一次：反制成功領滿加倍；搶匪付完若餘額為0入獄，否則不入獄；不足額併假釋債",
)
async def counter_rob_slash(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("請在伺服器頻道使用。", ephemeral=True)
    victim_id = str(interaction.user.id)
    ensure_user_exists(interaction.user.id, 0)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(role,'civilian'), revenge_pending, revenge_robber_id, revenge_amount FROM users WHERE user_id=%s",
        (victim_id,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return await interaction.response.send_message("找不到帳號資料。", ephemeral=True)
    vrole = (row[0] or "civilian")
    pending = int(row[1] or 0)
    robber_id = row[2]
    base_amt = int(row[3] or 0)

    if vrole != "civilian":
        c.execute(
            "UPDATE users SET revenge_pending=0, revenge_robber_id=NULL, revenge_amount=0 WHERE user_id=%s",
            (victim_id,),
        )
        conn.commit()
        conn.close()
        return await interaction.response.send_message(
            "你已非**平民**身分，先前的「加倍搶回」機會已作廢。",
            ephemeral=True,
        )

    if not pending or not robber_id or base_amt <= 0:
        conn.close()
        return await interaction.response.send_message(
            "你沒有可用的加倍搶回機會（僅**平民**被搶劫**成功**後會獲得一次）。",
            ephemeral=True,
        )

    c.execute("SELECT level, balance FROM users WHERE user_id=%s", (victim_id,))
    vrow = c.fetchone()
    victim_level = int((vrow[0] if vrow else 1) or 1)
    c.execute("SELECT level, balance FROM users WHERE user_id=%s", (str(robber_id),))
    rrow = c.fetchone()
    if not rrow:
        c.execute(
            "UPDATE users SET revenge_pending=0, revenge_robber_id=NULL, revenge_amount=0 WHERE user_id=%s",
            (victim_id,),
        )
        conn.commit()
        conn.close()
        return await interaction.response.send_message("找不到搶匪帳號，機會已清除。", ephemeral=True)
    robber_level = int((rrow[0] if rrow else 1) or 1)
    robber_balance = int((rrow[1] if rrow else 0) or 0)

    doubled = base_amt * 2
    pay = min(doubled, robber_balance)
    debt_from_shortfall = max(0, doubled - pay)
    level_gap = victim_level - robber_level
    # /counter_rob 基礎成功率 COUNTER_ROB_BASE_SUCCESS_RATE；每差 1 等調整 1%
    success_rate = COUNTER_ROB_BASE_SUCCESS_RATE + (level_gap * 0.01)
    success_rate = max(0.05, min(0.95, success_rate))
    roll_ok = random.random() < success_rate

    transferred = 0
    robber_imprisoned = False
    if roll_ok:
        now_cr = now_tw_naive()
        c.execute(
            "UPDATE users SET balance=GREATEST(0, balance-%s), bail_debt=COALESCE(bail_debt,0)+%s WHERE user_id=%s",
            (pay, debt_from_shortfall, str(robber_id)),
        )
        c.execute(
            "SELECT COALESCE(balance,0), COALESCE(in_prison,0) FROM users WHERE user_id=%s",
            (str(robber_id),),
        )
        _rb_after = c.fetchone()
        bal_after = int((_rb_after[0] if _rb_after else 0) or 0)
        already_pr = int((_rb_after[1] if _rb_after else 0) or 0)
        if bal_after == 0 and not already_pr:
            c.execute(
                "UPDATE users SET in_prison=1, prison_start=%s WHERE user_id=%s AND COALESCE(in_prison,0)=0",
                (now_cr, str(robber_id)),
            )
            c.execute(
                """INSERT INTO prison_records
                   (criminal_id, cop_id, wanted_stars, confiscated_amount, cop_reward, bail_cost, arrested_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (str(robber_id), victim_id, 0, pay, 0, BAIL_COST, now_cr),
            )
            robber_imprisoned = True
        c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (doubled, victim_id))
        transferred = doubled

    c.execute(
        "UPDATE users SET revenge_pending=0, revenge_robber_id=NULL, revenge_amount=0 WHERE user_id=%s",
        (victim_id,),
    )
    conn.commit()
    conn.close()
    pct = int(round(success_rate * 100))

    if transferred > 0:
        log_transaction(victim_id, transferred, f"平民加倍搶回（對搶匪:{robber_id}）")
        if pay > 0:
            log_transaction(robber_id, -pay, f"被平民加倍搶回（受害者:{victim_id}）")
        robber_mem = interaction.guild.get_member(int(robber_id))
        rn = robber_mem.display_name if robber_mem else robber_id
        _am_success = discord.AllowedMentions(
            users=[
                discord.Object(id=interaction.user.id),
                discord.Object(id=int(robber_id)),
            ]
        )
        debt_note = ""
        if debt_from_shortfall > 0:
            debt_note = f"\n（搶匪當場僅能支付 `{pay:,}`，差額 **`{debt_from_shortfall:,}`** 已併入其假釋債務 `/bail`）"
        prison_note = ""
        if robber_imprisoned:
            prison_note = f"\n<@{robber_id}> **餘額歸零**，已**入獄**（`/bail` 假釋：`{BAIL_COST:,}` + 累計欠款）。"
        return await interaction.response.send_message(
            f"{interaction.user.mention} **加倍搶回成功！**（本次成功率約 {pct}%）\n"
            f"你已拿回 **`{transferred:,}`** 東雲幣（加倍目標 **`{doubled:,}`**）。\n"
            f"<@{robber_id}>（`{discord.utils.escape_markdown(rn)}`）被扣 **`{pay:,}`** 東雲幣。{debt_note}{prison_note}",
            ephemeral=False,
            allowed_mentions=_am_success,
        )
    _am_shout = discord.AllowedMentions(users=[discord.Object(id=interaction.user.id)])
    return await interaction.response.send_message(
        f"{interaction.user.mention} ❌ **加倍搶回失敗**（本次成功機率約 {pct}%；反制專用基礎成功率公式：基礎 {int(round(COUNTER_ROB_BASE_SUCCESS_RATE * 100))}%，每級差 ±1%）。"
        f"你的**唯一機會**已用盡。",
        ephemeral=False,
        allowed_mentions=_am_shout,
    )


@bot.tree.command(name="bail", description=f"繳納假釋金（基礎 {BAIL_COST:,} + 累計欠款）出獄")
async def bail_slash(interaction: discord.Interaction):
    ensure_user_exists(interaction.user.id, 0)
    uid = str(interaction.user.id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(in_prison,0), COALESCE(balance,0), COALESCE(bail_debt,0) FROM users WHERE user_id=%s",
        (uid,),
    )
    row = c.fetchone()
    if not row or not int(row[0] or 0):
        conn.close()
        return await interaction.response.send_message("你不在監獄裡。", ephemeral=True)
    bal = int(row[1] or 0)
    debt = int(row[2] or 0)
    total_bail = BAIL_COST + debt
    if bal < total_bail:
        conn.close()
        return await interaction.response.send_message(
            f"假釋須繳 **基礎 `{BAIL_COST:,}`**"
            + (f" + **欠款 `{debt:,}`**" if debt else "")
            + f" = **合計 `{total_bail:,}`** 東雲幣，你的餘額不足。",
            ephemeral=True,
        )
    now = now_tw_naive()
    c.execute(
        """UPDATE users SET balance=balance-%s, bail_debt=0, in_prison=0, prison_start=NULL
           WHERE user_id=%s AND balance >= %s""",
        (total_bail, uid, total_bail),
    )
    if c.rowcount == 0:
        conn.close()
        return await interaction.response.send_message("扣款失敗（餘額不足）。", ephemeral=True)
    c.execute(
        "UPDATE prison_records SET released_at=%s WHERE criminal_id=%s AND released_at IS NULL ORDER BY id DESC LIMIT 1",
        (now, uid),
    )
    conn.commit()
    conn.close()
    log_transaction(interaction.user.id, -total_bail, "監獄假釋金（含累計欠款）")
    await interaction.response.send_message(
        f"✅ 已繳納 **`{total_bail:,}`** 東雲幣（基礎 `{BAIL_COST:,}`"
        + (f" + 清償欠款 `{debt:,}`" if debt else "")
        + "），你已出獄。",
        ephemeral=True,
    )


@bot.tree.command(name="rescue", description="破產救濟計畫，餘額為 0 元時可領 1,000 (每人限領 10 次)")
async def rescue(interaction: discord.Interaction):
    ensure_user_exists(interaction.user.id, 50000)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance, last_rescue, rescue_count FROM users WHERE user_id=%s", (str(interaction.user.id),))
    row = c.fetchone()
    if row[0] > 0:
        conn.close()
        return await interaction.response.send_message(
            f"💰 還沒破產（餘額: {row[0]}），請這位賭狗先去賭到傾家蕩產！完全歸零時再來領。",
            ephemeral=True,
        )
    if row[2] >= 10:
        conn.close()
        return await interaction.response.send_message(
            "🚫 抱歉，你的救濟次數已達 10 次上限。這輩子不能再領了，賭鬼！",
            ephemeral=True,
        )
    
    now = now_tw_naive()
    if row[1] and (now - row[1]).total_seconds() < 3600:
        rem = 3600 - (now - row[1]).total_seconds()
        conn.close()
        return await interaction.response.send_message(f"🕒 銀行還不想給你錢！請再等 `{int(rem//60)}` 分鐘。", ephemeral=True)
        
    inflation_mult, _, _ = get_inflation_multiplier()
    rescue_reward = max(500, min(50000, int(1000 * inflation_mult)))
    c.execute(
        "UPDATE users SET balance=balance+%s, last_rescue=%s, rescue_count=rescue_count+1 WHERE user_id=%s",
        (rescue_reward, now, str(interaction.user.id)),
    )
    conn.commit()
    conn.close()
    log_transaction(interaction.user.id, rescue_reward, "賭狗破產救濟")
    claim_no = row[2] + 1
    embed = discord.Embed(title="✅ 破產救濟發放", color=discord.Color.green())
    embed.add_field(name="獲得", value=f"`{rescue_reward:,}` 東雲幣", inline=False)
    embed.add_field(name="累計次數", value=f"`{claim_no}/10`", inline=False)
    embed.set_footer(text="請謹慎下注，避免再次破產")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="bj", description="開始 21 點")
@app_commands.describe(bet="注額")
async def bj(interaction: discord.Interaction, bet: int = 1000):
    if not IS_EVENT_ACTIVE:
        return await interaction_send(interaction, "打烊", ephemeral=True)
    if bet < 100:
        return await interaction_send(interaction, "低消 100", ephemeral=True)
    await interaction_defer_if_needed(interaction)
    await ensure_user_exists_async(interaction.user.id, 50000)
    sv = SetupView(interaction.user, bet)
    await interaction_send(interaction, embed=sv.build_embed(), view=sv)

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
    ensure_user_exists(interaction.user.id, 50000)
    ensure_user_exists(member.id, 0)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=%s", (str(interaction.user.id),))
    sender_before_row = c.fetchone()
    c.execute("SELECT balance FROM users WHERE user_id=%s", (str(member.id),))
    receiver_before_row = c.fetchone()
    sender_before = int((sender_before_row[0] if sender_before_row else 0) or 0)
    receiver_before = int((receiver_before_row[0] if receiver_before_row else 0) or 0)
    c.execute(
        "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
        (amount, str(interaction.user.id), amount)
    )
    if c.rowcount == 0:
        conn.close()
        return await interaction.response.send_message("餘額不足，無法轉帳", ephemeral=True)

    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(member.id)))
    sender_after = sender_before - amount
    receiver_after = receiver_before + amount
    conn.commit()
    conn.close()

    note_text = (note or "").strip()
    if len(note_text) > 100:
        note_text = note_text[:100]
    if note_text:
        out_reason = f"轉帳給 {member.id}（備註: {note_text}）"
        in_reason = f"收到 {interaction.user.id} 的轉帳（備註: {note_text}）"
    else:
        out_reason = f"轉帳給 {member.id}"
        in_reason = f"收到 {interaction.user.id} 的轉帳"
    log_transaction(interaction.user.id, -amount, out_reason)
    log_transaction(member.id, amount, in_reason)

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
    ensure_user_exists(interaction.user.id, 50000)
    if total_amount < count or total_amount <= 0:
        return await interaction.response.send_message("總金額需大於 0，且至少要能每包 1 元。", ephemeral=True)
    if count < 1 or count > 100:
        return await interaction.response.send_message("份數需介於 1 到 100。", ephemeral=True)
    if not try_deduct_balance(interaction.user.id, total_amount, "發送紅包扣款"):
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
    except:
        pass

# ──────────────────────────────────────────────────────────────────────
# E 卡決鬥（仿《賭博默示錄》）：
# 兩大局制，第一大局隨機分派國王方／奴隸方，第二大局交換陣營。
# 國王方持 👑×1 + 🧑×4；奴隸方持 🗡️×1 + 🧑×4。
# 同牌（民vs民）平手 → 各自消耗該牌後進入該大局下一小局。
# 勝負規則（克制）：👑 > 🧑、🧑 > 🗡️、🗡️ > 👑。
# 計分：奴隸贏王 +3；其餘決勝（國王抓平民／平民抓奴隸）+1。
# 兩大局結束後，依雙方積分比例分配彩池（共 2×注額）。
# ──────────────────────────────────────────────────────────────────────
DUEL_CARDS = {
    "king": ("👑", "王"),
    "citizen": ("🧑", "民"),
    "slave": ("🗡️", "奴"),
}


def _duel_resolve_round(em_pick: str, sl_pick: str) -> str:
    """em_pick 為國王方出牌（king/citizen），sl_pick 為奴隸方出牌（slave/citizen）。
    回傳 'emperor'｜'slave'｜'draw'。"""
    if em_pick == "citizen" and sl_pick == "citizen":
        return "draw"
    if em_pick == "king" and sl_pick == "slave":
        return "slave"
    return "emperor"


class EDuelInviteView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
        super().__init__(timeout=120)
        self.challenger = challenger
        self.opponent = opponent
        self.bet = int(bet)
        self.message: typing.Optional[discord.Message] = None
        self.resolved = False

    async def _refund_challenger(self):
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (self.bet, str(self.challenger.id)),
        )
        conn.commit()
        conn.close()
        log_transaction(self.challenger.id, self.bet, "E卡決鬥取消退款")

    @discord.ui.button(label="接受", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("這場決鬥不是邀請你的。", ephemeral=True)
        if self.resolved:
            return await interaction.response.defer()
        if not try_deduct_balance(self.opponent.id, self.bet, "E卡決鬥下注"):
            return await interaction.response.send_message(
                f"❌ 你的餘額不足，需要 `{self.bet:,}` 東雲幣才能接受此決鬥。",
                ephemeral=True,
            )
        self.resolved = True
        for child in self.children:
            child.disabled = True
        match = EDuelMatch(self.challenger, self.opponent, self.bet)
        play = EDuelPlayView(match)
        match.view = play
        match.channel = interaction.channel
        await interaction.response.edit_message(
            content=None,
            embed=match.build_selection_embed(),
            view=play,
        )
        try:
            match.message = await interaction.original_response()
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="拒絕", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("這場決鬥不是邀請你的。", ephemeral=True)
        if self.resolved:
            return await interaction.response.defer()
        self.resolved = True
        await self._refund_challenger()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"❌ {self.opponent.mention} 拒絕了 {self.challenger.mention} 的 E 卡決鬥。"
                f"已退回挑戰者注金 `{self.bet:,}`。"
            ),
            view=self,
        )
        self.stop()

    async def on_timeout(self):
        if self.resolved:
            return
        self.resolved = True
        await self._refund_challenger()
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(
                    content=(
                        f"⌛ {self.opponent.mention} 未在時限內回應 {self.challenger.mention} 的 E 卡決鬥邀請，"
                        f"挑戰者注金 `{self.bet:,}` 已退回。"
                    ),
                    view=self,
                )
        except Exception:
            pass


class _DuelCardButton(discord.ui.Button):
    def __init__(self, key: str, match: "EDuelMatch", picker_id: int, count: int):
        emoji, label = DUEL_CARDS[key]
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"{label} ×{count}",
            emoji=emoji,
        )
        self.key = key
        self.match = match
        self.picker_id = picker_id

    async def callback(self, interaction: discord.Interaction):
        await self.match.record_pick(interaction, self.picker_id, self.key)


class EDuelPickerView(discord.ui.View):
    """每位玩家私下選牌的 ephemeral 視圖；按鈕依當下剩餘持牌動態產生。"""

    def __init__(self, match: "EDuelMatch", picker_id: int):
        super().__init__(timeout=120)
        hand = match.hands.get(picker_id, {})
        for key in ("king", "citizen", "slave"):
            cnt = int(hand.get(key, 0) or 0)
            if cnt <= 0:
                continue
            self.add_item(_DuelCardButton(key, match, picker_id, cnt))


class EDuelMatch:
    """兩大局 E 卡決鬥的對局狀態與流程控制；全程於單一訊息原地更新 embed。"""

    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
        self.challenger = challenger
        self.opponent = opponent
        self.bet = int(bet)
        first_emperor = random.choice([challenger.id, opponent.id])
        first_slave = opponent.id if first_emperor == challenger.id else challenger.id
        self.game_roles: typing.List[typing.Dict[int, str]] = [
            {first_emperor: "emperor", first_slave: "slave"},
            {first_emperor: "slave", first_slave: "emperor"},
        ]
        self.game_no = 1
        self.round_in_game = 1
        self.scores: typing.Dict[int, int] = {challenger.id: 0, opponent.id: 0}
        self.round_history: typing.List[typing.Dict[str, typing.Any]] = []
        self.last_round: typing.Optional[typing.Dict[str, typing.Any]] = None
        self.picks: typing.Dict[int, str] = {}
        self.hands: typing.Dict[int, typing.Dict[str, int]] = self._make_hands(0)
        self.channel: typing.Optional[discord.abc.Messageable] = None
        self.view: typing.Optional["EDuelPlayView"] = None
        self.message: typing.Optional[discord.Message] = None
        self.settled = False

    def _make_hands(self, game_idx: int) -> typing.Dict[int, typing.Dict[str, int]]:
        roles = self.game_roles[game_idx]
        return {
            uid: ({"king": 1, "citizen": 4} if r == "emperor" else {"slave": 1, "citizen": 4})
            for uid, r in roles.items()
        }

    def _role_of(self, uid: int, game_no: typing.Optional[int] = None) -> str:
        gn = game_no or self.game_no
        return self.game_roles[gn - 1].get(uid, "emperor")

    def _uid_for_role(self, role: str, game_no: typing.Optional[int] = None) -> int:
        gn = game_no or self.game_no
        for uid, r in self.game_roles[gn - 1].items():
            if r == role:
                return uid
        return 0

    def _member_for_uid(self, uid: int) -> discord.Member:
        return self.challenger if self.challenger.id == uid else self.opponent

    def _format_hand(self, uid: int) -> str:
        h = self.hands.get(uid, {})
        role = self._role_of(uid)
        if role == "emperor":
            return f"👑 ×{int(h.get('king', 0) or 0)}　🧑 ×{int(h.get('citizen', 0) or 0)}"
        return f"🗡️ ×{int(h.get('slave', 0) or 0)}　🧑 ×{int(h.get('citizen', 0) or 0)}"

    def _role_label(self, role: str) -> str:
        return "👑 國王方" if role == "emperor" else "🗡️ 奴隸方"

    def _player_status_value(self, uid: int) -> str:
        return "✅ 已選牌" if uid in self.picks else "⏳ 等待選牌中…"

    def _last_round_outcome_text(self) -> str:
        entry = self.last_round
        if not entry:
            return ""
        em_member = self._member_for_uid(entry["em_uid"])
        sl_member = self._member_for_uid(entry["sl_uid"])
        em_e, em_l = DUEL_CARDS[entry["em_pick"]]
        sl_e, sl_l = DUEL_CARDS[entry["sl_pick"]]
        if entry["result"] == "draw":
            tag = "🤝 平手（民 vs 民），雙方消耗該牌"
        elif entry["result"] == "emperor":
            tag = f"🏆 {em_member.display_name}（👑 國王方）+1"
        else:
            tag = f"🏆 {sl_member.display_name}（🗡️ 奴隸方）+3"
        return (
            f"第 {entry['game']} 大局・第 {entry['round']} 小局　"
            f"👑 {em_e} {em_l}　vs　🗡️ {sl_e} {sl_l}　— {tag}"
        )

    def build_selection_embed(self) -> discord.Embed:
        em_uid = self._uid_for_role("emperor")
        sl_uid = self._uid_for_role("slave")
        em_member = self._member_for_uid(em_uid)
        sl_member = self._member_for_uid(sl_uid)
        emb = discord.Embed(
            title=f"⚔️ 第 {self.game_no} 大局・第 {self.round_in_game} 小局｜選牌中",
            description=(
                f"{self.challenger.mention}　**VS**　{self.opponent.mention}\n"
                f"注額：`{self.bet:,}`　｜　彩池：`{self.bet * 2:,}` 東雲幣\n"
                f"本大局陣營：👑 {em_member.mention}　vs　🗡️ {sl_member.mention}\n"
                "勝：👑 > 🧑、🧑 > 🗡️、🗡️ > 👑（民vs民平手繼續）\n"
                "計分：奴隸贏王 +3｜其餘決勝 +1"
            ),
            color=0x5865F2,
        )
        if self.last_round:
            emb.add_field(name="上一小局結果", value=self._last_round_outcome_text(), inline=False)
        emb.add_field(
            name=f"{em_member.display_name}　👑 國王方｜剩餘",
            value=self._format_hand(em_uid),
            inline=True,
        )
        emb.add_field(name="\u200b", value="⚔️", inline=True)
        emb.add_field(
            name=f"{sl_member.display_name}　🗡️ 奴隸方｜剩餘",
            value=self._format_hand(sl_uid),
            inline=True,
        )
        emb.add_field(
            name="目前積分",
            value=(
                f"{self.challenger.display_name}: `{self.scores[self.challenger.id]}`　｜　"
                f"{self.opponent.display_name}: `{self.scores[self.opponent.id]}`"
            ),
            inline=False,
        )
        emb.add_field(
            name=self.challenger.display_name,
            value=self._player_status_value(self.challenger.id),
            inline=True,
        )
        emb.add_field(name="\u200b", value="\u200b", inline=True)
        emb.add_field(
            name=self.opponent.display_name,
            value=self._player_status_value(self.opponent.id),
            inline=True,
        )
        emb.set_footer(text="第二大局會交換陣營｜長時間未動作將退款")
        return emb

    def build_picker_embed(self, picker_id: int) -> discord.Embed:
        h = self.hands.get(picker_id, {})
        role = self._role_of(picker_id)
        order = ("king", "citizen") if role == "emperor" else ("slave", "citizen")
        parts = []
        for key in order:
            e, l = DUEL_CARDS[key]
            cnt = int(h.get(key, 0) or 0)
            if cnt > 0:
                parts.append(f"{e} {l} ×{cnt}")
        hand_text = "　".join(parts) if parts else "（已無牌）"
        emb = discord.Embed(
            title=f"🎴 第 {self.game_no} 大局・第 {self.round_in_game} 小局｜選牌",
            description=(
                f"你本大局：{self._role_label(role)}\n"
                f"你的剩餘持牌：{hand_text}\n"
                "勝：👑 > 🧑、🧑 > 🗡️、🗡️ > 👑"
            ),
            color=0x5865F2,
        )
        emb.set_footer(text="送出後不可更改｜長時間未動作將退款")
        return emb

    async def _refresh_message(self) -> None:
        if not self.message or not self.view:
            return
        try:
            await self.message.edit(embed=self.build_selection_embed(), view=self.view)
        except Exception:
            pass

    def _format_history(self) -> str:
        if not self.round_history:
            return "（尚無紀錄）"
        lines = []
        last_game = 0
        for entry in self.round_history:
            if entry["game"] != last_game:
                last_game = entry["game"]
                lines.append(f"**第 {last_game} 大局**")
            em_e, _ = DUEL_CARDS[entry["em_pick"]]
            sl_e, _ = DUEL_CARDS[entry["sl_pick"]]
            em_member = self._member_for_uid(entry["em_uid"])
            sl_member = self._member_for_uid(entry["sl_uid"])
            res = entry["result"]
            if res == "draw":
                tag = "🤝 平手"
            elif res == "emperor":
                tag = f"🏆 {em_member.display_name} +1"
            else:
                tag = f"🏆 {sl_member.display_name} +3"
            lines.append(f"　第 {entry['round']} 小局：👑 {em_e}　vs　🗡️ {sl_e}　— {tag}")
        return "\n".join(lines)

    async def _refund_both(self, reason: str) -> None:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (self.bet, str(self.challenger.id)),
        )
        cur.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (self.bet, str(self.opponent.id)),
        )
        conn.commit()
        conn.close()
        log_transaction(self.challenger.id, self.bet, reason)
        log_transaction(self.opponent.id, self.bet, reason)

    async def _settle_match(self) -> typing.Dict[str, int]:
        pot = self.bet * 2
        s_a = self.scores[self.challenger.id]
        s_b = self.scores[self.opponent.id]
        total = s_a + s_b
        if total <= 0:
            a_amt = self.bet
            b_amt = pot - a_amt
        else:
            a_amt = pot * s_a // total
            b_amt = pot - a_amt
        conn = get_db_connection()
        cur = conn.cursor()
        if a_amt > 0:
            cur.execute(
                "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                (a_amt, str(self.challenger.id)),
            )
        if b_amt > 0:
            cur.execute(
                "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                (b_amt, str(self.opponent.id)),
            )
        conn.commit()
        conn.close()
        if a_amt > 0:
            log_transaction(self.challenger.id, a_amt, f"E卡決鬥分配（積分 {s_a}:{s_b}）")
        if b_amt > 0:
            log_transaction(self.opponent.id, b_amt, f"E卡決鬥分配（積分 {s_a}:{s_b}）")
        return {"a": a_amt, "b": b_amt, "s_a": s_a, "s_b": s_b}

    def _build_final_embed(self, payouts: typing.Dict[str, int]) -> discord.Embed:
        s_a = payouts["s_a"]
        s_b = payouts["s_b"]
        a_amt = payouts["a"]
        b_amt = payouts["b"]
        if s_a == s_b:
            outcome = f"🤝 兩大局後積分平手 **{s_a} : {s_b}**，平分彩池。"
            color = 0xFFD166
        elif s_a > s_b:
            outcome = f"🏆 {self.challenger.mention} 以 **{s_a} : {s_b}** 勝出！"
            color = 0x57F287
        else:
            outcome = f"🏆 {self.opponent.mention} 以 **{s_b} : {s_a}** 勝出！"
            color = 0x57F287
        emb = discord.Embed(title="⚔️ E 卡決鬥｜整場結算", description=outcome, color=color)
        emb.add_field(
            name="積分",
            value=(
                f"{self.challenger.display_name}: `{s_a}`　｜　"
                f"{self.opponent.display_name}: `{s_b}`"
            ),
            inline=False,
        )
        emb.add_field(
            name="彩池分配",
            value=(
                f"{self.challenger.display_name}: `{a_amt:,}`　｜　"
                f"{self.opponent.display_name}: `{b_amt:,}`"
            ),
            inline=False,
        )
        emb.add_field(name="對戰過程", value=self._format_history()[:1024], inline=False)
        emb.set_footer(text="計分：奴贏王 +3｜其餘決勝 +1｜兩大局制（第二大局交換陣營）")
        return emb

    async def record_pick(self, interaction: discord.Interaction, picker_id: int, key: str):
        if interaction.user.id != picker_id:
            return await interaction.response.send_message("這個按鈕不是給你的。", ephemeral=True)
        if self.settled:
            return await interaction.response.send_message("此場決鬥已結束。", ephemeral=True)
        if picker_id in self.picks:
            return await interaction.response.send_message("你本小局已經選過了。", ephemeral=True)
        if int(self.hands.get(picker_id, {}).get(key, 0) or 0) <= 0:
            return await interaction.response.send_message("你已沒有這張牌。", ephemeral=True)
        self.picks[picker_id] = key
        emoji, label = DUEL_CARDS[key]
        confirm_emb = discord.Embed(
            title=f"✅ 已送出（第 {self.game_no} 大局・第 {self.round_in_game} 小局）",
            description=f"你出的是：{emoji} **{label}**\n等待對手出牌與翻牌結算…",
            color=0x57F287,
        )
        await interaction.response.edit_message(content=None, embed=confirm_emb, view=None)
        if not self.settled and len(self.picks) < 2:
            await self._refresh_message()
        if len(self.picks) == 2 and not self.settled:
            await self._process_round()

    async def _process_round(self):
        em_uid = self._uid_for_role("emperor")
        sl_uid = self._uid_for_role("slave")
        em_pick = self.picks[em_uid]
        sl_pick = self.picks[sl_uid]
        em_hand = self.hands[em_uid]
        sl_hand = self.hands[sl_uid]
        em_hand[em_pick] = max(0, int(em_hand.get(em_pick, 0)) - 1)
        sl_hand[sl_pick] = max(0, int(sl_hand.get(sl_pick, 0)) - 1)
        result = _duel_resolve_round(em_pick, sl_pick)
        if result == "emperor":
            self.scores[em_uid] += 1
        elif result == "slave":
            self.scores[sl_uid] += 3
        entry = {
            "game": self.game_no,
            "round": self.round_in_game,
            "em_uid": em_uid,
            "sl_uid": sl_uid,
            "em_pick": em_pick,
            "sl_pick": sl_pick,
            "result": result,
        }
        self.round_history.append(entry)
        self.last_round = entry

        if result == "draw":
            self.picks.clear()
            self.round_in_game += 1
            await self._refresh_message()
            return

        if self.game_no < 2:
            self.game_no = 2
            self.round_in_game = 1
            self.hands = self._make_hands(1)
            self.picks.clear()
            await self._refresh_message()
            return

        self.settled = True
        payouts = await self._settle_match()
        if self.message and self.view:
            for child in self.view.children:
                child.disabled = True
            self.view.stop()
            try:
                await self.message.edit(embed=self._build_final_embed(payouts), view=None)
            except Exception:
                pass

    async def handle_timeout(self, view: "EDuelPlayView") -> None:
        if self.settled or self.view is not view:
            return
        self.settled = True
        await self._refund_both("E卡決鬥逾時退款")
        pending = []
        if self.challenger.id not in self.picks:
            pending.append(self.challenger.mention)
        if self.opponent.id not in self.picks:
            pending.append(self.opponent.mention)
        try:
            for child in view.children:
                child.disabled = True
            if self.message:
                await self.message.edit(
                    content=(
                        f"⌛ E 卡決鬥逾時（第 {self.game_no} 大局・第 {self.round_in_game} 小局未完成；"
                        f"未選：{', '.join(pending) or '—'}）。雙方注金 `{self.bet:,}` 已退回。"
                    ),
                    embed=None,
                    view=view,
                )
        except Exception:
            pass


class EDuelPlayView(discord.ui.View):
    """全場單一訊息使用的選牌 view。"""

    def __init__(self, match: EDuelMatch):
        super().__init__(timeout=300)
        self.match = match

    @discord.ui.button(label="選牌（私下）", style=discord.ButtonStyle.primary, emoji="🎴")
    async def pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        m = self.match
        if interaction.user.id not in (m.challenger.id, m.opponent.id):
            return await interaction.response.send_message("你不是這場決鬥的玩家。", ephemeral=True)
        if m.settled:
            return await interaction.response.send_message("此場決鬥已結束。", ephemeral=True)
        if interaction.user.id in m.picks:
            return await interaction.response.send_message(
                "你本小局已經選過了，等對手出牌。", ephemeral=True
            )
        view = EDuelPickerView(m, interaction.user.id)
        await interaction.response.send_message(
            embed=m.build_picker_embed(interaction.user.id),
            view=view,
            ephemeral=True,
        )

    async def on_timeout(self):
        await self.match.handle_timeout(self)


@bot.tree.command(
    name="duel",
    description="E 卡決鬥（賭博默示錄風）：兩大局制，第二大局交換陣營，依積分分配彩池",
)
@app_commands.describe(member="對手", bet="雙方下注金額（兩邊相同）")
async def duel_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    bet: int,
):
    if not interaction.guild:
        return await interaction_send(interaction, "請在伺服器頻道使用。", ephemeral=True)
    if member.bot:
        return await interaction_send(interaction, "不能對機器人發起決鬥。", ephemeral=True)
    if member.id == interaction.user.id:
        return await interaction_send(interaction, "不能對自己發起決鬥。", ephemeral=True)
    if bet <= 0:
        return await interaction_send(interaction, "注金需大於 0。", ephemeral=True)
    await interaction_defer_if_needed(interaction)
    await ensure_user_exists_async(interaction.user.id, 50000)
    await ensure_user_exists_async(member.id, 50000)
    if not await try_deduct_balance_async(interaction.user.id, bet, "E卡決鬥下注"):
        return await interaction_send(
            interaction,
            f"❌ 你的餘額不足，需要 `{bet:,}` 東雲幣才能發起決鬥。",
            ephemeral=True,
        )
    view = EDuelInviteView(interaction.user, member, bet)
    sent = await interaction_send(
        interaction,
        content=(
            f"⚔️ {interaction.user.mention} 向 {member.mention} 發起 **E 卡決鬥**！\n"
            f"注額：`{bet:,}` 東雲幣\n"
            f"持牌：國王方 👑×1+🧑×4　奴隸方 🗡️×1+🧑×4\n"
            f"勝：👑 > 🧑、🧑 > 🗡️、🗡️ > 👑\n"
            f"計分：奴贏王 +3｜其餘決勝 +1\n"
            f"規則：兩大局制，第一大局隨機分派陣營，第二大局交換；依積分分配彩池\n"
            f"{member.mention} 請於 **120 秒** 內點 **接受** 或 **拒絕**。"
        ),
        view=view,
    )
    try:
        view.message = sent or await interaction.original_response()
    except Exception:
        pass


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
    except:
        pass


async def _is_user_in_guild(guild: discord.Guild, user_id: int) -> bool:
    """以快取或 API 確認使用者是否仍在該伺服器。"""
    if guild.get_member(user_id):
        return True
    try:
        await guild.fetch_member(user_id)
        return True
    except discord.NotFound:
        return False
    except Exception:
        return False


LEADERBOARD_POOL = 400
LEADERBOARD_RANK_SCAN = 800


@bot.tree.command(name="leaderboard", description="前 10 名")
async def leaderboard(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    await ensure_user_exists_async(interaction.user.id, 50000)
    guild = interaction.guild
    conn = get_db_connection()
    c = conn.cursor()
    admin_ids = [str(x) for x in ALLOWED_HOST_IDS]
    ph = ",".join(["%s"] * len(admin_ids))

    c.execute("SELECT balance FROM users WHERE user_id=%s", (str(interaction.user.id),))
    my_row = c.fetchone()
    my_bal = int((my_row[0] if my_row else 0) or 0)

    if guild:
        c.execute(
            f"SELECT user_id, balance FROM users WHERE user_id NOT IN ({ph}) ORDER BY balance DESC LIMIT %s",
            tuple(admin_ids) + (LEADERBOARD_POOL,),
        )
        pool = c.fetchall()
        data: typing.List[typing.Tuple] = []
        for row in pool:
            if len(data) >= 10:
                break
            uid = int(row[0])
            if await _is_user_in_guild(guild, uid):
                data.append(row)

        richer: typing.List[typing.Tuple] = []
        if str(interaction.user.id) in admin_ids:
            my_rank: typing.Union[str, int] = "不列入"
        else:
            c.execute(
                f"SELECT user_id FROM users WHERE user_id NOT IN ({ph}) AND balance > %s ORDER BY balance DESC LIMIT %s",
                tuple(admin_ids) + (my_bal, LEADERBOARD_RANK_SCAN),
            )
            richer = c.fetchall()
            ahead = 0
            for (uid_str,) in richer:
                if await _is_user_in_guild(guild, int(uid_str)):
                    ahead += 1
            my_rank = ahead + 1

        conn.close()
        title = "🏆 排行榜（本伺服器）"
        note = "\n\n※ 僅列出**目前仍在本伺服器**的成員（已退群者不含在內）。"
        if str(interaction.user.id) not in admin_ids and len(richer) >= LEADERBOARD_RANK_SCAN:
            note += f"\n※ 你的名次僅掃描「餘額較高」累計前 {LEADERBOARD_RANK_SCAN} 人中的本群成員；極端情況下為參考值。"
    else:
        c.execute(
            f"SELECT user_id, balance FROM users WHERE user_id NOT IN ({ph}) ORDER BY balance DESC LIMIT 10",
            tuple(admin_ids),
        )
        data = c.fetchall()
        if str(interaction.user.id) in admin_ids:
            my_rank = "不列入"
        else:
            c.execute(
                f"SELECT COUNT(*) FROM users WHERE user_id NOT IN ({ph}) AND balance > %s",
                tuple(admin_ids) + (my_bal,),
            )
            rank_row = c.fetchone()
            my_rank = (rank_row[0] if rank_row else 0) + 1
        conn.close()
        title = "🏆 排行榜（全站）"
        note = "\n\n※ 在伺服器頻道使用時，榜單會改為**僅本伺服器成員**。"

    lines = [f"{i+1}. <@{uid}>: {int(bal):,}" for i, (uid, bal) in enumerate(data)]
    msg = "\n".join(lines) if lines else "（尚無符合條件的成員）"
    msg += f"\n\n📍 你的目前名次：**#{my_rank}**（餘額 `{my_bal:,}`）{note}"
    emb = discord.Embed(title=title, description=msg)
    if guild:
        await interaction.followup.send(embed=emb)
    else:
        await interaction_send(interaction, embed=emb)

@bot.tree.command(name="casino_stats", description="查看經濟總金流統計（回收率/總發幣量/流通量）")
async def casino_stats(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    total_issued, total_recovered, circulation = await fetch_casino_stats_rows_async()

    recovery_rate = (total_recovered / total_issued * 100) if total_issued > 0 else 0.0
    net_issued = total_issued - total_recovered
    embed = discord.Embed(title="🏦 經濟總金流統計", color=0x2b2d31)
    embed.add_field(name="金錢回收率", value=f"`{recovery_rate:.2f}%`", inline=False)
    embed.add_field(name="總發幣量", value=f"`{total_issued:,}` 東雲幣", inline=False)
    embed.add_field(name="總回收量", value=f"`{total_recovered:,}` 東雲幣", inline=False)
    embed.add_field(name="淨發行量", value=f"`{net_issued:,}` 東雲幣", inline=False)
    embed.add_field(name="目前流通量", value=f"`{circulation:,}` 東雲幣", inline=False)
    embed.set_footer(text="計算基準：casino_logs（logs 全期鏡像總帳）與 users.balance")
    await interaction_send(interaction, embed=embed)

@bot.tree.command(name="lvleaderboard", description="等級排行榜前 10 名")
async def lvleaderboard(interaction: discord.Interaction):
    await interaction_defer_if_needed(interaction)
    await ensure_user_exists_async(interaction.user.id, 50000)
    guild = interaction.guild
    conn = get_db_connection()
    c = conn.cursor()
    admin_ids = [str(x) for x in ALLOWED_HOST_IDS]
    ph = ",".join(["%s"] * len(admin_ids))

    c.execute("SELECT level, exp FROM users WHERE user_id=%s", (str(interaction.user.id),))
    me = c.fetchone()
    if me:
        my_level, my_exp = int(me[0] or 1), int(me[1] or 0)
    else:
        my_level, my_exp = 1, 0

    if guild:
        c.execute(
            f"SELECT user_id, level, exp FROM users WHERE user_id NOT IN ({ph}) ORDER BY level DESC, exp DESC LIMIT %s",
            tuple(admin_ids) + (LEADERBOARD_POOL,),
        )
        pool = c.fetchall()
        data: typing.List[typing.Tuple] = []
        for row in pool:
            if len(data) >= 10:
                break
            uid = int(row[0])
            if await _is_user_in_guild(guild, uid):
                data.append(row)

        richer_lv: typing.List[typing.Tuple] = []
        if str(interaction.user.id) in admin_ids:
            my_rank: typing.Union[str, int] = "不列入"
        else:
            c.execute(
                f"""SELECT user_id FROM users WHERE user_id NOT IN ({ph})
                AND (level > %s OR (level = %s AND exp > %s))
                ORDER BY level DESC, exp DESC LIMIT %s""",
                tuple(admin_ids) + (my_level, my_level, my_exp, LEADERBOARD_RANK_SCAN),
            )
            richer_lv = c.fetchall()
            ahead = 0
            for (uid_str,) in richer_lv:
                if await _is_user_in_guild(guild, int(uid_str)):
                    ahead += 1
            my_rank = ahead + 1

        conn.close()
        title = "🧠 Lv 排行榜（本伺服器）"
        note = "\n\n※ 僅列出**目前仍在本伺服器**的成員。"
        if str(interaction.user.id) not in admin_ids and len(richer_lv) >= LEADERBOARD_RANK_SCAN:
            note += f"\n※ 你的名次僅掃描等級／EXP 較高者累計前 {LEADERBOARD_RANK_SCAN} 人中的本群成員；極端情況下為參考值。"
    else:
        c.execute(
            f"SELECT user_id, level, exp FROM users WHERE user_id NOT IN ({ph}) ORDER BY level DESC, exp DESC LIMIT 10",
            tuple(admin_ids),
        )
        data = c.fetchall()
        if str(interaction.user.id) in admin_ids:
            my_rank = "不列入"
        else:
            c.execute(
                f"SELECT COUNT(*) FROM users WHERE user_id NOT IN ({ph}) AND (level > %s OR (level = %s AND exp > %s))",
                tuple(admin_ids) + (my_level, my_level, my_exp),
            )
            rank_row = c.fetchone()
            my_rank = (rank_row[0] if rank_row else 0) + 1
        conn.close()
        title = "🧠 Lv 排行榜（全站）"
        note = "\n\n※ 在伺服器頻道使用時，榜單會改為**僅本伺服器成員**。"

    if not data:
        return await interaction_send(interaction, "目前沒有符合條件的等級資料。", ephemeral=True)

    msg = "\n".join(
        [f"{i+1}. <@{uid}>: Lv.{int(lv)} | EXP {int(exp):,}" for i, (uid, lv, exp) in enumerate(data)]
    )
    msg += f"\n\n📍 你的目前名次：**#{my_rank}**（Lv.{my_level} | EXP {my_exp:,}）{note}"
    emb = discord.Embed(title=title, description=msg)
    if guild:
        await interaction.followup.send(embed=emb)
    else:
        await interaction_send(interaction, embed=emb)

# ··············································································
# （【十四】錦標賽 — 仍屬 [G · 玩家 Slash]）
# ··············································································

# ==============================================================================
# 【十四】Slash 指令：錦標賽（報名、賽程、比分、晉級與管理員裁定）
# 報名與卡組、發布對戰表、比分提交與雙方確認、晉級鏈、管理員改判／重開場次等。
# ==============================================================================

@bot.tree.command(name="check_players", description="[管理員] 查看所有報名玩家與卡組")
async def check_players(interaction: discord.Interaction):
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 你沒有管理員權限。", ephemeral=True)
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT player_game_id, deck_name, deck_image_url FROM tournament_players ORDER BY created_at ASC")
        rows = cursor.fetchall()
        if not rows:
            return await interaction.response.send_message("目前無人報名。", ephemeral=True)

        lines = ["📋 **報名清單：**"]
        for player_game_id, deck_name, deck_image_url in rows:
            pid = player_game_id or "未知玩家"
            dname = deck_name or "未命名卡組"
            if deck_image_url:
                lines.append(f"👤 {pid} - {dname} [查看卡組]({deck_image_url})")
            else:
                lines.append(f"👤 {pid} - {dname}（無卡組連結）")

        chunks = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 1900:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        await interaction.response.send_message(chunks[0], ephemeral=True)
        for part in chunks[1:]:
            await interaction.followup.send(part, ephemeral=True)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@bot.tree.command(name="publish_bracket", description="[管理員] 開賽並建立單淘 BO3 賽程")
async def publish_bracket(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 你沒有管理員權限。", ephemeral=True)
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT player_game_id FROM tournament_players ORDER BY created_at ASC")
        rows = cursor.fetchall()
        players = [r[0] for r in rows]
        num_p = len(players)
        if num_p < 2:
            return await interaction.response.send_message("至少需要 2 名玩家才能發布對戰表。", ephemeral=True)

        bracket_size = 1
        while bracket_size < num_p:
            bracket_size *= 2
        total_rounds = int(math.log2(bracket_size))

        cursor.execute("DELETE FROM tournament_matches")
        cursor.execute("UPDATE tournament_meta SET status='running', total_rounds=%s, current_round=1, champion_player_id=NULL, started_at=NOW() WHERE id=1", (total_rounds,))

        seeded = players + [None] * (bracket_size - num_p)

        # 建立全部輪次的 match 殼，後續晉級直接寫入既有槽位
        for rnd in range(1, total_rounds + 1):
            match_count = bracket_size // (2 ** rnd)
            for m in range(1, match_count + 1):
                cursor.execute(
                    "INSERT INTO tournament_matches (round_no, match_no) VALUES (%s, %s)",
                    (rnd, m)
                )

        auto_winners = []
        first_round_pairs = []
        for idx in range(0, bracket_size, 2):
            match_no = (idx // 2) + 1
            p1 = seeded[idx]
            p2 = seeded[idx + 1]
            cursor.execute(
                "UPDATE tournament_matches SET p1_player_id=%s, p2_player_id=%s WHERE round_no=1 AND match_no=%s",
                (p1, p2, match_no)
            )
            if p1 and p2:
                first_round_pairs.append(f"M{match_no}: `{p1}` vs `{p2}`")
            elif p1 and not p2:
                cursor.execute(
                    "UPDATE tournament_matches SET winner_player_id=%s, status='completed', p1_score=2, p2_score=0, p1_confirmed=1, p2_confirmed=1 WHERE round_no=1 AND match_no=%s",
                    (p1, match_no)
                )
                auto_winners.append((1, match_no, p1))
            elif p2 and not p1:
                cursor.execute(
                    "UPDATE tournament_matches SET winner_player_id=%s, status='completed', p1_score=0, p2_score=2, p1_confirmed=1, p2_confirmed=1 WHERE round_no=1 AND match_no=%s",
                    (p2, match_no)
                )
                auto_winners.append((1, match_no, p2))

        for rno, mno, winner in auto_winners:
            _advance_winner(conn, rno, mno, winner, total_rounds)
        _refresh_champion_if_single_left(conn)

        conn.commit()

        cursor.execute(
            "SELECT round_no, match_no, p1_player_id, p2_player_id, p1_score, p2_score, winner_player_id, status FROM tournament_matches ORDER BY round_no, match_no"
        )
        match_rows = cursor.fetchall()
        match_dicts = [
            {
                "round_no": r[0], "match_no": r[1], "p1_player_id": r[2], "p2_player_id": r[3],
                "p1_score": r[4], "p2_score": r[5], "winner_player_id": r[6], "status": r[7]
            }
            for r in match_rows
        ]
        lines = _build_tournament_bracket_lines(match_dicts, total_rounds)

        embed = discord.Embed(
            title="🏆 BO3 單淘汰賽程已建立",
            description=f"總人數：**{num_p}**｜總輪數：**{total_rounds}**",
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        if first_round_pairs:
            embed.add_field(name="⚔️ 第一輪對戰", value="\n".join(first_round_pairs)[:1024], inline=False)
        preview = "\n".join(lines[:20])
        if preview:
            embed.add_field(name="📌 賽程總覽", value=preview[:1024], inline=False)
        embed.set_footer(text="玩家可用 /tournament_submit_score 提交比分，/tournament_confirm_score 確認後自動晉級。")
        await interaction.response.send_message(embed=embed)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@bot.tree.command(name="tournament_register", description="報名比賽並填寫卡組")
@app_commands.describe(deck_name="卡組名稱", deck_image_url="卡組圖片連結(必填)")
async def tournament_register(interaction: discord.Interaction, deck_name: str, deck_image_url: str):
    pid = str(interaction.user.id)
    dname = deck_name.strip()
    durl = deck_image_url.strip()
    if not dname or not durl:
        return await interaction.response.send_message("卡組名稱、卡組圖片連結皆為必填。", ephemeral=True)
    reg_start, reg_end = get_tournament_window()
    now = now_tw_naive()
    if not reg_start:
        return await interaction.response.send_message("⛔ 目前未開放報名，請管理員先設定 `/tournament_window_set`。", ephemeral=True)
    if reg_start and now < reg_start:
        ts = tw_naive_to_discord_ts(reg_start)
        return await interaction.response.send_message(f"⏳ 報名尚未開始，開始時間：<t:{ts}:F>", ephemeral=True)
    if reg_end and now > reg_end:
        ts = tw_naive_to_discord_ts(reg_end)
        return await interaction.response.send_message(f"⛔ 報名已截止，截止時間：<t:{ts}:F>", ephemeral=True)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM tournament_players WHERE player_game_id=%s", (pid,))
    exists = c.fetchone()
    if exists:
        conn.close()
        return await interaction.response.send_message("你已報名，請改用 `/tournament_update_deck`。", ephemeral=True)
    c.execute("SELECT player_game_id FROM tournament_players WHERE player_discord_id=%s LIMIT 1", (str(interaction.user.id),))
    same_user = c.fetchone()
    if same_user:
        conn.close()
        return await interaction.response.send_message(f"你已報名過一次（ID：`{same_user[0]}`），每人僅可報名一次。", ephemeral=True)
    try:
        c.execute(
            "INSERT INTO tournament_players (player_game_id, player_discord_id, deck_name, deck_image_url) VALUES (%s, %s, %s, %s)",
            (pid, str(interaction.user.id), dname, durl)
        )
    except pymysql.err.IntegrityError:
        conn.close()
        return await interaction.response.send_message("你已報名過一次，每人僅可報名一次。", ephemeral=True)
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ 報名成功：`{pid}`（你的 Discord ID）- `{dname}`")

@bot.tree.command(name="tournament_update_deck", description="更新自己報名的卡組資料")
@app_commands.describe(player_game_id="比賽用玩家ID", deck_name="新卡組名稱", deck_image_url="新卡組圖片連結(必填)")
async def tournament_update_deck(interaction: discord.Interaction, player_game_id: str, deck_name: str, deck_image_url: str):
    pid = player_game_id.strip()
    dname = deck_name.strip()
    durl = deck_image_url.strip()
    if not pid or not dname or not durl:
        return await interaction.response.send_message("玩家ID、卡組名稱、卡組圖片連結皆為必填。", ephemeral=True)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT player_game_id FROM tournament_players WHERE player_discord_id=%s LIMIT 1", (str(interaction.user.id),))
    owned = c.fetchone()
    if not owned:
        conn.close()
        return await interaction.response.send_message("你尚未報名，無法更新卡組。", ephemeral=True)
    if owned[0] != pid:
        conn.close()
        return await interaction.response.send_message(f"你只能更新自己報名的 ID：`{owned[0]}`", ephemeral=True)
    c.execute(
        "UPDATE tournament_players SET deck_name=%s, deck_image_url=%s WHERE player_game_id=%s AND player_discord_id=%s",
        (dname, durl, pid, str(interaction.user.id))
    )
    conn.commit()
    affected = c.rowcount
    conn.close()
    if affected == 0:
        return await interaction.response.send_message("更新失敗，請確認報名資料。", ephemeral=True)
    await interaction.response.send_message(f"✅ 已更新 `{pid}` 的卡組資料。")

@bot.tree.command(name="tournament_remove", description="[管理員] 取消玩家報名")
@app_commands.describe(player_game_id="比賽用玩家ID")
async def tournament_remove(interaction: discord.Interaction, player_game_id: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 你沒有管理員權限。", ephemeral=True)
    pid = player_game_id.strip()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM tournament_players WHERE player_game_id=%s", (pid,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    if affected == 0:
        return await interaction.response.send_message("找不到該玩家ID。", ephemeral=True)
    await interaction.response.send_message(f"🗑️ 已取消 `{pid}` 的報名。")

@bot.tree.command(name="tournament_list", description="查看比賽報名名單 ID（可翻頁）")
@app_commands.describe(page="頁碼（每頁 20 筆）")
async def tournament_list(interaction: discord.Interaction, page: int = 1):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT player_game_id, created_at FROM tournament_players ORDER BY created_at ASC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return await interaction.response.send_message("目前無人報名。", ephemeral=True)
    page_size = 20
    page = max(1, int(page))
    all_lines = [f"{i+1}. `{pid}`" for i, (pid, _) in enumerate(rows)]
    view = LinePagerView(
        owner_id=interaction.user.id,
        title="📋 比賽報名名單",
        lines=all_lines,
        page_size=page_size,
        start_page=page,
        footer_prefix=f"共 {len(rows)} 人"
    )
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
    try:
        view.message = await interaction.original_response()
    except:
        pass

@bot.tree.command(name="tournament_window_set", description="[管理員] 設定報名起始與截止時間")
@app_commands.describe(
    start_time="開始時間（台灣時間）格式：YYYY-MM-DD HH:MM",
    end_time="截止時間（台灣時間）格式：YYYY-MM-DD HH:MM"
)
async def tournament_window_set(interaction: discord.Interaction, start_time: str, end_time: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 你沒有管理員權限。", ephemeral=True)
    try:
        reg_start = parse_tw_datetime(start_time)
        reg_end = parse_tw_datetime(end_time)
    except Exception:
        return await interaction.response.send_message("時間格式錯誤，請用 `YYYY-MM-DD HH:MM`。", ephemeral=True)
    if reg_end <= reg_start:
        return await interaction.response.send_message("截止時間必須晚於開始時間。", ephemeral=True)
    set_tournament_window(reg_start, reg_end)
    s_ts = tw_naive_to_discord_ts(reg_start)
    e_ts = tw_naive_to_discord_ts(reg_end)
    await interaction.response.send_message(
        f"✅ 已設定報名時間窗：\n開始：<t:{s_ts}:F>\n截止：<t:{e_ts}:F>",
        ephemeral=True
    )

@bot.tree.command(name="tournament_window_show", description="查看目前報名起始與截止時間")
async def tournament_window_show(interaction: discord.Interaction):
    reg_start, reg_end = get_tournament_window()
    if not reg_start and not reg_end:
        return await interaction.response.send_message("目前尚未設定報名時間窗（預設為關閉報名）。", ephemeral=True)
    s_ts = tw_naive_to_discord_ts(reg_start) if reg_start else None
    e_ts = tw_naive_to_discord_ts(reg_end) if reg_end else None
    msg = "🗓️ 目前報名時間窗：\n"
    msg += f"開始：{f'<t:{s_ts}:F>' if s_ts else '未設定'}\n"
    msg += f"截止：{f'<t:{e_ts}:F>' if e_ts else '未設定'}"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="tournament_bracket", description="查看目前 BO3 單淘汰賽程與進度")
async def tournament_bracket(interaction: discord.Interaction):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT status, total_rounds, current_round, champion_player_id FROM tournament_meta WHERE id=1")
    meta = c.fetchone()
    c.execute(
        "SELECT round_no, match_no, p1_player_id, p2_player_id, p1_score, p2_score, winner_player_id, status FROM tournament_matches ORDER BY round_no, match_no"
    )
    rows = c.fetchall()
    c.execute("SELECT player_game_id FROM tournament_players ORDER BY created_at ASC")
    players = c.fetchall()
    conn.close()
    status = (meta[0] if meta else "running") or "running"
    total_rounds = (meta[1] if meta else 1) or 1
    current_round = (meta[2] if meta else 1) or 1
    champion = meta[3] if meta else None
    if not rows:
        registered = len(players)
        embed = discord.Embed(
            title="🏟️ 目前賽程",
            description=f"狀態：**{status}**（尚未開賽）\n目前報名人數：**{registered}**",
            color=0x2b2d31
        )
        embed.add_field(name="提示", value="管理員可使用 `/publish_bracket` 建立賽程。", inline=False)
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    payload = [
        {
            "round_no": r[0], "match_no": r[1], "p1_player_id": r[2], "p2_player_id": r[3],
            "p1_score": r[4], "p2_score": r[5], "winner_player_id": r[6], "status": r[7]
        }
        for r in rows
    ]
    lines = _build_tournament_bracket_lines(payload, total_rounds)
    embed = discord.Embed(
        title="🏟️ 目前賽程",
        description=f"狀態：**{status}**｜目前輪次：**R{current_round}**",
        color=0x2b2d31
    )
    if champion:
        embed.add_field(name="👑 冠軍", value=f"`{champion}`", inline=False)
    text = "\n".join(lines)
    if len(text) <= 3900:
        embed.add_field(name="對戰表", value=text[:1024], inline=False)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message(embed=embed)
        chunks = [text[i:i+1800] for i in range(0, len(text), 1800)]
        for ch in chunks:
            await interaction.followup.send(ch, ephemeral=True)

@bot.tree.command(name="tournament_submit_score", description="提交本場 BO3 比分（待雙方確認）")
@app_commands.describe(round_no="輪次（例如 1）", match_no="場次（例如 2）", my_score="你的局數（BO3 請填 0-2）", opponent_score="對手局數（BO3 請填 0-2）")
async def tournament_submit_score(interaction: discord.Interaction, round_no: int, match_no: int, my_score: int, opponent_score: int):
    if my_score < 0 or opponent_score < 0 or my_score > 2 or opponent_score > 2:
        return await interaction.response.send_message("比分必須在 0~2。", ephemeral=True)
    if my_score == opponent_score:
        return await interaction.response.send_message("BO3 不可平手，請重新輸入。", ephemeral=True)
    if max(my_score, opponent_score) != 2:
        return await interaction.response.send_message("BO3 需由其中一方先達到 2 勝。", ephemeral=True)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT player_game_id FROM tournament_players WHERE player_discord_id=%s LIMIT 1", (str(interaction.user.id),))
    player_row = c.fetchone()
    player_id = player_row[0] if player_row else str(interaction.user.id)
    c.execute(
        "SELECT p1_player_id, p2_player_id, status FROM tournament_matches WHERE round_no=%s AND match_no=%s",
        (round_no, match_no)
    )
    m = c.fetchone()
    if not m:
        conn.close()
        return await interaction.response.send_message("找不到這場對戰。", ephemeral=True)
    p1, p2, status = m
    if status == "completed":
        conn.close()
        return await interaction.response.send_message("此對戰已完賽。", ephemeral=True)
    if player_id not in (p1, p2):
        conn.close()
        return await interaction.response.send_message("你不是這場對戰的選手。", ephemeral=True)
    if not p1 or not p2:
        conn.close()
        return await interaction.response.send_message("此場次尚未湊齊雙方選手。", ephemeral=True)

    p1_score = my_score if player_id == p1 else opponent_score
    p2_score = opponent_score if player_id == p1 else my_score
    c.execute(
        "UPDATE tournament_matches SET p1_score=%s, p2_score=%s, p1_confirmed=0, p2_confirmed=0, reported_by=%s, reported_at=NOW(), status='pending' WHERE round_no=%s AND match_no=%s",
        (p1_score, p2_score, player_id, round_no, match_no)
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"📝 已提交 R{round_no} M{match_no} 比分：`{p1}` {p1_score} : {p2_score} `{p2}`\n請雙方使用 `/tournament_confirm_score` 確認。"
    )

@bot.tree.command(name="tournament_confirm_score", description="確認（或駁回）本場提交比分，雙方確認後自動晉級")
@app_commands.describe(round_no="輪次", match_no="場次", approve="是否同意這次提交的比分")
async def tournament_confirm_score(interaction: discord.Interaction, round_no: int, match_no: int, approve: bool = True):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT player_game_id FROM tournament_players WHERE player_discord_id=%s LIMIT 1", (str(interaction.user.id),))
    player_row = c.fetchone()
    player_id = player_row[0] if player_row else str(interaction.user.id)
    c.execute("SELECT total_rounds FROM tournament_meta WHERE id=1")
    meta = c.fetchone()
    total_rounds = (meta[0] if meta else 1) or 1
    c.execute(
        "SELECT p1_player_id, p2_player_id, p1_score, p2_score, p1_confirmed, p2_confirmed, status FROM tournament_matches WHERE round_no=%s AND match_no=%s",
        (round_no, match_no)
    )
    m = c.fetchone()
    if not m:
        conn.close()
        return await interaction.response.send_message("找不到這場對戰。", ephemeral=True)
    p1, p2, p1_score, p2_score, p1_cf, p2_cf, status = m
    if status == "completed":
        conn.close()
        return await interaction.response.send_message("此對戰已完賽。", ephemeral=True)
    if player_id not in (p1, p2):
        conn.close()
        return await interaction.response.send_message("你不是這場對戰的選手。", ephemeral=True)
    if p1_score is None or p2_score is None:
        conn.close()
        return await interaction.response.send_message("目前尚未提交比分。", ephemeral=True)

    if not approve:
        c.execute(
            "UPDATE tournament_matches SET p1_score=NULL, p2_score=NULL, p1_confirmed=0, p2_confirmed=0, reported_by=NULL, reported_at=NULL WHERE round_no=%s AND match_no=%s",
            (round_no, match_no)
        )
        conn.commit()
        conn.close()
        return await interaction.response.send_message("↩️ 你已駁回比分，請重新提交。")

    if player_id == p1 and not p1_cf:
        c.execute("UPDATE tournament_matches SET p1_confirmed=1 WHERE round_no=%s AND match_no=%s", (round_no, match_no))
    elif player_id == p2 and not p2_cf:
        c.execute("UPDATE tournament_matches SET p2_confirmed=1 WHERE round_no=%s AND match_no=%s", (round_no, match_no))

    c.execute(
        "SELECT p1_score, p2_score, p1_confirmed, p2_confirmed FROM tournament_matches WHERE round_no=%s AND match_no=%s",
        (round_no, match_no)
    )
    latest = c.fetchone()
    lp1, lp2, lp1cf, lp2cf = latest
    if lp1cf and lp2cf:
        winner = p1 if lp1 > lp2 else p2
        c.execute(
            "UPDATE tournament_matches SET winner_player_id=%s, status='completed' WHERE round_no=%s AND match_no=%s",
            (winner, round_no, match_no)
        )
        _advance_winner(conn, round_no, match_no, winner, total_rounds)
        _refresh_champion_if_single_left(conn)
        c.execute(
            "SELECT MIN(round_no) FROM tournament_matches WHERE status <> 'completed'"
        )
        next_round_row = c.fetchone()
        next_round = next_round_row[0] if next_round_row and next_round_row[0] else total_rounds
        c.execute("UPDATE tournament_meta SET current_round=%s WHERE id=1", (next_round,))
        conn.commit()
        conn.close()
        return await interaction.response.send_message(
            f"✅ R{round_no} M{match_no} 比分確認完成：`{p1}` {lp1}:{lp2} `{p2}`\n🏁 晉級：`{winner}`"
        )

    conn.commit()
    conn.close()
    await interaction.response.send_message("✅ 已記錄你的確認，等待對手確認。")

@bot.tree.command(name="tournament_admin_set_result", description="[管理員] 直接裁定某場比分並自動晉級")
@app_commands.describe(round_no="輪次", match_no="場次", p1_score="P1 局數", p2_score="P2 局數")
async def tournament_admin_set_result(interaction: discord.Interaction, round_no: int, match_no: int, p1_score: int, p2_score: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 你沒有管理員權限。", ephemeral=True)
    if p1_score < 0 or p2_score < 0 or p1_score > 2 or p2_score > 2:
        return await interaction.response.send_message("比分必須在 0~2。", ephemeral=True)
    if p1_score == p2_score:
        return await interaction.response.send_message("BO3 不可平手。", ephemeral=True)
    if max(p1_score, p2_score) != 2:
        return await interaction.response.send_message("BO3 需由其中一方先達到 2 勝。", ephemeral=True)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT total_rounds FROM tournament_meta WHERE id=1")
    meta = c.fetchone()
    total_rounds = (meta[0] if meta else 1) or 1
    c.execute(
        "SELECT p1_player_id, p2_player_id, status FROM tournament_matches WHERE round_no=%s AND match_no=%s",
        (round_no, match_no)
    )
    m = c.fetchone()
    if not m:
        conn.close()
        return await interaction.response.send_message("找不到這場對戰。", ephemeral=True)
    p1, p2, status = m
    if not p1 or not p2:
        conn.close()
        return await interaction.response.send_message("此場次尚未湊齊雙方選手。", ephemeral=True)
    if status == "completed":
        conn.close()
        return await interaction.response.send_message("此對戰已完賽。", ephemeral=True)

    winner = p1 if p1_score > p2_score else p2
    c.execute(
        "UPDATE tournament_matches SET p1_score=%s, p2_score=%s, p1_confirmed=1, p2_confirmed=1, winner_player_id=%s, status='completed', reported_by=%s, reported_at=NOW() WHERE round_no=%s AND match_no=%s",
        (p1_score, p2_score, winner, f"admin:{interaction.user.id}", round_no, match_no)
    )
    _advance_winner(conn, round_no, match_no, winner, total_rounds)
    _refresh_champion_if_single_left(conn)
    c.execute("SELECT MIN(round_no) FROM tournament_matches WHERE status <> 'completed'")
    next_round_row = c.fetchone()
    next_round = next_round_row[0] if next_round_row and next_round_row[0] else total_rounds
    c.execute("UPDATE tournament_meta SET current_round=%s WHERE id=1", (next_round,))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"🛠️ 已裁定 R{round_no} M{match_no}：`{p1}` {p1_score}:{p2_score} `{p2}`\n🏁 晉級：`{winner}`"
    )

@bot.tree.command(name="tournament_admin_advance", description="[管理員] 指定某場晉級者（棄權/失聯）")
@app_commands.describe(round_no="輪次", match_no="場次", winner_player_id="晉級玩家ID（需為該場選手）", reason="原因（例如 棄權/失聯）")
async def tournament_admin_advance(interaction: discord.Interaction, round_no: int, match_no: int, winner_player_id: str, reason: str = "管理員裁定"):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 你沒有管理員權限。", ephemeral=True)
    winner_player_id = winner_player_id.strip()
    if not winner_player_id:
        return await interaction.response.send_message("請填入有效的 winner_player_id。", ephemeral=True)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT total_rounds FROM tournament_meta WHERE id=1")
    meta = c.fetchone()
    total_rounds = (meta[0] if meta else 1) or 1
    c.execute(
        "SELECT p1_player_id, p2_player_id, status FROM tournament_matches WHERE round_no=%s AND match_no=%s",
        (round_no, match_no)
    )
    m = c.fetchone()
    if not m:
        conn.close()
        return await interaction.response.send_message("找不到這場對戰。", ephemeral=True)
    p1, p2, status = m
    if status == "completed":
        conn.close()
        return await interaction.response.send_message("此對戰已完賽。", ephemeral=True)
    if winner_player_id not in (p1, p2):
        conn.close()
        return await interaction.response.send_message("指定晉級者不是此場選手。", ephemeral=True)

    if winner_player_id == p1:
        p1_score, p2_score = 2, 0
    else:
        p1_score, p2_score = 0, 2
    c.execute(
        "UPDATE tournament_matches SET p1_score=%s, p2_score=%s, p1_confirmed=1, p2_confirmed=1, winner_player_id=%s, status='completed', reported_by=%s, reported_at=NOW() WHERE round_no=%s AND match_no=%s",
        (p1_score, p2_score, winner_player_id, f"admin:{interaction.user.id}:{reason[:80]}", round_no, match_no)
    )
    _advance_winner(conn, round_no, match_no, winner_player_id, total_rounds)
    _refresh_champion_if_single_left(conn)
    c.execute("SELECT MIN(round_no) FROM tournament_matches WHERE status <> 'completed'")
    next_round_row = c.fetchone()
    next_round = next_round_row[0] if next_round_row and next_round_row[0] else total_rounds
    c.execute("UPDATE tournament_meta SET current_round=%s WHERE id=1", (next_round,))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"⏭️ 已指定 R{round_no} M{match_no} 晉級：`{winner_player_id}`（{reason}）"
    )

@bot.tree.command(name="tournament_admin_reopen_match", description="[管理員] 重新開啟已完賽場次並回滾後續晉級")
@app_commands.describe(round_no="輪次", match_no="場次")
async def tournament_admin_reopen_match(interaction: discord.Interaction, round_no: int, match_no: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 你沒有管理員權限。", ephemeral=True)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT total_rounds FROM tournament_meta WHERE id=1")
    meta = c.fetchone()
    total_rounds = (meta[0] if meta else 1) or 1
    c.execute(
        "SELECT p1_player_id, p2_player_id, status FROM tournament_matches WHERE round_no=%s AND match_no=%s",
        (round_no, match_no)
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return await interaction.response.send_message("找不到這場對戰。", ephemeral=True)
    p1, p2, status = row
    if status != "completed":
        conn.close()
        return await interaction.response.send_message("此場尚未完賽，不需要重開。", ephemeral=True)

    c.execute(
        "UPDATE tournament_matches SET p1_score=NULL, p2_score=NULL, p1_confirmed=0, p2_confirmed=0, winner_player_id=NULL, status='pending', reported_by=NULL, reported_at=NULL WHERE round_no=%s AND match_no=%s",
        (round_no, match_no)
    )
    _clear_downstream_from_match(conn, round_no, match_no, total_rounds)
    c.execute("UPDATE tournament_meta SET status='running', champion_player_id=NULL WHERE id=1")
    _refresh_champion_if_single_left(conn)
    c.execute("SELECT MIN(round_no) FROM tournament_matches WHERE status <> 'completed'")
    next_round_row = c.fetchone()
    next_round = next_round_row[0] if next_round_row and next_round_row[0] else 1
    c.execute("UPDATE tournament_meta SET current_round=%s WHERE id=1", (next_round,))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"🔄 已重開 R{round_no} M{match_no}（`{p1 or 'TBD'}` vs `{p2 or 'TBD'}`），並回滾後續晉級鏈。"
    )

# ··············································································
# [H · 主機後台與程式進入點]
# ··············································································

# ==============================================================================
# 【十五】Slash：主機後台、長文分行工具與程式進入點
# 限 ALLOWED_HOST_IDS 的 /give、/ban、全服重置等；_chunk_text_lines 供多則訊息列表
# 排版；最後以 DISCORD_TOKEN 啟動 bot。
# ==============================================================================


def is_slash_host(interaction: discord.Interaction):
    return interaction.user.id in ALLOWED_HOST_IDS


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


@bot.tree.command(name="setlevel", description="[管理員] 直接設定玩家等級")
@app_commands.describe(
    level="要設定到幾等（1~100）",
    member="玩家（選人）",
    user_id="或填使用者 ID／貼提及",
)
async def setlevel_slash(
    interaction: discord.Interaction,
    level: int,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    if level < 1 or level > MAX_LEVEL:
        return await interaction.response.send_message(f"等級需介於 1~{MAX_LEVEL}。", ephemeral=True)
    m_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=False
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    member = m_user

    ensure_user_exists(member.id, 0)
    lv_row = get_level_stats(member.id)
    old_exp = int(lv_row[0] or 0) if lv_row else 0
    old_level = int(lv_row[1] or 1) if lv_row else 1
    target_exp = exp_required_for_level(level)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET level=%s, exp=%s WHERE user_id=%s", (int(level), int(target_exp), str(member.id)))
    conn.commit()
    conn.close()

    milestone_note = ""
    if level > old_level:
        crossed = [m for m in LEVEL_MILE_TIERS if old_level < m <= level]
        if crossed:
            await process_level_ups(member, old_level, level)
            milestone_note = f"\n🎯 已同步觸發里程碑流程：Lv.{', '.join(map(str, crossed))}"

    await interaction.response.send_message(
        f"✅ 已將 {member.mention} 設定為 **Lv.{level}**\n"
        f"原本：Lv.{old_level} / EXP `{old_exp:,}`\n"
        f"現在：Lv.{level} / EXP `{target_exp:,}`"
        f"{milestone_note}"
    )

@bot.tree.command(name="give", description="[管理員] 發放東雲幣給玩家")
@app_commands.describe(
    amount="發放數量",
    member="玩家（選人）",
    user_id="或填使用者 ID／貼提及",
    note="備註（選填）",
)
async def give_slash(
    interaction: discord.Interaction,
    amount: int,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
    note: str = "",
):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("數量必須大於 0", ephemeral=True)
    m_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=False
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    member = m_user
    ensure_user_exists(member.id, 0)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=%s", (str(member.id),))
    before_row = c.fetchone()
    before_bal = int((before_row[0] if before_row else 0) or 0)
    c.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, str(member.id)))
    after_bal = before_bal + amount
    conn.commit()
    conn.close()
    note_text = (note or "").strip()
    if len(note_text) > 100:
        note_text = note_text[:100]
    reason = f"管理員發放（備註: {note_text}）" if note_text else "管理員發放"
    log_transaction(member.id, amount, reason)
    embed = discord.Embed(title="✅ 發放成功", color=discord.Color.green())
    embed.add_field(name="對象", value=member.mention, inline=False)
    embed.add_field(name="發放金額", value=f"`{amount:,}` 東雲幣", inline=False)
    embed.add_field(name="餘額變化", value=f"`{before_bal:,}` → `{after_bal:,}`", inline=False)
    embed.add_field(name="備註", value=note_text if note_text else "（無）", inline=False)
    embed.set_footer(text=f"操作人：{interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="take", description="[管理員] 扣除玩家東雲幣")
@app_commands.describe(
    amount="扣除數量",
    member="玩家（選人）",
    user_id="或填使用者 ID／貼提及",
    note="備註（選填）",
)
async def take_slash(
    interaction: discord.Interaction,
    amount: int,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
    note: str = "",
):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("數量必須大於 0", ephemeral=True)
    m_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=False
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    member = m_user
    ensure_user_exists(member.id, 0)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=%s", (str(member.id),))
    before_row = c.fetchone()
    before_bal = int((before_row[0] if before_row else 0) or 0)
    c.execute("UPDATE users SET balance=GREATEST(0, balance-%s) WHERE user_id=%s", (amount, str(member.id)))
    after_bal = max(0, before_bal - amount)
    conn.commit()
    conn.close()
    note_text = (note or "").strip()
    if len(note_text) > 100:
        note_text = note_text[:100]
    reason = f"管理員扣除（備註: {note_text}）" if note_text else "管理員扣除"
    log_transaction(member.id, -amount, reason)
    embed = discord.Embed(title="✅ 扣款成功", color=discord.Color.green())
    embed.add_field(name="對象", value=member.mention, inline=False)
    embed.add_field(name="扣除金額", value=f"`{amount:,}` 東雲幣", inline=False)
    embed.add_field(name="餘額變化", value=f"`{before_bal:,}` → `{after_bal:,}`", inline=False)
    embed.add_field(name="備註", value=note_text if note_text else "（無）", inline=False)
    embed.set_footer(text=f"操作人：{interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ban", description="[管理員] 將玩家加入黑名單")
@app_commands.describe(member="玩家（選人）", user_id="或填使用者 ID／貼提及")
async def ban_slash(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    m_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=False
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    member = m_user
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT IGNORE INTO blacklist (user_id) VALUES (%s)", (str(member.id),))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"{member.mention} 已加入黑名單。")

@bot.tree.command(name="unban", description="[管理員] 將玩家移出黑名單")
@app_commands.describe(member="玩家（選人，未必在伺服器）", user_id="或填使用者 ID／貼提及")
async def unban_slash(
    interaction: discord.Interaction,
    member: typing.Optional[discord.Member] = None,
    user_id: typing.Optional[str] = None,
):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    m_user, err = await resolve_slash_target(
        interaction, member, user_id, required=True, in_guild_only=False
    )
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    member = m_user
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM blacklist WHERE user_id=%s", (str(member.id),))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"{member.mention} 已解除黑名單。")

@bot.tree.command(name="resetall_zero", description="[管理員] 全伺服器餘額清零")
async def resetall_zero_slash(interaction: discord.Interaction):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance=0")
    conn.commit()
    conn.close()
    await interaction.response.send_message("💥 全伺服器帳戶餘額已清零。")

@bot.tree.command(name="resetall_default", description="[管理員] 全伺服器重置為 50,000")
async def resetall_default_slash(interaction: discord.Interaction):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET balance=50000, rescue_count=0, total_games=0, wins=0, total_profit=0")
    conn.commit()
    conn.close()
    await interaction.response.send_message("🔄 全服已重置為 50,000，並重置統計。")

@bot.tree.command(name="clear_tournament_players", description="[管理員] 清空比賽報名資料")
@app_commands.describe(confirm="請輸入 CLEAR_TOURNAMENT 確認執行")
async def clear_tournament_players_slash(interaction: discord.Interaction, confirm: str):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    if confirm.strip().upper() != "CLEAR_TOURNAMENT":
        return await interaction.response.send_message("⚠️ 確認字串錯誤。請輸入 `CLEAR_TOURNAMENT`。", ephemeral=True)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM tournament_players")
    conn.commit()
    conn.close()
    await interaction.response.send_message("🧹 已清空所有比賽報名資料（tournament_players）。")

@bot.tree.command(name="lock", description="[管理員] 開關賭場營業狀態")
async def lock_slash(interaction: discord.Interaction):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    global IS_EVENT_ACTIVE
    IS_EVENT_ACTIVE = not IS_EVENT_ACTIVE
    await interaction.response.send_message(f"賭場狀態已切換：`{IS_EVENT_ACTIVE}`")

@bot.tree.command(name="adminhelp", description="[管理員] 查看管理指令清單")
async def adminhelp_slash(interaction: discord.Interaction):
    if not is_slash_host(interaction):
        return await interaction.response.send_message("❌ 你沒有權限使用此指令！", ephemeral=True)
    help_text = """**👑 管理員 Slash 指令清單**
一般玩家說明請用：`/help`

/give member amount - 發錢
/take member amount - 扣錢
/ban member - 黑名單
/unban member - 解除黑名單
/lock - 暫停/開放賭場
/resetall_zero - 全服餘額清零
/resetall_default - 全服重置為 50,000
/say text channel - 指定頻道發言
/redpacket total_amount count seconds - 發紅包"""
    await interaction.response.send_message(help_text, ephemeral=True)

bot.run(os.getenv('DISCORD_TOKEN'))