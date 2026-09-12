"""Flask 路由: 快照 SSE 推送 (/api/stream/quotes)。

替代前端 10s 轮询 /api/quote: 一条长连接按间隔推送当前标的快照,
上游压力由 fetch_quotes 的 30s 快照缓存控制 (每连接 ≤2 次上游调用/分钟,
远低于 AF 快照额度 60/min 的 4/5 = 48/min)。

线程预算: waitress threads=8, 每条 SSE 常驻一个线程 ——
QUOTE_SSE_MAX_CLIENTS (默认 3) 用信号量限并发, 超限返回 429 由前端回退轮询。
"""
from __future__ import annotations

import json
import os
import threading
import time

from flask import Response, request

import market
from api import api_bp
from api.common import _error, _require_user

QUOTE_SSE_INTERVAL = max(3.0, float(os.environ.get("QUOTE_SSE_INTERVAL", "10")))
QUOTE_SSE_MAX_CLIENTS = max(1, int(os.environ.get("QUOTE_SSE_MAX_CLIENTS", "3")))
QUOTE_SSE_MAX_SYMBOLS = 50

_sse_slots = threading.BoundedSemaphore(QUOTE_SSE_MAX_CLIENTS)


@api_bp.route("/api/stream/quotes", methods=["GET"])
def stream_quotes():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbols_raw = request.args.get("symbols") or ""
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        return _error("缺少 symbols 参数")
    symbols = symbols[:QUOTE_SSE_MAX_SYMBOLS]

    if not _sse_slots.acquire(blocking=False):
        return _error("SSE 连接数已达上限, 回退轮询", 429)

    def generate():
        try:
            # retry: 断线后浏览器 EventSource 重连间隔
            yield "retry: 5000\n\n"
            while True:
                try:
                    quotes = market.fetch_quotes(symbols)
                    payload = json.dumps(quotes, ensure_ascii=False, cls=market.NumpyEncoder)
                    yield f"data: {payload}\n\n"
                except Exception:
                    # 单次快照失败: 发注释帧保活, 下轮重试
                    yield ": tick\n\n"
                time.sleep(QUOTE_SSE_INTERVAL)
        finally:
            _sse_slots.release()

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
