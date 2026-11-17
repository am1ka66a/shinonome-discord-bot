"""Bounded read-only audit queries. Ledger IDs and short-lived log IDs are distinct."""
import time
from dataclasses import dataclass

from bot_modules.db import db_cursor


@dataclass(frozen=True)
class LedgerPage:
    rows: list
    upper_id: int
    has_more: bool


def _ledger_page(c, uid, upper, before=None, limit=10):
    limit = max(1, min(int(limit), 25))
    params = [str(uid), int(upper)]
    condition = ""
    if before is not None:
        condition = " AND id<%s"
        params.append(int(before))
    params.append(limit + 1)
    c.execute(
        "SELECT id,amount,reason,created_at,source_log_id FROM casino_logs "
        "WHERE user_id=%s AND id<=%s" + condition + " ORDER BY id DESC LIMIT %s",
        tuple(params),
    )
    rows = c.fetchall()
    return LedgerPage(rows[:limit], int(upper), len(rows) > limit)


def account_snapshot(uid, connect=None):
    with db_cursor(connect=connect) as c:
        c.execute(
            "SELECT COALESCE(balance,0),COALESCE(exp,0),COALESCE(level,1),"
            "COALESCE(total_games,0),COALESCE(wins,0),COALESCE(total_profit,0) "
            "FROM users WHERE user_id=%s", (str(uid),),
        )
        account = c.fetchone()
        if account is None:
            return None
        c.execute("SELECT 1 FROM blacklist WHERE user_id=%s", (str(uid),))
        blacklisted = c.fetchone() is not None
        c.execute("SELECT COALESCE(MAX(id),0) FROM casino_logs WHERE user_id=%s", (str(uid),))
        upper = int(c.fetchone()[0])
        return {"account": tuple(account), "blacklisted": blacklisted,
                "page": _ledger_page(c, uid, upper), "queried_at": int(time.time())}


def ledger_page(uid, upper, before=None, connect=None):
    with db_cursor(connect=connect) as c:
        return _ledger_page(c, uid, upper, before)


def reset_operations(uid=None, connect=None):
    with db_cursor(connect=connect) as c:
        sql = "SELECT o.request_id,o.actor_id,o.action,o.result_json,o.created_at FROM bot_admin_operations o"
        args = ()
        if uid is not None:
            sql += " JOIN bot_admin_snapshots s ON s.request_id=o.request_id WHERE s.user_id=%s"
            args = (str(uid),)
        c.execute(sql + " ORDER BY o.created_at DESC,o.request_id DESC LIMIT 50", args)
        return c.fetchall()


def reset_details(request_id, uid=None, connect=None):
    with db_cursor(connect=connect) as c:
        c.execute("SELECT request_id,actor_id,action,result_json,created_at FROM bot_admin_operations WHERE request_id=%s",
                  (str(request_id),))
        operation = c.fetchone()
        if operation is None:
            return None
        sql = ("SELECT s.user_id,s.snapshot_json,u.balance FROM bot_admin_snapshots s "
               "LEFT JOIN users u ON u.user_id=s.user_id WHERE s.request_id=%s")
        args = [str(request_id)]
        if uid is not None:
            sql += " AND s.user_id=%s"
            args.append(str(uid))
        c.execute(sql + " ORDER BY s.user_id LIMIT 100", tuple(args))
        return operation, c.fetchall()


def pending_settlements(connect=None):
    now = int(time.time())
    with db_cursor(connect=connect) as c:
        condition = "phase='settling' OR (phase='active' AND expires_at<=%s)"
        c.execute("SELECT COUNT(*) FROM bot_game_sessions WHERE " + condition, (now,))
        total = int(c.fetchone()[0])
        c.execute(
            "SELECT session_id,kind,phase,expires_at FROM bot_game_sessions WHERE " + condition +
            " ORDER BY expires_at,session_id LIMIT 100", (now,),
        )
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) FROM russian_roulette_active_matches")
        roulette_count = int(c.fetchone()[0])
        return {"total": total, "rows": rows, "roulette_count": roulette_count}
