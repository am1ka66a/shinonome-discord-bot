import datetime
import typing

from bot_modules import config
from bot_modules import domain_sync
from bot_modules import economy_repo
from bot_modules.db import get_db_connection


def _day_key_from_dt(dt: datetime.datetime) -> str:
    return dt.date().isoformat()


def fetch_user_cooldowns_sync(user_id: int) -> typing.Dict[str, typing.Any]:
    uid = str(user_id)
    now = config.now_tw_naive()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT last_beg, last_rescue, last_rob, last_bicycle, last_role_change,
                  last_good_citizen_cert_action, last_wanted_buyout, role, balance, rescue_count,
                  good_citizen_cert_active
           FROM users WHERE user_id=%s""",
        (uid,),
    )
    row = c.fetchone()
    c.execute("SELECT last_claim FROM daily_claims WHERE user_id=%s", (uid,))
    daily_row = c.fetchone()
    conn.close()

    items: typing.List[typing.Dict[str, typing.Any]] = []

    today_tw = now.date()
    tomorrow_tw = today_tw + datetime.timedelta(days=1)
    daily_next = datetime.datetime.combine(tomorrow_tw, datetime.time.min, tzinfo=config.TW_TZ)
    daily_ready = not (daily_row and daily_row[0] == today_tw)
    items.append(
        {
            "name": "每日簽到 `/daily`",
            "ready": daily_ready,
            "next_dt": None if daily_ready else daily_next.replace(tzinfo=None),
        }
    )

    bank_info = economy_repo.refresh_hourly_bank(get_db_connection, config.now_tw_naive, config.MAX_LEVEL, user_id)
    hourly_ready = bool(bank_info and int(bank_info.get("bank") or 0) > 0)
    hourly_next_sec = int((bank_info or {}).get("next_in_seconds") or 0)
    hourly_next_dt = now + datetime.timedelta(seconds=hourly_next_sec) if hourly_next_sec > 0 else None
    items.append(
        {
            "name": "每小時簽到 `/hourly`",
            "ready": hourly_ready,
            "next_dt": None if hourly_ready else hourly_next_dt,
            "note": f"累積 {int((bank_info or {}).get('bank') or 0)} 格" if bank_info else None,
        }
    )

    def _cd(name: str, last_ts, cooldown_sec: int, always: bool = True):
        if not always and not last_ts:
            return
        ready = True
        next_dt = None
        if last_ts:
            elapsed = (now - last_ts).total_seconds()
            if elapsed < cooldown_sec:
                ready = False
                next_dt = last_ts + datetime.timedelta(seconds=cooldown_sec)
        items.append({"name": name, "ready": ready, "next_dt": next_dt})

    if row:
        last_beg, last_rescue, last_rob, last_bicycle = row[0], row[1], row[2], row[3]
        last_role_change, last_gc, last_buyout = row[4], row[5], row[6]
        role = domain_sync._user_role_value(row[7])
        balance = int(row[8] or 0)
        rescue_count = int(row[9] or 0)

        _cd("乞討 `/beg`", last_beg, 120)
        if balance <= 0 and rescue_count < 10:
            _cd("破產救濟 `/rescue`", last_rescue, 3600)
        if role == "criminal":
            _cd("搶劫 `/rob`", last_rob, config.ROB_COOLDOWN_SECONDS)
            _cd("通緝買斷 `/wanted_buyout`", last_buyout, domain_sync.WANTED_BUYOUT_COOLDOWN_SECONDS, always=bool(last_buyout))
        _cd("轉職 `/role_choose`", last_role_change, domain_sync.ROLE_CHANGE_COOLDOWN_SECONDS, always=bool(last_role_change))
        if role == "civilian":
            _cd("良民證 `/good_citizen`", last_gc, domain_sync.GOOD_CITIZEN_CERT_COOLDOWN_SECONDS, always=bool(last_gc))
        _cd("偷腳踏車 `/bicycle`", last_bicycle, config.BICYCLE_COOLDOWN_SECONDS, always=bool(last_bicycle))

    return {"items": items}


def fetch_user_profile_sync(user_id: int) -> typing.Optional[typing.Dict[str, typing.Any]]:
    uid = str(user_id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT balance, total_games, wins, total_profit, exp, level,
                  COALESCE(role,'civilian'), COALESCE(wanted_stars,0), COALESCE(in_prison,0),
                  COALESCE(arrest_count,0), COALESCE(good_citizen_cert_active,0), COALESCE(bail_debt,0)
           FROM users WHERE user_id=%s""",
        (uid,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    c.execute("SELECT COUNT(*) + 1 FROM users WHERE balance > %s", (int(row[0] or 0),))
    bal_rank = int((c.fetchone() or [1])[0])
    c.execute(
        "SELECT COUNT(*) + 1 FROM users WHERE level > %s OR (level = %s AND exp > %s)",
        (int(row[5] or 1), int(row[5] or 1), int(row[4] or 0)),
    )
    lv_rank = int((c.fetchone() or [1])[0])
    conn.close()
    total_games = int(row[1] or 0)
    wins = int(row[2] or 0)
    return {
        "balance": int(row[0] or 0),
        "total_games": total_games,
        "wins": wins,
        "total_profit": int(row[3] or 0),
        "exp": int(row[4] or 0),
        "level": int(row[5] or 1),
        "role": domain_sync._user_role_value(row[6]),
        "wanted_stars": int(row[7] or 0),
        "in_prison": int(row[8] or 0),
        "arrest_count": int(row[9] or 0),
        "good_citizen_active": int(row[10] or 0),
        "bail_debt": int(row[11] or 0),
        "balance_rank": bal_rank,
        "level_rank": lv_rank,
        "win_rate": (wins * 100.0 / total_games) if total_games > 0 else 0.0,
    }


def fetch_user_ranks_sync(user_id: int) -> typing.Optional[typing.Dict[str, typing.Any]]:
    profile = fetch_user_profile_sync(user_id)
    if not profile:
        return None
    return {
        "balance": profile["balance"],
        "level": profile["level"],
        "exp": profile["exp"],
        "balance_rank": profile["balance_rank"],
        "level_rank": profile["level_rank"],
    }


def fetch_compare_sync(user_a: int, user_b: int) -> typing.Dict[str, typing.Any]:
    a = fetch_user_profile_sync(user_a)
    b = fetch_user_profile_sync(user_b)
    return {"a": a, "b": b}


def lottery_day_key(dt: typing.Optional[datetime.datetime] = None) -> str:
    return _day_key_from_dt(dt or config.now_tw_naive())
