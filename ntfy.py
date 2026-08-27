"""ntfy 推送 (薄包装 myappnotify)。

配置来自环境变量 NTFY_URL / NTFY_USER / NTFY_PASSWORD
(URL 无 path 时还需 NTFY_TOPIC),
由 server.py 的 .env 加载器注入; 单跑时本模块会自行读取 visual/.env。
失败只打日志, 不抛异常。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from myappnotify import send_ntfy as _send_ntfy

log = logging.getLogger("ntfy")

SCRIPT_DIR = Path(__file__).resolve().parent


def _env_ready():
    """URL+账号齐全, 且 topic 已在 URL path 或 NTFY_TOPIC 中。"""
    url = os.environ.get("NTFY_URL", "").strip()
    user = os.environ.get("NTFY_USER", "").strip()
    password = os.environ.get("NTFY_PASSWORD", "").strip()
    if not (url and user and password):
        return False
    if urlparse(url).path.strip("/"):
        return True
    return bool(os.environ.get("NTFY_TOPIC", "").strip())


def _load_env():
    """未齐备时从 visual/.env (其次上一级) 补环境变量, 不覆盖已有值。"""
    if _env_ready():
        return
    for env_dir in (SCRIPT_DIR, SCRIPT_DIR.parent):
        env_file = env_dir / ".env"
        if not env_file.exists():
            continue
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError as e:
            log.warning(f"读取 {env_file} 失败: {e}")
        break


def send_markdown(title, text):
    """推送 markdown 到 ntfy。未配置或失败返回 False, 不抛。"""
    _load_env()
    return _send_ntfy(title, text)
