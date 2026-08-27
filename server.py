#!/usr/bin/env python3
"""
Visual K线图 HTTP 服务器
========================
Flask + Waitress；行情数据见 market.py，鉴权/限流见 security.py。

用法:
  python -u visual/server.py                   # 默认 localhost:8888
  python -u visual/server.py --port 9999
  python -u visual/server.py --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
from pathlib import Path

# Windows GBK 编码兼容
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app import bootstrap_admin, create_app, start_background_jobs  # noqa: E402
from security import RATE_LIMIT_PER_MIN  # noqa: E402

log = logging.getLogger("server")


def main():
    parser = argparse.ArgumentParser(description="Visual K线图 HTTP 服务器")
    parser.add_argument("--port", type=int, default=8888, help="监听端口 (default: 8888)")
    parser.add_argument("--host", type=str, default="localhost", help="监听地址 (default: localhost)")
    args = parser.parse_args()

    app = create_app()
    bootstrap_admin()
    start_background_jobs()

    print(f"""
╔══════════════════════════════════════════╗
║   📈 Visual K线图 股票可视化              ║
║   数据源: 麦蕊(日K) + AlphaFeed(分时/分钟) ║
║   地址: http://{args.host}:{args.port}             ║
║   速率限制: {RATE_LIMIT_PER_MIN} 次/分钟           ║
╚══════════════════════════════════════════╝
""", flush=True)

    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8)
    except KeyboardInterrupt:
        log.info("服务器已停止")


if __name__ == "__main__":
    main()
