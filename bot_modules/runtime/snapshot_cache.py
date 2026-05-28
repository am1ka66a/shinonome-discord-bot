import asyncio
import time
import typing

import discord

LEADERBOARD_SNAPSHOT_SECONDS = 30.0
WANTED_CACHE_SECONDS = 10.0
GOOD_CITIZEN_CACHE_SECONDS = 15.0
GUILD_MEMBER_CACHE_SECONDS = 20.0
CASINO_STATS_SNAPSHOT_SECONDS = 60.0
METRICS_LOG_INTERVAL_SECONDS = 120

get_balance_leaderboard_rows_cached: typing.Optional[typing.Callable] = None
get_level_leaderboard_rows_cached: typing.Optional[typing.Callable] = None
get_casino_stats_rows_cached: typing.Optional[typing.Callable] = None
get_wanted_list_rows_cached: typing.Optional[typing.Callable] = None
get_good_citizen_rows_cached: typing.Optional[typing.Callable] = None
get_wanted_status_cached: typing.Optional[typing.Callable] = None
cleanup_local_caches: typing.Optional[typing.Callable] = None
get_lb_balance_snapshot_age: typing.Optional[typing.Callable] = None
get_lb_level_snapshot_age: typing.Optional[typing.Callable] = None
get_casino_stats_snapshot_age: typing.Optional[typing.Callable] = None


def register_snapshot_cache(bot, ctx: typing.Dict[str, typing.Any]) -> typing.Dict[str, typing.Any]:
    logger = ctx["logger"]
    db_to_thread = ctx["db_to_thread"]
    fetch_balance_leaderboard_snapshot = ctx["fetch_balance_leaderboard_snapshot"]
    fetch_level_leaderboard_snapshot = ctx["fetch_level_leaderboard_snapshot"]
    fetch_casino_stats_rows = ctx["fetch_casino_stats_rows"]
    fetch_wanted_list_rows_sync = ctx["fetch_wanted_list_rows_sync"]
    fetch_good_citizen_rows_sync = ctx["fetch_good_citizen_rows_sync"]
    fetch_wanted_status_row_sync = ctx["fetch_wanted_status_row_sync"]

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

    async def _get_balance_leaderboard_rows_cached() -> typing.List[typing.Tuple]:
        nonlocal _lb_balance_cache
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

    async def _get_level_leaderboard_rows_cached() -> typing.List[typing.Tuple]:
        nonlocal _lb_level_cache
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

    async def _get_casino_stats_rows_cached() -> typing.Tuple[int, int, int]:
        nonlocal _casino_stats_cache
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
        nonlocal _lb_balance_cache, _lb_level_cache
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
        nonlocal _casino_stats_cache
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

    async def _get_wanted_list_rows_cached() -> typing.List[typing.Tuple]:
        nonlocal _wanted_list_cache
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

    async def _get_good_citizen_rows_cached() -> typing.List[typing.Tuple]:
        nonlocal _good_citizen_cache
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

    async def _get_wanted_status_cached(user_id: int):
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

    def _cleanup_local_caches() -> None:
        now_ts = time.time()
        for store, ttl in (
            (_guild_member_cache, GUILD_MEMBER_CACHE_SECONDS),
            (_wanted_status_cache, WANTED_CACHE_SECONDS),
        ):
            expired_keys = [k for k, (ts, _v) in store.items() if (now_ts - ts) >= ttl]
            for k in expired_keys:
                store.pop(k, None)

    async def emit_cache_metrics_log_task():
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

    def _get_lb_balance_snapshot_age() -> typing.Optional[float]:
        if _lb_balance_cache is None:
            return None
        return time.time() - _lb_balance_cache[0]

    def _get_lb_level_snapshot_age() -> typing.Optional[float]:
        if _lb_level_cache is None:
            return None
        return time.time() - _lb_level_cache[0]

    def _get_casino_stats_snapshot_age() -> typing.Optional[float]:
        if _casino_stats_cache is None:
            return None
        return time.time() - _casino_stats_cache[0]

    global get_balance_leaderboard_rows_cached
    global get_level_leaderboard_rows_cached
    global get_casino_stats_rows_cached
    global get_wanted_list_rows_cached
    global get_good_citizen_rows_cached
    global get_wanted_status_cached
    global cleanup_local_caches
    global get_lb_balance_snapshot_age
    global get_lb_level_snapshot_age
    global get_casino_stats_snapshot_age

    get_balance_leaderboard_rows_cached = _get_balance_leaderboard_rows_cached
    get_level_leaderboard_rows_cached = _get_level_leaderboard_rows_cached
    get_casino_stats_rows_cached = _get_casino_stats_rows_cached
    get_wanted_list_rows_cached = _get_wanted_list_rows_cached
    get_good_citizen_rows_cached = _get_good_citizen_rows_cached
    get_wanted_status_cached = _get_wanted_status_cached
    cleanup_local_caches = _cleanup_local_caches
    get_lb_balance_snapshot_age = _get_lb_balance_snapshot_age
    get_lb_level_snapshot_age = _get_lb_level_snapshot_age
    get_casino_stats_snapshot_age = _get_casino_stats_snapshot_age

    return {
        "emit_cache_metrics_log_task": emit_cache_metrics_log_task,
        "refresh_leaderboard_snapshots_task": refresh_leaderboard_snapshots_task,
        "refresh_casino_stats_snapshot_task": refresh_casino_stats_snapshot_task,
    }
