"""Flask 路由: 图表画线同步 (/api/drawings, 按 用户+代码+周期 整体存取)。"""
from __future__ import annotations

import logging

from flask import request

import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_user
from logger import sanitize_error as _sanitize_error

log = logging.getLogger("api")


@api_bp.route("/api/drawings", methods=["GET"])
def drawings_get():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip()
    period = (request.args.get("period") or "").strip()
    if not symbol or not period:
        return _error("缺少 symbol/period 参数")
    drawings = trades.get_chart_drawings(user["id"], symbol, period)
    return _json({"symbol": symbol, "period": period, "drawings": drawings})


@api_bp.route("/api/drawings", methods=["PUT"])
def drawings_put():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    symbol = (body.get("symbol") or "").strip()
    period = (body.get("period") or "").strip()
    if not symbol or not period:
        return _error("缺少 symbol/period 参数")
    try:
        n = trades.save_chart_drawings(user["id"], symbol, period, body.get("drawings"))
    except ValueError as e:
        log.warning("保存画线失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("画线数据无效", 400)
    return _json({"ok": True, "count": n})


@api_bp.route("/api/drawings", methods=["DELETE"])
def drawings_delete():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip()
    period = (request.args.get("period") or "").strip()
    if not symbol or not period:
        return _error("缺少 symbol/period 参数")
    trades.delete_chart_drawings(user["id"], symbol, period)
    return _json({"ok": True})
