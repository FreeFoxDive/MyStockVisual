"""Flask 路由: 快照 SSE 推送 (/api/stream/quotes)。

全池快照发布后唤醒订阅；池外和 legacy 路径按 1.25s 检查，沿用 48/min 预算。
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
import zlib
from contextlib import closing

from flask import Response, request

try:
    from compression import zstd
except ImportError:
    zstd = None
    try:
        import zstandard
    except ImportError:
        zstandard = None
else:
    zstandard = None

import market
import market_hours
import perf
import quote_snapshots
from api import api_bp
from api.common import _error, _json, _require_user

QUOTE_SSE_INTERVAL = max(1.25, float(os.environ.get("QUOTE_SSE_INTERVAL", "1.25")))
INDICATOR_SSE_INTERVAL = max(5.0, float(os.environ.get("INDICATOR_SSE_INTERVAL", "5")))
DEPTH_SSE_INTERVAL = 60.0 / market.DEPTH_RATE_PER_MIN
QUOTE_SSE_MAX_CLIENTS = max(1, int(os.environ.get("QUOTE_SSE_MAX_CLIENTS", "3")))
# 保活/断线探测间隔: 客户端断开后最多 ~1s 归还并发槽, 避免连续换股叠满 3 槽返回 429
QUOTE_SSE_TICK = max(0.05, float(os.environ.get("QUOTE_SSE_TICK", "0.25")))
QUOTE_SSE_MAX_SYMBOLS = 50
SSE_ZSTD_MIN_SYMBOLS = 20
# 单连接最长寿命: 到期正常结束流(非异常), 浏览器按 retry 自动重连,
# 避免半死连接(客户端消失但 TCP 未报错)无限占用 waitress 线程。
QUOTE_SSE_MAX_LIFETIME = max(1.0, float(os.environ.get("QUOTE_SSE_MAX_LIFETIME", "1800")))

_sse_slots = threading.BoundedSemaphore(QUOTE_SSE_MAX_CLIENTS)

# 探测重试间隔钳位: 下界避免打转, 上界避免"提示算错"(日历降级/服务端刚重启)时
# 整段时间不再复探 —— 最长半小时必回来一次。
_RETRY_MIN, _RETRY_MAX = 5, 1800


def _sse_encoding(symbol_count):
    accepted = request.accept_encodings
    user_agent = request.user_agent.string
    webkit = "AppleWebKit/" in user_agent and "Chrome/" not in user_agent and "Chromium/" not in user_agent
    # Apple WebKit can stall on zstd with HTTP/1.1 chunked HTTPS (WebKit #311087).
    offers = ("gzip",)
    if (symbol_count >= SSE_ZSTD_MIN_SYMBOLS and (zstd is not None or zstandard is not None)
            and not webkit and any(name.lower() == "zstd" for name, _ in accepted)):
        offers = ("zstd", "gzip")
    encoding = accepted.best_match(offers)
    return encoding if encoding and accepted[encoding] >= accepted["identity"] else None


def _sse_compressor(encoding):
    if encoding == "gzip":
        codec = zlib.compressobj(level=3, wbits=31)
        return lambda frame: codec.compress(frame) + codec.flush(zlib.Z_SYNC_FLUSH), codec.flush
    if zstd is not None:
        codec = zstd.ZstdCompressor(level=3)
        return lambda frame: codec.compress(frame, codec.FLUSH_BLOCK), codec.flush
    codec = zstandard.ZstdCompressor(level=3).compressobj()
    return lambda frame: codec.compress(frame) + codec.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK), codec.flush


def _encode_sse(frames, encoding):
    counter = f"sse_wire_{encoding}_bytes"
    with closing(frames):
        compress_frame, finish = _sse_compressor(encoding)
        for frame in frames:
            chunk = compress_frame(frame.encode("utf-8"))
            perf.bump(counter, len(chunk))
            yield chunk
        trailer = finish()
        if trailer:
            perf.bump(counter, len(trailer))
            yield trailer


def _retry_after_sec() -> int:
    """下次值得再来建流的秒数, 由下一次可建流时刻推导。"""
    secs = market_hours.market_status()["next_stream_in_sec"]
    if secs is None:
        return _RETRY_MIN
    return int(min(max(secs, _RETRY_MIN), _RETRY_MAX))


def _market_frame() -> str:
    """行情相位帧 (event: market)。

    只在连接建立时与相位变化时发, 不随快照周期重复 —— 客户端据此切自动刷新、
    算下一次唤醒, 不必再自己定时轮询 /api/ping(那正是"开盘后最多 60s 不刷"的根源)。
    """
    st = market_hours.market_status()
    # 字段名与 /api/ping、/api/kline 的 meta 完全一致 (同一份 market_status),
    # 前端只有一套解析逻辑, 不因通道不同而出两套口径。
    payload = {
        "session_phase": st["session_phase"],
        "time": st["time"],
        "quote_live": st["quote_live"],
        "stream_allowed": st["stream_allowed"],
        "in_session": st["in_session"],
        "is_trading_day": st["is_trading_day"],
        "next_open_at": st["next_open_at"],
        "next_open_in_sec": st["next_open_in_sec"],
        "next_live_at": st["next_live_at"],
        "next_live_in_sec": st["next_live_in_sec"],
        "calendar_source": st["calendar_source"],
        "server_ms": st["server_ms"],
    }
    return "event: market\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@api_bp.route("/api/stream/quotes", methods=["GET"])
def stream_quotes():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    # 交易日 09:00 起即可建流 (盘前/午休/盘后都允许): 这段只发保活帧、不取上游
    # 数据, 提前连上换来 09:15 首帧零握手延迟。"要不要取数"在 generate() 内单独判。
    if not market_hours.can_connect_stream():
        return _json_error_out_of_session()
    symbols_raw = request.args.get("symbols") or ""
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        return _error("缺少 symbols 参数")
    symbols = list(dict.fromkeys(symbols))[:QUOTE_SSE_MAX_SYMBOLS]
    with_depth = request.args.get("depth") == "1"
    tail_period = request.args.get("tail")
    chart_precision = request.args.get("precision") == "chart"
    if tail_period not in (None, "1d", "1w", "1M"):
        return _error("不支持的指标周期", 400)
    tail_adjust = market.kline_source.normalize_adjust(request.args.get("adjust"))
    try:
        tail_count = max(1, min(int(request.args.get("count", "1006")), 1500))
    except ValueError:
        return _error("无效 count", 400)

    encoding = _sse_encoding(len(symbols))
    slot = _sse_slots
    if not slot.acquire(blocking=False):
        return _error("SSE 连接数已达上限, 回退轮询", 429)

    def release_slot():
        nonlocal slot
        if slot is not None:
            slot.release()
            slot = None

    def generate():
        token = object()
        worker = quote_snapshots.current()
        delivered = {}
        last_revision = -1
        last_keepalive = 0.0

        def frame(kind, payload):
            body = json.dumps(payload, ensure_ascii=False, cls=market.NumpyEncoder, separators=(",", ":"))
            event = f"event: {kind}\n" if kind != "quote" else ""
            data = f"{event}data: {body}\n\n"
            perf.bump(f"sse_{kind}_frames")
            perf.bump(f"sse_{kind}_bytes", len(data.encode("utf-8")))
            return data

        def quote_frame(quotes, trading_day):
            changed = {}
            for symbol, quote in quotes.items():
                previous = delivered.get(symbol)
                if previous == quote:
                    continue
                stamp = market._safe_epoch(quote.get("timestamp")) or 0
                old_stamp = market._safe_epoch((previous or {}).get("timestamp")) or 0
                if stamp >= old_stamp:
                    changed[symbol] = {**quote, "is_trading_day": trading_day}
                    delivered[symbol] = quote
            return frame("quote", changed) if changed else None

        try:
            market.register_quote_interest(token, symbols)
            # retry: 断线后浏览器 EventSource 重连间隔
            yield "retry: 5000\n\n"
            # 首帧即报相位: 客户端连上就知道当前状态, 省掉一次 /api/ping
            last_phase = market_hours.session_phase()
            yield _market_frame()
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
                # 相位一变就推: 每 TICK 只做一次 session_phase() 查表 (缓存日历),
                # 完整的 market_status() 只在切换那一刻算一次。
                phase = market_hours.session_phase()
                if phase != last_phase:
                    # 09:30 刚开盘: 当拍就推 tail, 不必再等满一个指标间隔才出今日 bar。
                    if phase == "trading" and last_phase != "trading":
                        last_tail = now - INDICATOR_SSE_INTERVAL
                    last_phase = phase
                    yield _market_frame()
                if worker:
                    revision = worker.revision
                    due = now - last_push >= QUOTE_SSE_INTERVAL
                    active = phase in market_hours.LIVE_PHASES
                    if (active or phase == "break") and (revision != last_revision or (active and due)):
                        last_revision = revision
                        try:
                            quotes, missing = market.display_quote_cache(symbols)
                            td = market_hours.is_trading_day()
                            data = quote_frame(quotes, td)
                            if data:
                                yield data
                            if active and due:
                                last_push = now
                                if missing:
                                    fresh = market.fetch_quotes(missing, only_requested=True)
                                    tagged = market.merge_display_quotes({s: quotes[s] for s in missing if s in quotes}, fresh)
                                    data = quote_frame(tagged, td)
                                    if data:
                                        yield data
                        except Exception:
                            pass
                    if now - last_keepalive >= 1:
                        last_keepalive = now
                        yield ": keepalive\n\n"
                elif now - last_push >= QUOTE_SSE_INTERVAL:
                    last_push = now
                    try:
                        # SSE 连接可跨时段保持，但盘外不请求上游；前端 active 门控会在
                        # 盘外主动关闭，服务端也做第二道保护避免误拉行情。
                        # 门控用 is_live(): 集合竞价期快照已有效, 而连续竞价之外
                        # (午休/盘后) 仍不取数。
                        if not market_hours.is_live():
                            yield ": out-of-session\n\n"
                            time.sleep(QUOTE_SSE_TICK)
                            continue
                        quotes = market.fetch_quotes(symbols)
                        # 每条快照附交易日标志: 前端据此拦截非交易日用残留快照补当日 bar
                        td = market_hours.is_trading_day()
                        yield frame("quote", {s: {**q, "is_trading_day": td} for s, q in quotes.items()})
                    except Exception:
                        # 单次快照失败: 发注释帧保活, 下轮重试
                        yield ": tick\n\n"
                else:
                    yield ": keepalive\n\n"
                if with_depth and market_hours.is_live() and now - last_depth >= DEPTH_SSE_INTERVAL:
                    last_depth = now
                    try:
                        depths = {s: d for s in symbols if (d := market.fetch_depth(s)) is not None}
                        if depths:
                            yield frame("depth", depths)
                    except Exception:
                        yield ": depth unavailable\n\n"
                if tail_period and market_hours.is_live() and now - last_tail >= INDICATOR_SSE_INTERVAL:
                    last_tail = now
                    try:
                        from api.kline import build_kline_tail, compact_chart_tail
                        bars = build_kline_tail(
                            symbols[0], tail_period, tail_count, tail_adjust,
                            quote_fresh=False)
                        if chart_precision:
                            bars = compact_chart_tail(bars)
                        yield frame("bars", {symbols[0]: bars})
                    except Exception:
                        yield ": indicators unavailable\n\n"
                if worker:
                    worker.wait(last_revision if active or phase == "break" else worker.revision, QUOTE_SSE_TICK)
                else:
                    time.sleep(QUOTE_SSE_TICK)
        finally:
            market.unregister_quote_interest(token)
            release_slot()

    frames = generate()
    resp = Response(_encode_sse(frames, encoding) if encoding else frames, mimetype="text/event-stream")
    resp.call_on_close(release_slot)
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.vary.add("Accept-Encoding")
    if len(symbols) >= SSE_ZSTD_MIN_SYMBOLS:
        resp.vary.add("User-Agent")
    if encoding:
        resp.headers["Content-Encoding"] = encoding
    return resp


def _json_error_out_of_session():
    """EventSource 无法读取错误 body，仍返回机器可识别的 HTTP 状态和重试时间。

    retry_after 由下一次可建流时刻推导 (钳在 [5, 1800]): 开盘那一刻浏览器自己
    重连回来, 不再靠前端定时轮询去发现"可以连了"。
    """
    retry = _retry_after_sec()
    resp = Response(json.dumps({
        "ok": False,
        "code": "SSE_OUT_OF_SESSION",
        "error": "非交易时段不提供实时 SSE",
        "in_session": False,
        "session_phase": market_hours.session_phase(),
        "next_stream_in_sec": retry,
        "retry_after": retry,
    }, ensure_ascii=False), mimetype="application/json")
    resp.status_code = 425
    resp.headers["Retry-After"] = str(retry)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@api_bp.route("/api/stream/status", methods=["GET"])
def stream_status():
    """前端建立 EventSource 前的轻量探测。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    st = market_hours.market_status()
    allowed = market_hours.can_connect_stream()
    return _json({
        "ok": True,
        "sse_allowed": allowed,
        "in_session": st["in_session"],
        "session_phase": st["session_phase"],
        "quote_live": st["quote_live"],
        "next_live_at": st["next_live_at"],
        "next_live_in_sec": st["next_live_in_sec"],
        "retry_after": _RETRY_MIN if allowed else _retry_after_sec(),
    })
