"""Keep one bot process per database using a dedicated MySQL session lock."""
import hashlib
import threading

import pymysql

from bot_modules.db import _mysql_connect_kwargs


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
                c.execute("SELECT GET_LOCK(%s,0)", (self.name,))
                if c.fetchone()[0] != 1:
                    raise RuntimeError("已有機器人使用這個資料庫；拒絕重複啟動")
                c.execute("SELECT CONNECTION_ID()")
                self.owner = int(c.fetchone()[0])
                self.connection = conn
            except BaseException:
                conn.close()
                raise

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
                self.connection.close()  # MySQL releases this session's named lock.
                self.connection = None
