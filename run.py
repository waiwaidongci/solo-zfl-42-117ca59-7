"""启动入口：python3 run.py [--host 0.0.0.0] [--port 8000] [--db data/app.db] [--no-seed]"""
from __future__ import annotations

import argparse

from app.db import Database
from app.server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="漆线雕学徒培训与上岗资格台")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/app.db")
    parser.add_argument("--no-seed", action="store_true", help="不写入演示种子数据")
    args = parser.parse_args()

    db = Database(args.db)
    httpd = make_server(args.host, args.port, db, run_seed=not args.no_seed)
    print(f"漆线雕资格台已启动：http://{args.host}:{args.port}/  （数据 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭……")
    finally:
        httpd.server_close()
        db.close()


if __name__ == "__main__":
    main()
