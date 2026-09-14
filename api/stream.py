"""Flask 路由: 快照 SSE 推送 (/api/stream/quotes)。

快照默认 1.25s 检查新数据，所有连接共享按代码批量采集和 48/min 滚动预算。
可选 depth / bars 事件复用连接；五档使用套餐限额的 4/5（默认 24/min）。
请求耗时计入周期，不积压请求、不突发补发；HTTP 回退复用相同缓存与预算。

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

QUOTE_SSE_INTERVAL = max(1.25, float(os.environ.get("QUOTE_SSE_INTERVAL", "1.25")))
INDICATOR_SSE_INTERVAL = max(5.0, float(os.environ.get("INDICATOR_SSE_INTERVAL", "10")))
DEPTH_SSE_INTERVAL = 60.0 / market.DEPTH_RATE_PER_MIN
QUOTE_SSE_MAX_CLIENTS = max(1, int(os.environ.get("QUOTE_SSE_MAX_CLIENTS", "3")))
# 保活/断线探测间隔: 客户端断开后最多 ~1s 归还并发槽, 避免连续换股叠满 3 槽返回 429
QUOTE_SSE_TICK = max(0.05, float(os.environ.get("QUOTE_SSE_TICK", "0.25")))
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
    symbols = list(dict.fromkeys(symbols))[:QUOTE_SSE_MAX_SYMBOLS]
    with_depth = request.args.get("depth") == "1"
    tail_period = request.args.get("tail")
    if tail_period not in (None, "1d", "1w", "1M"):
        return _error("不支持的指标周期", 400)
    tail_adjust = market.kline_source.normalize_adjust(request.args.get("adjust"))
    try:
        tail_count = max(1, min(int(request.args.get("count", "1006")), 1500))
    except ValueError:
        return _error("无效 count", 400)

    if not _sse_slots.acquire(blocking=False):
        return _error("SSE 连接数已达上限, 回退轮询", 429)

    def generate():
        token = object()
        try:
            market.register_quote_interest(token, symbols)
            # retry: 断线后浏览器 EventSource 重连间隔
            yield "retry: 5000\n\n"
            # 按 TICK 小步唤醒: 未到推送点发注释帧保活, 一旦客户端断开, 下一次
            # 写入即失败并触发 finally 归还槽位 (而非等满 INTERVAL 才醒)。
            start = time.monotonic()
            last_tail = start - INDICATOR_SSE_INTERVAL
            last_depth = start - DEPTH_SSE_INTERVAL
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
                if with_depth and now - last_depth >= DEPTH_SSE_INTERVAL:
                    last_depth = now
                    try:
                        depths = {s: d for s in symbols if (d := market.fetch_depth(s)) is not None}
                        if depths:
                            payload = json.dumps(depths, ensure_ascii=False, cls=market.NumpyEncoder)
                            yield f"event: depth\ndata: {payload}\n\n"
                    except Exception:
                        yield ": depth unavailable\n\n"
                if tail_period and now - last_tail >= INDICATOR_SSE_INTERVAL:
                    last_tail = now
                    try:
                        from api.kline import build_kline_tail
                        bars = build_kline_tail(symbols[0], tail_period, tail_count, tail_adjust)
                        payload = json.dumps({symbols[0]: bars}, ensure_ascii=False, cls=market.NumpyEncoder)
                        yield f"event: bars\ndata: {payload}\n\n"
                    except Exception:
                        yield ": indicators unavailable\n\n"
                time.sleep(QUOTE_SSE_TICK)
        finally:
            market.unregister_quote_interest(token)
            _sse_slots.release()

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
