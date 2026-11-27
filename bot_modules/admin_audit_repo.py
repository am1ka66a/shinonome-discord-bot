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
        c.execute("SELECT COUNT(*) FROM russian_roulette_active_matches WHERE phase='settling'")
        roulette_pending = int(c.fetchone()[0])
        return {"total": total, "rows": rows, "roulette_count": roulette_count, "roulette_pending": roulette_pending}


@dataclass(frozen=True)
class ReconcileReport:
    accounts: int
    matched: int
    drifted: list          # [(user_id, balance, ledger_sum, offset, drift)] sorted by |drift| desc
    total_drift: int
    orphan_users: int
    orphan_amount: int
    baseline: object       # (actor_id, created_at, accounts, total_offset) or None
    checked_at: int


def _account_differences(c):
    c.execute(
        "SELECT u.user_id, COALESCE(u.balance,0), COALESCE(l.total,0), b.offset_amount "
        "FROM users u "
        "LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM casino_logs GROUP BY user_id) l "
        "ON l.user_id=u.user_id "
        "LEFT JOIN bot_reconcile_baseline b ON b.user_id=u.user_id"
    )
    return c.fetchall()


def reconcile_report(connect=None, limit=100):
    """Compare every balance with the sum of its permanent ledger (casino_logs).

    drift = balance - ledger_sum - baseline_offset. A baseline absorbs history
    that predates the ledger; any later drift means money moved without a
    matching ledger row. Read-only.
    """
    with db_cursor(connect=connect) as c:
        rows = _account_differences(c)
        c.execute(
            "SELECT COUNT(DISTINCT user_id), COALESCE(SUM(amount),0) FROM casino_logs "
            "WHERE user_id IS NOT NULL AND user_id NOT IN (SELECT user_id FROM users)"
        )
        orphan_users, orphan_amount = c.fetchone()
        c.execute("SELECT actor_id, created_at, accounts, total_offset FROM bot_reconcile_meta WHERE id=1")
        baseline = c.fetchone()
    drifted = []
    for uid, balance, ledger_sum, offset in rows:
        balance, ledger_sum, offset = int(balance), int(ledger_sum), int(offset or 0)
        drift = balance - ledger_sum - offset
        if drift:
            drifted.append((str(uid), balance, ledger_sum, offset, drift))
    drifted.sort(key=lambda r: (-abs(r[4]), r[0]))
    return ReconcileReport(
        accounts=len(rows), matched=len(rows) - len(drifted),
        drifted=drifted[:max(1, int(limit))], total_drift=sum(r[4] for r in drifted),
        orphan_users=int(orphan_users or 0), orphan_amount=int(orphan_amount or 0),
        baseline=tuple(baseline) if baseline else None, checked_at=int(time.time()),
    )


def set_reconcile_baseline(actor_id, connect=None):
    """Record today's per-account differences as the accepted baseline.

    Touches only the baseline tables; balances and ledgers are never changed.
    """
    with db_cursor(commit=True, connect=connect) as c:
        rows = _account_differences(c)
        c.execute("DELETE FROM bot_reconcile_baseline")
        offsets = [(str(uid), int(bal) - int(total))
                   for uid, bal, total, _ in rows if int(bal) - int(total) != 0]
        for uid, offset in offsets:
            c.execute("INSERT INTO bot_reconcile_baseline (user_id, offset_amount) VALUES (%s, %s)", (uid, offset))
        c.execute("DELETE FROM bot_reconcile_meta WHERE id=1")
        c.execute(
            "INSERT INTO bot_reconcile_meta (id, actor_id, accounts, total_offset) VALUES (1, %s, %s, %s)",
            (str(actor_id), len(offsets), sum(o for _, o in offsets)),
        )
    return {"accounts": len(offsets), "total_offset": sum(o for _, o in offsets)}
