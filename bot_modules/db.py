import os
from urllib.parse import urlparse

import pymysql
from dbutils.pooled_db import PooledDB
import threading

_mysql_pool = None
_mysql_pool_lock = threading.Lock()


def _mysql_connect_kwargs() -> dict:
    """供連線池建立時傳入 pymysql.connect 的參數。"""
    mysql_url = os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL")
    if mysql_url:
        parsed = urlparse(mysql_url)
        if parsed.scheme.startswith("mysql"):
            return {
                "host": parsed.hostname,
                "port": parsed.port or 3306,
                "user": parsed.username,
                "password": parsed.password,
                "database": (parsed.path or "/").lstrip("/"),
                "charset": "utf8mb4",
                "init_command": "SET time_zone = '+08:00'",
            }
    return {
        "host": os.getenv("MYSQLHOST") or os.getenv("DB_HOST"),
        "port": int(os.getenv("MYSQLPORT") or os.getenv("DB_PORT", 3306)),
        "user": os.getenv("MYSQLUSER") or os.getenv("DB_USER"),
        "password": os.getenv("MYSQLPASSWORD") or os.getenv("DB_PASS"),
        "database": os.getenv("MYSQLDATABASE") or os.getenv("DB_NAME"),
        "charset": "utf8mb4",
        "init_command": "SET time_zone = '+08:00'",
    }


def _get_mysql_pool() -> PooledDB:
    global _mysql_pool
    if _mysql_pool is None:
        with _mysql_pool_lock:
            if _mysql_pool is None:
                kw = _mysql_connect_kwargs()
                _mysql_pool = PooledDB(
                    creator=pymysql,
                    mincached=int(os.getenv("MYSQL_POOL_MINCACHED", "1")),
                    maxcached=int(os.getenv("MYSQL_POOL_MAXCACHED", "8")),
                    maxconnections=int(os.getenv("MYSQL_POOL_MAX", "16")),
                    blocking=True,
                    ping=1,
                    **kw,
                )
    return _mysql_pool


def get_db_connection():
    """從連線池取得連線；用完請 commit／rollback 並 close（會歸還池中）。"""
    return _get_mysql_pool().connection()


def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS users
                 (user_id VARCHAR(255) PRIMARY KEY, balance BIGINT, rescue_count INT DEFAULT 0,
                  total_games INT DEFAULT 0, wins INT DEFAULT 0, total_profit BIGINT DEFAULT 0,
                  last_work TIMESTAMP NULL, last_beg TIMESTAMP NULL, last_rescue TIMESTAMP NULL, last_rob TIMESTAMP NULL, last_robbed TIMESTAMP NULL,
                  exp BIGINT DEFAULT 0, level INT DEFAULT 1,
                  last_hourly_claim TIMESTAMP NULL, hourly_bank INT DEFAULT 0,
                  good_citizen_cert_active TINYINT(1) DEFAULT 0, last_good_citizen_cert_action TIMESTAMP NULL,
                  good_citizen_cert_broken_until TIMESTAMP NULL)"""
    )
    # 確保現有表也有新欄位 (Migration)
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_work TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_beg TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_rescue TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_rob TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_robbed TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN exp BIGINT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN level INT DEFAULT 1")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_hourly_claim TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN hourly_bank INT DEFAULT 0")
    except Exception:
        pass
    # 警察／搶匪／通緝／監獄
    try:
        c.execute("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'civilian'")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN wanted_stars INT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN wanted_hunted_count INT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_five_robs TEXT NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN in_prison TINYINT(1) DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN prison_start TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN arrest_count INT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN revenge_pending TINYINT(1) DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN revenge_robber_id VARCHAR(255) NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN revenge_amount BIGINT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_wanted_buyout TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_bicycle TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_role_change TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN bail_debt BIGINT DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN good_citizen_cert_active TINYINT(1) DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_good_citizen_cert_action TIMESTAMP NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN good_citizen_cert_broken_until TIMESTAMP NULL")
    except Exception:
        pass

    c.execute(
        """CREATE TABLE IF NOT EXISTS wanted_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        criminal_id VARCHAR(255) NOT NULL,
        cop_id VARCHAR(255) NOT NULL,
        wanted_stars INT NOT NULL,
        caught TINYINT(1) DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS prison_records (
        id INT AUTO_INCREMENT PRIMARY KEY,
        criminal_id VARCHAR(255) NOT NULL,
        cop_id VARCHAR(255) NOT NULL,
        wanted_stars INT NOT NULL,
        confiscated_amount BIGINT NOT NULL,
        cop_reward BIGINT NOT NULL,
        bail_cost BIGINT DEFAULT 100000,
        arrested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        released_at TIMESTAMP NULL
        )"""
    )

    c.execute(
        """CREATE TABLE IF NOT EXISTS activity_stats
                 (user_id VARCHAR(255) PRIMARY KEY, msg_count INT DEFAULT 0,
                  last_msg_reward TIMESTAMP NULL, last_vc_reward TIMESTAMP NULL,
                  last_exp_reward TIMESTAMP NULL)"""
    )
    try:
        c.execute("ALTER TABLE activity_stats ADD COLUMN last_exp_reward TIMESTAMP NULL")
    except Exception:
        pass

    c.execute("""CREATE TABLE IF NOT EXISTS blacklist (user_id VARCHAR(255) PRIMARY KEY)""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_claims (user_id VARCHAR(255) PRIMARY KEY, last_claim DATE)""")
    c.execute(
        """CREATE TABLE IF NOT EXISTS logs (id INT AUTO_INCREMENT PRIMARY KEY, user_id VARCHAR(255), amount BIGINT, reason VARCHAR(255), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    )
    try:
        c.execute("CREATE INDEX idx_logs_user_created ON logs (user_id, created_at)")
    except Exception:
        pass
    # 經濟總帳鏡像（對應 logs 每一筆；不受 logs_retention_task 清理）
    c.execute(
        """CREATE TABLE IF NOT EXISTS casino_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id VARCHAR(255),
            amount BIGINT,
            reason VARCHAR(255),
            source_log_id INT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    try:
        c.execute("ALTER TABLE casino_logs ADD COLUMN source_log_id INT NULL")
    except Exception:
        pass
    try:
        c.execute("CREATE UNIQUE INDEX uq_casino_logs_source_log_id ON casino_logs (source_log_id)")
    except Exception:
        pass
    try:
        c.execute("CREATE INDEX idx_casino_logs_user_created ON casino_logs (user_id, created_at)")
    except Exception:
        pass
    # 一次性／增量對齊：以 logs.id 去重，將既有流水完整鏡像到 casino_logs（全期總金流）
    try:
        c.execute("SELECT COUNT(*) FROM casino_logs WHERE source_log_id IS NOT NULL")
        mapped_row = c.fetchone()
        mapped_cnt = int((mapped_row[0] if mapped_row else 0) or 0)
        if mapped_cnt == 0:
            c.execute("SELECT COUNT(*) FROM casino_logs")
            cl_any_row = c.fetchone()
            cl_any_cnt = int((cl_any_row[0] if cl_any_row else 0) or 0)
            if cl_any_cnt > 0:
                try:
                    c.execute("TRUNCATE TABLE casino_logs")
                except Exception:
                    c.execute("DELETE FROM casino_logs")
            c.execute(
                "INSERT INTO casino_logs (user_id, amount, reason, source_log_id, created_at) "
                "SELECT user_id, amount, reason, id, created_at FROM logs"
            )
        else:
            c.execute(
                "INSERT INTO casino_logs (user_id, amount, reason, source_log_id, created_at) "
                "SELECT l.user_id, l.amount, l.reason, l.id, l.created_at FROM logs l "
                "LEFT JOIN casino_logs c ON c.source_log_id = l.id "
                "WHERE c.source_log_id IS NULL"
            )
    except Exception:
        pass
    try:
        c.execute("CREATE INDEX idx_users_level_exp ON users (level, exp)")
    except Exception:
        pass
    c.execute(
        """CREATE TABLE IF NOT EXISTS level_milestone_claims (
                 user_id VARCHAR(255) NOT NULL,
                 milestone INT NOT NULL,
                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (user_id, milestone)
                 )"""
    )
    # 清除舊版里程碑（10/25/50），改為 20/40/60/80/100 後不再使用
    try:
        c.execute("DELETE FROM level_milestone_claims WHERE milestone IN (10, 25, 50)")
    except Exception:
        pass
    from bot_modules.lottery_repo import init_lottery_tables
    from bot_modules.milestone_guild_repo import (
        init_milestone_guild_whitelist_table,
        reload_milestone_guild_whitelist_cache,
        seed_milestone_guild_whitelist_from_env,
    )
    from bot_modules.rr_repo import init_rr_stats_table
    from bot_modules.rr_match_repo import init_rr_match_table

    init_lottery_tables(c)
    init_rr_stats_table(c)
    init_rr_match_table(c)
    init_milestone_guild_whitelist_table(c)
    conn.commit()
    conn.close()
    seed_milestone_guild_whitelist_from_env()
    reload_milestone_guild_whitelist_cache()


def log_transaction(user_id, amount, reason):
    if amount == 0:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO logs (user_id, amount, reason) VALUES (%s, %s, %s)",
        (str(user_id), amount, reason),
    )
    new_log_id = c.lastrowid
    c.execute(
        "INSERT INTO casino_logs (user_id, amount, reason, source_log_id) VALUES (%s, %s, %s, %s)",
        (str(user_id), amount, reason, new_log_id),
    )
    conn.commit()
    conn.close()


def log_casino_transaction(user_id, amount, reason, source_log_id=None, created_at=None):
    """logs 鏡像列（給 /casino_stats 全期總金流；不受一般 logs 清理影響）。"""
    if amount == 0:
        return
    conn = get_db_connection()
    c = conn.cursor()
    if created_at is not None:
        c.execute(
            "INSERT INTO casino_logs (user_id, amount, reason, source_log_id, created_at) VALUES (%s, %s, %s, %s, %s)",
            (str(user_id), amount, reason, source_log_id, created_at),
        )
    else:
        c.execute(
            "INSERT INTO casino_logs (user_id, amount, reason, source_log_id) VALUES (%s, %s, %s, %s)",
            (str(user_id), amount, reason, source_log_id),
        )
    conn.commit()
    conn.close()
