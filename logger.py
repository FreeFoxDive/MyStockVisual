"""统一日志配置: 标准库 logging, 北京时间时间戳。

服务进程 (server.py) 与单跑 (monitor.py) 共用。各模块用
`logger = logging.getLogger(__name__)` 打日志, 无需各自配置。

- 默认 StreamHandler 输出到 stdout (Docker 下被 docker logs 捕获)。
- 设置环境变量 LOG_FILE 时启用 RotatingFileHandler (本地持久化, 自动轮转)。
- 级别可用环境变量 LOG_LEVEL (DEBUG/INFO/WARNING/ERROR) 覆盖, 默认 INFO。
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CST = timezone(timedelta(hours=8))
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s %(message)s"


class _CSTFormatter(logging.Formatter):
    """asctime 固定按北京时间格式化, 不受容器/系统时区影响。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_CST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


_configured = False


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

    fmt = _CSTFormatter(_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file is None:
        log_file = os.environ.get("LOG_FILE", "").strip() or None
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


def get_logger(name):
    return logging.getLogger(name)
