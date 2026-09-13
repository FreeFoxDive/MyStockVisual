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
import market_hours
from api import api_bp
from api.common import _error, _require_user

QUOTE_SSE_INTERVAL = max(3.0, float(os.environ.get("QUOTE_SSE_INTERVAL", "10")))
QUOTE_SSE_MAX_CLIENTS = max(1, int(os.environ.get("QUOTE_SSE_MAX_CLIENTS", "3")))
# 保活/断线探测间隔: 客户端断开后最多 ~1s 归还并发槽, 避免连续换股叠满 3 槽返回 429
QUOTE_SSE_TICK = max(0.05, float(os.environ.get("QUOTE_SSE_TICK", "1")))
QUOTE_SSE_MAX_SYMBOLS = 50
# 单连接最长寿命: 到期正常结束流(非异常), 浏览器按 retry 自动重连,
# 避免半死连接(客户端消失但 TCP 未报错)无限占用 waitress 线程。
QUOTE_SSE_MAX_LIFETIME = max(1.0, float(os.environ.get("QUOTE_SSE_MAX_LIFETIME", "1800")))

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
            # 按 TICK 小步唤醒: 未到推送点发注释帧保活, 一旦客户端断开, 下一次
            # 写入即失败并触发 finally 归还槽位 (而非等满 INTERVAL 才醒)。
            start = time.monotonic()
            last_push = start - QUOTE_SSE_INTERVAL  # 首帧立即推送
            while True:
                now = time.monotonic()
                if now - start >= QUOTE_SSE_MAX_LIFETIME:
                    # 到期正常结束(返回而非抛异常/非 200), 浏览器会按 retry 重连
                    yield ": rotate\n\n"
                    return
                if now - last_push >= QUOTE_SSE_INTERVAL:
                    last_push = now
                    try:
                        quotes = market.fetch_quotes(symbols)
                        # 每条快照附交易日标志: 前端据此拦截非交易日用残留快照补当日 bar
                        td = market_hours.is_trading_day()
                        payload = json.dumps(
                            {s: {**q, "is_trading_day": td} for s, q in quotes.items()},
                            ensure_ascii=False, cls=market.NumpyEncoder,
                        )
                        yield f"data: {payload}\n\n"
                    except Exception:
                        # 单次快照失败: 发注释帧保活, 下轮重试
                        yield ": tick\n\n"
                else:
                    yield ": keepalive\n\n"
                time.sleep(QUOTE_SSE_TICK)
        finally:
            _sse_slots.release()

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
