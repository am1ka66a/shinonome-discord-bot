from bot_modules import config
from bot_modules.db import db_cursor, log_transaction


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


def fetch_casino_stats_rows():
    with db_cursor() as c:
        c.execute("SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) FROM casino_logs")
        issued_row = c.fetchone()
        c.execute("SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) FROM casino_logs")
        recovered_row = c.fetchone()
        c.execute("SELECT COALESCE(SUM(balance), 0) FROM users")
        circulation_row = c.fetchone()
    return (
        int((issued_row[0] if issued_row else 0) or 0),
        int((recovered_row[0] if recovered_row else 0) or 0),
        int((circulation_row[0] if circulation_row else 0) or 0),
    )


def fetch_balance_leaderboard_snapshot():
    with db_cursor() as c:
        c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC")
        return c.fetchall()


def fetch_level_leaderboard_snapshot():
    with db_cursor() as c:
        c.execute("SELECT user_id, level, exp FROM users ORDER BY level DESC, exp DESC")
        return c.fetchall()


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
    uid = str(user_id)
    bal = int(startup_balance)
    with db_cursor(commit=True) as c:
        c.execute(
            "INSERT IGNORE INTO users (user_id, balance) VALUES (%s, %s)",
            (uid, bal),
        )
        inserted = c.rowcount > 0
    # log_transaction 會另開連線，必須在歸還連線之後才呼叫
    if inserted and bal != 0:
        log_transaction(uid, bal, config.REASON_USER_INITIAL_BALANCE)


async def ensure_user_exists_async(user_id, startup_balance=config.DEFAULT_STARTUP_BALANCE):
    # 避免在 event loop 內直接執行同步 DB IO
    import asyncio

    await asyncio.to_thread(ensure_user_exists, user_id, startup_balance)
