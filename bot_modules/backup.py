"""Streaming consistent exports and restore drills restricted to a local test DB."""
import datetime
import gzip
import hashlib
import json
import re
from pathlib import Path

import pymysql


def identifier(value):
    return "`" + value.replace("`", "``") + "`"


def export_database(conn, output):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".partial")
    rows = 0
    try:
        c = conn.cursor()
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        c.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        c.execute("SHOW FULL TABLES WHERE Table_type='BASE TABLE'")
        tables = [row[0] for row in c.fetchall()]
        schemas = {}
        for name in tables:
            c.execute("SHOW CREATE TABLE " + identifier(name))
            schemas[name] = c.fetchone()[1]
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            def emit(value):
                stream.write(json.dumps(value, ensure_ascii=True) + "\n")
            emit({"format": "shinonome-backup-v1", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
            for name in tables:
                emit({"sql": schemas[name]})
                with conn.cursor(pymysql.cursors.SSCursor) as reader:
                    reader.execute("SELECT * FROM " + identifier(name))
                    columns = ",".join(identifier(column[0]) for column in reader.description)
                    prefix = "INSERT INTO " + identifier(name) + "(" + columns + ") VALUES ("
                    while True:
                        batch = reader.fetchmany(500)
                        if not batch:
                            break
                        for row in batch:
                            values = ",".join("X'" + value.hex() + "'" if isinstance(value, bytes) else conn.escape(value) for value in row)
                            emit({"sql": prefix + values + ")"})
                            rows += 1
            for name in tables:
                c.execute("SHOW CREATE TABLE " + identifier(name))
                current = re.sub(r"AUTO_INCREMENT=\d+", "", c.fetchone()[1])
                original = re.sub(r"AUTO_INCREMENT=\d+", "", schemas[name])
                if current != original:
                    raise RuntimeError("匯出期間結構發生變更，備份已取消")
            emit({"complete": True, "tables": len(tables), "rows": rows})
        conn.rollback()  # End the read-only snapshot without changing production.
        temporary.replace(output)
    except BaseException:
        conn.rollback()
        temporary.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256()
    with output.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"tables": len(tables), "rows": rows, "sha256": digest.hexdigest()}


def restore_test_database(conn, source):
    if conn.host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("還原演練只允許本機測試資料庫")
    c = conn.cursor()
    c.execute("SELECT DATABASE()")
    if c.fetchone()[0] != "shinonome_restore_test":
        raise RuntimeError("還原演練只允許 shinonome_restore_test")
    c.execute("SHOW TABLES")
    if c.fetchall():
        raise RuntimeError("測試資料庫必須為空，不會覆寫現有資料")
    c.execute("SET time_zone='+08:00'")
    c.execute("SET FOREIGN_KEY_CHECKS=0")
    try:
        with gzip.open(source, "rt", encoding="utf-8") as stream:
            if json.loads(next(stream)).get("format") != "shinonome-backup-v1":
                raise ValueError("不支援的備份格式")
            completed = None
            for line in stream:
                record = json.loads(line)
                if record.get("complete"):
                    completed = record
                elif "sql" in record:
                    c.execute(record["sql"])
            if completed is None:
                raise ValueError("備份未完整匯出")
        conn.commit()
        return completed
    finally:
        c.execute("SET FOREIGN_KEY_CHECKS=1")
