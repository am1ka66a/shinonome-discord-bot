import json
import threading
import typing
import uuid

from bot_modules.db import db_cursor

_lock = threading.Lock()
_user_active: typing.Dict[int, str] = {}


def init_rr_match_table(cursor) -> None:
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS russian_roulette_active_matches (
            match_id VARCHAR(36) PRIMARY KEY,
            guild_id VARCHAR(255) NOT NULL,
            channel_id VARCHAR(255) NOT NULL,
            message_id VARCHAR(255) NULL,
            mode VARCHAR(16) NOT NULL,
            phase VARCHAR(24) NOT NULL,
            payload JSON NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )"""
    )


def new_match_id() -> str:
    return str(uuid.uuid4())


def _payload_participants(payload: typing.Dict[str, typing.Any]) -> typing.List[int]:
    out: typing.List[int] = []
    for raw in payload.get("participant_ids") or []:
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def unregister_match_users(match_id: str, user_ids: typing.Iterable[int]) -> None:
    mid = str(match_id)
    with _lock:
        for uid in user_ids:
            if _user_active.get(int(uid)) == mid:
                del _user_active[int(uid)]


def sync_match_users(match_id: str, user_ids: typing.Iterable[int]) -> None:
    """讓忙碌名單與這場的名單一致：補上新加入者，並放掉已退賽的人。"""
    mid = str(match_id)
    current = {int(uid) for uid in user_ids}
    with _lock:
        stale = [uid for uid, held in _user_active.items() if held == mid and uid not in current]
        for uid in stale:
            del _user_active[uid]
        for uid in current:
            _user_active[uid] = mid


def user_active_match_id(user_id: int) -> typing.Optional[str]:
    with _lock:
        return _user_active.get(int(user_id))


def any_user_busy(user_ids: typing.Iterable[int]) -> typing.Optional[int]:
    with _lock:
        for uid in user_ids:
            if int(uid) in _user_active:
                return int(uid)
    return None


def rebuild_user_cache_from_rows(rows: typing.List[typing.Dict[str, typing.Any]]) -> None:
    with _lock:
        _user_active.clear()
        for row in rows:
            match_id = str(row["match_id"])
            payload = row.get("payload") or {}
            if payload.get("settled"):
                continue
            for uid in _payload_participants(payload):
                _user_active[uid] = match_id


def save_match_sync(
    *,
    match_id: str,
    guild_id: int,
    channel_id: int,
    message_id: typing.Optional[int],
    mode: str,
    phase: str,
    payload: typing.Dict[str, typing.Any],
) -> None:
    participant_ids = _payload_participants(payload)
    sync_match_users(match_id, participant_ids)
    with db_cursor(commit=True) as c:
        c.execute(
            """INSERT INTO russian_roulette_active_matches
               (match_id, guild_id, channel_id, message_id, mode, phase, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
               guild_id=VALUES(guild_id),
               channel_id=VALUES(channel_id),
               message_id=VALUES(message_id),
               mode=VALUES(mode),
               phase=VALUES(phase),
               payload=VALUES(payload)""",
            (
                str(match_id),
                str(guild_id),
                str(channel_id),
                str(message_id) if message_id else None,
                str(mode),
                str(phase),
                json.dumps(payload, ensure_ascii=False),
            ),
        )


def delete_match_sync(match_id: str, payload: typing.Optional[typing.Dict[str, typing.Any]] = None) -> None:
    if payload is None:
        row = fetch_match_sync(match_id)
        payload = (row or {}).get("payload") or {}
    unregister_match_users(match_id, _payload_participants(payload))
    with db_cursor(commit=True) as c:
        c.execute("DELETE FROM russian_roulette_active_matches WHERE match_id=%s", (str(match_id),))


def fetch_match_sync(match_id: str) -> typing.Optional[typing.Dict[str, typing.Any]]:
    with db_cursor() as c:
        c.execute(
            """SELECT match_id, guild_id, channel_id, message_id, mode, phase, payload
               FROM russian_roulette_active_matches WHERE match_id=%s""",
            (str(match_id),),
        )
        row = c.fetchone()
    if not row:
        return None
    payload = row[6]
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {
        "match_id": str(row[0]),
        "guild_id": int(row[1]),
        "channel_id": int(row[2]),
        "message_id": int(row[3]) if row[3] else None,
        "mode": str(row[4]),
        "phase": str(row[5]),
        "payload": payload,
    }


def fetch_active_matches_sync() -> typing.List[typing.Dict[str, typing.Any]]:
    with db_cursor() as c:
        c.execute(
            """SELECT match_id, guild_id, channel_id, message_id, mode, phase, payload
               FROM russian_roulette_active_matches"""
        )
        rows = c.fetchall() or []
    out: typing.List[typing.Dict[str, typing.Any]] = []
    for row in rows:
        payload = row[6]
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if payload.get("settled"):
            continue
        out.append(
            {
                "match_id": str(row[0]),
                "guild_id": int(row[1]),
                "channel_id": int(row[2]),
                "message_id": int(row[3]) if row[3] else None,
                "mode": str(row[4]),
                "phase": str(row[5]),
                "payload": payload,
            }
        )
    rebuild_user_cache_from_rows(out)
    return out
