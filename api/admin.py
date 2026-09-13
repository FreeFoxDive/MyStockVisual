"""Flask 路由: 管理员用户管理 (/api/admin/users*)。"""
from __future__ import annotations

import logging

import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_admin
from logger import sanitize_error as _sanitize_error

log = logging.getLogger("api")


@api_bp.route("/api/admin/users", methods=["GET"])
def admin_users_list():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    return _json(trades.list_users())


@api_bp.route("/api/admin/users", methods=["POST"])
def admin_users_create():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return _error("用户名和密码不能为空")
    try:
        user_id = trades.create_user(username, password, is_admin=False)
    except ValueError as e:
        log.warning("创建用户失败 %s: %s", username, _sanitize_error(e))
        return _error("用户名已存在或不符合要求", 409)
    return _json({"ok": True, "id": user_id, "username": username})


@api_bp.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
def admin_users_delete(user_id):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    try:
        deleted = trades.delete_user(user_id)
    except ValueError as e:
        log.warning("删除用户失败 id=%s: %s", user_id, _sanitize_error(e))
        return _error("该用户不可删除", 400)
    if not deleted:
        return _error("用户不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/admin/users/<int:user_id>/reset-password", methods=["POST"])
def admin_users_reset(user_id):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    password = body.get("password") or ""
    if not password:
        return _error("密码不能为空")
    try:
        updated = trades.reset_password(user_id, password)
    except ValueError as e:
        log.warning("重置密码失败 id=%s: %s", user_id, _sanitize_error(e))
        return _error("密码不符合要求（至少 6 位）", 400)
    if not updated:
        return _error("用户不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/admin/users/<int:user_id>/monitor", methods=["POST"])
def admin_users_monitor(user_id):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    enabled = body.get("enabled")
    if enabled not in (True, False, 0, 1, "0", "1", "true", "false"):
        return _error("enabled 必须为布尔值")
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("1", "true")
    else:
        enabled = bool(enabled)
    if not trades.set_user_monitor(user_id, enabled):
        return _error("用户不存在", 404)
    return _json({"ok": True, "id": user_id, "monitor_enabled": enabled})
