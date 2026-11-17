"""Create an ignored local backup; no credentials or row contents are printed."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

if __name__ == "__main__":
    import os
    import pymysql
    from local_dns import install
    from bot_modules.backup import export_database
    from bot_modules.db import _mysql_connect_kwargs
    install(os.environ)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    conn = pymysql.connect(**_mysql_connect_kwargs())
    try:
        summary = export_database(conn, args.output)
        args.output.with_name(args.output.name + ".manifest.json").write_text(json.dumps(summary,indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary))
    finally:
        conn.close()
