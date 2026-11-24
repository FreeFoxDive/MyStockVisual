"""Flask 路由: 账户量化分析 (/api/analysis/quant)。"""
from __future__ import annotations

import logging

from flask import current_app, request

import quant_analysis
import trades
from api import api_bp
from api.common import _error, _json, _require_user
from logger import sanitize_error as _sanitize_error
from market import TTLCache

log = logging.getLogger("api")


@api_bp.record_once
def _init_position_stats_cache(state):
    state.app.extensions["model_position_stats_cache"] = TTLCache(ttl_seconds=15, max_entries=256)


def _flag(raw, default):
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes")


@api_bp.route("/api/analysis/quant", methods=["GET"])
def analysis_quant():
    """当前账户的模型净值与绩效。只读本人交易, 不拉别人的记录。"""
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    try:
        payload = quant_analysis.compute_quant_analysis(
            user["id"],
            model_id=request.args.get("model_id"),
            start=request.args.get("from"),
            end=request.args.get("to"),
            capital=request.args.get("capital") or 100_000,
            mode=request.args.get("mode"),
            deduct_fees=_flag(request.args.get("deduct_fees"), True),
            compare=_flag(request.args.get("compare"), False),
            fee_config=trades.get_user_fees(user["id"]),
        )
    except quant_analysis.AnalysisError as exc:
        return _error(exc.message, 400)
    except Exception as exc:
        log.warning("量化分析失败 uid=%s: %s", user["id"], _sanitize_error(exc))
        return _error("分析失败", 500)
    return _json(payload)


@api_bp.route("/api/analysis/model-position-stats", methods=["GET"])
def model_position_stats():
    user = _require_user()
    if not user:
        return _error("未登录", 401)
    try:
        summary = quant_analysis.compute_model_position_stats(
            user["id"], request.args.get("model_id"),
            cache=current_app.extensions["model_position_stats_cache"],
        )
    except quant_analysis.AnalysisError as exc:
        return _error(exc.message, 400)
    except Exception as exc:
        log.warning("仓位模型统计失败 uid=%s: %s", user["id"], _sanitize_error(exc))
        return _error("模型统计失败", 500)
    return _json({"summary": summary})
