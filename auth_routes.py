"""Flask Blueprint: /api/auth/login|logout|me"""
from __future__ import annotations

import json
import time

from flask import Blueprint, g, make_response, request

import trades
from security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    apply_csrf_cookie,
    apply_session_cookie,
    check_login_ip,
    clear_login_failure,
    client_ip,
    login_lock_remaining,
    new_csrf_token,
    record_login_failure,
    request_is_https,
)

auth_bp = Blueprint("auth", __name__)


def _json(data, code=200):
    body = json.dumps(data, ensure_ascii=False)
    resp = make_response(body, code)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _error(msg, code=400):
    return _json({"error": msg}, code)


@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    ip = client_ip(request.headers, request.remote_addr)
    if not check_login_ip(ip):
        return _error("登录尝试过于频繁，请稍后重试", 429)

    body = request.get_json(silent=True)
    if body is None:
        return _error("请求体无效 JSON", 400)
    if not isinstance(body, dict):
        return _error("请求体无效 JSON", 400)

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return _error("用户名和密码不能为空")

    remaining = login_lock_remaining(username)
    if remaining > 0:
        return _error(f"账号已锁定，请 {remaining // 60 + 1} 分钟后重试", 429)

    result = trades.login(username, password)
    if not result:
        record_login_failure(username)
        time.sleep(0.5)
        return _error("用户名或密码错误", 401)

    clear_login_failure(username)
    token, expires = result
    https = request_is_https(request.headers)
    resp = _json({"ok": True, "username": username, "expires_at": expires})
    apply_session_cookie(resp, token, https=https)
    apply_csrf_cookie(resp, new_csrf_token(), https=https)
    return resp


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        trades.delete_session(token)
    https = request_is_https(request.headers)
    resp = _json({"ok": True})
    apply_session_cookie(resp, None, clear=True, https=https)
    return resp


@auth_bp.route("/api/auth/me", methods=["GET"])
def auth_me():
    user = getattr(g, "user", None)
    if not user:
        return _error("未登录", 401)

    resp = _json({
        "username": user["username"],
        "is_admin": user["is_admin"],
        "monitor_enabled": bool(user.get("is_admin") or user.get("monitor_enabled")),
    })
    if not request.cookies.get(CSRF_COOKIE):
        apply_csrf_cookie(resp, new_csrf_token(), https=request_is_https(request.headers))
    return resp
