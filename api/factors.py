"""Flask 路由: 因子库状态 (/api/factors/status)。

页面用它显示「因子库: 2026-09-18 已就绪 (7221 只)」/「构建中 45%」/「异常: …」,
以及最近 5 个交易日的完成情况 (选股扫描读的就是这份快照, 版本即交易日)。
"""
from __future__ import annotations

import logging

from flask import request

import factors
from api import api_bp
from api.common import _error, _json, _require_admin, _require_user

log = logging.getLogger("api")


@api_bp.route("/api/factors/status", methods=["GET"])
def factors_status():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    try:
        data = factors.status()
    except Exception as e:
        log.warning("读取因子库状态失败: %s", e)
        return _json({"state": "unknown", "error": "状态读取失败", "last_days": [],
                      "snapshot_day": None})
    # 失败详情只给管理员: 上游异常原文可能含内部地址/响应体, 不对普通用户下发
    if not user.get("is_admin") and data.get("error"):
        data["error"] = "构建失败（详情见服务端日志）"
    return _json(data)


@api_bp.route("/api/factors/rebuild", methods=["POST"])
def factors_rebuild():
    """手动触发一次构建 (仅管理员): 因子库缺失/失败时不必等到次日 18:00。

    子进程执行 (不占 Web 进程内存), 立即返回; 进度看 /api/factors/status。开不了跑时如实回报原因
    (已在跑 / 当日已完成 / 重试次数用尽 / 内存压力), 不能让管理员以为已经触发。
    """
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    day = (body.get("day") or "").strip() or None
    reason = factors.build_blocked_reason(day=day, force=force)
    if reason:
        return _json({"ok": False, "started": False, "reason": reason, "force": force})
    try:
        import memguard
        if memguard.under_pressure():
            return _json({"ok": False, "started": False, "reason": "内存压力，已拒绝重建",
                          "force": force})
    except Exception:
        pass

    try:
        factors.spawn_manual(day=day, force=force)
    except factors.BuildAlreadyRunning as e:
        return _json({"ok": False, "started": False, "reason": str(e), "force": force})
    except Exception as e:
        log.warning("手动构建因子库拉起失败: %s", e)
        return _json({"ok": False, "started": False, "reason": "拉起构建失败", "force": force})
    return _json({"ok": True, "started": True, "force": force, "day": day})
