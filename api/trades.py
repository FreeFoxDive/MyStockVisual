"""Flask 路由: 交易记录/费率/交易工具 (/api/trades*|fees|trade-reasons|repo-maturity)。"""
from __future__ import annotations

import logging

from flask import request

import trades
from api import api_bp
from api.common import _error, _json, _read_json_body, _require_user
from logger import sanitize_error as _sanitize_error

log = logging.getLogger("api")


@api_bp.route("/api/trade-reasons", methods=["GET"])
def trade_reasons():
    return _json({"entry": trades.ENTRY_REASONS, "exit": trades.EXIT_REASONS})


@api_bp.route("/api/repo-maturity", methods=["GET"])
def repo_maturity():
    """逆回购到期日预览。

    与 create_trade 入库口径完全一致 (自然日 + XSHG 交易日历顺延, 含节假日),
    前端不再自行用"只跳周末"的近似逻辑。
    """
    if not _require_user():
        return _error("未登录", 401)
    entry_date = (request.args.get("entry_date") or "").strip()
    try:
        tenor = int(request.args.get("tenor"))
    except (TypeError, ValueError):
        return _error("tenor 无效")
    return _json({"maturity": trades._repo_maturity(entry_date, tenor)})


@api_bp.route("/api/trades", methods=["GET"])
def trades_list():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    filters = {
        "status": request.args.get("status"),
        "symbol": request.args.get("symbol"),
        "q": request.args.get("q"),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
        "model_id": request.args.get("model_id"),
        "limit": request.args.get("limit"),
        "offset": request.args.get("offset"),
    }
    deduct = (request.args.get("deduct_fees") or "").lower() in ("1", "true")
    fee_config = trades.get_user_fees(user["id"]) if deduct else None
    records, total = trades.list_trades(user["id"], filters, fee_config=fee_config)
    return _json({"trades": records, "total": total})


@api_bp.route("/api/trades", methods=["POST"])
def trades_create():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    # 先取 _clean 的固定校验文案 (非异常路径), 避免异常原文回传响应;
    # create_trade 仍会自行校验, 这里的 except 只兜底非校验类异常。
    err = trades.validate_trade(body)
    if err:
        return _error(err, 400)
    try:
        trade = trades.create_trade(user["id"], body)
    except ValueError as e:
        log.warning("创建交易失败: %s", _sanitize_error(e))
        return _error("交易数据无效", 400)
    return _json({"trade": trade}, 201)


@api_bp.route("/api/trades/<int:tid>", methods=["PUT"])
def trades_update(tid):
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体无效 JSON", 400)
    existing = trades.get_trade(user["id"], tid)
    if not existing:
        return _error("记录不存在", 404)
    err = trades.validate_trade(body, existing)
    if err:
        return _error(err, 400)
    try:
        trade = trades.update_trade(user["id"], tid, body)
    except ValueError as e:
        log.warning("更新交易失败 id=%s: %s", tid, _sanitize_error(e))
        return _error("交易数据无效", 400)
    if trade is None:
        return _error("记录不存在", 404)
    return _json({"trade": trade})


@api_bp.route("/api/trades/<int:tid>", methods=["DELETE"])
def trades_delete(tid):
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    if not trades.delete_trade(user["id"], tid):
        return _error("记录不存在", 404)
    return _json({"ok": True})


@api_bp.route("/api/trades/stats", methods=["GET"])
def trades_stats():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    start = request.args.get("from")
    end = request.args.get("to")
    deduct = (request.args.get("deduct_fees") or "").lower() in ("1", "true")
    fee_config = trades.get_user_fees(user["id"]) if deduct else None
    return _json(trades.compute_stats(
        user["id"], start, end, deduct_fees=deduct, fee_config=fee_config
    ))


@api_bp.route("/api/fees", methods=["GET"])
def fees_get():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    return _json({"fees": trades.get_user_fees(user["id"])})


@api_bp.route("/api/fees", methods=["PUT"])
def fees_put():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    body = _read_json_body()
    if body is None:
        return _error("请求体必须是 JSON 对象", 400)
    try:
        fees = trades.update_user_fees(user["id"], body)
    except ValueError as e:
        log.warning("费率配置无效 uid=%s: %s", user["id"], _sanitize_error(e))
        return _error("费率配置无效", 400)
    return _json({"fees": fees})
