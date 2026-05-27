import typing

from bot_modules import config
from bot_modules.db import get_db_connection, log_transaction


def is_blacklisted(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE user_id=%s", (str(user_id),))
    res = c.fetchone()
    conn.close()
    return res is not None


def get_user_stats(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT balance, total_games, wins, total_profit FROM users WHERE user_id=%s",
        (str(user_id),),
    )
    res = c.fetchone()
    conn.close()
    return res


def fetch_record_rows(user_id, limit: int = 50):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT amount, reason, created_at FROM logs WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
        (str(user_id), int(limit)),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def fetch_casino_stats_rows():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) FROM casino_logs")
    issued_row = c.fetchone()
    c.execute("SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) FROM casino_logs")
    recovered_row = c.fetchone()
    c.execute("SELECT COALESCE(SUM(balance), 0) FROM users")
    circulation_row = c.fetchone()
    conn.close()
    return (
        int((issued_row[0] if issued_row else 0) or 0),
        int((recovered_row[0] if recovered_row else 0) or 0),
        int((circulation_row[0] if circulation_row else 0) or 0),
    )


def fetch_casino_share_stats_rows(days: int = 7):
    days = max(1, int(days))
    conn = get_db_connection()
    c = conn.cursor()
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
    conn.close()
    total = int((total_row[0] if total_row else 0) or 0)
    recent = int((recent_row[0] if recent_row else 0) or 0)
    return total, recent, by_reason_rows


def ensure_user_exists(user_id, startup_balance=config.DEFAULT_STARTUP_BALANCE):
    uid = str(user_id)
    bal = int(startup_balance)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT IGNORE INTO users (user_id, balance) VALUES (%s, %s)",
        (uid, bal),
    )
    inserted = c.rowcount > 0
    conn.commit()
    conn.close()
    if inserted and bal != 0:
        log_transaction(uid, bal, config.REASON_USER_INITIAL_BALANCE)


async def ensure_user_exists_async(user_id, startup_balance=config.DEFAULT_STARTUP_BALANCE):
    # 避免在 event loop 內直接執行同步 DB IO
    import asyncio

    await asyncio.to_thread(ensure_user_exists, user_id, startup_balance)
