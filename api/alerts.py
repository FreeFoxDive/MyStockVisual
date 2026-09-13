"""Flask 路由: 任意条件预警 (/api/alerts CRUD)。"""
from __future__ import annotations

from flask import request

import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_user


@api_bp.route("/api/alerts", methods=["GET"])
def alerts_list():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    return _json({"alerts": trades.list_price_alerts(user["id"])})


@api_bp.route("/api/alerts", methods=["POST"])
def alerts_create():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return _error("缺少 symbol")
    if not body.get("rule"):
        return _error("缺少 rule")
    try:
        alert_id = trades.create_price_alert(
            user["id"], symbol, body.get("name"), body.get("rule"), body.get("note"))
    except ValueError as e:
        return _error(str(e), 400)
    return _json({"ok": True, "id": alert_id}, 201)


@api_bp.route("/api/alerts/<int:alert_id>", methods=["PUT"])
def alerts_update(alert_id):
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    ok = trades.update_price_alert(user["id"], alert_id, body)
    return _json({"ok": bool(ok)})


@api_bp.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def alerts_delete(alert_id):
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    ok = trades.delete_price_alert(user["id"], alert_id)
    return _json({"ok": bool(ok)})


@api_bp.route("/api/trendline-monitors", methods=["GET"])
def trendline_monitors_list():
    """当前用户的趋势线监控列表 (配置挂在画线上, 这里只读展示)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    return _json({"monitors": trades.list_trendline_monitors(user["id"])})


@api_bp.route("/api/trendline-monitors/<drawing_id>", methods=["PUT"])
def trendline_monitor_update(drawing_id):
    """启停某条画线的趋势线监控 (配置存 chart_drawings JSON, 只改 enabled 标志)。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    enabled = body.get("enabled")
    if enabled not in (True, False):
        return _error("enabled 必须为布尔值")
    ok, symbol, period = trades.set_trendline_monitor_enabled(
        user["id"], drawing_id, enabled)
    return _json({"ok": bool(ok), "symbol": symbol, "period": period})
