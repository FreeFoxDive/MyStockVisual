"""Flask 路由: 当前用户域 (/api/me/search-history、/api/monitor/status)。"""
from __future__ import annotations

import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_user


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
        from logger import redact_message
        st["last_error"] = redact_message(st["last_error"])
    return _json(st)
