"""钉钉群机器人推送 (visual 自包含, 不依赖项目根)。

配置来自环境变量 DINGDING_WEB_HOOK_TOKEN / DINGDING_BOT_SIGN,
由 server.py 的 .env 加载器注入; 单跑时本模块会自行读取 visual/.env。
失败只打日志, 不抛异常。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("dingtalk")

DINGDING_WEBHOOK = "https://oapi.dingtalk.com/robot/send"
SCRIPT_DIR = Path(__file__).resolve().parent


def _load_env():
    """未设置时从 visual/.env (其次上一级) 补环境变量, 不覆盖已有值。"""
    if os.environ.get("DINGDING_WEB_HOOK_TOKEN") and os.environ.get("DINGDING_BOT_SIGN"):
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
    """推送 markdown 到钉钉群机器人。未配置或失败返回 False, 不抛。"""
    _load_env()
    token = os.environ.get("DINGDING_WEB_HOOK_TOKEN", "").strip()
    secret = os.environ.get("DINGDING_BOT_SIGN", "").strip()
    if not token or not secret:
        log.warning("未配置 DINGDING_WEB_HOOK_TOKEN/DINGDING_BOT_SIGN, 跳过推送")
        return False

    ts = str(round(time.time() * 1000))
    string_to_sign = "{}\n{}".format(ts, secret)
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    url = "{}?access_token={}&timestamp={}&sign={}".format(
        DINGDING_WEBHOOK, token, ts, sign
    )
    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8", "ignore"))
        if result.get("errcode") == 0:
            log.info("推送成功")
            return True
        log.warning(f"推送失败: {result}")
    except Exception as e:
        log.warning(f"推送失败: {e}")
    return False
