from bot_modules import config
from bot_modules.db import call_with_db_retry, db_cursor
from bot_modules.tx_ops import change_balance


def is_blacklisted(user_id):
    with db_cursor() as c:
        c.execute("SELECT 1 FROM blacklist WHERE user_id=%s", (str(user_id),))
        res = c.fetchone()
    return res is not None


def get_user_stats(user_id):
    with db_cursor() as c:
        c.execute(
            "SELECT balance, total_games, wins, total_profit FROM users WHERE user_id=%s",
            (str(user_id),),
        )
        return c.fetchone()


def fetch_record_rows(user_id, limit: int = 50):
    with db_cursor() as c:
        c.execute(
            "SELECT amount, reason, created_at FROM logs WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (str(user_id), int(limit)),
        )
        return c.fetchall()


def _fetch_casino_stats_rows_once():
    with db_cursor() as c:
        # 一次掃描 casino_logs，避免 60 秒背景快照連掃兩遍把連線拖死
        c.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) "
            "FROM casino_logs"
        )
        issued_row = c.fetchone()
        c.execute("SELECT COALESCE(SUM(balance), 0) FROM users")
        circulation_row = c.fetchone()
    return (
        int((issued_row[0] if issued_row else 0) or 0),
        int((issued_row[1] if issued_row else 0) or 0),
        int((circulation_row[0] if circulation_row else 0) or 0),
    )


def fetch_casino_stats_rows():
    return call_with_db_retry(_fetch_casino_stats_rows_once)


def _fetch_balance_leaderboard_snapshot_once():
    with db_cursor() as c:
        c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC")
        return c.fetchall()


def fetch_balance_leaderboard_snapshot():
    return call_with_db_retry(_fetch_balance_leaderboard_snapshot_once)


def _fetch_ai_balances_once(user_ids, top: int = 5):
    ids = sorted({str(uid) for uid in user_ids})
    with db_cursor() as c:
        balances = {}
        if ids:
            marks = ",".join(["%s"] * len(ids))
            c.execute(f"SELECT user_id, balance FROM users WHERE user_id IN ({marks})", tuple(ids))
            balances = {str(uid): int(bal or 0) for uid, bal in c.fetchall()}
        c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT %s", (int(top),))
        leaders = [(str(uid), int(bal or 0)) for uid, bal in c.fetchall()]
    return balances, leaders


def fetch_ai_balances(user_ids, top: int = 5):
    """Balances of the given users plus the top of the balance board, for the AI chat (read only)."""
    return call_with_db_retry(lambda: _fetch_ai_balances_once(user_ids, top))


def _fetch_level_leaderboard_snapshot_once():
    with db_cursor() as c:
        c.execute("SELECT user_id, level, exp FROM users ORDER BY level DESC, exp DESC")
        return c.fetchall()


def fetch_level_leaderboard_snapshot():
    return call_with_db_retry(_fetch_level_leaderboard_snapshot_once)


def fetch_casino_share_stats_rows(days: int = 7):
    days = max(1, int(days))
    with db_cursor() as c:
        c.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM logs "
            "WHERE user_id=%s AND amount > 0 AND reason LIKE %s",
            (config.CASINO_RECOVERY_SHARE_TARGET_ID, f"{config.CASINO_RECOVERY_SHARE_REASON_PREFIX}%"),
        )
        total_row = c.fetchone()
        c.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM logs "
            "WHERE user_id=%s AND amount > 0 AND reason LIKE %s "
            "AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)",
            (
                config.CASINO_RECOVERY_SHARE_TARGET_ID,
                f"{config.CASINO_RECOVERY_SHARE_REASON_PREFIX}%",
                days,
            ),
        )
        recent_row = c.fetchone()
        c.execute(
            "SELECT reason, COALESCE(SUM(amount), 0) AS s FROM logs "
            "WHERE user_id=%s AND amount > 0 AND reason LIKE %s "
            "GROUP BY reason ORDER BY s DESC LIMIT 10",
            (config.CASINO_RECOVERY_SHARE_TARGET_ID, f"{config.CASINO_RECOVERY_SHARE_REASON_PREFIX}%"),
        )
        by_reason_rows = c.fetchall()
    total = int((total_row[0] if total_row else 0) or 0)
    recent = int((recent_row[0] if recent_row else 0) or 0)
    return total, recent, by_reason_rows


def ensure_user_exists(user_id, startup_balance=config.DEFAULT_STARTUP_BALANCE):
    """建立帳戶並保證每位玩家只拿一次起始資金。

    帳戶可能先被轉帳、紅包或聊天獎勵以 0 或入帳金額建立（startup_granted=0）。
    玩家第一次以 startup_balance>0 呼叫時補發一次，餘額與流水在同一筆交易寫入。
    """
    uid = str(user_id)
    bal = int(startup_balance)
    with db_cursor(commit=True) as c:
        if bal <= 0:
            c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, 0)", (uid,))
            return
        c.execute(
            "INSERT IGNORE INTO users (user_id, balance, startup_granted) VALUES (%s, 0, 1)",
            (uid,),
        )
        granted = c.rowcount > 0
        if not granted:
            c.execute("UPDATE users SET startup_granted=1 WHERE user_id=%s AND startup_granted=0", (uid,))
            granted = c.rowcount > 0
        if granted:
            change_balance(c, uid, bal, config.REASON_USER_INITIAL_BALANCE)


async def ensure_user_exists_async(user_id, startup_balance=config.DEFAULT_STARTUP_BALANCE):
    # 避免在 event loop 內直接執行同步 DB IO
    import asyncio

    await asyncio.to_thread(ensure_user_exists, user_id, startup_balance)
