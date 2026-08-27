"""Flask application factory for Visual K-line web app."""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from urllib.parse import quote

# ── 路径 + .env (须在 import market / routes 之前) ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load_dotenv():
    for env_dir in (SCRIPT_DIR, PROJECT_DIR):
        env_file = env_dir / ".env"
        if not env_file.exists():
            continue
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k not in os.environ:
                        os.environ[k] = v


_load_dotenv()

from flask import Flask, g, make_response, redirect, request, send_from_directory  # noqa: E402

from security import (  # noqa: E402
    CSP_HEADER,
    CSRF_COOKIE,
    PUBLIC_API_GET,
    PUBLIC_API_POST,
    apply_csrf_cookie,
    check_rate_limit,
    current_user_from_request,
    new_csrf_token,
    request_is_https,
    validate_csrf,
)

log = logging.getLogger("app")
STATIC_DIR = SCRIPT_DIR / "static"


def _api_json_error(msg, code):
    import json
    resp = make_response(json.dumps({"error": msg}, ensure_ascii=False), code)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def create_app():
    from logger import configure as _log_configure
    _log_configure()

    app = Flask(
        __name__,
        static_folder=None,  # 自管静态路径 (URL 无 /static 前缀)
    )
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000
    # 与自定义 session cookie 对齐的 Flask 配置 (文档/一致性; 鉴权仍走 SQLite token)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_PATH"] = "/"

    from auth_routes import auth_bp
    from api_routes import api_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)

    @app.before_request
    def _before():
        g.user = current_user_from_request()
        g.https = request_is_https(request.headers)

        if not request.path.startswith("/api/"):
            return None

        if not check_rate_limit():
            return _api_json_error("请求过于频繁，请稍后重试", 429)

        method = request.method.upper()
        if method == "GET":
            is_public = request.path in PUBLIC_API_GET
        elif method == "POST":
            is_public = request.path in PUBLIC_API_POST
        else:
            is_public = False

        if not is_public and not g.user:
            return _api_json_error("未登录", 401)

        # CSRF: 变更方法需 cookie + header 双提交
        if method in ("POST", "PUT", "DELETE"):
            header_tok = (
                request.headers.get("X-CSRF-Token")
                or request.headers.get("X-CSRFToken")
            )
            csrf_cookie = request.cookies.get(CSRF_COOKIE)

            if request.path in PUBLIC_API_POST:
                # login/logout: 无 csrf cookie 时放行; 有则校验
                if csrf_cookie and not validate_csrf(csrf_cookie, header_tok):
                    return _api_json_error("CSRF 校验失败", 403)
            else:
                if not validate_csrf(csrf_cookie, header_tok):
                    resp = _api_json_error("CSRF 校验失败", 403)
                    if not csrf_cookie:
                        apply_csrf_cookie(
                            resp, new_csrf_token(),
                            https=getattr(g, "https", False),
                        )
                    return resp
        return None

    @app.after_request
    def _after(resp):
        resp.headers["Content-Security-Policy"] = CSP_HEADER
        # 首次访问 HTML / auth/me 时下发 csrf cookie
        if request.method == "GET":
            path = request.path
            need_bootstrap = (
                path == "/"
                or path.endswith(".html")
                or path == "/api/auth/me"
            )
            if need_bootstrap and not request.cookies.get(CSRF_COOKIE):
                already = CSRF_COOKIE in (resp.headers.getlist("Set-Cookie") or []) or any(
                    str(v).startswith(f"{CSRF_COOKIE}=")
                    for v in resp.headers.getlist("Set-Cookie")
                )
                if not already:
                    https = getattr(g, "https", False) or request_is_https(request.headers)
                    apply_csrf_cookie(resp, new_csrf_token(), https=https)
        return resp

    def _redirect_login(target):
        next_url = ""
        if target and target.startswith("/") and not target.startswith("//"):
            next_url = "?next=" + quote(target)
        resp = redirect("/login.html" + next_url, code=302)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _send_static(rel):
        # 防穿越: send_from_directory 已限制在 STATIC_DIR
        resp = send_from_directory(STATIC_DIR, rel)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/login.html")
    def login_page():
        return _send_static("login.html")

    @app.route("/")
    @app.route("/index.html")
    def index_page():
        if not g.user:
            return _redirect_login(request.path if request.path != "/" else "/index.html")
        return _send_static("index.html")

    @app.route("/css/<path:filename>")
    def css_files(filename):
        return _send_static(f"css/{filename}")

    @app.route("/js/<path:filename>")
    def js_files(filename):
        return _send_static(f"js/{filename}")

    @app.route("/<path:filename>")
    def pages_or_spa(filename):
        if filename.endswith((".js", ".css")):
            return _send_static(filename)
        if filename.endswith(".html"):
            if not g.user:
                return _redirect_login("/" + filename)
            return _send_static(filename)
        # SPA fallback
        if not g.user:
            return _redirect_login("/" + filename)
        return _send_static("index.html")

    return app


def bootstrap_admin():
    """初始化 DB 并从 ADMIN_USERNAME/PASSWORD 同步管理员。"""
    import trades

    try:
        trades.init_db()
        log.info(f"交易记录数据库已就绪: {trades._db_path}")
    except Exception as e:
        log.warning(f"交易记录数据库初始化失败: {e}")

    admin_user = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_pass = os.environ.get("ADMIN_PASSWORD", "")
    if admin_user and admin_pass:
        try:
            if trades.count_admins() == 0:
                trades.create_user(admin_user, admin_pass, is_admin=True)
                log.info(f"已创建管理员账号: {admin_user}")
            else:
                status = trades.sync_admin_password(admin_user, admin_pass)
                if status == "updated":
                    log.info(f"管理员 {admin_user} 口令已与 .env 同步 (旧口令/会话已失效)")
                elif status == "unchanged":
                    log.info("管理员口令与 .env 一致")
                elif status == "not_admin":
                    log.warning(f"用户 {admin_user} 存在但非管理员, 忽略 .env 口令")
                else:
                    log.warning(f"已存在其他管理员, 忽略 .env 的 {admin_user}")
        except Exception as e:
            log.warning(f"管理员账号同步失败: {e}")
    elif admin_user or admin_pass:
        log.warning("ADMIN_USERNAME 与 ADMIN_PASSWORD 需同时设置")
    else:
        log.warning("未设置 ADMIN_USERNAME/ADMIN_PASSWORD，无管理员时无法创建用户")


def start_background_jobs():
    """磁盘缓存清理、股票列表预热、质押定时刷新、持仓监控。"""
    import market

    try:
        market._disk_cache.cleanup()
        log.info(f"磁盘缓存已清理 (上限 {market.CACHE_MAX_MB}MB)")
    except Exception:
        pass

    threading.Thread(target=market._load_stock_list, daemon=True).start()
    threading.Thread(target=market._pledge_scheduler, daemon=True).start()

    try:
        import monitor as _mon
        _mon.start_background(market.get_af, fallback_quotes=market.fetch_quotes)
        log.info("持仓监控线程已启动")
    except Exception as e:
        log.warning(f"持仓监控启动失败: {e}")
