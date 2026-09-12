"""Flask API 包: 所有 /api/* 路由按领域拆分维护。

api_bp 由本模块创建, 子模块 import 时在其上注册路由;
登录态/CSRF/限流由 app.py 的 before_request 在应用层统一处理, 与本包结构无关。
"""
from __future__ import annotations

from flask import Blueprint

api_bp = Blueprint("api", __name__)

# 子模块 import 即在 api_bp 上注册路由 (顺序无依赖)
from api import admin  # noqa: E402,F401
from api import alerts  # noqa: E402,F401
from api import cn_data  # noqa: E402,F401
from api import alerts  # noqa: E402,F401
from api import auth  # noqa: E402,F401
from api import drawings  # noqa: E402,F401
from api import kline  # noqa: E402,F401
from api import market  # noqa: E402,F401
from api import me  # noqa: E402,F401
from api import models  # noqa: E402,F401
from api import monitor  # noqa: E402,F401
from api import screener  # noqa: E402,F401
from api import stream  # noqa: E402,F401
from api import trades  # noqa: E402,F401
from api.auth import auth_bp  # noqa: E402,F401

__all__ = ["api_bp", "auth_bp"]
