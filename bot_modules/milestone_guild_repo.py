import os
import threading
import typing

from bot_modules.db import get_db_connection

_cache_lock = threading.Lock()
_milestone_guild_whitelist: typing.Set[int] = set()


def init_milestone_guild_whitelist_table(cursor) -> None:
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS level_milestone_guild_whitelist (
            guild_id VARCHAR(255) PRIMARY KEY,
            added_by VARCHAR(255) NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )


def _parse_guild_id(raw: typing.Any) -> typing.Optional[int]:
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def reload_milestone_guild_whitelist_cache() -> typing.Set[int]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT guild_id FROM level_milestone_guild_whitelist")
    rows = c.fetchall() or []
    conn.close()
    loaded = {_parse_guild_id(row[0]) for row in rows}
    loaded.discard(None)
    with _cache_lock:
        _milestone_guild_whitelist.clear()
        _milestone_guild_whitelist.update(loaded)
    return set(_milestone_guild_whitelist)


def seed_milestone_guild_whitelist_from_env() -> bool:
    """若白名單為空，從 LEVEL_MILESTONE_GUILD_ID 匯入一筆（向下相容）。"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM level_milestone_guild_whitelist")
    count = int((c.fetchone() or [0])[0] or 0)
    if count > 0:
        conn.close()
        return False
    env_gid = _parse_guild_id(os.getenv("LEVEL_MILESTONE_GUILD_ID", ""))
    if env_gid is None:
        conn.close()
        return False
    c.execute(
        "INSERT IGNORE INTO level_milestone_guild_whitelist (guild_id, added_by) VALUES (%s, %s)",
        (str(env_gid), "env:LEVEL_MILESTONE_GUILD_ID"),
    )
    conn.commit()
    conn.close()
    return True


def is_milestone_guild_allowed(guild_id: typing.Optional[int]) -> bool:
    gid = _parse_guild_id(guild_id)
    if gid is None:
        return False
    with _cache_lock:
        return gid in _milestone_guild_whitelist


def add_milestone_guild_sync(guild_id: int, added_by: typing.Optional[int] = None) -> typing.Dict[str, typing.Any]:
    gid = _parse_guild_id(guild_id)
    if gid is None:
        return {"ok": False, "reason": "bad_guild_id"}
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT IGNORE INTO level_milestone_guild_whitelist (guild_id, added_by) VALUES (%s, %s)",
        (str(gid), str(added_by) if added_by is not None else None),
    )
    inserted = c.rowcount > 0
    conn.commit()
    conn.close()
    reload_milestone_guild_whitelist_cache()
    return {"ok": True, "inserted": inserted, "guild_id": gid}


def remove_milestone_guild_sync(guild_id: int) -> typing.Dict[str, typing.Any]:
    gid = _parse_guild_id(guild_id)
    if gid is None:
        return {"ok": False, "reason": "bad_guild_id"}
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM level_milestone_guild_whitelist WHERE guild_id=%s", (str(gid),))
    removed = c.rowcount > 0
    conn.commit()
    conn.close()
    reload_milestone_guild_whitelist_cache()
    return {"ok": True, "removed": removed, "guild_id": gid}


def list_milestone_guilds_sync() -> typing.List[int]:
    with _cache_lock:
        if not _milestone_guild_whitelist:
            reload_milestone_guild_whitelist_cache()
        return sorted(_milestone_guild_whitelist)
