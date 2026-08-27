"""HTTP 安全: CSP、速率限制、登录爆破防护、会话 Cookie、CSRF 双提交、客户端 IP。"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time

import trades
from logger import sanitize_error as sanitize_error  # re-export

log = logging.getLogger(__name__)

SESSION_COOKIE = "session"
SESSION_MAX_AGE = trades.SESSION_TTL_DAYS * 24 * 3600
CSRF_COOKIE = "csrf_token"

PUBLIC_API_GET = {"/api/ping"}
PUBLIC_API_POST = {"/api/auth/login", "/api/auth/logout"}

# CSP: 'unsafe-inline' 必需 (页面内联 script/style)
CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self' https://static.cloudflareinsights.com https://cloudflareinsights.com"
)

RATE_LIMIT_PER_MIN = 120
_rate_limit_tokens = float(RATE_LIMIT_PER_MIN)
_rate_limit_lock = threading.Lock()
_rate_limit_last_refill = time.time()

LOGIN_FAIL_MAX = 5
LOGIN_LOCK_SECONDS = 15 * 60
LOGIN_PER_IP_LIMIT = 5
LOGIN_PER_IP_CAP = 1000
LOGIN_FAIL_CAP = 1000
LOGIN_STATE_TTL = 1800

_login_bf_lock = threading.Lock()
_login_failures = {}  # username -> [count, locked_until, seen]
_login_attempts = {}  # ip -> [tokens, last]


def check_rate_limit():
    """全局 API 令牌桶。True=允许。"""
    global _rate_limit_tokens, _rate_limit_last_refill
    with _rate_limit_lock:
        now = time.time()
        elapsed = now - _rate_limit_last_refill
        _rate_limit_tokens = min(
            RATE_LIMIT_PER_MIN,
            _rate_limit_tokens + elapsed * (RATE_LIMIT_PER_MIN / 60.0),
        )
        _rate_limit_last_refill = now
        if _rate_limit_tokens >= 1:
            _rate_limit_tokens -= 1
            return True
        return False


def check_login_ip(ip):
    now = time.time()
    with _login_bf_lock:
        tokens, last = _login_attempts.get(ip, (LOGIN_PER_IP_LIMIT, now))
        tokens = min(LOGIN_PER_IP_LIMIT, tokens + (now - last) * (LOGIN_PER_IP_LIMIT / 60.0))
        if tokens < 1:
            _login_attempts[ip] = (tokens, now)
            return False
        _login_attempts[ip] = (tokens - 1, now)
        return True


def login_lock_remaining(username):
    now = time.time()
    with _login_bf_lock:
        entry = _login_failures.get(username)
        if not entry:
            return 0
        _, locked_until, _ = entry
        if locked_until and locked_until > now:
            return int(locked_until - now)
        return 0


def record_login_failure(username):
    now = time.time()
    with _login_bf_lock:
        count, _, _ = _login_failures.get(username, (0, 0, now))
        count += 1
        locked_until = now + LOGIN_LOCK_SECONDS if count >= LOGIN_FAIL_MAX else 0
        _login_failures[username] = (count, locked_until, now)
        _prune_login_state_locked(now)


def clear_login_failure(username):
    with _login_bf_lock:
        _login_failures.pop(username, None)


def _prune_login_state_locked(now):
    stale_users = [
        u for u, (_, lt, seen) in _login_failures.items()
        if (lt and lt <= now) or (now - seen > LOGIN_STATE_TTL)
    ]
    for u in stale_users:
        _login_failures.pop(u, None)
    if len(_login_failures) > LOGIN_FAIL_CAP:
        ordered = sorted(_login_failures.items(), key=lambda kv: kv[1][2])
        for u, _ in ordered[: len(ordered) - LOGIN_FAIL_CAP]:
            _login_failures.pop(u, None)

    stale_ips = [ip for ip, (_, last) in _login_attempts.items() if now - last > LOGIN_STATE_TTL]
    for ip in stale_ips:
        _login_attempts.pop(ip, None)
    if len(_login_attempts) > LOGIN_PER_IP_CAP:
        ordered = sorted(_login_attempts.items(), key=lambda kv: kv[1][1])
        for ip, _ in ordered[: len(ordered) - LOGIN_PER_IP_CAP]:
            _login_attempts.pop(ip, None)


def client_ip(environ_or_headers, remote_addr):
    """environ dict (WSGI) 或 header-get callable。只信任 CF-Connecting-IP。"""
    if callable(environ_or_headers):
        cf = environ_or_headers("CF-Connecting-IP")
    elif hasattr(environ_or_headers, "get"):
        cf = environ_or_headers.get("CF-Connecting-IP") or environ_or_headers.get("HTTP_CF_CONNECTING_IP")
    else:
        cf = None
    if cf:
        return str(cf).strip().split(",")[0].strip()
    return remote_addr or "0.0.0.0"


def request_is_https(headers):
    xfp = (headers.get("X-Forwarded-Proto") or headers.get("HTTP_X_FORWARDED_PROTO") or "").strip().lower()
    if xfp == "https":
        return True
    cf = headers.get("CF-Visitor") or headers.get("HTTP_CF_VISITOR") or ""
    return "https" in str(cf).lower()


def apply_session_cookie(resp, token, *, clear=False, https=False):
    """Flask 标准 Response.set_cookie 写会话 Cookie。"""
    if clear:
        resp.set_cookie(
            SESSION_COOKIE,
            "",
            max_age=0,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=bool(https),
        )
    else:
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_MAX_AGE,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=bool(https),
        )
    return resp


def apply_csrf_cookie(resp, token, *, https=False):
    """可读 CSRF Cookie（双提交）；不用 HttpOnly。"""
    resp.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        path="/",
        httponly=False,
        samesite="Lax",
        secure=bool(https),
    )
    return resp


def current_user_from_request(req=None):
    """从 Flask request.cookies 解析会话用户。"""
    from flask import request as flask_request
    req = req or flask_request
    token = req.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return trades.get_session(token)


def new_csrf_token():
    return secrets.token_hex(16)


def validate_csrf(cookie_token, header_token):
    """双提交: csrf cookie 值与 X-CSRF-Token 一致。"""
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(str(cookie_token), str(header_token))
