import datetime
import json
import os
import typing


ALLOWED_HOST_IDS = [
    531308526262550528,
    600177596088582185,
    1027248561177509919,
    1309551323682701444,
]
SIDE_BET_RATIO = 0.5
MAX_LEVEL = 100
DISCORD_MESSAGE_CAP = 2000
EXP_COOLDOWN_SECONDS = 45
CHAT_EXP_MULTIPLIER = 3
GAMBLE_EXP_MIN = 12
GAMBLE_EXP_MAX = 38
RED_PACKET_MIN_SECONDS = 10
ROB_COOLDOWN_SECONDS = 1800  # 30 分鐘
ROB_VICTIM_PROTECT_SECONDS = 3600
ROB_BASE_SUCCESS_RATE = 0.60
COUNTER_ROB_BASE_SUCCESS_RATE = 0.30
# /rob 與失敗反噬上限（固定 10,000,000）
ROB_STEAL_CAP = 10_000_000
ROB_FAIL_PENALTY_CAP = 10_000_000

CASINO_RECOVERY_SHARE_ENABLED = str(
    os.getenv("CASINO_RECOVERY_SHARE_ENABLED", "true")
).strip().lower() in {"1", "true", "yes", "on"}
CASINO_RECOVERY_SHARE_TARGET_ID = str(
    os.getenv("CASINO_RECOVERY_SHARE_TARGET_ID", "531308526262550528")
).strip()
try:
    CASINO_RECOVERY_SHARE_RATE = float(os.getenv("CASINO_RECOVERY_SHARE_RATE", "0.10"))
except Exception:
    CASINO_RECOVERY_SHARE_RATE = 0.10
CASINO_RECOVERY_SHARE_RATE = max(0.0, min(1.0, CASINO_RECOVERY_SHARE_RATE))
CASINO_RECOVERY_SHARE_REASON_PREFIX = "賭場回收分潤"

BICYCLE_COOLDOWN_SECONDS = 10 * 60
COINFLIP_MIN_BET = 1_000
COINFLIP_MAX_BET = 10_000_000
LOTTERY_TICKET_COST = 10_000
LOTTERY_MAX_TICKETS_PER_BUY = 50
LOTTERY_DRAW_CHECK_SECONDS = 3600
RUSSIAN_ROULETTE_CHAMBERS = 6
MSG_DB_FLUSH_EVERY_SECONDS = 8
MSG_DB_FLUSH_COUNT = 3
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "14"))
LOG_PURGE_INTERVAL_SECONDS = int(os.getenv("LOG_PURGE_INTERVAL_SECONDS", str(24 * 3600)))
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_STARTUP_BALANCE = 50_000
REASON_USER_INITIAL_BALANCE = "帳號建立初始資金"


def now_tw_naive() -> datetime.datetime:
    return datetime.datetime.now(TW_TZ).replace(tzinfo=None)


MINECRAFT_DEATH_MESSAGES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "minecraft_death_messages_zh_tw.json",
)
MINECRAFT_ITEMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "minecraft_items_zh_tw.json",
)
CUSTOM_DEATH_MESSAGES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "custom_death_messages_zh_tw.json",
)
CUSTOM_ITEMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "custom_items_zh_tw.json",
)
DEFAULT_MINECRAFT_DEATH_MESSAGES = [
    "{target} 死了",
    "{target} 在嘗試與地形理論辯論時失敗了",
    "{target} 以為自己能扛住這一下",
]
DEFAULT_MINECRAFT_ITEMS = [
    "鑽石劍",
    "下界合金劍",
    "弓",
    "弩",
    "三叉戟",
    "鐵斧",
    "鑽石斧",
    "終界水晶",
    "TNT 炸藥",
    "火焰彈",
    "烈焰棒",
    "不死圖騰",
    "附魔金蘋果",
    "終界珍珠",
    "地獄石",
    "黑曜石",
    "床",
    "熔岩桶",
    "水桶",
    "鐵砧",
    "盾牌",
    "雪球",
    "雞蛋",
    "釣魚竿",
    "鵝卵石",
    "鑽石鎬",
    "下界之星",
    "煙火火箭",
    "歌萊果",
    "苦力怕頭顱",
]


def _load_string_list_from_json(path: str, key: str, default: typing.List[str]) -> typing.List[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get(key) if isinstance(data, dict) else None
        if not isinstance(items, list):
            return default[:]
        cleaned = [str(x).strip() for x in items if isinstance(x, str) and x.strip()]
        return cleaned if cleaned else default[:]
    except Exception:
        return default[:]


def _merge_unique_lists(primary: typing.List[str], extra: typing.List[str]) -> typing.List[str]:
    seen = set()
    merged: typing.List[str] = []
    for item in primary + extra:
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


def load_minecraft_death_messages() -> typing.List[str]:
    base = _load_string_list_from_json(
        MINECRAFT_DEATH_MESSAGES_PATH,
        "messages",
        DEFAULT_MINECRAFT_DEATH_MESSAGES,
    )
    custom = _load_string_list_from_json(CUSTOM_DEATH_MESSAGES_PATH, "messages", [])
    return _merge_unique_lists(base, custom)


def load_minecraft_items() -> typing.List[str]:
    base = _load_string_list_from_json(
        MINECRAFT_ITEMS_PATH,
        "items",
        DEFAULT_MINECRAFT_ITEMS,
    )
    custom = _load_string_list_from_json(CUSTOM_ITEMS_PATH, "items", [])
    return _merge_unique_lists(base, custom)


MINECRAFT_DEATH_MESSAGES = load_minecraft_death_messages()
MINECRAFT_ITEMS = load_minecraft_items()
