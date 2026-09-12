"""Flask 路由: 行情快照/搜索/盘口/配额 (/api/ping|search|quote|quotes|depth|pledge|quota)。"""
from __future__ import annotations

import logging

from flask import request

import market_hours
from api import api_bp
from api.common import _error, _json, _require_user
from logger import sanitize_error as _sanitize_error
from market import (
    _fetch_mairui_quota,
    _is_index_symbol,
    _load_pledge,
    _search_stocks,
    fetch_depth,
    fetch_quote,
    fetch_quotes,
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
        return _json(q)
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
        return _json(fetch_quotes(symbols, fresh=fresh))
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
