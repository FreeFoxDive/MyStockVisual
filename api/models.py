"""Flask 路由: 交易模型 (/api/models CRUD+restore)。"""
from __future__ import annotations

import logging

import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_admin, _require_user
from logger import sanitize_error as _sanitize_error

log = logging.getLogger("api")


@api_bp.route("/api/models", methods=["GET"])
def models_list():
    if not _require_user():
        return _error("未登录", 401)
    return _json(trades.list_models(active_only=False))


@api_bp.route("/api/models", methods=["POST"])
def models_create():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    try:
        mid = trades.create_model(
            body.get("name"), body.get("description", ""),
            body.get("hold_days"),
        )
    except ValueError as e:
        log.warning("创建模型失败: %s", _sanitize_error(e))
        return _error("模型名称不符合要求或已存在", 409)
    return _json({"ok": True, "id": mid})


@api_bp.route("/api/models/<int:mid>", methods=["PUT"])
def models_update(mid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    try:
        hold_days = body["hold_days"] if "hold_days" in body else trades._UNSET
        updated = trades.update_model(
            mid, body.get("name"), body.get("description", ""), hold_days,
        )
    except ValueError as e:
        log.warning("更新模型失败 id=%s: %s", mid, _sanitize_error(e))
        return _error("模型名称不符合要求或已存在", 409)
    if not updated:
        return _error("模型不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/models/<int:mid>", methods=["DELETE"])
def models_delete(mid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    if not trades.delete_model(mid):
        return _error("模型不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/models/<int:mid>/restore", methods=["POST"])
def models_restore(mid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    try:
        restored = trades.restore_model(mid)
    except ValueError as e:
        log.warning("恢复模型失败 id=%s: %s", mid, _sanitize_error(e))
        return _error("模型名称不符合要求或已存在", 409)
    if not restored:
        return _error("模型不存在", 404)
    return _json({"ok": True})
