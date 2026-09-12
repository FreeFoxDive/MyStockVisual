# -*- coding: utf-8 -*-
"""画线模式冒烟测试服务器: 临时 DB + 独立端口, 不触碰 data/trades.db。"""
import sys
import tempfile
from pathlib import Path

VISUAL_DIR = Path(__file__).resolve().parent
if str(VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(VISUAL_DIR))

import trades  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="visual_smoke_"))
trades.init_db(_tmp / "smoke.db")
if not any(u["username"] == "smoke" for u in trades.list_users()):
    trades.create_user("smoke", "smoke12345", is_admin=True)

from app import create_app  # noqa: E402

app = create_app()
print(f"SMOKE_DB={_tmp / 'smoke.db'}", flush=True)
app.run(host="127.0.0.1", port=8899, threaded=True)
