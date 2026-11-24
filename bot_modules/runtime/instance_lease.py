"""Keep one bot process per database using a dedicated MySQL session lock."""
import asyncio
import hashlib
import threading

import pymysql

from bot_modules.db import _mysql_connect_kwargs, is_transient_db_error

LEASE_IDLE_TIMEOUT_SECONDS = 60
LEASE_HEARTBEAT_SECONDS = 5


class LeaseBusyError(RuntimeError):
    """Another session still owns the lock; callers must wait, never steal it."""


class InstanceLease:
    def __init__(self, connect=None):
        self.connect = connect or pymysql.connect
        self.connection = None
        self.name = None
        self.owner = None
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            kwargs = _mysql_connect_kwargs()
            self.name = "shinonome:bot:" + hashlib.sha256(
                str(kwargs.get("database")).encode()
            ).hexdigest()[:32]
            conn = self.connect(**dict(kwargs, autocommit=True, read_timeout=5, write_timeout=5))
            try:
                c = conn.cursor()
                # Scope expiry to this dedicated connection. A network-partitioned
                # session must not keep the lock for MySQL's default eight hours.
                c.execute("SET SESSION wait_timeout=%s", (LEASE_IDLE_TIMEOUT_SECONDS,))
                c.execute("SELECT GET_LOCK(%s,0)", (self.name,))
                acquired = c.fetchone()[0]
                if acquired == 0:
                    raise LeaseBusyError("已有機器人使用這個資料庫；拒絕重複啟動")
                if acquired != 1:
                    raise RuntimeError("資料庫未能確認執行鎖結果")
                c.execute("SELECT CONNECTION_ID()")
                self.owner = int(c.fetchone()[0])
                self.connection = conn
            except BaseException:
                conn.close()
                raise

    async def acquire_with_retry(self, logger, *, initial_delay=5, max_delay=30):
        """Wait offline for a free lock/healthy DB without exhausting restarts."""
        if not 0 < initial_delay <= max_delay:
            raise ValueError("執行鎖重試間隔不正確")
        delay, failures = initial_delay, 0
        while True:
            attempt = asyncio.create_task(asyncio.to_thread(self.acquire))
            try:
                # A cancelled to_thread worker can still acquire a lock. Wait for
                # completion and close it before propagating shutdown cancellation.
                await asyncio.shield(attempt)
            except asyncio.CancelledError:
                try:
                    await attempt
                except Exception:
                    pass
                await asyncio.to_thread(self.close)
                raise
            except Exception as error:
                if not isinstance(error, LeaseBusyError) and not is_transient_db_error(error):
                    raise
                failures += 1
                if failures == 1:
                    reason = "鎖仍被占用" if isinstance(error, LeaseBusyError) else "資料庫暫時無法連線"
                    logger.warning("等待機器人執行權：%s；將持續重試，最多每 %s 秒一次", reason, max_delay)
                logger.debug("取得執行權第 %s 次失敗，%s 秒後重試", failures, delay, exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)
            else:
                if failures:
                    logger.info("已取得機器人執行權，等待重試 %s 次後恢復啟動", failures)
                return

    def verify(self):
        with self.lock:
            if self.connection is None:
                raise RuntimeError("機器人執行權已釋放")
            # Reconnecting would lose the lock while appearing healthy.
            self.connection.ping(reconnect=False)
            c = self.connection.cursor()
            c.execute("SELECT IS_USED_LOCK(%s)", (self.name,))
            if c.fetchone()[0] != self.owner:
                raise RuntimeError("機器人執行權已遺失")

    def close(self):
        with self.lock:
            if self.connection is not None:
                conn, self.connection = self.connection, None
                self.owner = None
                conn.close()  # MySQL releases this session's named lock.
