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


class _RecentMySQLConnection(pymysql.connections.Connection):
    """Avoid a separate network ping after a recent successful query."""

    def query(self, *args, **kwargs):
        try:
            result = super().query(*args, **kwargs)
        except BaseException:
            self._last_verified_query = None
            raise
        self._last_verified_query = time.monotonic()
        return result

    def ping(self, reconnect=True):
        verified = getattr(self, "_last_verified_query", None)
        interval = float(os.getenv("MYSQL_HEALTHCHECK_IDLE_SECONDS", "15"))
        if self._sock is not None and verified is not None and time.monotonic() - verified < interval:
            return
        result = super().ping(reconnect=reconnect)
        self._last_verified_query = time.monotonic()
        return result


class _PooledMySQLDriver:
    # DBUtils accepts a DB-API provider; expose the original exception classes
    # so its failure detection remains identical to the PyMySQL module.
    connect = _RecentMySQLConnection
    threadsafety = pymysql.threadsafety
    OperationalError = pymysql.OperationalError
    InterfaceError = pymysql.InterfaceError
    InternalError = pymysql.InternalError


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
                "read_timeout": int(os.getenv("MYSQL_READ_TIMEOUT", "30")),
                "write_timeout": int(os.getenv("MYSQL_WRITE_TIMEOUT", "30")),
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
        "read_timeout": int(os.getenv("MYSQL_READ_TIMEOUT", "30")),
        "write_timeout": int(os.getenv("MYSQL_WRITE_TIMEOUT", "30")),
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
                    creator=_PooledMySQLDriver,
                    mincached=int(os.getenv("MYSQL_POOL_MINCACHED", "1")),
                    maxcached=int(os.getenv("MYSQL_POOL_MAXCACHED", "8")),
                    maxconnections=int(os.getenv("MYSQL_POOL_MAX", "16")),
                    # Keep healthy connections; recycling every 50 statements
                    # adds repeated remote authentication and connection setup.
                    maxusage=int(os.getenv("MYSQL_POOL_MAXUSAGE", "0")),
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
    from bot_modules.migrations import run_migrations
    with db_conn(connect=lambda: pymysql.connect(**_mysql_connect_kwargs())) as conn:
        run_migrations(conn)


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
