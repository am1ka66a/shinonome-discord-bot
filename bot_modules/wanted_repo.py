import datetime
import typing

from bot_modules.db import db_cursor


def fetch_wanted_status_row_sync(ensure_user_exists, get_db_connection, user_id: int):
    ensure_user_exists(user_id, 50000)
    with db_cursor() as c:
        c.execute(
            """SELECT COALESCE(role,'civilian'), COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0),
                  COALESCE(in_prison,0), last_five_robs, COALESCE(arrest_count,0),
                  COALESCE(revenge_pending,0), COALESCE(revenge_amount,0), COALESCE(bail_debt,0),
                  COALESCE(good_citizen_cert_active,0)
           FROM users WHERE user_id=%s""",
            (str(user_id),),
        )
        row = c.fetchone()
    return row


def fetch_wanted_list_rows_sync(get_db_connection):
    with db_cursor() as c:
        c.execute(
            """SELECT user_id, COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0),
                  COALESCE(in_prison,0), last_five_robs
           FROM users WHERE wanted_stars > 0
           ORDER BY wanted_stars DESC, user_id ASC LIMIT 50"""
        )
        rows = c.fetchall()
    return rows


def pay_bail_sync(
    ensure_user_exists,
    get_db_connection,
    log_transaction_in_tx,
    bail_cost: int,
    user_id: int,
    now: datetime.datetime,
) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(user_id, 0)
    uid = str(user_id)
    with db_cursor(commit=True) as c:
        c.execute(
            "SELECT COALESCE(in_prison,0), COALESCE(balance,0), COALESCE(bail_debt,0) FROM users WHERE user_id=%s FOR UPDATE",
            (uid,),
        )
        row = c.fetchone()
        if not row or not int(row[0] or 0):
            return {"ok": False, "reason": "not_in_prison"}
        bal = int(row[1] or 0)
        debt = int(row[2] or 0)
        total_bail = bail_cost + debt
        if bal < total_bail:
            return {"ok": False, "reason": "insufficient", "debt": debt, "total_bail": total_bail}
        c.execute(
            """UPDATE users SET balance=balance-%s, bail_debt=0, in_prison=0, prison_start=NULL
           WHERE user_id=%s AND balance >= %s""",
            (total_bail, uid, total_bail),
        )
        if c.rowcount == 0:
            return {"ok": False, "reason": "deduct_failed"}
        c.execute(
            "UPDATE prison_records SET released_at=%s WHERE criminal_id=%s AND released_at IS NULL ORDER BY id DESC LIMIT 1",
            (now, uid),
        )
        log_transaction_in_tx(c, user_id, -total_bail, "監獄假釋金（含累計欠款）")
    return {"ok": True, "debt": debt, "total_bail": total_bail}


def wanted_buyout_sync(
    ensure_user_exists,
    get_db_connection,
    user_role_value_func,
    log_transaction_in_tx,
    wanted_buyout_cost: int,
    wanted_buyout_cooldown_seconds: int,
    user_id: int,
    now: datetime.datetime,
) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(user_id, 50000)
    uid = str(user_id)
    cost = int(wanted_buyout_cost)
    with db_cursor(commit=True) as c:
        c.execute(
            "SELECT role, COALESCE(wanted_stars,0), COALESCE(balance,0), COALESCE(in_prison,0), last_wanted_buyout FROM users WHERE user_id=%s FOR UPDATE",
            (uid,),
        )
        row = c.fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        role = user_role_value_func(row[0])
        stars = int(row[1] or 0)
        bal = int(row[2] or 0)
        in_pr = int(row[3] or 0)
        last_buyout = row[4]
        if role != "criminal":
            return {"ok": False, "reason": "not_criminal"}
        if in_pr:
            return {"ok": False, "reason": "in_prison"}
        if stars <= 0:
            return {"ok": False, "reason": "no_stars"}
        if bal < cost:
            return {"ok": False, "reason": "insufficient", "balance": bal}
        if last_buyout is not None:
            elapsed = (now - last_buyout).total_seconds()
            if elapsed < int(wanted_buyout_cooldown_seconds):
                next_dt = last_buyout + datetime.timedelta(seconds=int(wanted_buyout_cooldown_seconds))
                return {"ok": False, "reason": "cooldown", "next_dt": next_dt}
        c.execute(
            """UPDATE users SET balance=balance-%s,
           wanted_stars=0, wanted_hunted_count=0, last_five_robs=NULL, last_wanted_buyout=%s
           WHERE user_id=%s AND balance >= %s""",
            (cost, now, uid, cost),
        )
        if c.rowcount == 0:
            return {"ok": False, "reason": "deduct_failed"}
        log_transaction_in_tx(c, user_id, -cost, "通緝買斷（消除通緝星）")
    return {"ok": True, "stars_was": stars, "new_balance": bal - cost}


def break_citizen_sync(
    ensure_user_exists,
    get_db_connection,
    lock_user_rows,
    get_locked_user_balance,
    log_transaction_in_tx,
    good_citizen_destroy_cost: int,
    good_citizen_broken_lock_days: int,
    attacker_id: int,
    target_id: int,
    now: datetime.datetime,
) -> typing.Dict[str, typing.Any]:
    ensure_user_exists(attacker_id, 50000)
    ensure_user_exists(target_id, 0)
    with db_cursor(commit=True) as c:
        lock_user_rows(c, [attacker_id, target_id])

        attacker_bal = get_locked_user_balance(c, attacker_id)
        if attacker_bal < int(good_citizen_destroy_cost):
            return {"ok": False, "reason": "insufficient", "balance": attacker_bal}
        c.execute(
            """SELECT COALESCE(good_citizen_cert_active,0), good_citizen_cert_broken_until
           FROM users WHERE user_id=%s FOR UPDATE""",
            (str(target_id),),
        )
        t_row = c.fetchone()
        if not t_row:
            return {"ok": False, "reason": "target_not_found"}
        target_active = int(t_row[0] or 0)
        target_broken_until = t_row[1]
        if target_active != 1:
            return {"ok": False, "reason": "target_not_active", "target_broken_until": target_broken_until}
        broken_until = now + datetime.timedelta(days=int(good_citizen_broken_lock_days))
        c.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s AND balance >= %s",
            (int(good_citizen_destroy_cost), str(attacker_id), int(good_citizen_destroy_cost)),
        )
        if c.rowcount == 0:
            return {"ok": False, "reason": "deduct_failed"}
        c.execute(
            """UPDATE users
           SET good_citizen_cert_active=0,
               good_citizen_cert_broken_until=%s,
               last_good_citizen_cert_action=%s
           WHERE user_id=%s""",
            (broken_until, now, str(target_id)),
        )
        log_transaction_in_tx(c, attacker_id, -int(good_citizen_destroy_cost), f"摧毀良民證（目標:{target_id}）")
    return {"ok": True, "broken_until": broken_until}


def fetch_good_citizen_rows_sync(get_db_connection) -> typing.List[typing.Tuple[typing.Any, typing.Any, typing.Any]]:
    with db_cursor() as c:
        c.execute(
            """SELECT user_id, COALESCE(balance,0), last_good_citizen_cert_action
           FROM users
           WHERE COALESCE(good_citizen_cert_active,0)=1
           ORDER BY last_good_citizen_cert_action DESC, user_id ASC
           LIMIT 100"""
        )
        rows = c.fetchall()
    return rows
