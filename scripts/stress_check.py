import argparse
import asyncio
import os
import sys
import time
from typing import Dict, List, Tuple

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from bot_modules.db import get_db_connection
from bot_modules.wanted_repo import (
    fetch_good_citizen_rows_sync,
    fetch_wanted_list_rows_sync,
)


def build_db_config_summary() -> str:
    mysql_url = os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL")
    if mysql_url:
        masked = mysql_url
        if "@" in masked and "://" in masked:
            head, tail = masked.split("@", 1)
            scheme_sep = head.find("://")
            if scheme_sep >= 0:
                creds = head[scheme_sep + 3 :]
                if ":" in creds:
                    user = creds.split(":", 1)[0]
                    head = head[: scheme_sep + 3] + user + ":***"
                else:
                    head = head[: scheme_sep + 3] + "***"
            masked = head + "@" + tail
        return f"MYSQL_URL/DATABASE_URL={masked}"
    host = os.getenv("MYSQLHOST") or os.getenv("DB_HOST") or "localhost"
    port = os.getenv("MYSQLPORT") or os.getenv("DB_PORT") or "3306"
    user = os.getenv("MYSQLUSER") or os.getenv("DB_USER") or "(empty)"
    db = os.getenv("MYSQLDATABASE") or os.getenv("DB_NAME") or "(empty)"
    pwd = os.getenv("MYSQLPASSWORD") or os.getenv("DB_PASS")
    pwd_state = "set" if pwd else "empty"
    return f"MYSQLHOST/DB_HOST={host}, MYSQLPORT/DB_PORT={port}, MYSQLUSER/DB_USER={user}, MYSQLDATABASE/DB_NAME={db}, password={pwd_state}"


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def load_sample_user_ids(limit: int = 500) -> List[str]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users ORDER BY user_id ASC LIMIT %s", (int(limit),))
    rows = c.fetchall()
    conn.close()
    return [str(r[0]) for r in rows if r and r[0] is not None]


def fetch_wanted_status_row_sync(user_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(role,'civilian'), COALESCE(wanted_stars,0), COALESCE(wanted_hunted_count,0),
                  COALESCE(in_prison,0), last_five_robs, COALESCE(arrest_count,0),
                  COALESCE(revenge_pending,0), COALESCE(revenge_amount,0), COALESCE(bail_debt,0),
                  COALESCE(good_citizen_cert_active,0)
           FROM users WHERE user_id=%s""",
        (str(user_id),),
    )
    row = c.fetchone()
    conn.close()
    return row


def db_work(kind: str, user_ids: List[str], idx: int):
    if kind == "wanted_status":
        uid = user_ids[idx % len(user_ids)]
        return fetch_wanted_status_row_sync(uid)
    if kind == "wanted_list":
        return fetch_wanted_list_rows_sync(get_db_connection)
    return fetch_good_citizen_rows_sync(get_db_connection)


async def run_once(
    kind: str,
    sem: asyncio.Semaphore,
    latencies: List[float],
    errors: List[str],
    user_ids: List[str],
    idx: int,
) -> None:
    async with sem:
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(db_work, kind, user_ids, idx)
        except Exception as e:
            errors.append(str(e))
        latencies.append((time.perf_counter() - t0) * 1000.0)


async def bench(concurrency: int, total: int, kind: str) -> Dict[str, float]:
    user_ids = await asyncio.to_thread(load_sample_user_ids, 500)
    if not user_ids:
        raise RuntimeError("users table is empty; cannot benchmark wanted_status.")
    sem = asyncio.Semaphore(concurrency)
    latencies: List[float] = []
    errors: List[str] = []
    t0 = time.perf_counter()
    await asyncio.gather(
        *[
            run_once(kind, sem, latencies, errors, user_ids, i)
            for i in range(total)
        ]
    )
    elapsed = time.perf_counter() - t0
    rps = (total / elapsed) if elapsed > 0 else 0.0
    return {
        "elapsed_s": elapsed,
        "rps": rps,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "max_ms": max(latencies) if latencies else 0.0,
        "errors": len(errors),
    }


def fmt_result(kind: str, concurrency: int, total: int, r: Dict[str, float]) -> str:
    return (
        f"[{kind}] c={concurrency:>3} total={total:>4} | "
        f"elapsed={r['elapsed_s']:.3f}s rps={r['rps']:.1f} | "
        f"p50={r['p50_ms']:.2f}ms p95={r['p95_ms']:.2f}ms "
        f"p99={r['p99_ms']:.2f}ms max={r['max_ms']:.2f}ms err={int(r.get('errors', 0))}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight async stress check (50/100/200)")
    parser.add_argument("--kind", default="wanted_status", choices=["wanted_status", "wanted_list", "good_citizen"])
    parser.add_argument("--concurrency", default="50,100,200", help="comma-separated concurrency list")
    parser.add_argument("--factor", type=int, default=10, help="total requests = concurrency * factor")
    args = parser.parse_args()

    groups: List[Tuple[int, int]] = []
    for part in str(args.concurrency).split(","):
        c = max(1, int(part.strip()))
        groups.append((c, c * max(1, args.factor)))

    print(f"Stress kind={args.kind}, groups={groups}")
    print(f"DB config summary: {build_db_config_summary()}")
    for concurrency, total in groups:
        try:
            result = await bench(concurrency, total, args.kind)
            print(fmt_result(args.kind, concurrency, total, result))
        except Exception as e:
            print("")
            print("Benchmark aborted due to DB connection/query error.")
            print(f"Error: {e}")
            print("Likely causes:")
            print("1) MySQL service is not running / host not reachable")
            print("2) Environment variables are missing or point to localhost unexpectedly")
            print("3) Credentials/database name are incorrect")
            print("")
            print(f"Current config: {build_db_config_summary()}")
            print("Fix env, then rerun this script.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
