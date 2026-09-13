"""api 包共用助手: JSON 响应与登录态取值 (无路由)。"""
from __future__ import annotations

import json
import logging

from flask import Response, g, request

from market import NumpyEncoder

log = logging.getLogger("api")


def _json(data, code=200):
    body = json.dumps(data, ensure_ascii=False, cls=NumpyEncoder)
    resp = Response(body, status=code, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _error(msg, code=400):
    return _json({"error": msg}, code)


def _monitor_error_label(raw):
    """监控 last_error 的对外文案: 只返回固定分类, 不回传异常原文 (原文仅进日志)。"""
    low = (raw or "").lower()
    if any(k in low for k in ("429", "rate", "limit", "too many", "throttle")):
        return "请求过于频繁"
    if "timeout" in low or "timed out" in low:
        return "上游超时"
    if any(k in low for k in ("connect", "disconnect", "reset", "refused", "unreachable")):
        return "上游连接失败"
    return "监控运行异常"


def _require_user():
    user = getattr(g, "user", None)
    if not user:
        return None
    return user


def _require_admin():
    user = _require_user()
    if not user:
        return None
    if not user.get("is_admin"):
        return False  # authenticated but not admin
    return user


def _read_json_body():
    """Match server.py: None = invalid JSON; {} = empty/oversized; dict otherwise."""
    raw = request.get_data(cache=True, as_text=False) or b""
    if len(raw) > 1_000_000:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
