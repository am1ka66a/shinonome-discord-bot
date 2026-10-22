"""Start the bot with the deployment Python/dependencies and private .env."""
import argparse
import importlib.metadata
import logging
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]


def preflight():
    if sys.version_info[:2] != (3, 11):
        raise ValueError("請使用專案 .venv 內的 Python 3.11。")
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        package, expected = line.split("==", 1)
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            raise ValueError(f"缺少套件 {package}；請先安裝 requirements.txt。") from None
        if actual != expected:
            raise ValueError(f"{package} 版本不符；請使用 requirements.txt 的固定版本。")
    from dotenv import load_dotenv
    if not (ROOT / ".env").is_file():
        raise ValueError("尚未建立 .env；請複製 .env.example 並在電腦上填入設定。")
    load_dotenv(ROOT / ".env", override=False)
    if not os.getenv("DISCORD_TOKEN", "").strip():
        raise ValueError(".env 尚未填寫 DISCORD_TOKEN。")
    if not (os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL")):
        missing = [key for key in ("MYSQLHOST", "MYSQLPORT", "MYSQLUSER", "MYSQLPASSWORD", "MYSQLDATABASE") if not os.getenv(key, "").strip()]
        if missing:
            raise ValueError(".env 尚未填寫：" + ", ".join(missing))
    sys.path.insert(0, str(ROOT))
    from bot_modules.db import _mysql_connect_kwargs
    settings = _mysql_connect_kwargs()
    if not settings["host"] or settings["host"].endswith(".railway.internal"):
        raise ValueError("本機需要可連線的 MySQL 主機；Railway 內網網址無法從本機使用。")
    if not 1 <= int(settings["port"]) <= 65535:
        raise ValueError("MySQL 連接埠需介於 1 到 65535。")


def main():
    parser = argparse.ArgumentParser(description="本機啟動東雲機器人；先停止使用同一 Token 的雲端 Bot。")
    parser.add_argument("--check", action="store_true", help="只檢查設定，不登入 Discord 或變更資料庫")
    parser.add_argument("--check-db", action="store_true", help="檢查設定及資料庫登入，只執行 SELECT 1")
    args = parser.parse_args()
    try:
        preflight()
        if not args.check:
            from local_dns import install
            install(os.environ)
        if args.check_db:
            from bot_modules.db import db_cursor
            try:
                with db_cursor() as c:
                    c.execute("SELECT 1")
                    c.fetchone()
            except Exception as exc:
                raise ValueError(f"資料庫連線檢查失敗（{type(exc).__name__}）；請核對主機、連接埠及帳密。") from None
            print("設定與資料庫唯讀連線檢查通過。")
            return 0
        if args.check:
            print("本機設定檢查通過；尚未登入 Discord 或連接資料庫。")
            return 0
    except (ValueError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Keep one local process; the cloud instance must be stopped separately.
    state_dir = ROOT / ".local-state"
    state_dir.mkdir(exist_ok=True)
    lock = (state_dir / "bot.lock").open("a+b")
    try:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        print("這個專案已有本機 Bot 在執行。", file=sys.stderr)
        return 2
    try:
        os.chdir(ROOT)
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        runtime_log = str(log_dir / "local-runtime.log")
        os.environ.setdefault("BOT_LOG_FILE", runtime_log)
        logging.basicConfig(filename=runtime_log, encoding="utf-8", level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        os.environ["SHINONOME_LOCAL_LAUNCHER"] = str(Path(__file__).resolve())
        print("本機 Bot 啟動中；請保持電腦開機且不休眠。Ctrl+C 可停止。")
        runpy.run_path(str(ROOT / "bot.py"), run_name="__main__")
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
