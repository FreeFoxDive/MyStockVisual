"""Flask 路由: 当前用户域 (/api/me/search-history、/api/monitor/status)。"""
from __future__ import annotations

from flask import request

import trades
from api import api_bp
from api.common import _error, _json, _monitor_error_label, _read_json_body, _require_user


@api_bp.route("/api/me/search-history", methods=["GET"])
def search_history_get():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    return _json({"history": trades.get_search_history(user["id"])})


@api_bp.route("/api/me/search-history", methods=["PUT"])
def search_history_put():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    history = trades.set_search_history(
        user["id"], body.get("history") or [], allow_clear=False,
    )
    return _json({"ok": True, "history": history})


@api_bp.route("/api/me/search-history", methods=["DELETE"])
def search_history_delete():
    """删除单条搜索历史 (?symbol=000001.SZ)。

    走独立接口而非 PUT 短列表: PUT 有意拒绝清空 (防本地缓存被清后误抹账号历史)，
    而用户显式删除最后一条时必须能落库，否则下次同步又合并回来。
    """
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return _error("缺少 symbol 参数", 400)
    return _json({"ok": True, "history": trades.delete_search_history(user["id"], symbol)})


@api_bp.route("/api/me/panel-config", methods=["GET"])
def panel_config_get():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    return _json({"config": trades.get_panel_config(user["id"])})


@api_bp.route("/api/me/panel-config", methods=["PUT"])
def panel_config_put():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    config = trades.set_panel_config(
        user["id"], body.get("config") or {}, allow_clear=False,
    )
    return _json({"ok": True, "config": config})


@api_bp.route("/api/monitor/status", methods=["GET"])
def monitor_status():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    try:
        import monitor as _mon
        st = _mon.get_status()
    except Exception:
        st = {"running": False, "backend": None, "last_poll": None, "n_symbols": 0}
    alerts = trades.list_monitor_alerts(user["id"], limit=20)
    st["alerts"] = alerts
    st["monitor_enabled"] = bool(user.get("is_admin") or user.get("monitor_enabled"))
    if st.get("last_error"):
        st["last_error"] = _monitor_error_label(st["last_error"])
    return _json(st)
