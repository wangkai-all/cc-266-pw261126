"""启动入口：python -m app.main [--reset] [--port 8000]"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.init_db import init
from app.server import build_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    parser = argparse.ArgumentParser(description="用途视图脱敏策略引擎（本地演示）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reset", action="store_true", help="删除旧库重新初始化")
    args = parser.parse_args()

    db_path = os.path.join(ROOT, "data", "demo.db")
    export_dir = os.path.join(ROOT, "data", "exports")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    info = init(db_path, reset=args.reset)
    print(f"[init] {info}")

    server, store, jobs = build_server(db_path, export_dir, args.host, args.port)
    print(f"[serve] http://{args.host}:{args.port}/  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jobs.shutdown()
        server.shutdown()
        store.close()


if __name__ == "__main__":
    main()
