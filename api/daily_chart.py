"""图表日K仓状态与手动全量（/api/daily-chart/status|rebuild）。"""
from __future__ import annotations

import logging

from flask import request

import build_daily_chart_cache as chart_build
import daily_chart_store as chart_store
from api import api_bp
from api.common import _error, _json, _require_admin, _require_user

log = logging.getLogger("api")


@api_bp.route("/api/daily-chart/status", methods=["GET"])
def daily_chart_status():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    try:
        data = chart_store.status()
    except Exception as exc:
        log.warning("读取图表日K仓状态失败: %s", exc)
        return _json({"as_of": None, "building": False, "error": "状态读取失败"})
    if not user.get("is_admin"):
        if data.get("error"):
            data["error"] = "构建失败（详情见服务端日志）"
        if data.get("index_error"):
            data["index_error"] = "指数拉取失败（详情见服务端日志）"
    return _json(data)


@api_bp.route("/api/daily-chart/rebuild", methods=["POST"])
def daily_chart_rebuild():
    """管理员手动全量。force 可破盘中/未到点/已是最新（仅测试）；每日成功仍最多 2 次。"""
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    reason = chart_build.build_blocked_reason(force=force)
    if reason:
        return _json({"ok": False, "started": False, "reason": reason, "force": force})
    try:
        chart_build.spawn_build(force=force)
    except RuntimeError:
        return _json({"ok": False, "started": False, "reason": "已有全量构建在进行", "force": force})
    except Exception as exc:
        log.warning("图表日K全量拉起失败: %s", exc)
        return _json({"ok": False, "started": False, "reason": "拉起构建失败", "force": force})
    return _json({"ok": True, "started": True, "force": force})
