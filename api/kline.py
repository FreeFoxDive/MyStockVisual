"""Flask 路由: K线/筹码/分时/末根增量 (/api/kline|kline/tail|chips|intraday)。"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from datetime import date, datetime, time as clock_time, timedelta
from functools import lru_cache
import hashlib
import json
import logging
import os
import threading
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from flask import request

import kline_source
import market_hours
import perf
import premium
import quote_snapshots
from api import api_bp
from api.common import _error, _json
from chips import get_chips
from indicators import compute_all_indicators, intraday_avg_price
from logger import sanitize_error as _sanitize_error
from market import (
    MINUTE_COUNTS,
    MINUTE_PERIODS,
    TTLCache,
    _fetch_instrument_meta,  # 测试仍 patch 此名; 热路径改用 peek_instrument_meta
    _intraday_is_index,
    _is_etf,
    _is_index_symbol,
    _safe_float,
    _safe_int,
    _symbol_market,
    fetch_af_history,
    fetch_kline_ex,
    fetch_quote,
    kline_cache,
    kline_cache_long,
    kline_cache_minute,
    normalize_symbol,
    peek_instrument_meta,
    _quote_cache_lookup,
    display_quote_cache,
)

log = logging.getLogger("api")

# 日K: 3年可见 (3×252) + RSI 收敛 warmup 250
DAILY_COUNT = 3 * 252 + 250  # 1006
HISTORY_PERIODS = ("1d", "1w", "1M")
_history_cache = TTLCache(ttl_seconds=900, max_entries=128)
_history_pending = {}
_history_lock = threading.Lock()

# 图表当日 bar 的快照新鲜度: 默认复用 quote_cache (TTL 1.25s), 不再每次强制一次
# 实时行情往返 —— 实测该往返在 0~700ms 抖动, 是磁盘 TTL 抬高后热路径上仅剩的
# 耗时来源。当日 bar 仍**完全由后端快照产出** (契约不变: 前端不派生 OHLCV),
# 只是允许最多旧 1.25s; 缓存未命中时图表路径短等下一枚令牌 (不放大 48/min)。
# 前端另有 SSE 报价流及 /api/kline/tail (SSE 默认 5s)
# 持续纠正末根 bar。
# 成交校验 (market.get_daily_bar) 不经过这里, 始终强制新鲜快照。
# KLINE_TODAY_BAR_FRESH=1 → legacy 模式下每次强制拉新快照。
TODAY_BAR_QUOTE_FRESH = os.environ.get("KLINE_TODAY_BAR_FRESH", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# 末根增量接口的短 TTL: 多客户端共用一次全量指标计算。
# 默认 2s, 下限 1s。必须短于 SSE 的 INDICATOR_SSE_INTERVAL (默认 5s): 否则 SSE
# 连续两帧命中同一份缓存, revision 不变, 前端会把活着的流判成指标停滞。
# 单次约 30ms (1006 根, 磁盘命中); 5s 推送先试跑, 环境变量可调回 15s 观察 CPU。
KLINE_TAIL_TTL = max(1.0, float(os.environ.get("KLINE_TAIL_TTL", "2")))
_tail_cache = TTLCache(KLINE_TAIL_TTL, max_entries=30)
_tail_version_cache = TTLCache(15, max_entries=40)
_tail_locks = [threading.Lock() for _ in range(16)]
_TAIL_CACHE_BARS = 10
_intraday_versions = OrderedDict()
_intraday_lock = threading.Lock()


def _today_quote_fields(df):
    """拼当日 bar 所用快照的交易所时间与服务端 revision; 没有则都是 None。"""
    raw = getattr(df, "attrs", {}).get("today_quote_meta") if df is not None else None
    if not isinstance(raw, dict):
        return {"today_quote_timestamp": None, "today_quote_revision": None}
    ts, rev = raw.get("timestamp"), raw.get("revision")
    try:
        ts = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts = None
    try:
        rev = int(rev) if rev is not None else None
    except (TypeError, ValueError):
        rev = None
    return {"today_quote_timestamp": ts, "today_quote_revision": rev}


def _tail_omits_today_bar(df, period, adjust):
    """盘中日K (非后复权) 没拼出今日 bar 时, 不要把这份残缺结果冻进 tail 缓存。"""
    if period != "1d" or adjust == kline_source.ADJUST_HFQ or df is None:
        return False
    return bool(getattr(df, "attrs", {}).get("today_bar_missing"))


def _attach_quote(resp, symbol):
    """给 kline 响应挂实时快照; 失败静默, quote 缺席即可。

    盘中走 fetch_quote (缓存优先)。盘后只读本地快照 (含长 TTL 收盘快照), 不发网络:
    本地日K已命中时再等一次 AlphaFeed 会让切股一直「加载中」。
    """
    try:
        sym = normalize_symbol(symbol)
        if quote_snapshots.current():
            quotes, _ = display_quote_cache([sym])
            q = quotes.get(sym) or _quote_cache_lookup(sym, not market_hours.is_live())
        elif market_hours.is_live():
            q = fetch_quote(sym)
        else:
            q = _quote_cache_lookup(sym, True)
        if q:
            now = market_hours.now()
            quote = dict(q)
            quote["is_trading_day"] = market_hours.is_trading_day(now)
            resp["quote"] = quote
    except Exception:
        pass


# 单根 bar 的字段与转换器 (顺序即 JSON 字段顺序)。逐行与向量化序列化共用同一张
# 表, 避免两处字段清单漂移 (漏字段会静默让前端某条指标曲线消失)。
_BAR_FIELDS = (
    ("open", _safe_float), ("high", _safe_float), ("low", _safe_float),
    ("close", _safe_float), ("volume", _safe_int), ("amount", _safe_float),
    ("ma5", _safe_float), ("ma10", _safe_float), ("ma20", _safe_float),
    ("macd_dif", _safe_float), ("macd_dea", _safe_float), ("macd_hist", _safe_float),
    ("rsi6", _safe_float), ("rsi12", _safe_float), ("rsi24", _safe_float),
    ("kdj_k", _safe_float), ("kdj_d", _safe_float), ("kdj_j", _safe_float),
    ("atr14", _safe_float), ("atr20", _safe_float), ("ema13", _safe_float), ("impulse", _safe_int),
    ("obv", _safe_float), ("maobv", _safe_float),
    ("vol_ma5", _safe_float), ("vol_ma10", _safe_float), ("vol_ma20", _safe_float),
    ("boll_mid", _safe_float), ("boll_up", _safe_float), ("boll_low", _safe_float),
    ("wr14", _safe_float), ("cci14", _safe_float),
    ("bias6", _safe_float), ("bias12", _safe_float), ("bias24", _safe_float),
    ("dmi_pdi", _safe_float), ("dmi_mdi", _safe_float), ("dmi_adx", _safe_float),
)
_HISTORY_INPUT_FIELDS = ("open", "high", "low", "close", "volume")
_HISTORY_INDICATORS = tuple((name, convert) for name, convert in _BAR_FIELDS
                            if name not in (*_HISTORY_INPUT_FIELDS, "amount"))
_CANDLE_PRICES = frozenset(("open", "high", "low", "close"))
_CHART_INDICATORS = frozenset(
    name for name, convert in _BAR_FIELDS
    if convert is _safe_float and name not in _CANDLE_PRICES and name != "amount"
) | {"avg_price"}
_CROSS_PAIRS = (("ma5", "ma20"), ("macd_dif", "macd_dea"))
_FLOAT32_MAX = float(np.finfo(np.float32).max)


@lru_cache(maxsize=8192)
def _short_chart_price(value):
    if value is None or abs(value) > _FLOAT32_MAX:
        return value
    stored = np.float32(value)
    return float(str(stored)) if float(stored) == value else value


def _compact_chart_rows(fields, rows):
    positions = {field: index for index, field in enumerate(fields)}
    prices = [positions[f] for f in fields if f in _CANDLE_PRICES]
    indicators = [positions[f] for f in fields if f in _CHART_INDICATORS]
    crosses = [(positions[a], positions[b]) for a, b in _CROSS_PAIRS if a in positions and b in positions]
    for row in rows:
        originals = [(a, b, row[a], row[b]) for a, b in crosses]
        for i in prices:
            row[i] = _short_chart_price(row[i])
        for i in indicators:
            value = row[i]
            if value is not None:
                # Preserve nonzero values that would become zero, e.g. a negative MACD bar.
                row[i] = round(value, 6) or value
        for a, b, left, right in originals:
            # Rounding is monotonic. Keeping pairs that become equal preserves crossover signs.
            if row[a] == row[b] and left != right:
                row[a], row[b] = left, right


def _bar_date(idx, period):
    """单根 bar 的日期文本 (分钟周期精确到分钟, 其余到日)。"""
    if hasattr(idx, "strftime"):
        return idx.strftime(
            "%Y-%m-%d %H:%M" if period in MINUTE_PERIODS else "%Y-%m-%d"
        )
    return str(idx)


def _serialize_bar(idx, row, period):
    """单根 bar → JSON entry (含全部指标)。保留供逐行/单根调用方与测试使用。"""
    out = {"date": _bar_date(idx, period)}
    for name, conv in _BAR_FIELDS:
        out[name] = conv(row.get(name))
    return out


def serialize_bars(df, period, *, packed=False):
    """整段 K 线 → JSON entries (含全部指标)。

    替代 `[_serialize_bar(i, r, period) for i, r in df.iterrows()]`: iterrows
    每行构造一个 Series, 实测 1006 根约 86ms, 是指标计算 (约 29ms) 的 3 倍,
    也是 /api/kline 最大的单项 CPU。这里按列 tolist() 一次成型 (numpy 标量 →
    Python 原生类型, 与逐行取值口径一致), 再按行拼 dict。
    """
    n = len(df)
    if n == 0:
        return []
    idx = df.index
    if hasattr(idx, "strftime"):
        dates = idx.strftime(
            "%Y-%m-%d %H:%M" if period in MINUTE_PERIODS else "%Y-%m-%d"
        )
    else:
        dates = [str(i) for i in idx]
    cols = {name: (df[name].tolist() if name in df.columns else None)
            for name, _ in _BAR_FIELDS}
    fields = [(name, conv, cols[name]) for name, conv in _BAR_FIELDS]
    out = []
    for i in range(n):
        if packed:
            out.append([dates[i], *(conv(vals[i]) if vals is not None else None
                                    for _, conv, vals in fields)])
            continue
        bar = {"date": dates[i]}
        for name, conv, vals in fields:
            bar[name] = conv(vals[i]) if vals is not None else None
        out.append(bar)
    if packed:
        _compact_chart_rows(["date", *(name for name, _ in _BAR_FIELDS)], out)
    return out


def _intraday_response(symbol, period, count, day, bars, *, is_index):
    fields = list(bars[0])
    rows = [list(bar.values()) for bar in bars]
    _compact_chart_rows(fields, rows)
    encoded = json.dumps(rows, separators=(",", ":")).encode()
    version = hashlib.blake2b(f"{day}:{is_index}:".encode() + encoded, digest_size=12).hexdigest()
    key = (symbol, period, count, version)
    previous = request.args.get("since")
    with _intraday_lock:
        old = _intraday_versions.get((symbol, period, count, previous))
        _intraday_versions[key] = (day, is_index, rows)
        _intraday_versions.move_to_end(key)
        if len(_intraday_versions) > 32:
            _intraday_versions.popitem(last=False)
    current = {row[0]: row for row in rows}
    delta = bool(old and old[:2] == (day, is_index) and all(row[0] in current for row in old[2]))
    if delta:
        previous_rows = {row[0]: row for row in old[2]}
        rows = [row for row in rows if previous_rows.get(row[0]) != row]
    return _json({"symbol": symbol, "period": period, "date": day, "is_index": is_index, "version": version,
                  "delta": delta, "fields": fields, "rows": rows}, compact=True)


def _session_meta(now=None):
    """接口 meta 的统一时段字段 (唯一口径来自 market_hours)。

    与 /api/ping、/api/stream/status 同源 (market_status), 前端据 quote_live /
    next_live_in_sec 判断要不要刷新、何时唤醒 —— 集合竞价期 quote_live 为真,
    in_session 仍为假, 两者不能互相替代。
    """
    st = market_hours.market_status(now)
    return {
        "server_time": st["time"],
        "is_trading_day": st["is_trading_day"],
        "session_phase": st["session_phase"],
        "is_auction": st["is_auction"],
        "quote_live": st["quote_live"],
        "next_live_at": st["next_live_at"],
        "next_live_in_sec": st["next_live_in_sec"],
    }


@api_bp.route("/api/kline", methods=["GET"])
def kline():
    """K线 + 全部指标 (前端主图数据源)。

    观测: KLINE_PERF_LOG=1 时打一条 kline_perf 日志, 分解磁盘命中 / 实际数据源 /
    强制行情往返 / 指标计算 / 序列化耗时, 用于定位切换标的忽快忽慢的瓶颈。
    响应头 Server-Timing 的 app 是路由内部总耗时，不含服务器排队及网络传输。
    """
    timing = {}
    t0 = time.perf_counter()
    response = None
    try:
        response = _kline_body(timing)
        return response
    finally:
        perf.add_ms(timing, "total_ms", (time.perf_counter() - t0) * 1000.0)
        if response is not None:
            response.headers["Server-Timing"] = ", ".join(
                f"{'app' if key == 'total_ms' else key[:-3]};dur={value}"
                for key, value in timing.items() if key.endswith("_ms")
            )
        if perf.ENABLED:
            hot = {k: v for k, v in perf.counters().items() if v}
            log.info("kline_perf %s counters=%s", perf.fmt(
                timing,
                symbol=request.args.get("symbol"),
                period=request.args.get("period") or "1d",
            ), hot or "-")


def _listing_day(symbol):
    value = str((peek_instrument_meta(symbol) or {}).get("listing_date") or "")
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _history_close_matches(close, anchor):
    return close is not None and anchor is not None and np.isclose(close, anchor, rtol=1e-6, atol=0.0005)


def _history_warmup(bars, limit, warmup):
    seed = bars.iloc[:-limit].tail(warmup)
    fields = list(_HISTORY_INPUT_FIELDS)
    return [[_safe_float(value) for value in row] for row in seed[fields].itertuples(index=False, name=None)]


@api_bp.route("/api/kline", methods=["POST"])
def kline_history_indicators():
    """用已拼接的 OHLCV 重算整段指标，不取行情或写入数据。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("period") not in HISTORY_PERIODS:
        return _error("无效的历史周期")
    rows, warmup = data.get("rows"), data.get("warmup")
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 20_000
            or type(warmup) is not int or not 0 <= warmup <= min(249, len(rows) - 1)
            or any(not isinstance(row, list) or len(row) != len(_HISTORY_INPUT_FIELDS) for row in rows)):
        return _error("无效的历史行情")
    try:
        values = np.asarray(rows, dtype=float)
    except (TypeError, ValueError, OverflowError):
        return _error("无效的历史行情数值")
    if values.ndim != 2 or np.isinf(values).any():
        return _error("无效的历史行情数值")
    frame = pd.DataFrame(values, columns=list(_HISTORY_INPUT_FIELDS))
    computed, _ = compute_all_indicators(frame, data["period"], serialize=False)
    fields = [name for name, _ in _HISTORY_INDICATORS]
    columns = [computed[name].iloc[warmup:].tolist() for name in fields]
    result = [[convert(value) for (_, convert), value in zip(_HISTORY_INDICATORS, row)]
              for row in zip(*columns)]
    _compact_chart_rows(fields, result)
    return _json({"fields": fields, "rows": result}, compact=True)


def _fetch_history_page(symbol, period, adjust, cursor, anchor_close, limit, warmup):
    # 请求包含游标这根，以确认复权口径，也能补齐日 K 仓边界上的残缺周/月柱。
    zone = {"us": "America/New_York", "hk": "Asia/Hong_Kong"}.get(_symbol_market(symbol), "Asia/Shanghai")
    end = datetime.combine(cursor + timedelta(days=1), clock_time.min, ZoneInfo(zone))
    frame = fetch_af_history(symbol, period, limit + max(249, warmup) + 2,
                             adjust, int(end.timestamp() * 1000) - 1)
    if frame is None:
        raise ValueError("历史行情缺少标的")
    payload = {"symbol": symbol, "period": period, "meta": {"adjust": adjust, "source": "alphafeed"},
               "history_cursor": True, "history_remote": True, "history_extended": True,
               "fields": ["date", *(name for name, _ in _BAR_FIELDS)], "rows": [],
               "warmup_closes": [], "has_more": False}
    if frame.empty:
        return {**payload, "history_end": cursor.isoformat()}

    days = frame.index.date
    if days[-1] != cursor:
        raise ValueError("历史游标与当前图表无法对齐")
    close = _safe_float(frame["close"].iloc[-1])
    if not _history_close_matches(close, anchor_close):
        raise ValueError("历史行情复权口径与当前图表不一致")

    frame, _ = compute_all_indicators(frame, period, serialize=False)
    bars = frame.iloc[:-1]
    payload["rows"] = serialize_bars(bars.tail(limit), period, packed=True)
    if payload["rows"] or period != "1d":
        payload["history_warmup"] = _history_warmup(bars, limit, warmup)
    payload["warmup_closes"] = [_short_chart_price(_safe_float(close))
                                for close in bars["close"].iloc[:-limit].tail(warmup)]
    if period != "1d":
        payload["anchor_row"] = serialize_bars(frame.tail(1), period, packed=True)[0]
    # 短页可能是上游返回上限。用最早一根再探一次，直到上游也只返回游标本身。
    first = date.fromisoformat(payload["rows"][0][0]) if payload["rows"] else cursor
    listing = _listing_day(symbol) if period == "1d" else None
    payload["has_more"] = bool(payload["rows"]) and first != listing
    if not payload["has_more"]:
        payload["history_end"] = first.isoformat()
    return payload


def _history_response(symbol, period, adjust, cursor, anchor_close, limit, warmup):
    key = (symbol, period, adjust, cursor, anchor_close, limit, warmup)
    with _history_lock:
        cached = _history_cache.get(key)
        if cached is not None:
            return _json(cached, compact=True)
        pending = _history_pending.get(key)
        leader = pending is None
        if leader:
            pending = _history_pending[key] = Future()

    if leader:
        try:
            payload = _fetch_history_page(*key)
            _history_cache.set(key, payload)
            pending.set_result(payload)
        except Exception as exc:
            pending.set_exception(exc)
            log.warning("历史K线加载失败 %s %s %s: %s", symbol, period, cursor, _sanitize_error(exc))
        finally:
            with _history_lock:
                del _history_pending[key]
    try:
        return _json(pending.result(timeout=10), compact=True)
    except Exception:
        return _error("历史行情暂时无法加载，请重试", 503)


def _kline_body(timing):
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")

    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    # 复权: forward(前,默认) / hfq(后) / none(不复权); 分钟周期固定前复权 (源口径)
    adjust = kline_source.normalize_adjust(request.args.get("adjust"))
    if period in MINUTE_PERIODS:
        adjust = kline_source.ADJUST_FORWARD
    # 日K: 3年可见 + RSI 收敛 warmup (DAILY_COUNT 模块常量, tail 接口须同 count)
    if period == "1d":
        default_count = DAILY_COUNT
    else:
        default_count = MINUTE_COUNTS.get(period, 200)
    try:
        count = min(int(request.args.get("count") or str(default_count)), 1500)
    except ValueError:
        count = default_count

    packed = request.args.get("format") == "rows"
    before = request.args.get("before") if packed else None
    history = request.args.get("history") if packed and period in HISTORY_PERIODS else None
    history_cursor = history in ("1", "local", "remote")
    cursor = None
    anchor_close = None
    if history_cursor and before:
        try:
            cursor = date.fromisoformat(before)
            before = cursor.isoformat()
        except ValueError:
            return _error("无效的历史日期")
        raw_anchor = request.args.get("anchor_close")
        anchor_close = _safe_float(raw_anchor)
        if raw_anchor is not None and (anchor_close is None or not np.isfinite(anchor_close)):
            return _error("无效的历史接缝价格")
    with_gap_seed = (packed and not before and period in ("1d", "1w", "1M", "60m")
                     and request.args.get("gaps") == "1")
    try:
        limit = max(1, min(1500, int(request.args.get("limit", "1500")))) if packed else None
        if history in ("1", "remote") and before:
            limit = min(300, limit)
        warmup = max(0, min(249, int(request.args.get("warmup", "0")))) if packed else 0
    except ValueError:
        return _error("无效 limit 或 warmup")
    if history == "remote" and cursor:
        return _history_response(symbol, period, adjust, cursor, anchor_close, limit, warmup)
    revision = time.time_ns() // 1000
    cache_key = f"{symbol}:{period}:{count}:{kline_source.adjust_tag(adjust)}"
    if packed:
        cache_key += f":rows:{limit}:{before or ''}:{int(with_gap_seed)}:{warmup}"
        if history_cursor:
            cache_key += f":history:{history}:{anchor_close}"
    skip_1d_cache = period == "1d" and market_hours.is_trading_day(
        market_hours.now().date().isoformat()
    )
    if period in MINUTE_PERIODS:
        cache = kline_cache_minute
    elif period in ("1w", "1M"):
        cache = kline_cache_long
    else:
        cache = kline_cache
    cached = None if skip_1d_cache else cache.get(cache_key)
    if cached:
        if timing is not None:
            timing["disk"] = "mem"
        resp = cached.copy()
        resp["meta"] = dict(resp.get("meta") or {})
        resp["meta"].update(cached=True, **_session_meta())
        with perf.Span(timing, "attach_quote_ms"):
            _attach_quote(resp, symbol)
        with perf.Span(timing, "json_ms"):
            return _json(resp, compact=packed)

    try:
        df, name, source = fetch_kline_ex(symbol, period, count, adjust=adjust,
                                         timing=timing, quote_fresh=TODAY_BAR_QUOTE_FRESH,
                                         quote_wait_budget=not TODAY_BAR_QUOTE_FRESH,
                                         **({"append_today": False} if cursor else {}))
    except Exception as e:
        log.warning("获取K线失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("获取K线失败，请稍后重试", 500)

    # 请求的复权无数据 (如 AF 不支持后复权) → 回退前复权重取一次, 保住图表
    adjust_fallback = False
    if df is None and not cursor and adjust != kline_source.ADJUST_FORWARD and period not in MINUTE_PERIODS:
        log.warning("%s %s 无 %s 复权数据, 回退前复权", symbol, period, adjust)
        adjust = kline_source.ADJUST_FORWARD
        adjust_fallback = True
        try:
            df, name, source = fetch_kline_ex(symbol, period, count, adjust=adjust,
                                             timing=timing,
                                             quote_fresh=TODAY_BAR_QUOTE_FRESH,
                                             quote_wait_budget=not TODAY_BAR_QUOTE_FRESH)
        except Exception as e:
            log.warning("复权回退获取失败 %s: %s", symbol, _sanitize_error(e))
        if df is None:
            return _error(f"无法获取 {symbol} 的K线数据", 404)

    if cursor:
        anchors = df.loc[df.index.date == cursor, "close"] if df is not None else ()
        local_close = _safe_float(anchors.iloc[-1]) if len(anchors) else None
        if history == "1" and (not len(anchors) or cursor <= df.index[0].date()):
            return _history_response(symbol, period, adjust, cursor,
                                     anchor_close if anchor_close is not None else local_close, limit, warmup)
        if anchor_close is not None and not _history_close_matches(local_close, anchor_close):
            return _error("历史行情与当前图表无法对齐，请刷新后重试", 503)
    if df is None:
        return _error(f"无法获取 {symbol} 的K线数据", 404)

    quote_fields = _today_quote_fields(df)
    try:
        with perf.Span(timing, "indicators_ms"):
            df, indicators = compute_all_indicators(df, period, serialize=not packed)
    except Exception as e:
        log.warning("指标计算失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("指标计算失败", 500)

    # 主图已是前复权, impulse 直接用 compute_all_indicators 结果 (不再另拉 qfq)

    is_etf = _is_etf(symbol)
    deferred = []
    # 溢价依赖 akshare 净值 (无缓存无超时), 原内联在此处会拖慢每次 ETF 切换。
    # 改为只预热后台计算, 立即返回; 客户端用 /api/kline/deferred 取回 (见 premium.py)。
    if is_etf and period not in MINUTE_PERIODS:
        premium.request(symbol, period, count, df=df)
        deferred.append("premium")

    with perf.Span(timing, "serialize_ms"):
        last_date = _bar_date(df.index[-1], period) if len(df) else None
        window = df
        if before:
            window = df[[_bar_date(idx, period) < before for idx in df.index]]
        has_more = packed and len(window) > limit
        warmup_closes = window["close"].iloc[:-limit].tail(warmup) if warmup else ()
        if packed:
            window = window.tail(limit)
        klines = serialize_bars(window, period, packed=packed)

    # 股本不挡主图: 只读内存缓存。缺的时候由同时发出的 /api/stock-info 补全并带回 float_shares。
    inst_meta = peek_instrument_meta(symbol) or {}
    now = market_hours.now()
    resp = {
        "symbol": symbol,
        "name": name,
        "period": period,
        "count": len(klines),
        "_revision": revision,
        "is_etf": is_etf,
        "is_index": _is_index_symbol(symbol),
        "float_shares": inst_meta.get("float_shares"),
        "total_shares": inst_meta.get("total_shares"),
        "instrument_type": inst_meta.get("type"),
        "obv_params": indicators.get("obv", {}).get("params"),
        "macd_params": indicators.get("macd", {}).get("params"),
        "boll_params": indicators.get("boll", {}).get("params"),
        "meta": {
            "cached": False,
            **_session_meta(now),
            "last_trade_date": last_date,
            "source": source,
            "adjust": adjust,
            "adjust_fallback": adjust_fallback,
            **quote_fields,
            # 慢派生字段清单: 客户端据此拉 /api/kline/deferred 补齐 (现仅 ETF 溢价)
            "deferred": deferred,
        },
    }
    if packed:
        from indicators import get_macd_params
        resp.update(fields=["date", *(name for name, _ in _BAR_FIELDS)], rows=klines,
                    has_more=has_more, macd_params=get_macd_params(period),
                    obv_params={"ma_period": 30}, boll_params={"n": 20, "k": 2})
        if history_cursor:
            resp["history_cursor"] = True
            first = date.fromisoformat(klines[0][0]) if klines else cursor
            resp["history_remote"] = bool(first and first <= df.index[0].date())
            listing = _listing_day(symbol) if period == "1d" else None
            resp["has_more"] = not (listing and first == listing)
            if not resp["has_more"]:
                resp["history_end"] = first.isoformat()
            if cursor and klines:
                resp["history_warmup"] = _history_warmup(df.loc[df.index.date < cursor], limit, warmup)
        if warmup:
            resp["warmup_closes"] = [_short_chart_price(_safe_float(value)) for value in warmup_closes]
        if with_gap_seed and has_more:
            # The browser runs the same gap scanner over earlier high/low data.
            # Include only the fields it consumes, without indicators or OHLCV duplicates.
            older = df.iloc[:-limit]
            resp["gap_seed"] = [
                [_bar_date(idx, period), _short_chart_price(_safe_float(high)),
                 _short_chart_price(_safe_float(low)), (_safe_int(volume) or 0) > 0]
                for idx, high, low, volume in older[["high", "low", "volume"]].itertuples(name=None)
            ]
    else:
        resp["klines"] = klines

    # 缓存里不存 quote: TTLCache 存引用, 事后挂 quote 会污染缓存条目,
    # 让后续命中拿到最长 TTL 前的旧快照。改为存副本 (不含 quote), 每次返回前现挂。
    with perf.Span(timing, "attach_quote_ms"):
        _attach_quote(resp, symbol)
    if not skip_1d_cache and not adjust_fallback:
        cache.set(cache_key, {k: v for k, v in resp.items() if k != "quote"})

    with perf.Span(timing, "json_ms"):
        return _json(resp, compact=packed)


@api_bp.route("/api/kline/tail", methods=["GET"])
def kline_tail():
    """日/周/月K 末 N 根 (含全部指标), 供图表增量刷新。

    与 /api/kline 同口径 (同一 fetch_kline_ex + compute_all_indicators), count
    必须与图表一致 —— 否则 OBV 等全序列指标会漂移。非交易日/盘前时服务端本就不
    拼当日 bar, 故返回的末根仍是最近交易日, 前端按 date 合并不会凭空多一根。
    """
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    if period in MINUTE_PERIODS or period not in ("1d", "1w", "1M"):
        return _error("tail 仅支持 1d/1w/1M", 400)
    adjust = kline_source.normalize_adjust(request.args.get("adjust"))
    default_count = DAILY_COUNT if period == "1d" else MINUTE_COUNTS.get(period, 300)
    try:
        count = min(int(request.args.get("count") or str(default_count)), 1500)
    except ValueError:
        count = default_count
    try:
        n = max(1, min(int(request.args.get("n") or "1"), 10))
    except ValueError:
        n = 1

    try:
        payload = build_kline_tail(symbol, period, count, adjust, n)
        if request.args.get("precision") == "chart":
            return _json(compact_chart_tail(payload), compact=True)
        return _json(payload)
    except LookupError:
        # LookupError 的文本可能包含上游响应或内部路径，不应直接返回给客户端。
        return _error("无法获取K线数据", 404)
    except Exception:
        return _error("指标更新失败", 500)


def build_kline_tail(symbol, period, count, adjust, n=2, quote_fresh=True):
    # n 不进 key: 缓存末 _TAIL_CACHE_BARS 根, 不同 n / 多客户端共用一次计算。
    # legacy 模式的 HTTP tail 可强刷，SSE 继续复用短缓存。全池按快照版本共享指标。
    cache_key = f"{symbol}:{period}:{count}:{kline_source.adjust_tag(adjust)}"
    if quote_snapshots.current() and period == "1d" and adjust != kline_source.ADJUST_HFQ:
        quotes, missing = display_quote_cache([symbol], live_info=False)
        if not missing and symbol in quotes:
            quote = quotes[symbol]
            cache_key = (cache_key, quote["_revision"], market_hours.session_phase())
            with _tail_locks[hash(cache_key) % len(_tail_locks)]:
                return _build_kline_tail(symbol, period, count, adjust, n, False, cache_key, _tail_version_cache)
    return _build_kline_tail(symbol, period, count, adjust, n, quote_fresh, cache_key, _tail_cache)


def compact_chart_tail(payload):
    bars = payload["bars"]
    if not bars:
        return payload
    fields = list(bars[0])
    rows = [[bar.get(field) for field in fields] for bar in bars]
    _compact_chart_rows(fields, rows)
    return {**payload, "bars": [dict(zip(fields, row)) for row in rows]}


def _build_kline_tail(symbol, period, count, adjust, n, quote_fresh, cache_key, cache):
    cached = cache.get(cache_key)
    if cached is not None:
        bars = list(cached["bars"][-n:])
        resp = dict(cached)
        resp["bars"] = bars
        resp["count"] = len(bars)
        return resp

    revision = time.time_ns() // 1000
    try:
        df, name, source = fetch_kline_ex(symbol, period, count, adjust=adjust,
                                           quote_fresh=quote_fresh)
    except Exception as e:
        log.warning("获取K线失败(tail) %s %s: %s", symbol, period, _sanitize_error(e))
        raise RuntimeError("获取K线失败") from e
    if df is None:
        raise LookupError(f"无法获取 {symbol} 的K线数据")
    quote_fields = _today_quote_fields(df)
    omit_cache = _tail_omits_today_bar(df, period, adjust)
    try:
        df, _ind = compute_all_indicators(df, period, serialize=False)
    except Exception as e:
        log.warning("指标计算失败(tail) %s %s: %s", symbol, period, _sanitize_error(e))
        raise RuntimeError("指标计算失败") from e

    bars = serialize_bars(df.tail(_TAIL_CACHE_BARS), period)
    now = market_hours.now()
    stored = {
        "symbol": symbol,
        "_revision": revision,
        "name": name or symbol,
        "period": period,
        "count": len(bars),
        "bars": bars,
        "meta": {
            **_session_meta(now),
            "last_trade_date": bars[-1]["date"] if bars else None,
            "source": source,
            "adjust": adjust,
            **quote_fields,
        },
    }
    if not omit_cache:
        if not isinstance(cache_key, tuple) or quote_fields["today_quote_revision"] == cache_key[1]:
            cache.set(cache_key, stored)
    out_bars = list(bars[-n:])
    resp = dict(stored)
    resp["bars"] = out_bars
    resp["count"] = len(out_bars)
    return resp


@api_bp.route("/api/kline/deferred", methods=["GET"])
def kline_deferred():
    """慢派生字段的补齐接口 (现仅 ETF 溢价 premium)。

    溢价走 akshare 净值 (无缓存无超时), 若内联在 /api/kline 会拖慢每次 ETF 切换,
    故 /api/kline 只预热并在 meta.deferred 里声明; 客户端随后拉这里补齐。

    未就绪返回 200 + ready=false (不是错误): 前端退避重试, 不弹错误提示。
    """
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    if period in MINUTE_PERIODS or period not in ("1d", "1w", "1M"):
        return _error("deferred 仅支持 1d/1w/1M", 400)
    fields = [f.strip() for f in (request.args.get("fields") or "premium").split(",")]
    if "premium" not in fields:
        return _error("不支持的 fields", 400)
    try:
        count = max(1, min(int(request.args.get("count") or str(DAILY_COUNT)), 1500))
    except ValueError:
        count = DAILY_COUNT
    result = premium.get(symbol, period, count)
    start = request.args.get("from")
    if start and result and result.get("ready") and "dates" in result:
        result = {**result}
        if request.args.get("version") == str(result.get("generated_at")):
            result.pop("dates")
            result.pop("values")
        else:
            pairs = [(date, value) for date, value in zip(result["dates"], result["values"]) if date >= start]
            result["dates"] = [date for date, _ in pairs]
            result["values"] = [value for _, value in pairs]
    return _json({"symbol": symbol, "period": period, "premium": result}, compact=bool(start))


@api_bp.route("/api/chips", methods=["GET"])
def chips():
    """筹码分布 (股票/ETF); 指数或无数据返回 chips=null。

    period 1d/1w/1M 只决定日线回看窗口 (210/600/600 根), 算法与粒度不变。
    指数无份额与换手率, 筹码无意义, 故仍拦截。
    """
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    if period not in ("1d", "1w", "1M"):
        period = "1d"
    if _is_index_symbol(symbol):
        return _json({"symbol": symbol, "chips": None})
    try:
        data = get_chips(symbol, period)
    except Exception as e:
        log.warning("获取筹码分布异常 %s: %s", symbol, _sanitize_error(e))
        return _error("获取筹码分布失败", 500)
    return _json({"symbol": symbol, "chips": data})


@api_bp.route("/api/intraday", methods=["GET"])
def intraday():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "5m"
    try:
        count = max(1, min(int(request.args.get("count") or "120"), 250))
    except ValueError:
        count = 120
    try:
        # 分时走 "intraday" 类别: 优先 AlphaFeed 日内走势接口 (只回当日, 权限不可用
        # 当日退回分钟K批量), 失败继续沿链回退; df 已标准化前复权。
        # 注意不能挂到 "minute" 类别 —— 那是跨天分钟K历史视图 (1m 要 1200 根),
        # 换成只回当日的接口会把 1m/5m/15m/30m/60m 视图打坏。
        market = _symbol_market(symbol)
        session_minutes = {"cn": 240, "hk": 330, "us": 390}[market]
        fetch_count = count
        if period in MINUTE_PERIODS:
            minutes = int(period[:-1])
            # 标准化至少需要五根；60m 的 A 股取数可能需要补上一交易日。
            fetch_count = max(count, 5, (session_minutes + minutes - 1) // minutes)
        df, _src = kline_source.fetch_kline_df(
            "intraday", symbol, period, fetch_count, adjust="forward"
        )
        if df is None or len(df) == 0:
            return _error(f"无法获取 {symbol} 的分钟线", 404)
        last_day = df.index.normalize().max()
        mask = df.index.normalize() == last_day
        df = df[mask]
        if len(df) == 0:
            return _error("无分时数据", 404)
        df, indicators = compute_all_indicators(df, period="1d", with_atr_val=True)
        # 指数的量额汇总自成分股，两者相除得不到指数点位。
        is_index = _intraday_is_index(symbol, market)
        if is_index:
            points = df["close"]
            avgs = points.where(np.isfinite(points) & (points > 0)).expanding().mean().tolist()
        elif is_index is False:
            avgs = intraday_avg_price(df["close"], df["volume"], df.get("amount"))
        else:
            avgs = [None] * len(df)
        bars = []
        start = max(0, len(df) - count)
        for i, (idx, row) in enumerate(df.iloc[start:].iterrows(), start=start):
            ts = idx.strftime("%H:%M") if hasattr(idx, "strftime") else str(idx)[-8:-3]
            bars.append({
                "time": ts,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "volume": _safe_int(row.get("volume")),
                "amount": _safe_float(row.get("amount")),
                "avg_price": _safe_float(avgs[i]),
                "macd_dif": indicators["macd"]["dif"][i],
                "macd_dea": indicators["macd"]["dea"][i],
                "macd_hist": indicators["macd"]["hist"][i],
                "kdj_k": indicators["kdj"]["k"][i] if "kdj" in indicators else None,
                "kdj_d": indicators["kdj"]["d"][i] if "kdj" in indicators else None,
                "kdj_j": indicators["kdj"]["j"][i] if "kdj" in indicators else None,
                "rsi6": indicators["rsi"]["rsi6"][i] if "rsi" in indicators else None,
                "rsi12": indicators["rsi"]["rsi12"][i] if "rsi" in indicators else None,
                "rsi24": indicators["rsi"]["rsi24"][i] if "rsi" in indicators else None,
                "atr14": indicators["atr"]["values"][i] if "atr" in indicators else None,
                # 成交量面板的提示框与日K 同口径 (成交量 + VOL MA + 成交额), 指标已在
                # compute_all_indicators 里算好, 这里只是补进 bar。
                "vol_ma5": _safe_float(row.get("vol_ma5")),
                "vol_ma10": _safe_float(row.get("vol_ma10")),
                "vol_ma20": _safe_float(row.get("vol_ma20")),
            })
        if request.args.get("format") == "rows":
            return _intraday_response(symbol, period, count, last_day.strftime("%Y-%m-%d"), bars,
                                      is_index=is_index is True)
        return _json({
            "symbol": symbol,
            "is_index": is_index is True,
            "period": period,
            # 当日日期: bar 只有 HH:MM, 而图表左上角标题要「分时 · 2026-09-18」这种口径;
            # 不能让客户端用本地时钟推"今天" —— 周末/节假日打开时会说错日子。
            "date": last_day.strftime("%Y-%m-%d"),
            "bars": bars,
        })
    except Exception as e:
        log.warning("获取分钟线失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("获取分钟线失败，请稍后重试", 500)
