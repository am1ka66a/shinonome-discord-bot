import json
import threading
import typing
import uuid

from bot_modules.db import db_cursor
from bot_modules.tx_ops import change_balance, lock_user_rows

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
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS russian_roulette_settlements (
            match_id VARCHAR(36) NOT NULL,
            event_key VARCHAR(64) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (match_id, event_key)
        )"""
    )


def _apply_event_sync(*, match_id: str, event_key: str, credits: typing.Dict[int, int],
                      results: typing.Dict[int, typing.Tuple[bool, int]], reason: str,
                      payload: typing.Dict[str, typing.Any], finished: bool, next_phase="playing") -> bool:
    """Commit payouts, both stat tables, and the match checkpoint exactly once together."""
    with db_cursor(commit=True) as c:
        c.execute("SELECT match_id FROM russian_roulette_active_matches WHERE match_id=%s FOR UPDATE", (str(match_id),))
        if not c.fetchone():
            return False
        c.execute(
            "INSERT IGNORE INTO russian_roulette_settlements (match_id, event_key) VALUES (%s, %s)",
            (str(match_id), event_key),
        )
        if c.rowcount == 0:
            return False
        user_ids = sorted(set(credits) | set(results))
        if len(lock_user_rows(c, user_ids)) != len(user_ids):
            raise ValueError("Russian roulette settlement references a missing user")
        for uid in user_ids:
            amount = int(credits.get(uid, 0))
            if amount < 0:
                raise ValueError("Settlement credits must be nonnegative")
            if amount:
                change_balance(c, uid, amount, reason)
            if uid in results:
                win, profit = results[uid]
                c.execute(
                    "UPDATE users SET total_games=total_games+1, wins=wins+%s, "
                    "total_profit=total_profit+%s WHERE user_id=%s",
                    (int(win), int(profit), str(uid)),
                )
                c.execute(
                    "INSERT INTO russian_roulette_stats (user_id, games, wins, profit) VALUES (%s, 1, %s, %s) "
                    "ON DUPLICATE KEY UPDATE games=games+1, wins=wins+%s, profit=profit+%s",
                    (str(uid), int(win), int(profit), int(win), int(profit)),
                )
        if finished:
            c.execute("DELETE FROM russian_roulette_active_matches WHERE match_id=%s", (str(match_id),))
        else:
            c.execute(
                "UPDATE russian_roulette_active_matches SET phase=%s, payload=%s WHERE match_id=%s",
                (next_phase, json.dumps(payload, ensure_ascii=False), str(match_id)),
            )
    if finished:
        unregister_match_users(match_id, _payload_participants(payload))
    else:
        sync_match_users(match_id, _payload_participants(payload))
    return True


PENDING_EVENT_KEY = "pending_settlement"


def _decode_payload(raw):
    if isinstance(raw, (str, bytes, bytearray)):
        return json.loads(raw)
    return raw or {}


def prepare_event_sync(*, match_id, event_key, credits, results, reason, payload, finished, next_phase="playing"):
    """Durably choose one result before any payout; retries retain that result."""
    with db_cursor(commit=True) as c:
        c.execute("SELECT payload FROM russian_roulette_active_matches WHERE match_id=%s FOR UPDATE", (str(match_id),))
        row = c.fetchone()
        if row is None:
            return False
        current = _decode_payload(row[0])
        pending = current.get(PENDING_EVENT_KEY)
        if pending:
            if pending["event_key"] != event_key:
                raise ValueError("輪盤仍有結算等待處理，不能覆蓋結果")
            return True
        c.execute("SELECT 1 FROM russian_roulette_settlements WHERE match_id=%s AND event_key=%s", (str(match_id), event_key))
        if c.fetchone():
            return False
        credits = {str(uid): int(amount) for uid, amount in credits.items()}
        if any(amount < 0 for amount in credits.values()):
            raise ValueError("Settlement credits must be nonnegative")
        state = dict(payload)
        state[PENDING_EVENT_KEY] = dict(event_key=event_key, credits=credits,
            results={str(uid): value for uid,value in results.items()}, reason=reason,
            finished=bool(finished), next_phase=next_phase)
        c.execute("UPDATE russian_roulette_active_matches SET phase='settling',payload=%s WHERE match_id=%s",
                  (json.dumps(state,ensure_ascii=False),str(match_id)))
    return True


def apply_pending_event_sync(match_id):
    with db_cursor() as c:
        c.execute("SELECT payload FROM russian_roulette_active_matches WHERE match_id=%s", (str(match_id),))
        row = c.fetchone()
        if row is None:
            return False
        state = dict(_decode_payload(row[0]))
    pending = state.pop(PENDING_EVENT_KEY,None)
    if not pending:
        return False
    return _apply_event_sync(match_id=str(match_id),event_key=pending["event_key"],
        credits={int(uid):amount for uid,amount in pending["credits"].items()},
        results={int(uid):value for uid,value in pending["results"].items()},reason=pending["reason"],
        payload=state,finished=pending["finished"],next_phase=pending.get("next_phase","playing"))


def settle_event_sync(**kwargs):
    if not prepare_event_sync(**kwargs):
        return False
    return apply_pending_event_sync(kwargs["match_id"])


def recover_pending_events_sync():
    with db_cursor() as c:
        c.execute("SELECT match_id FROM russian_roulette_active_matches WHERE phase='settling' LIMIT 100")
        ids = [row[0] for row in c.fetchall()]
    recovered = 0
    for match_id in ids:
        try:
            recovered += int(apply_pending_event_sync(match_id))
        except Exception:
            import logging
            logging.getLogger(__name__).exception("輪盤結算保留待重試 match=%s", match_id)
    return recovered


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
            if payload.get("settled") and not payload.get(PENDING_EVENT_KEY):
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
    with db_cursor(commit=True) as c:
        c.execute("SELECT payload FROM russian_roulette_active_matches WHERE match_id=%s FOR UPDATE",(str(match_id),))
        prior = c.fetchone()
        if prior and _decode_payload(prior[0]).get(PENDING_EVENT_KEY):
            raise ValueError("輪盤仍有待結算結果，不能覆蓋存檔")
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
    sync_match_users(match_id, participant_ids)


class _InsufficientStake(ValueError):
    pass


def fund_match_sync(*, charges, event_key, reason, match_id, guild_id, channel_id,
                    message_id, mode, phase, payload):
    """A stake and its canonical match state either both commit or both roll back."""
    from bot_modules.maintenance import casino_open
    charges = {int(uid):int(amount) for uid,amount in charges.items()}
    if not charges or any(amount <= 0 for amount in charges.values()):
        raise ValueError("注金必須大於零")
    participants = _payload_participants(payload)
    if not set(charges).issubset(participants):
        raise ValueError("下注者必須在對局名單中")
    try:
        with db_cursor(commit=True) as c:
            if not casino_open(c):
                return False
            c.execute("SELECT payload FROM russian_roulette_active_matches WHERE match_id=%s FOR UPDATE",(str(match_id),))
            prior = c.fetchone()
            c.execute("INSERT IGNORE INTO russian_roulette_settlements (match_id,event_key) VALUES (%s,%s)",
                      (str(match_id),f"stake:{event_key}"))
            if c.rowcount == 0:
                if prior is None:
                    return False
                participants = _payload_participants(_decode_payload(prior[0])) if prior else []
            else:
                if prior and _decode_payload(prior[0]).get(PENDING_EVENT_KEY):
                    raise ValueError("這場輪盤仍在結算，不能繼續下注")
                if len(lock_user_rows(c,participants)) != len(set(participants)):
                    raise _InsufficientStake()
                c.execute("SELECT match_id,payload FROM russian_roulette_active_matches WHERE match_id<>%s",(str(match_id),))
                for other_id,raw in c.fetchall():
                    if set(participants).intersection(_payload_participants(_decode_payload(raw))):
                        raise ValueError("玩家已有進行中的輪盤，不能重複下注")
                for uid,amount in charges.items():
                    c.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=%s",(str(uid),))
                    row = c.fetchone()
                    if row is None or int(row[0]) < amount:
                        raise _InsufficientStake()
                    change_balance(c, uid, -amount, reason)
                c.execute("""INSERT INTO russian_roulette_active_matches
                    (match_id,guild_id,channel_id,message_id,mode,phase,payload) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE message_id=VALUES(message_id),phase=VALUES(phase),payload=VALUES(payload)""",
                    (str(match_id),str(guild_id),str(channel_id),str(message_id) if message_id else None,
                     mode,phase,json.dumps(payload,ensure_ascii=False)))
    except _InsufficientStake:
        return False
    sync_match_users(match_id,participants)
    return True


def delete_match_sync(match_id: str, payload: typing.Optional[typing.Dict[str, typing.Any]] = None) -> None:
    if payload is None:
        row = fetch_match_sync(match_id)
        payload = (row or {}).get("payload") or {}
    with db_cursor(commit=True) as c:
        c.execute("DELETE FROM russian_roulette_active_matches WHERE match_id=%s", (str(match_id),))
    unregister_match_users(match_id, _payload_participants(payload))


def fetch_match_sync(match_id: str, *, lock=False) -> typing.Optional[typing.Dict[str, typing.Any]]:
    with db_cursor(commit=lock) as c:
        c.execute(
            """SELECT match_id, guild_id, channel_id, message_id, mode, phase, payload
               FROM russian_roulette_active_matches WHERE match_id=%s""" + (" FOR UPDATE" if lock else ""),
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


def fetch_active_matches_sync(*, rebuild_cache=True) -> typing.List[typing.Dict[str, typing.Any]]:
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
        if payload.get("settled") and not payload.get(PENDING_EVENT_KEY):
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
    if rebuild_cache:
        rebuild_user_cache_from_rows(out)
    return out
