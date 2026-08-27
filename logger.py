"""统一日志配置: 标准库 logging, 北京时间时间戳。

服务进程 (server.py) 与单跑 (monitor.py) 共用。各模块用
`logger = logging.getLogger(__name__)` 打日志, 无需各自配置。

- 默认 StreamHandler 输出到 stdout (Docker 下被 docker logs 捕获)。
- 设置环境变量 LOG_FILE 时启用 RotatingFileHandler (本地持久化, 自动轮转)。
- 级别可用环境变量 LOG_LEVEL (DEBUG/INFO/WARNING/ERROR) 覆盖, 默认 INFO。
- 密钥/token 自动脱敏 (前4+***+后4); 密码字段永不输出明文。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CST = timezone(timedelta(hours=8))
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s %(message)s"

# 密码类: 整段替换, 不留前后缀
_PASSWORD_KEYS = (
    "password", "passwd", "pwd", "口令", "secret", "client_secret",
)
# token/key 类: mask 前后缀
_SECRET_KEYS = (
    "api_key", "apikey", "access_token", "refresh_token", "session",
    "token", "licence", "license", "lid", "authorization", "bearer",
    "af_api_key", "mairui", "webhook",
)

_REDACTED = "[REDACTED]"
_MASK_MIN = 8  # 短于此时全 ***


def mask_secret(value, head=4, tail=4):
    """行业惯例: 保留前 head / 后 tail 字符, 中间 ***。过短则全 ***。密码请用 redact_message。"""
    if value is None:
        return _REDACTED
    s = str(value)
    if not s:
        return _REDACTED
    if len(s) < _MASK_MIN:
        return "***"
    if head + tail >= len(s):
        return "***"
    return f"{s[:head]}***{s[-tail:]}"


def _mask_kv(match):
    key = match.group(1)
    sep = match.group(2)
    val = match.group(3)
    # 去掉 JSON 引号外壳再 mask
    raw = val
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    key_l = key.lower()
    if any(k in key_l for k in _PASSWORD_KEYS):
        return f"{key}{sep}{_REDACTED}"
    if any(k in key_l for k in _SECRET_KEYS) or any(
        x in key_l for x in ("api_key", "token", "licence", "license", "webhook", "authorization", "secret")
    ):
        return f"{key}{sep}{mask_secret(raw)}"
    return match.group(0)


# ENV 风格: MAIRUI_PAID_API_KEY=... / password: ...
_ENV_SECRET_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_KEY|LICENCE|LICENSE|WEBHOOK|AUTHORIZATION)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)([^\s&;,\"'}]+|\"[^\"]*\"|'[^']*')"
)

# 短字段名: password= / token= / api_key=
_KV_RE = re.compile(
    r"(?i)\b("
    + "|".join(re.escape(k) for k in sorted(set(_PASSWORD_KEYS + _SECRET_KEYS), key=len, reverse=True))
    + r")\b(\s*[=:]\s*)([^\s&;,\"'}]+|\"[^\"]*\"|'[^']*')"
)

# URL query: access_token=... & api_key=...
_URL_QUERY_RE = re.compile(
    r"(?i)([?&])("
    + "|".join(re.escape(k) for k in ("access_token", "api_key", "token", "sign", "licence", "license", "lid", "password", "passwd"))
    + r")=([^&\s\"']+)"
)

# 麦蕊等: path 末段疑似 licence (长 hex/alnum)
_PATH_LICENCE_RE = re.compile(
    r"(https?://[^\s\"']*?/(?:lskx|hsrl|licenceinfo)[^\s\"']*?/)([A-Za-z0-9_-]{16,})(/?|\?|$)"
)


def _mask_url_query(match):
    prefix, key, val = match.group(1), match.group(2), match.group(3)
    key_l = key.lower()
    if any(k in key_l for k in _PASSWORD_KEYS):
        return f"{prefix}{key}={_REDACTED}"
    return f"{prefix}{key}={mask_secret(val)}"


def _mask_path_licence(match):
    return f"{match.group(1)}{mask_secret(match.group(2))}{match.group(3)}"


def redact_message(msg):
    """对整条日志/错误字符串做脱敏: 密码→[REDACTED], token/key→前4***后4。"""
    if msg is None:
        return msg
    s = str(msg)
    if not s:
        return s
    s = _URL_QUERY_RE.sub(_mask_url_query, s)
    s = _PATH_LICENCE_RE.sub(_mask_path_licence, s)
    s = _ENV_SECRET_RE.sub(_mask_kv, s)
    s = _KV_RE.sub(_mask_kv, s)
    return s


def sanitize_error(e):
    """对外 API/状态用的错误文案: 限流归类 + 密钥脱敏 + 截断。"""
    msg = redact_message(str(e))
    low = msg.lower()
    if any(k in low for k in ("rate", "limit", "too many", "throttle")):
        return "请求过于频繁，请稍后重试"
    if any(k in low for k in ("api", "key", "auth", "token", "permission", "licence", "license")):
        return "服务暂不可用，请稍后重试"
    if len(msg) > 120:
        return msg[:120] + "..."
    return msg


class _RedactFilter(logging.Filter):
    """对 record.getMessage() 结果脱敏后写回 msg/args。"""

    def filter(self, record):
        try:
            msg = record.getMessage()
            redacted = redact_message(msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True


class _CSTFormatter(logging.Formatter):
    """asctime 固定按北京时间格式化, 不受容器/系统时区影响。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_CST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


_configured = False
_redact_filter = _RedactFilter()


def configure(level=None, log_file=None, max_bytes=5 * 1024 * 1024, backup_count=3):
    """配置根 logger, 重复调用幂等 (首个生效)。

    level 为空时读环境变量 LOG_LEVEL; log_file 为空时读 LOG_FILE。
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    if level is None:
        level = os.environ.get("LOG_LEVEL", "").strip().upper() or logging.INFO
    if isinstance(level, str):
        level = getattr(logging, level, logging.INFO)
    root.setLevel(level)
    root.addFilter(_redact_filter)

    fmt = _CSTFormatter(_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.addFilter(_redact_filter)
    root.addHandler(stream)

    if log_file is None:
        log_file = os.environ.get("LOG_FILE", "").strip() or None
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(_redact_filter)
        root.addHandler(fh)


def get_logger(name):
    return logging.getLogger(name)
