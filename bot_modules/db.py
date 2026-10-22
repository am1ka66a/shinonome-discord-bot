import contextlib
import os
import time
from urllib.parse import urlparse

import pymysql
from dbutils.pooled_db import PooledDB
import threading

# PyMySQL 在 socket 已被對端關掉、或 Railway proxy 丟空封包時，會變成 InterfaceError(0, '')。
# DBUtils 會嘗試重連；重連當下若 DB 還不可達，就會把這個空錯誤原樣丟回來。
_TRANSIENT_DB_CODES = {0, 2003, 2006, 2013, 2014, 2045, 4031}

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
                "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
            }
    return {
        "host": os.getenv("MYSQLHOST") or os.getenv("DB_HOST"),
        "port": int(os.getenv("MYSQLPORT") or os.getenv("DB_PORT", 3306)),
        "user": os.getenv("MYSQLUSER") or os.getenv("DB_USER"),
        "password": os.getenv("MYSQLPASSWORD") or os.getenv("DB_PASS"),
        "database": os.getenv("MYSQLDATABASE") or os.getenv("DB_NAME"),
        "charset": "utf8mb4",
        "init_command": "SET time_zone = '+08:00'",
        "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
    }


def _ensure_mysql_auth_deps() -> None:
    """caching_sha2_password 在非 SSL 新建連線時要用 cryptography 做 RSA。"""
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "缺少 cryptography，無法用 MySQL 8 的 caching_sha2_password 認證。"
            "請把 cryptography 裝進 requirements 後重新部署。"
        ) from exc


def _get_mysql_pool() -> PooledDB:
    global _mysql_pool
    if _mysql_pool is None:
        with _mysql_pool_lock:
            if _mysql_pool is None:
                _ensure_mysql_auth_deps()
                kw = _mysql_connect_kwargs()
                _mysql_pool = PooledDB(
                    creator=pymysql,
                    mincached=int(os.getenv("MYSQL_POOL_MINCACHED", "1")),
                    maxcached=int(os.getenv("MYSQL_POOL_MAXCACHED", "8")),
                    maxconnections=int(os.getenv("MYSQL_POOL_MAX", "16")),
                    # 用到一定次數就丟，避免連線活超過 MySQL wait_timeout / proxy idle
                    maxusage=int(os.getenv("MYSQL_POOL_MAXUSAGE", "50")),
                    blocking=True,
                    # ping=2（建立游標時檢查）而非預設的 1（自池中取出時檢查）：
                    # DBUtils 重連失敗時會靜默發出死連線，之後 query 才炸成
                    # InterfaceError(0, '')。檢查點放在建游標時，steady_db 的
                    # 內建重試（失敗後重開游標再執行一次）才會一併重建底層連線。
                    # 若重連當下 DB 仍不可達，call_with_db_retry 會再隔一小段時間重試整段操作。
                    ping=int(os.getenv("MYSQL_POOL_PING", "2")),
                    **kw,
                )
    return _mysql_pool


def is_transient_db_error(exc: BaseException) -> bool:
    """判斷是否為連線被掐斷、空封包、server gone away 這類可重試的錯誤。"""
    if isinstance(exc, (pymysql.err.InterfaceError, pymysql.err.OperationalError, pymysql.err.InternalError)):
        code = exc.args[0] if exc.args else None
        if code in _TRANSIENT_DB_CODES or code in ("", None):
            return True
    text = str(exc)
    return text == "(0, '')" or "InterfaceError(0, '')" in text or "(0, '')" in text


def call_with_db_retry(func, *args, retries: int = 3, delay: float = 0.25, **kwargs):
    """整段操作重試。用在唯讀快照：DBUtils 當下重連失敗時，隔一小段時間再拿新連線。"""
    last_exc: BaseException | None = None
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_transient_db_error(exc) or attempt >= attempts - 1:
                raise
            time.sleep(delay * (2 ** attempt))
    raise last_exc  # pragma: no cover


def get_db_connection():
    """從連線池取得連線；用完請 commit／rollback 並 close（會歸還池中）。"""
    return _get_mysql_pool().connection()


def _release_conn(conn, *, discard: bool = False) -> None:
    """歸還連線。discard=True 時先關掉底層 socket，避免把死連線放回池裡。"""
    if discard:
        try:
            inner = getattr(conn, "_con", None)
            if inner is not None:
                inner.close()
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass


@contextlib.contextmanager
def db_conn(connect=None):
    """連線 context manager：離開時保證歸還連線，發生例外時先 rollback。

    connect 可傳入自訂的取連線函式（供以依賴注入方式取得連線的模組使用）。
    """
    conn = (connect or get_db_connection)()
    discard = False
    try:
        yield conn
    except BaseException as exc:
        discard = is_transient_db_error(exc)
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _release_conn(conn, discard=discard)


@contextlib.contextmanager
def db_cursor(commit: bool = False, connect=None):
    """游標 context manager；commit=True 時正常離開會自動 commit。"""
    with db_conn(connect) as conn:
        yield conn.cursor()
        if commit:
            conn.commit()


def init_db():
    with db_conn() as conn:
        c = conn.cursor()
        from bot_modules.state_repo import init_state_tables
        init_state_tables(c)
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
    seed_milestone_guild_whitelist_from_env()
    reload_milestone_guild_whitelist_cache()


def log_transaction(user_id, amount, reason):
    if amount == 0:
        return
    with db_cursor(commit=True) as c:
        c.execute(
            "INSERT INTO logs (user_id, amount, reason) VALUES (%s, %s, %s)",
            (str(user_id), amount, reason),
        )
        new_log_id = c.lastrowid
        c.execute(
            "INSERT INTO casino_logs (user_id, amount, reason, source_log_id) VALUES (%s, %s, %s, %s)",
            (str(user_id), amount, reason, new_log_id),
        )


def log_casino_transaction(user_id, amount, reason, source_log_id=None, created_at=None):
    """logs 鏡像列（給 /casino_stats 全期總金流；不受一般 logs 清理影響）。"""
    if amount == 0:
        return
    with db_cursor(commit=True) as c:
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
