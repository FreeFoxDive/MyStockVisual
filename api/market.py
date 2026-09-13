"""Flask 路由: 行情快照/搜索/盘口/配额 (/api/ping|search|quote|quotes|depth|pledge|quota)。"""
from __future__ import annotations

import logging
import time

from flask import request

import market_hours
from api import api_bp
from api.common import _error, _json, _monitor_error_label, _require_user
from logger import sanitize_error as _sanitize_error
from market import (
    _fetch_mairui_quota,
    _is_index_symbol,
    _load_pledge,
    _search_stocks,
    fetch_depth,
    fetch_quote,
    fetch_quotes,
    fetch_stock_info,
    normalize_symbol,
)

log = logging.getLogger("api")


@api_bp.route("/api/ping", methods=["GET"])
def ping():
    now = market_hours.now()
    return _json({
        "ok": True,
        "time": str(now),
        "in_session": market_hours.in_session(now),
        "is_trading_day": market_hours.is_trading_day(now),
    })


# ── 深度健康检查 (/api/health) ──
_HEALTH_DB_TTL = 10.0
_health_db_cache = {"ts": 0.0, "ok": True}
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _is_loopback_request():
    addr = (request.remote_addr or "").strip().lower()
    return addr in _LOOPBACK


def _db_ok_cached():
    """轻量 DB 探活 + 短缓存(豁免限流后避免被未授权请求放大成 DB 压力)。"""
    now = time.time()
    if now - _health_db_cache["ts"] < _HEALTH_DB_TTL:
        return _health_db_cache["ok"]
    ok = True
    try:
        import trades
        trades.count_admins()
    except Exception:
        ok = False
    _health_db_cache.update(ts=now, ok=ok)
    return ok


def _monitor_health():
    import monitor
    st = monitor.get_status() or {}
    age = None
    if st.get("last_poll_ts"):
        age = round(time.time() - float(st["last_poll_ts"]), 1)
    return {
        "running": bool(st.get("running")),
        "backend": st.get("backend"),
        "last_poll_age_sec": age,
        "in_backoff": bool(st.get("in_backoff")),
        "last_error": _monitor_error_label(st["last_error"]) if st.get("last_error") else None,
    }


def _monitor_stalled(mon, now):
    if not market_hours.in_session(now):
        return False
    if mon.get("in_backoff"):
        return False
    try:
        import watchdog
        stall_sec = watchdog.STALL_SEC
    except Exception:
        stall_sec = 600.0
    age = mon.get("last_poll_age_sec")
    return age is not None and age > stall_sec


@api_bp.route("/api/health", methods=["GET"])
def health():
    """容器 HEALTHCHECK / 运维探针: 正常 200, 异常 503。

    仅 loopback 返回明细; 其余来源只给 {ok}(避免暴露内部状态给未认证用户)。
    """
    now = market_hours.now()
    db_ok = _db_ok_cached()
    mon = _monitor_health()
    ok = bool(db_ok and mon.get("running") and not _monitor_stalled(mon, now))
    payload = {"ok": ok, "time": str(now)}
    if _is_loopback_request():
        import watchdog
        wd = watchdog.get_state()
        payload["checks"] = {
            "monitor": mon,
            "watchdog": {
                "last_check": wd.get("last_check"),
                "breaker": bool(wd.get("breaker")),
                "running": watchdog.is_running(),
            },
            "db": {"ok": db_ok},
        }
    return _json(payload, 200 if ok else 503)


@api_bp.route("/api/search", methods=["GET"])
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return _json({"results": []})
    return _json({"results": _search_stocks(q)})


@api_bp.route("/api/quote", methods=["GET"])
def quote():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    try:
        q = fetch_quote(symbol)
        if q is None:
            return _error(f"无法获取 {symbol} 的快照", 404)
        # fetch_quote 返回 quote_cache 共享条目, 复制后再挂标志避免污染缓存;
        # 前端据此拦截非交易日用残留快照补当日 bar。
        now = market_hours.now()
        resp = dict(q)
        resp["is_trading_day"] = market_hours.is_trading_day(now)
        return _json(resp)
    except Exception as e:
        log.warning("获取快照失败 %s: %s", symbol, _sanitize_error(e))
        return _error("获取快照失败，请稍后重试", 500)


@api_bp.route("/api/quotes", methods=["GET"])
def quotes():
    raw = request.args.get("symbols") or ""
    symbols = [s.strip() for s in raw.split(",") if s.strip()]
    if not symbols:
        return _error("缺少 symbols 参数")
    fresh = (request.args.get("fresh") or "0").lower() in ("1", "true", "yes")
    try:
        quotes = fetch_quotes(symbols, fresh=fresh)
        # 与 /api/quote、SSE 口径一致: 每条快照带交易日标志 (不改写共享缓存条目)
        td = market_hours.is_trading_day(market_hours.now())
        return _json({s: {**q, "is_trading_day": td} for s, q in quotes.items()})
    except Exception as e:
        log.warning("批量快照失败: %s", _sanitize_error(e))
        return _error("获取快照失败，请稍后重试", 500)


@api_bp.route("/api/depth", methods=["GET"])
def depth():
    """五档盘口 (分时用); 失败/限流返回 depth=null。"""
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    try:
        d = fetch_depth(symbol)
    except Exception as e:
        log.warning("获取五档异常 %s: %s", symbol, _sanitize_error(e))
        return _error("获取五档失败", 500)
    return _json({"symbol": symbol, "depth": d})


@api_bp.route("/api/stock-info", methods=["GET"])
def stock_info():
    """侧栏基本信息 (行业/总手/成交额/换手/量比/涨跌停/N日涨幅/PE/PB/交易状态)。"""
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    try:
        return _json(fetch_stock_info(symbol))
    except Exception as e:
        log.warning("获取基本信息失败 %s: %s", symbol, _sanitize_error(e))
        return _error("获取基本信息失败，请稍后重试", 500)


@api_bp.route("/api/pledge", methods=["GET"])
def pledge():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    if _is_index_symbol(symbol):
        return _json({"symbol": symbol, "pledge": None})
    code = symbol.split(".")[0]
    data = _load_pledge().get(code)
    return _json({"symbol": symbol, "pledge": data if data else None})


@api_bp.route("/api/quota", methods=["GET"])
def quota():
    if not _require_user():
        return _error("未登录", 401)
    return _json(_fetch_mairui_quota())
