"""Flask 路由: 信号忽视记录 (/api/signal-ignores CRUD) 与预设剔除理由。

量化管理页 (/quant.html) 仅管理员可见, 所以这里全部接口都要求管理员
(普通用户即使拿到路由也用不上)。
"""
from __future__ import annotations

import logging

from flask import request

import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_admin
from logger import sanitize_error as _sanitize_error

log = logging.getLogger("api")


@api_bp.route("/api/ignore-reasons", methods=["GET"])
def ignore_reasons():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    return _json({"reasons": trades.IGNORE_REASONS})


@api_bp.route("/api/signal-ignores", methods=["GET"])
def signal_ignores_list():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    filters = {
        "q": request.args.get("q"),
        "model_id": request.args.get("model_id"),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
        "limit": request.args.get("limit"),
        "offset": request.args.get("offset"),
    }
    records, total = trades.list_signal_ignores(filters)
    return _json({"ignores": records, "total": total})


@api_bp.route("/api/signal-ignores", methods=["POST"])
def signal_ignores_create():
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    # 先取 _clean 的固定校验文案 (非异常路径), 避免异常原文回传响应
    err = trades.validate_signal_ignore(body)
    if err:
        return _error(err, 400)
    try:
        record = trades.create_signal_ignore(admin["id"], body)
    except ValueError as e:
        log.warning("创建信号忽视记录失败: %s", _sanitize_error(e))
        return _error("记录数据无效", 400)
    return _json({"ignore": record}, 201)


@api_bp.route("/api/signal-ignores/<int:iid>", methods=["PUT"])
def signal_ignores_update(iid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    existing = trades.get_signal_ignore(iid)
    if not existing:
        return _error("记录不存在", 404)
    err = trades.validate_signal_ignore(body, existing)
    if err:
        return _error(err, 400)
    try:
        record = trades.update_signal_ignore(iid, body)
    except ValueError as e:
        log.warning("更新信号忽视记录失败 id=%s: %s", iid, _sanitize_error(e))
        return _error("记录数据无效", 400)
    if record is None:
        return _error("记录不存在", 404)
    return _json({"ignore": record})


@api_bp.route("/api/signal-ignores/<int:iid>", methods=["DELETE"])
def signal_ignores_delete(iid):
    admin = _require_admin()
    if admin is None:
        return _error("未登录", 401)
    if admin is False:
        return _error("无权限", 403)
    if not trades.delete_signal_ignore(iid):
        return _error("记录不存在", 404)
    return _json({"ok": True})
