#!/usr/bin/env python3
"""
Visual K线图 HTTP 服务器
========================
基于 Python 标准库 http.server，零额外依赖。
代理麦蕊(实时/K线) + AlphaFeed(分时) + 服务端指标计算，为前端 ECharts 提供 JSON 数据。

用法:
  python -u visual/server.py                   # 默认 localhost:8888
  python -u visual/server.py --port 9999       # 自定义端口
  python -u visual/server.py --host 0.0.0.0    # 局域网可访问
"""

import argparse
import gzip
import io
import json
import os
import re
import shutil
import sys
import time
import threading
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
import urllib.request

import numpy as np
import pandas as pd

# Windows GBK 编码兼容
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 项目路径 ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))  # 独立部署时当前目录即 visual/

# ── 加载 .env (优先 visual/ 目录, 其次项目根目录) ──
for _env_dir in (SCRIPT_DIR, PROJECT_DIR):
    _env_file = _env_dir / ".env"
    if _env_file.exists():
        with open(_env_file, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k, _v = _k.strip(), _v.strip()
                    if _k not in os.environ:
                        os.environ[_k] = _v

# ── 日志: 标准库 logging, 北京时间时间戳, 级别/文件可经 env 覆盖 ──
import logging
from logger import configure as _log_configure

_log_configure()
log = logging.getLogger("server")

# ── AlphaFeed ──
AF_API_KEY = os.environ.get("AF_API_KEY", "")
if not AF_API_KEY:
    log.warning("未设置 AF_API_KEY 环境变量")
else:
    log.info("AF_API_KEY 已加载")

_af = None
_af_lock = threading.Lock()


def get_af():
    global _af
    if _af is None:
        with _af_lock:
            if _af is None:
                from alphafeed import AlphaFeed
                _af = AlphaFeed(api_key=AF_API_KEY)
    return _af


# ── 麦蕊智数 (Mairui) ──
# 优先付费版 token, 未配置才回退免费试用版
MAIRUI_API_KEY = os.environ.get("MAIRUI_PAID_API_KEY", "") or os.environ.get("MAIRUI_FREE_API_KEY", "")
if os.environ.get("MAIRUI_PAID_API_KEY"):
    log.info("MAIRUI_PAID_API_KEY 已加载 (付费版)")
elif MAIRUI_API_KEY:
    log.info("MAIRUI_FREE_API_KEY 已加载 (免费试用版)")
else:
    log.warning("未设置 MAIRUI_PAID_API_KEY / MAIRUI_FREE_API_KEY 环境变量")

_mr = None
_mr_lock = threading.Lock()


def get_mr():
    global _mr
    if _mr is None:
        with _mr_lock:
            if _mr is None:
                from mairui import Client
                _mr = Client(licence=MAIRUI_API_KEY)
    return _mr


# ── 麦蕊额度查询 (抓官方证书查询页, 麦蕊无额度 API) ──
_quota_cache = {"data": None, "ts": 0.0}
_quota_cache_lock = threading.Lock()
_QUOTA_CACHE_TTL = 120.0


def _fetch_mairui_quota():
    """抓取 mairui.club/licenceinfo 证书查询页并解析今日额度。

    麦蕊无查询额度的 API/SDK 方法, 只能抓官网页面 (服务端渲染, 额度内联在 HTML)。
    返回 {ok, version, today_used, today_remaining, total_used, total_remaining, expiry},
    失败返回 {"ok": False, "error": ...}。成功结果带 120s 内存缓存, 避免每次页面刷新都打官网。
    """
    now = time.time()
    with _quota_cache_lock:
        if _quota_cache["data"] is not None and now - _quota_cache["ts"] < _QUOTA_CACHE_TTL:
            return _quota_cache["data"]

    if not MAIRUI_API_KEY:
        return {"ok": False, "error": "未配置 MAIRUI_PAID_API_KEY / MAIRUI_FREE_API_KEY"}

    url = "https://mairui.club/licenceinfo?lid=" + MAIRUI_API_KEY
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # 表格结构: <td>版本</td><td class="licence-code">KEY</td><td>今日已用|剩余</td><td>总已用|剩余</td><td>有效期</td>
    m = re.search(
        r'<td>([^<]*)</td>\s*<td class="licence-code">[^<]*</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>',
        html,
        re.S,
    )
    if not m:
        return {"ok": False, "error": "额度页结构解析失败"}

    version = m.group(1).strip()
    expiry = m.group(4).strip()

    def _split(s):
        parts = [x.strip() for x in s.split("|")]
        return parts[0] if len(parts) > 0 else "", parts[1] if len(parts) > 1 else ""

    def _int_or_none(s):
        try:
            return int(s)
        except (ValueError, TypeError):
            return None

    today_used, today_remaining = _split(m.group(2).strip())
    total_used, total_remaining = _split(m.group(3).strip())

    data = {
        "ok": True,
        "version": version,
        "today_used": _int_or_none(today_used),
        "today_remaining": _int_or_none(today_remaining),
        "total_used": _int_or_none(total_used),
        "total_remaining": total_remaining,   # 可能为 "无限"
        "expiry": expiry,
    }
    with _quota_cache_lock:
        _quota_cache["data"] = data
        _quota_cache["ts"] = now
    return data


# ── 指数集合 + 名称映射 (懒加载内存缓存) ──
_index_symbols = None
_index_names = {}
_index_lock = threading.Lock()
_name_map = None
_name_map_lock = threading.Lock()


def _load_index_cache():
    """懒加载沪深指数列表 (symbol 集合 + symbol→名称), 服务生命周期内缓存一次"""
    global _index_symbols, _index_names
    if _index_symbols is not None:
        return _index_symbols
    with _index_lock:
        if _index_symbols is not None:
            return _index_symbols
        symbols, names = set(), {}
        try:
            rows = get_mr().index_list()
            for r in rows or []:
                sym = str(r.get("dm", "")).strip().upper()
                name = str(r.get("mc", "")).strip()
                if sym:
                    symbols.add(sym)
                    if name:
                        names[sym] = name
        except Exception as e:
            log.warning(f"加载指数列表失败: {e}")
        _index_symbols, _index_names = symbols, names
        return symbols


def _is_index_symbol(symbol):
    """判断 symbol 是否为沪深指数 (如 000001.SH 上证指数)"""
    return symbol in _load_index_cache()


def _lookup_name(symbol):
    """查标的名称: 指数 → 股票/ETF 列表。找不到返回 symbol 本身。"""
    if _is_index_symbol(symbol):
        return _index_names.get(symbol, symbol)
    global _name_map
    if _name_map is None:
        with _name_map_lock:
            if _name_map is None:
                _name_map = {s["symbol"]: s["name"] for s in _load_stock_list()}
    return _name_map.get(symbol, symbol)


# ── 指标计算 ──
from indicators import compute_all_indicators, compute_impulse, _safe_list, force_index

import market_hours

# ── 交易记录 ──
import trades

SESSION_COOKIE = "session"
SESSION_MAX_AGE = trades.SESSION_TTL_DAYS * 24 * 3600

# ── 无需登录即可访问的 API 端点 (其余 /api/* 一律要求登录) ──
# login/logout: 登录/登出本身必须公开; ping: 健康检查
PUBLIC_API_GET = {"/api/ping"}
PUBLIC_API_POST = {"/api/auth/login", "/api/auth/logout"}

# ── 请求速率限制 (令牌桶) ──
RATE_LIMIT_PER_MIN = 120
RATE_LIMIT_TOKENS = RATE_LIMIT_PER_MIN
RATE_LIMIT_LOCK = threading.Lock()
_rate_limit_last_refill = time.time()

def _check_rate_limit():
    """基于令牌桶的请求频率限制，返回 True=允许, False=拒绝"""
    global RATE_LIMIT_TOKENS, _rate_limit_last_refill
    with RATE_LIMIT_LOCK:
        now = time.time()
        elapsed = now - _rate_limit_last_refill
        # 每秒补充令牌
        RATE_LIMIT_TOKENS = min(RATE_LIMIT_PER_MIN,
                                RATE_LIMIT_TOKENS + elapsed * (RATE_LIMIT_PER_MIN / 60.0))
        _rate_limit_last_refill = now
        if RATE_LIMIT_TOKENS >= 1:
            RATE_LIMIT_TOKENS -= 1
            return True
        return False


# ── 登录爆破防护 ──
LOGIN_FAIL_MAX = 5                 # 连续失败次数上限，达到即锁定账号
LOGIN_LOCK_SECONDS = 15 * 60       # 账号锁定时长 (秒)
LOGIN_PER_IP_LIMIT = 5             # 每 IP 每分钟登录尝试上限
LOGIN_PER_IP_CAP = 1000            # per-IP 字典条目硬上限 (防内存膨胀)
LOGIN_FAIL_CAP = 1000              # 失败记录字典条目硬上限
LOGIN_STATE_TTL = 1800             # 无活动条目清理阈值 (秒)

LOGIN_BF_LOCK = threading.Lock()
# username -> [失败次数, 锁定截止时间戳, 最近活动时间戳]
_login_failures = {}
# ip -> [令牌数, 上次补充时间戳]
_login_attempts = {}


def _check_login_ip(ip):
    """登录接口独立 per-IP 限流。返回 True=允许, False=拒绝。"""
    now = time.time()
    with LOGIN_BF_LOCK:
        tokens, last = _login_attempts.get(ip, (LOGIN_PER_IP_LIMIT, now))
        tokens = min(LOGIN_PER_IP_LIMIT, tokens + (now - last) * (LOGIN_PER_IP_LIMIT / 60.0))
        if tokens < 1:
            _login_attempts[ip] = (tokens, now)
            return False
        _login_attempts[ip] = (tokens - 1, now)
        return True


def _login_lock_remaining(username):
    """返回账号剩余锁定时长(秒)，未锁定返回 0。"""
    now = time.time()
    with LOGIN_BF_LOCK:
        entry = _login_failures.get(username)
        if not entry:
            return 0
        _, locked_until, _ = entry
        if locked_until and locked_until > now:
            return int(locked_until - now)
        return 0


def _record_login_failure(username):
    """记录一次登录失败，连续达到阈值则锁定账号。"""
    now = time.time()
    with LOGIN_BF_LOCK:
        count, _, _ = _login_failures.get(username, (0, 0, now))
        count += 1
        locked_until = now + LOGIN_LOCK_SECONDS if count >= LOGIN_FAIL_MAX else 0
        _login_failures[username] = (count, locked_until, now)
        _prune_login_state_locked(now)


def _clear_login_failure(username):
    with LOGIN_BF_LOCK:
        _login_failures.pop(username, None)


def _prune_login_state_locked(now):
    """清理过期条目，防字典被随机用户名/来源 IP 撑爆。"""
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


# CSP: 'unsafe-inline' 是必需的，因为 index.html 使用内联 <script> 标签
CSP_HEADER = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https://static.cloudflareinsights.com https://cloudflareinsights.com"

# ── 磁盘缓存 ──
CACHE_DIR = SCRIPT_DIR / ".cache" / "klines"
CACHE_MAX_MB = 50


class DiskCache:
    """K线数据磁盘缓存 (JSON.gz, TTL + 总大小限制)"""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _key(self, symbol, period, count):
        return CACHE_DIR / f"{symbol}_{period}_{count}.json.gz"

    def get(self, symbol, period, count, ttl_seconds):
        fp = self._key(symbol, period, count)
        if not fp.exists():
            return None
        age = time.time() - fp.stat().st_mtime
        if age > ttl_seconds:
            return None
        try:
            with gzip.open(fp, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def set(self, symbol, period, count, data):
        fp = self._key(symbol, period, count)
        try:
            with gzip.open(fp, "wt", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, cls=NumpyEncoder)
        except Exception:
            pass

    def cleanup(self):
        """删除过期文件和超出配额的最旧文件"""
        now = time.time()
        files = sorted(CACHE_DIR.glob("*.json.gz"), key=lambda f: f.stat().st_mtime)
        total_size = 0
        kept = []
        for fp in files:
            size = fp.stat().st_size
            age = now - fp.stat().st_mtime
            if age > 86400:  # 超过24小时直接删
                fp.unlink(missing_ok=True)
                continue
            kept.append((fp, size))
            total_size += size
        # 超配额删最旧
        max_bytes = CACHE_MAX_MB * 1024 * 1024
        for fp, size in kept:
            if total_size <= max_bytes:
                break
            fp.unlink(missing_ok=True)
            total_size -= size


_disk_cache = DiskCache()


# ── 内存缓存 ──
class TTLCache:
    """简单的 TTL 内存缓存 (基于 OrderedDict 的 LRU 淘汰)"""

    def __init__(self, ttl_seconds=60):
        from collections import OrderedDict
        self._cache = OrderedDict()
        self._ttl = ttl_seconds
        self._max_entries = 500
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["time"] < self._ttl:
                # 移到末尾 (LRU)
                self._cache.move_to_end(key)
                return entry["data"]
            if entry:
                # 过期删除
                del self._cache[key]
        return None

    def set(self, key, data):
        with self._lock:
            # 如果已存在, 更新并移到末尾
            if key in self._cache:
                self._cache[key] = {"data": data, "time": time.time()}
                self._cache.move_to_end(key)
                return
            # 超过上限: 淘汰最旧的 (OrderedDict popitem(last=False))
            if len(self._cache) >= self._max_entries:
                try:
                    self._cache.popitem(last=False)
                except KeyError:
                    pass
            self._cache[key] = {"data": data, "time": time.time()}

    def clear(self):
        with self._lock:
            self._cache.clear()


# 分钟 K 线 (AlphaFeed 原生周期; 不做 3m 合成)
MINUTE_PERIODS = frozenset({"1m", "5m", "15m", "30m", "60m"})
MINUTE_COUNTS = {"1m": 1200, "5m": 480, "15m": 320, "30m": 320, "60m": 320}

# 缓存: 日K 120s, 分钟K 60s, 周/月K 300s, 快照 30s, 上限 500 条目
kline_cache = TTLCache(ttl_seconds=120)
kline_cache_minute = TTLCache(ttl_seconds=60)
kline_cache_long = TTLCache(ttl_seconds=300)
quote_cache = TTLCache(ttl_seconds=30)
_impulse_cache = TTLCache(ttl_seconds=120)


# ── 质押数据缓存 ──
# 全市场质押数据由 akshare.stock_gpzy_pledge_ratio_em 一次性批量返回, 无需逐股拉取。
# 磁盘缓存命名: .cache/pledge_ratio_YYYYMMDD.json.gz (日期=数据对应交易日, 一眼可辨)。
PLEDGE_FILE_PREFIX = "pledge_ratio_"
PLEDGE_TTL = 24 * 3600      # 每日收盘后更新一次, 24h 作为保鲜兜底
PLEDGE_KEEP_FILES = 7       # 磁盘只保留最近 7 个质押缓存文件

_pledge_cache = None        # {code: {ratio, shares, market_value, count}}
_pledge_ts = 0              # 内存缓存写入时间 (epoch)
_pledge_date = None         # 内存缓存对应的数据交易日 YYYYMMDD
_pledge_lock = threading.Lock()
_refreshing_pledge = False  # 后台刷新是否进行中 (stale-while-revalidate)


def _pledge_file_path(date_str):
    return SCRIPT_DIR / ".cache" / f"{PLEDGE_FILE_PREFIX}{date_str}.json.gz"


def _fetch_pledge():
    """从 akshare 拉取全市场质押数据, 返回 (date_str, pledge); 失败返回 (None, None)"""
    import akshare as ak
    from datetime import date, timedelta
    df = None
    got_date = None
    # 收盘后数据有延迟, 回溯最近 5 天找最新有数据的交易日
    for offset in range(5):
        d = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            df = ak.stock_gpzy_pledge_ratio_em(date=d)
            if df is not None and len(df) > 0:
                got_date = d
                break
        except Exception:
            continue
    if df is None:
        try:
            df = ak.stock_gpzy_pledge_ratio_em()
            got_date = date.today().strftime("%Y%m%d")
        except Exception:
            pass
    if df is None or len(df) == 0:
        return None, None
    pledge = {}
    for _, r in df.iterrows():
        code = str(r["股票代码"]).strip()
        if not code:
            continue
        pledge[code] = {
            "ratio": float(r.get("质押比例", 0) or 0),
            "shares": float(r.get("质押股数", 0) or 0),
            "market_value": float(r.get("质押市值", 0) or 0),
            "count": int(r.get("质押笔数", 0) or 0),
        }
    return got_date, pledge


def _pledge_from_disk():
    """读最新磁盘质押缓存, 返回 (date, pledge, ts); 无/失败返回 (None, None, 0)"""
    try:
        files = sorted(SCRIPT_DIR.joinpath(".cache").glob(f"{PLEDGE_FILE_PREFIX}*.json.gz"),
                       reverse=True)
        if not files:
            return None, None, 0
        fp = files[0]
        with gzip.open(fp, "rt", encoding="utf-8") as f:
            data = json.load(f)
        pledge = data.get("pledge")
        if not pledge:
            return None, None, 0
        return data.get("date"), pledge, data.get("ts", fp.stat().st_mtime)
    except Exception as e:
        log.warning(f"读取质押缓存失败: {e}")
    return None, None, 0


def _pledge_to_disk(date_str, pledge):
    """原子写入质押缓存 (gzip 临时文件 + replace), 并清理过期历史文件"""
    try:
        fp = _pledge_file_path(date_str)
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_name(fp.name + ".tmp")
        payload = {"date": date_str, "ts": time.time(), "count": len(pledge),
                   "pledge": pledge}
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(fp)
        # 仅保留最近 N 个 (按文件名日期倒序), 更早的删除
        all_files = sorted(fp.parent.glob(f"{PLEDGE_FILE_PREFIX}*.json.gz"), reverse=True)
        for old in all_files[PLEDGE_KEEP_FILES:]:
            try:
                old.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"写入质押缓存失败: {e}")


def _refresh_pledge_async():
    """后台异步刷新质押数据 (stale-while-revalidate), 同一时刻只跑一个

    拉取失败或日期未更新时保留旧缓存, 不覆盖、不清空, 仅记录日志等待下轮重试。
    """
    global _refreshing_pledge, _pledge_cache, _pledge_ts, _pledge_date
    with _pledge_lock:
        if _refreshing_pledge:
            return
        _refreshing_pledge = True

    def _worker():
        global _refreshing_pledge, _pledge_cache, _pledge_ts, _pledge_date
        try:
            date_str, pledge = _fetch_pledge()
            if not pledge:
                log.warning("质押数据刷新失败: 未获取到数据, 保留旧缓存")
                return
            # 非交易日或当天数据尚未更新: 拉到的仍是旧日期, 跳过重复写入
            if _pledge_date and date_str == _pledge_date:
                log.info(f"质押数据已是 {date_str} 最新, 跳过写入")
                return
            _pledge_to_disk(date_str, pledge)
            with _pledge_lock:
                _pledge_cache = pledge
                _pledge_ts = time.time()
                _pledge_date = date_str
            log.info(f"质押数据刷新完成: {date_str} 共 {len(pledge)} 条")
        except Exception as e:
            log.warning(f"质押数据刷新失败: {e}, 保留旧缓存")
        finally:
            with _pledge_lock:
                _refreshing_pledge = False

    threading.Thread(target=_worker, daemon=True).start()


def _load_pledge():
    """加载全市场质押数据 (内存+磁盘双层缓存, stale-while-revalidate)

    有缓存绝不阻塞: 内存新鲜直接用, 过期返回旧值并后台刷新; 磁盘兜底。
    仅在无任何缓存 (首次运行) 时才同步拉取。每日 15:30 定时刷新保证新鲜。
    """
    global _pledge_cache, _pledge_ts, _pledge_date
    now = time.time()

    # 1) 内存缓存新鲜 → 直接返回
    if _pledge_cache is not None and now - _pledge_ts < PLEDGE_TTL:
        return _pledge_cache

    # 2) 内存有但过期 → 返回旧数据 + 后台刷新 (不阻塞)
    if _pledge_cache is not None:
        _refresh_pledge_async()
        return _pledge_cache

    # 3) 磁盘缓存 → 加载返回; 过期则后台刷新 (不阻塞)
    date_str, pledge, ts = _pledge_from_disk()
    if pledge:
        with _pledge_lock:
            if _pledge_cache is None:
                _pledge_cache = pledge
                _pledge_ts = ts or now
                _pledge_date = date_str
        if now - (ts or 0) < PLEDGE_TTL:
            return pledge
        _refresh_pledge_async()
        return pledge

    # 4) 无任何缓存 (首次运行) → 同步拉取, 加锁避免并发重复拉取
    with _pledge_lock:
        if _pledge_cache is not None:
            return _pledge_cache
        log.info("首次加载全市场质押数据...")
        date_str, pledge = _fetch_pledge()
        if pledge:
            _pledge_cache = pledge
            _pledge_ts = time.time()
            _pledge_date = date_str
            _pledge_to_disk(date_str, pledge)
            log.info(f"已加载 {date_str} {len(pledge)} 条质押数据")
        else:
            _pledge_cache = {}
            log.warning("质押数据加载失败 (无缓存可用), 等待定时重试")
        return _pledge_cache


def _next_schedule_delay(now=None):
    """计算距离下一个 15:30 的秒数 (now 可注入便于测试, 默认当前时间)"""
    from datetime import timedelta
    now = now or market_hours.now()
    target = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _pledge_scheduler():
    """每日 15:30 定时刷新全市场质押数据 (A股收盘 15:00 后 30 分钟)"""
    while True:
        try:
            time.sleep(_next_schedule_delay())
            _refresh_pledge_async()
        except Exception as e:
            log.warning(f"质押定时任务异常: {e}")
            time.sleep(60)


# ── ETF 溢价 ──

def _is_etf(symbol):
    """判断是否为场内基金 (ETF/LOF/封闭式; 代码前缀: 51, 58, 15, 16, 56, 11, 18等)"""
    code = symbol.split(".")[0]
    return code[:2] in ("51", "58", "15", "16", "56", "11", "18") or code.startswith("5")


def _fetch_etf_nav(symbol):
    """从 akshare 获取 ETF 历史净值 (单位净值)"""
    code = symbol.split(".")[0]
    try:
        import akshare as ak
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        if df is None or len(df) == 0:
            return None
        df = df.rename(columns={"净值日期": "date", "单位净值": "nav"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).set_index("date").sort_index()
        return df[["nav"]]
    except Exception as e:
        log.warning(f"获取 {symbol} 净值失败: {e}")
        return None


# ── 数据获取 ──
def _fetch_fund_kline(symbol, period, count):
    """从麦蕊基金历史K线接口拉取 ETF K线 (SDK v1.2.0 无此方法, 直接 HTTP)。

    接口: GET /jj/lskx/{code}/{period}/{licence}  (jjhqdata#api-179)
    code 为 6 位数字(无 sh/sz 前缀), period=d/w/m, 字段 {t,d,o,h,l,c,v,a}。
    a 为成交额(基金接口固定 0)。接口不支持 lt 分页, 返回全量历史, 本地 tail 截取。
    返回标准化 DataFrame 或 None。
    """
    code = symbol.split(".")[0]
    mr_period = {"1d": "d", "1w": "w", "1M": "m"}.get(period, "d")
    url = f"https://api.mairuiapi.com/jj/lskx/{code}/{mr_period}/{MAIRUI_API_KEY}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        log.warning(f"麦蕊获取ETF K线失败 {symbol}: {e}")
        return None

    # dict = 错误响应, 空列表 = 无数据
    if not rows or isinstance(rows, dict):
        return None

    df = pd.DataFrame(rows)
    df = df.drop(columns=["d"], errors="ignore")  # d 与 t 同为日期, 保留 t
    df = df.rename(columns={
        "o": "open", "h": "high", "l": "low", "c": "close",
        "a": "amount", "v": "volume", "t": "trade_date",
    })
    df = _normalize(df)
    if df is not None and count:
        df = df.tail(count)
    if df is not None:
        # 基金接口不提供成交额 (a 恒为 0), 置 NaN → 前端显示 "—"
        df["amount"] = float("nan")
    return df


def _fetch_minute_kline(symbol, period, count):
    """从 AlphaFeed 拉取分钟 K 线, 返回标准化 DataFrame 或 None。"""
    try:
        af = get_af()
        dfs = af.klines.batch(
            [symbol], period=period, count=count, adjust="none", to_dataframe=True
        )
        df = dfs.get(symbol) if dfs else None
    except Exception as e:
        log.warning(f"AlphaFeed 获取分钟K线失败 {symbol} {period}: {e}")
        return None
    if df is None or len(df) == 0:
        return None
    return _normalize(df, prefer_time=True)


def _fetch_impulse_qfq(symbol, count):
    """用 AlphaFeed 前复权日K计算 Elder impulse 方向 (与 v7 动力管线口径一致)。

    主图 K 线保持未复权, 但动力系统蜡烛颜色改用前复权, 避免分红除权造成
    假的价格跳空污染 EMA13/MACD 方向。返回按 trade_date 索引的 int Series
    (1=红 / -1=绿 / 0=蓝); 任何失败返回 None (调用方回退未复权 impulse)。
    """
    cache_key = f"{symbol}:{count}"
    cached = _impulse_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        af = get_af()
        dfs = af.klines.batch(
            [symbol], period="1d", count=count, adjust="forward", to_dataframe=True
        )
        df = dfs.get(symbol) if dfs else None
    except Exception as e:
        log.warning(f"AlphaFeed 获取前复权日K失败 {symbol}: {e}")
        return None
    if df is None or len(df) == 0:
        return None
    df = _normalize(df)
    if df is None:
        return None
    try:
        impulse = compute_impulse(df["close"])
    except Exception as e:
        log.warning(f"前复权 impulse 计算失败 {symbol}: {e}")
        return None
    _impulse_cache.set(cache_key, impulse)
    return impulse


def fetch_kline(symbol, period, count):
    """获取 K 线数据（优先磁盘缓存），返回标准化 DataFrame。

    数据源路由：
    - 分钟 → AlphaFeed klines.batch
    - 指数 → 麦蕊 index_history
    - 股票 → 麦蕊 stock_history
    - ETF  → 麦蕊基金历史K线 (jj/lskx)
    """
    # 检查磁盘缓存 (日K/分钟 盘中 60s/盘后 300s, 周月K 600s)
    now = market_hours.now()
    in_trading = market_hours.in_session(now)
    if period in MINUTE_PERIODS:
        ttl = 60 if in_trading else 300
    elif period == "1d":
        ttl = 60 if in_trading else 300
    else:
        ttl = 600
    cached = _disk_cache.get(symbol, period, count, ttl)
    if cached:
        df = pd.DataFrame(cached["data"])
        # 分钟线优先 trade_time: JSON 常同时带 trade_date(日) 与 trade_time,
        # 若先按 date 建索引, 同日多根重复 → RSI get_loc 返回 slice
        if "trade_time" in df.columns:
            df["trade_time"] = pd.to_datetime(df["trade_time"])
            df = df.set_index("trade_time")
        elif "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("trade_date")
        df = df.sort_index()
        return df, cached.get("name", symbol)

    df, name = None, None

    if period in MINUTE_PERIODS:
        df = _fetch_minute_kline(symbol, period, count)
        if df is not None:
            name = _lookup_name(symbol)
    elif _is_etf(symbol):
        # ETF → 麦蕊基金历史K线 (jj/lskx)
        df = _fetch_fund_kline(symbol, period, count)
        if df is not None:
            name = _lookup_name(symbol)
    else:
        # 麦蕊 K 线 (指数/股票)
        api = get_mr()
        mr_period = {"1d": "d", "1w": "w", "1M": "m"}.get(period, "d")
        try:
            if _is_index_symbol(symbol):
                rows = api.index_history(symbol, mr_period, lt=count)
            else:
                rows = api.stock_history(symbol, mr_period, "n", lt=count)
        except Exception as e:
            log.warning(f"麦蕊获取K线失败 {symbol}: {e}")
            return None, None

        # dict = 错误响应 (如 {"error": "数据不存在"}), 空列表 = 无数据
        if not rows or isinstance(rows, dict):
            return None, None

        df = pd.DataFrame(rows)
        df = df.rename(columns={
            "o": "open", "h": "high", "l": "low", "c": "close",
            "a": "amount", "v": "volume", "t": "trade_date",
        })
        df = _normalize(df)
        if df is not None:
            name = _lookup_name(symbol)

    if df is None:
        return None, None

    # 存入磁盘缓存
    cache_data = {"name": name or symbol, "data": json.loads(df.reset_index().to_json(orient="records", date_format="iso"))}
    try:
        _disk_cache.set(symbol, period, count, cache_data)
    except Exception:
        pass

    return df, name or symbol


def _mr_quote_to_std(q, symbol):
    """把麦蕊实时行情 dict 转成标准 quote dict"""
    return {
        "last_price": _safe_float(q.get("p")),
        "prev_close": _safe_float(q.get("yc")),
        "open": _safe_float(q.get("o")),
        "high": _safe_float(q.get("h")),
        "low": _safe_float(q.get("l")),
        "volume": _safe_int(q.get("v")),
        "amount": _safe_float(q.get("cje")),
        "change_pct": _safe_float(q.get("pc")),    # 麦蕊实时 pc = 涨跌幅%
        "amplitude": _safe_float(q.get("zf")),     # zf = 振幅%
        "turnover_rate": _safe_float(q.get("tr")),  # tr = 换手率% (ETF/指数无此字段)
        "name": _lookup_name(symbol),
    }


MR_QUOTE_BATCH = 20        # 麦蕊 stock_ssjy_more 单次最多 20 只


def fetch_quotes(symbols, fresh=False):
    """批量获取实时快照（麦蕊：股票走 ssjy_more 批量，ETF 走 fund_real_time，指数走 index_real_time）。

    fresh=True 时跳过缓存强刷，失败/空数据回退到缓存（缓存超 30s 的 get() 返回 None）。
    返回 {symbol: quote}；未取到的 symbol 不出现在返回字典中。
    """
    symbols = [normalize_symbol(s) for s in symbols if s]
    symbols = list(dict.fromkeys(symbols))  # 去重保序
    if not symbols:
        return {}

    result = {}
    if not fresh:
        # 缓存优先：命中直接返回，只抓缺失的
        for s in symbols:
            cached = quote_cache.get(s)
            if cached:
                result[s] = cached
    to_fetch = [s for s in symbols if s not in result]
    if not to_fetch:
        return result

    # 按类型分组路由
    index_codes, stock_codes, etf_codes = [], [], []
    for s in to_fetch:
        if _is_index_symbol(s):
            index_codes.append(s)
        elif _is_etf(s):
            etf_codes.append(s)
        else:
            stock_codes.append(s)

    api = get_mr()

    def _emit(s, q):
        result[s] = _mr_quote_to_std(q, s)
        quote_cache.set(s, result[s])

    # 1) 指数 (单只 index_real_time)
    for s in index_codes:
        try:
            q = api.index_real_time(s)
            if isinstance(q, dict) and not q.get("error"):
                _emit(s, q)
        except Exception as e:
            log.warning(f"指数快照失败 {s}: {e}")

    # 2) ETF/基金 (单只 fund_real_time, code 6 位无后缀)
    for s in etf_codes:
        try:
            q = api.fund_real_time(s.split(".")[0])
            if isinstance(q, dict) and not q.get("error"):
                _emit(s, q)
        except Exception as e:
            log.warning(f"ETF快照失败 {s}: {e}")

    # 3) 股票 (批量 ssjy_more, 最多 20/次)
    for i in range(0, len(stock_codes), MR_QUOTE_BATCH):
        batch = stock_codes[i:i + MR_QUOTE_BATCH]
        try:
            rows = api.stock_ssjy_more([c.split(".")[0] for c in batch])
            if isinstance(rows, list):
                for q in rows:
                    if not isinstance(q, dict) or q.get("error"):
                        continue
                    code = str(q.get("dm", "")).strip()
                    s = next((x for x in batch if x.split(".")[0] == code), None)
                    if s is not None:
                        _emit(s, q)
        except Exception as e:
            log.warning(f"批量快照失败 {batch}: {e}")

    # 未取到的回退缓存 (fresh 模式此前跳过了缓存优先读取)
    for s in to_fetch:
        if s not in result:
            cached = quote_cache.get(s)
            if cached:
                result[s] = cached

    return result


def fetch_quote(symbol):
    """从麦蕊获取实时快照（单只，缓存优先）"""
    return fetch_quotes([symbol]).get(normalize_symbol(symbol))


def _normalize(df, prefer_time=False):
    """标准化 K 线 DataFrame: 设置日期索引，确保 OHLCV 列存在"""
    if df is None or len(df) < 5:
        return None
    # 选择正确的日期列 (分钟线优先用 trade_time，避免同日 bar 索引重复)
    if prefer_time:
        cols = ["trade_time", "trade_date"]
    else:
        cols = ["trade_date", "trade_time"]
    date_col = None
    for col in cols:
        if col in df.columns:
            date_col = col
            break
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()

    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"])
    return df if len(df) >= 5 else None


# ── 全量股票搜索缓存 ──
_stock_list = None
_stock_list_time = 0
_stock_lock = threading.Lock()
_refreshing = False  # 后台刷新是否进行中 (stale-while-revalidate)

STOCK_LIST_FILE = SCRIPT_DIR / ".cache" / "stock_list.json"
STOCK_LIST_TTL = 24 * 3600  # 股票列表变化缓慢, 24h 刷新一次即可


def _stock_list_from_disk():
    """读磁盘股票列表, 返回 (stocks, ts); 失败/无则 (None, 0)"""
    try:
        if STOCK_LIST_FILE.exists():
            data = json.loads(STOCK_LIST_FILE.read_text(encoding="utf-8"))
            stocks = data.get("stocks")
            ts = data.get("ts", 0)
            if stocks:
                return stocks, ts
    except Exception as e:
        log.warning(f"读取股票列表缓存失败: {e}")
    return None, 0


def _stock_list_to_disk(stocks, ts):
    """原子写入磁盘股票列表 (临时文件 + replace)"""
    try:
        STOCK_LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STOCK_LIST_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"stocks": stocks, "ts": ts}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(STOCK_LIST_FILE)
    except Exception as e:
        log.warning(f"写入股票列表缓存失败: {e}")


def _fetch_stock_list():
    """从麦蕊拉取 symbol+name 列表 (沪深A股 + 北交所 + 场内基金)"""
    api = get_mr()
    stocks = []

    def _append(rows):
        for r in rows or []:
            sym = str(r.get("dm", "")).strip()
            # 麦蕊部分简称含空格(如 "五 粮 液"), 去掉全部空白以免搜索/显示异常
            name = re.sub(r"\s+", "", str(r.get("mc", "")))
            if not sym or not name:
                continue
            code = sym.split(".")[0] if "." in sym else sym
            stocks.append({"symbol": sym, "name": name, "code": code})

    # stock_list 已含科创(688), 故无需再拉 star_stock_list
    # fund_list(沪深基金) 是 etf_list 的超集, 额外含 LOF/封闭式基金, 故用 fund_list
    for fn, label in ((api.stock_list, "沪深A股"),
                      (api.bj_stock_list, "北交所"),
                      (api.fund_list, "场内基金")):
        try:
            _append(fn())
        except Exception as e:
            log.warning(f"{label} 列表加载失败: {e}")
    return stocks


def _refresh_stock_list_async():
    """后台异步刷新股票列表 (stale-while-revalidate), 同一时刻只跑一个"""
    global _refreshing, _stock_list, _stock_list_time
    with _stock_lock:
        if _refreshing:
            return
        _refreshing = True

    def _worker():
        global _refreshing, _stock_list, _stock_list_time
        try:
            stocks = _fetch_stock_list()
            if stocks:
                ts = time.time()
                _stock_list_to_disk(stocks, ts)
                with _stock_lock:
                    _stock_list = stocks
                    _stock_list_time = ts
                log.info(f"后台刷新股票列表完成: {len(stocks)} 只标的")
        except Exception as e:
            log.warning(f"后台刷新股票列表失败: {e}")
        finally:
            with _stock_lock:
                _refreshing = False

    threading.Thread(target=_worker, daemon=True).start()


def _load_stock_list():
    """加载全量A股+ETF列表 (内存+磁盘双层缓存, stale-while-revalidate)

    有旧数据时绝不阻塞: 立即返回旧列表, 同时后台刷新。
    仅在无任何缓存 (首次运行) 时才同步拉取 API。
    """
    global _stock_list, _stock_list_time
    now = time.time()

    # 1) 内存缓存新鲜 → 直接返回
    if _stock_list is not None and now - _stock_list_time < STOCK_LIST_TTL:
        return _stock_list

    # 2) 内存有但过期 → 返回旧数据 + 后台刷新 (不阻塞)
    if _stock_list is not None:
        _refresh_stock_list_async()
        return _stock_list

    # 3) 磁盘缓存 → 加载返回; 过期则后台刷新 (不阻塞)
    stocks, ts = _stock_list_from_disk()
    if stocks:
        with _stock_lock:
            if _stock_list is None:
                _stock_list, _stock_list_time = stocks, ts
        if now - ts < STOCK_LIST_TTL:
            return stocks
        _refresh_stock_list_async()
        return stocks

    # 4) 无任何缓存 (首次运行) → 同步拉取, 加锁避免并发重复拉取
    with _stock_lock:
        if _stock_list is not None:
            return _stock_list
        log.info("首次加载全量A股+场内基金列表...")
        stocks = []
        for attempt in range(2):
            try:
                stocks = _fetch_stock_list()
                if stocks:
                    break
            except Exception as e:
                log.warning(f"加载列表({attempt+1}/2)失败: {e}")
            if attempt == 0:
                time.sleep(3)
        if stocks:
            ts = time.time()
            _stock_list, _stock_list_time = stocks, ts
            _stock_list_to_disk(stocks, ts)
            log.info(f"已加载 {len(stocks)} 只标的 (A股+场内基金)")
        else:
            _stock_list = []
        return _stock_list


def _search_stocks(query):
    """模糊搜索: 名称/代码精确 > 名称前缀 > 代码前缀 > 名称包含 > 代码包含"""
    stocks = _load_stock_list()
    if not stocks:
        return []
    q = query.strip().lower()
    results = []
    for s in stocks:
        name = s["name"].lower()
        code = s["code"]
        score = 0
        if name == q or code == q:
            score = 200   # 名称/代码精确匹配
        elif name.startswith(q):
            score = 150   # 名称前缀 (如 "酒ETF"/"白酒基金" 直接命中)
        elif code.startswith(q):
            score = 100   # 代码前缀
        elif q in name:
            score = 50    # 名称包含
        elif q in code:
            score = 30    # 代码包含
        if score > 0:
            results.append({**s, "score": score})
    results.sort(key=lambda x: -x["score"])
    return [{"symbol": r["symbol"], "name": r["name"], "code": r["code"]}
            for r in results[:30]]


# ── JSON 编码 ──
class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            if np.isnan(obj) or np.isinf(obj):
                return None
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, pd.Timestamp):
            return str(obj)
        return super().default(obj)


def _safe_float(v):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ── 股票代码标准化 ──
def normalize_symbol(raw):
    """将用户输入标准化为带交易所后缀的 symbol 格式 (如 000001.SZ)"""
    raw = raw.strip().upper()
    if raw.endswith(".SH") or raw.endswith(".SZ") or raw.endswith(".BJ"):
        return raw
    if raw.startswith("SH") or raw.startswith("SZ"):
        return raw
    if raw.startswith(("60", "68")):
        return f"{raw}.SH"
    if raw.startswith(("00", "30", "20")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8", "9")):
        return f"{raw}.BJ"
    if raw.startswith("5"):                    # 上交所基金 (50/51/52/53/55/56/58/59...)
        return f"{raw}.SH"
    if raw.startswith(("15", "16", "18")):     # 深交所基金 (LOF/ETF/封闭式)
        return f"{raw}.SZ"
    # 默认尝试 SH
    return f"{raw}.SH"


def _sanitize_error(e):
    """限流/安全错误信息过滤"""
    msg = str(e)
    low = msg.lower()
    if any(k in low for k in ("rate", "limit", "too many", "throttle")):
        return "请求过于频繁，请稍后重试"
    if any(k in low for k in ("api", "key", "auth", "token", "permission")):
        return "服务暂不可用，请稍后重试"
    if len(msg) > 120:
        return msg[:120] + "..."
    return msg


# ── HTTP Handler ──
class VisualHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, fmt, *args):
        # 简洁日志
        if args:
            log.info(fmt % args[:3])
        else:
            log.info(fmt)

    def _send_json(self, data, code=200, headers=None):
        body = json.dumps(data, ensure_ascii=False, cls=NumpyEncoder).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content, code=200):
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, code=400):
        self._send_json({"error": msg}, code)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ── API 路由: 所有 /api/* 做速率限制 + 统一鉴权 ──
        if path.startswith("/api/"):
            if not _check_rate_limit():
                return self._send_json({"error": "请求过于频繁，请稍后重试"}, 429)
            if path not in PUBLIC_API_GET and not self._current_user():
                return self._send_error("未登录", 401)

        if path == "/api/kline":
            return self._handle_kline(params)
        elif path == "/api/quote":
            return self._handle_quote(params)
        elif path == "/api/quotes":
            return self._handle_quotes(params)
        elif path == "/api/intraday":
            return self._handle_intraday(params)
        elif path == "/api/pledge":
            return self._handle_pledge(params)
        elif path == "/api/search":
            return self._handle_search(params)
        elif path == "/api/ping":
            now = market_hours.now()
            return self._send_json({
                "ok": True,
                "time": str(now),
                "in_session": market_hours.in_session(now),
                "is_trading_day": market_hours.is_trading_day(now),
            })
        elif path == "/api/auth/me":
            return self._handle_auth_me()
        elif path == "/api/admin/users":
            return self._handle_admin_users_list()
        elif path == "/api/monitor/status":
            return self._handle_monitor_status()
        elif path == "/api/trades/stats":
            return self._handle_trades_stats(params)
        elif path == "/api/trades":
            return self._handle_trades_list(params)
        elif path == "/api/trade-reasons":
            return self._send_json({"entry": trades.ENTRY_REASONS, "exit": trades.EXIT_REASONS})
        elif path == "/api/models":
            return self._handle_models_list()
        elif path == "/api/fees":
            return self._handle_fees_get()
        elif path == "/api/quota":
            return self._handle_quota()
        elif path == "/login.html":
            # 登录页公开 (未登录跳转目标)
            return self._serve_static("login.html")
        elif path == "/" or path == "/index.html":
            if not self._current_user():
                return self._redirect_to_login(path)
            return self._serve_static("index.html")
        elif path.endswith(".html"):
            # trades.html / admin.html 等受保护页面
            if not self._current_user():
                return self._redirect_to_login(path)
            return self._serve_static(path.lstrip("/"))
        elif path.endswith((".js", ".css")):
            # 静态资源公开 (登录页需加载样式/脚本)
            return self._serve_static(path.lstrip("/"))
        else:
            # 默认返回 index.html (SPA fallback); 未登录跳登录页
            if not self._current_user():
                return self._redirect_to_login(path)
            return self._serve_static("index.html")

    # ── 鉴权与 body 解析助手 ──
    def _client_ip(self):
        """识别真实客户端 IP：只信任 CF-Connecting-IP，缺失时回退 socket 对端。

        部署栈为 Cloudflare CDN → Caddy 反代 → 本服务 (Docker)。Cloudflare 会覆盖
        客户端自带的 CF-Connecting-IP 头并注入真实访客 IP，故该头在 CF 前置时可信。

        不信任 X-Forwarded-For：Cloudflare 对它只追加不覆盖，客户端预塞的伪造段
        会落在第一位，取 split(",")[0] 恰好取到伪造值，可被用于绕过 per-IP 限流。
        缺失 CF 头时回退 client_address[0]（反代/容器网关 IP，所有用户共享），
        仅作安全默认值，不放大（避免全员共享 IP 互相触发限流）。
        """
        cf = self.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip().split(",")[0].strip()
        return self.client_address[0]

    def _get_cookie(self, name):
        from http.cookies import SimpleCookie
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        if name in cookies:
            return cookies[name].value
        return None

    def _current_user(self):
        token = self._get_cookie(SESSION_COOKIE)
        if not token:
            return None
        return trades.get_session(token)

    def _set_session_cookie(self, token, clear=False):
        # 仅当反代转发 X-Forwarded-Proto: https 时加 Secure，保证本地 127.0.0.1 纯 HTTP
        # 仍可登录（浏览器会拒绝在 http 下回传带 Secure 的 cookie）。Caddy 未配置转发头
        # 时优雅降级为不带 Secure，与历史行为一致。
        secure = " Secure" if self.headers.get("X-Forwarded-Proto", "").strip().lower() == "https" else ""
        if clear:
            return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}{secure}"

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if not _check_rate_limit():
                return self._send_json({"error": "请求过于频繁，请稍后重试"}, 429)
            if path not in PUBLIC_API_POST and not self._current_user():
                return self._send_error("未登录", 401)
        if path == "/api/auth/login":
            return self._handle_login()
        elif path == "/api/auth/logout":
            return self._handle_logout()
        elif path == "/api/admin/users":
            return self._handle_admin_users_create()
        elif path.startswith("/api/admin/users/") and path.endswith("/monitor"):
            uid = path[len("/api/admin/users/"):-len("/monitor")]
            if uid.isdigit():
                return self._handle_admin_users_monitor(int(uid))
            return self._send_error("Not Found", 404)
        elif path.startswith("/api/admin/users/") and path.endswith("/reset-password"):
            uid = path[len("/api/admin/users/"):-len("/reset-password")]
            if uid.isdigit():
                return self._handle_admin_users_reset(int(uid))
            return self._send_error("Not Found", 404)
        elif path == "/api/models":
            return self._handle_models_create()
        elif path.startswith("/api/models/") and path.endswith("/restore"):
            mid = path[len("/api/models/"):-len("/restore")]
            if mid.isdigit():
                return self._handle_models_restore(int(mid))
            return self._send_error("Not Found", 404)
        elif path == "/api/trades":
            return self._handle_trades_create()
        else:
            return self._send_error("Not Found", 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if not _check_rate_limit():
                return self._send_json({"error": "请求过于频繁，请稍后重试"}, 429)
        if path == "/api/fees":
            return self._handle_fees_put()
        # /api/models/{id}
        if path.startswith("/api/models/"):
            mid = path[len("/api/models/"):]
            if mid.isdigit():
                return self._handle_models_update(int(mid))
        # /api/trades/{id}
        if path.startswith("/api/trades/"):
            tid = path[len("/api/trades/"):]
            if tid.isdigit():
                return self._handle_trades_update(int(tid))
        return self._send_error("Not Found", 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if not _check_rate_limit():
                return self._send_json({"error": "请求过于频繁，请稍后重试"}, 429)
        if path.startswith("/api/admin/users/"):
            uid = path[len("/api/admin/users/"):]
            if uid.isdigit():
                return self._handle_admin_users_delete(int(uid))
            return self._send_error("Not Found", 404)
        if path.startswith("/api/models/"):
            mid = path[len("/api/models/"):]
            if mid.isdigit():
                return self._handle_models_delete(int(mid))
        if path.startswith("/api/trades/"):
            tid = path[len("/api/trades/"):]
            if tid.isdigit():
                return self._handle_trades_delete(int(tid))
        return self._send_error("Not Found", 404)

    # ── API: 鉴权 ──
    def _handle_login(self):
        # 登录接口独立 per-IP 限流 (区别于全局令牌桶)
        if not _check_login_ip(self._client_ip()):
            return self._send_error("登录尝试过于频繁，请稍后重试", 429)

        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._send_error("用户名和密码不能为空")

        # 账号锁定：锁定期间即使密码正确也拒绝
        remaining = _login_lock_remaining(username)
        if remaining > 0:
            return self._send_error(f"账号已锁定，请 {remaining // 60 + 1} 分钟后重试", 429)

        result = trades.login(username, password)
        if not result:
            _record_login_failure(username)
            time.sleep(0.5)  # 拖慢自动化爆破
            return self._send_error("用户名或密码错误", 401)

        _clear_login_failure(username)
        token, expires = result
        return self._send_json(
            {"ok": True, "username": username, "expires_at": expires},
            headers={"Set-Cookie": self._set_session_cookie(token)},
        )

    def _handle_logout(self):
        token = self._get_cookie(SESSION_COOKIE)
        if token:
            trades.delete_session(token)
        return self._send_json(
            {"ok": True},
            headers={"Set-Cookie": self._set_session_cookie(None, clear=True)},
        )

    def _handle_auth_me(self):
        user = self._current_user()
        if not user:
            return self._send_error("未登录", 401)
        return self._send_json({
            "username": user["username"],
            "is_admin": user["is_admin"],
            "monitor_enabled": bool(user.get("is_admin") or user.get("monitor_enabled")),
        })

    # ── API: 交易记录 ──
    def _require_user(self):
        user = self._current_user()
        if not user:
            self._send_error("未登录", 401)
        return user

    def _require_admin(self):
        user = self._current_user()
        if not user:
            self._send_error("未登录", 401)
            return None
        if not user.get("is_admin"):
            self._send_error("无权限", 403)
            return None
        return user

    # ── API: 用户管理 (仅 admin) ──
    def _handle_admin_users_list(self):
        if not self._require_admin():
            return
        return self._send_json(trades.list_users())

    def _handle_admin_users_create(self):
        if not self._require_admin():
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._send_error("用户名和密码不能为空")
        try:
            user_id = trades.create_user(username, password, is_admin=False)
        except ValueError as e:
            return self._send_error(str(e), 409)
        return self._send_json({"ok": True, "id": user_id, "username": username})

    def _handle_admin_users_delete(self, user_id):
        if not self._require_admin():
            return
        try:
            deleted = trades.delete_user(user_id)
        except ValueError as e:
            return self._send_error(str(e), 400)
        if not deleted:
            return self._send_error("用户不存在", 404)
        return self._send_json({"ok": True})

    def _handle_admin_users_reset(self, user_id):
        if not self._require_admin():
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        password = body.get("password") or ""
        if not password:
            return self._send_error("密码不能为空")
        try:
            updated = trades.reset_password(user_id, password)
        except ValueError as e:
            return self._send_error(str(e), 400)
        if not updated:
            return self._send_error("用户不存在", 404)
        return self._send_json({"ok": True})

    def _handle_admin_users_monitor(self, user_id):
        if not self._require_admin():
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        enabled = body.get("enabled")
        if enabled not in (True, False, 0, 1, "0", "1", "true", "false"):
            return self._send_error("enabled 必须为布尔值")
        if isinstance(enabled, str):
            enabled = enabled.lower() in ("1", "true")
        else:
            enabled = bool(enabled)
        if not trades.set_user_monitor(user_id, enabled):
            return self._send_error("用户不存在", 404)
        return self._send_json({"ok": True, "id": user_id, "monitor_enabled": enabled})

    def _handle_monitor_status(self):
        user = self._require_user()
        if not user:
            return
        try:
            import monitor as _mon
            st = _mon.get_status()
        except Exception:
            st = {"running": False, "backend": None, "last_poll": None, "n_symbols": 0}
        alerts = trades.list_monitor_alerts(user["id"], limit=20)
        st["alerts"] = alerts
        st["monitor_enabled"] = bool(user.get("is_admin") or user.get("monitor_enabled"))
        return self._send_json(st)

    # ── API: 量化模型 (读: 登录用户; 写: 仅 admin) ──
    def _handle_models_list(self):
        if not self._require_user():
            return
        return self._send_json(trades.list_models(active_only=False))

    def _handle_models_create(self):
        if not self._require_admin():
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        try:
            mid = trades.create_model(
                body.get("name"), body.get("description", ""),
                body.get("hold_days"),
            )
        except ValueError as e:
            return self._send_error(str(e), 409)
        return self._send_json({"ok": True, "id": mid})

    def _handle_models_update(self, mid):
        if not self._require_admin():
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        try:
            hold_days = body["hold_days"] if "hold_days" in body else trades._UNSET
            updated = trades.update_model(
                mid, body.get("name"), body.get("description", ""), hold_days,
            )
        except ValueError as e:
            return self._send_error(str(e), 409)
        if not updated:
            return self._send_error("模型不存在", 404)
        return self._send_json({"ok": True})

    def _handle_models_delete(self, mid):
        if not self._require_admin():
            return
        if not trades.delete_model(mid):
            return self._send_error("模型不存在", 404)
        return self._send_json({"ok": True})

    def _handle_models_restore(self, mid):
        if not self._require_admin():
            return
        try:
            restored = trades.restore_model(mid)
        except ValueError as e:
            return self._send_error(str(e), 409)
        if not restored:
            return self._send_error("模型不存在", 404)
        return self._send_json({"ok": True})

    def _handle_trades_list(self, params):
        user = self._require_user()
        if not user:
            return
        filters = {
            "status": params.get("status", [None])[0],
            "symbol": params.get("symbol", [None])[0],
            "q": params.get("q", [None])[0],
            "from": params.get("from", [None])[0],
            "to": params.get("to", [None])[0],
            "model_id": params.get("model_id", [None])[0],
            "limit": params.get("limit", [None])[0],
            "offset": params.get("offset", [None])[0],
        }
        deduct = params.get("deduct_fees", [""])[0].lower() in ("1", "true")
        fee_config = trades.get_user_fees(user["id"]) if deduct else None
        records, total = trades.list_trades(user["id"], filters, fee_config=fee_config)
        return self._send_json({"trades": records, "total": total})

    def _handle_trades_create(self):
        user = self._require_user()
        if not user:
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        try:
            trade = trades.create_trade(user["id"], body)
        except ValueError as e:
            return self._send_error(str(e), 400)
        return self._send_json({"trade": trade}, 201)

    def _handle_trades_update(self, tid):
        user = self._require_user()
        if not user:
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体无效 JSON", 400)
        try:
            trade = trades.update_trade(user["id"], tid, body)
        except ValueError as e:
            return self._send_error(str(e), 400)
        if trade is None:
            return self._send_error("记录不存在", 404)
        return self._send_json({"trade": trade})

    def _handle_trades_delete(self, tid):
        user = self._require_user()
        if not user:
            return
        if not trades.delete_trade(user["id"], tid):
            return self._send_error("记录不存在", 404)
        return self._send_json({"ok": True})

    def _handle_trades_stats(self, params):
        user = self._require_user()
        if not user:
            return
        start = params.get("from", [None])[0]
        end = params.get("to", [None])[0]
        deduct = params.get("deduct_fees", [""])[0].lower() in ("1", "true")
        fee_config = trades.get_user_fees(user["id"]) if deduct else None
        return self._send_json(trades.compute_stats(
            user["id"], start, end, deduct_fees=deduct, fee_config=fee_config
        ))

    def _handle_fees_get(self):
        user = self._require_user()
        if not user:
            return
        return self._send_json({"fees": trades.get_user_fees(user["id"])})

    def _handle_fees_put(self):
        user = self._require_user()
        if not user:
            return
        body = self._read_json_body()
        if body is None:
            return self._send_error("请求体必须是 JSON 对象", 400)
        try:
            fees = trades.update_user_fees(user["id"], body)
        except ValueError as e:
            return self._send_error(f"费率配置无效: {_sanitize_error(e)}", 400)
        return self._send_json({"fees": fees})

    def _handle_quota(self):
        # 麦蕊额度 (需登录, 未登录不暴露额度信息)
        if not self._require_user():
            return
        return self._send_json(_fetch_mairui_quota())

    # ── 页面鉴权跳转 ──
    def _redirect_to_login(self, target):
        """未登录访问受保护页面 → 302 跳登录页, 带 next 回跳参数。

        next 仅接受以 "/" 开头且非 "//" 的值, 防开放重定向。
        """
        next_url = ""
        if target and target.startswith("/") and not target.startswith("//"):
            next_url = "?next=" + quote(target)
        self.send_response(302)
        self.send_header("Location", "/login.html" + next_url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # ── 静态文件 ──
    def _serve_static(self, filename):
        # 前端静态文件统一放在 static/ 子目录 (URL 仍为干净路径, 如 /trades.html /admin.html)
        if filename.endswith((".html", ".js", ".css")):
            filename = "static/" + filename
        filepath = (SCRIPT_DIR / filename).resolve()
        # 防止路径穿越: 确保解析后仍在 visual/ 目录内 (加 os.sep 防 visual-xxx 绕过)
        if not str(filepath).startswith(str(SCRIPT_DIR.resolve()) + os.sep):
            self._send_error("Forbidden", 403)
            return
        if not filepath.is_file():
            self._send_error("File not found", 404)
            return
        content = filepath.read_text(encoding="utf-8")
        ct = "text/html"
        if filename.endswith(".js"):
            ct = "application/javascript"
        elif filename.endswith(".css"):
            ct = "text/css"
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{ct}; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.end_headers()
        self.wfile.write(body)

    # ── API: K线数据 ──
    def _handle_kline(self, params):
        symbol_raw = params.get("symbol", [None])[0]
        if not symbol_raw:
            return self._send_error("缺少 symbol 参数")

        symbol = normalize_symbol(symbol_raw)
        period = params.get("period", ["1d"])[0]
        default_count = MINUTE_COUNTS.get(period, 200)
        count = min(int(params.get("count", [str(default_count)])[0]), 1500)

        # 检查缓存
        cache_key = f"{symbol}:{period}:{count}"
        if period in MINUTE_PERIODS:
            cache = kline_cache_minute
        elif period in ("1w", "1M"):
            cache = kline_cache_long
        else:
            cache = kline_cache
        cached = cache.get(cache_key)
        if cached:
            resp = cached.copy()
            resp["meta"]["cached"] = True
            return self._send_json(resp)

        # 获取数据
        try:
            df, name = fetch_kline(symbol, period, count)
        except Exception as e:
            return self._send_error(f"获取K线失败: {_sanitize_error(e)}", 500)

        if df is None:
            return self._send_error(f"无法获取 {symbol} 的K线数据", 404)

        # 计算指标
        try:
            df, indicators = compute_all_indicators(df, period)
        except Exception as e:
            log.warning(f"指标计算失败 {symbol} {period}: {e}")
            return self._send_error(f"指标计算失败: {_sanitize_error(e)}", 500)

        # 动力系统(Elder impulse) 方向改用 AlphaFeed 前复权日K (与 v7 口径一致);
        # 主图 OHLC / 其他指标仍用未复权真实价。缺前复权数据时回退现有未复权 impulse。
        if period == "1d":
            impulse_qfq = _fetch_impulse_qfq(symbol, count)
            if impulse_qfq is not None:
                aligned = impulse_qfq.reindex(df.index)
                df["impulse"] = aligned.fillna(df["impulse"]).astype(int)

        # ETF 溢价率 (日/周/月; 分钟净值无法对齐)
        premium_data = None
        is_etf = _is_etf(symbol)
        if is_etf and period not in MINUTE_PERIODS:
            nav_df = _fetch_etf_nav(symbol)
            if nav_df is not None and len(nav_df) > 0:
                # 对齐日期: 取最近匹配的净值
                df_sorted = df.sort_index()
                premiums = []
                for idx in df_sorted.index:
                    # 找 idx 当天或之前最近的净值
                    nav_date = idx.date() if hasattr(idx, 'date') else idx
                    nav_matches = nav_df[nav_df.index <= idx]
                    if len(nav_matches) > 0:
                        nav_val = float(nav_matches.iloc[-1]["nav"])
                        close_val = float(df_sorted.loc[idx, "close"])
                        prem = (close_val - nav_val) / nav_val * 100 if nav_val > 0 else None
                    else:
                        prem = None
                    premiums.append(prem)
                # 回填到 df (按原始顺序)
                prem_series = pd.Series(premiums, index=df_sorted.index)
                premium_data = {"values": _safe_list(prem_series), "params": {"source": "akshare fund_open_fund_info_em"}}

        # 构建响应
        klines = []
        for idx, row in df.iterrows():
            date_str = str(idx)
            if hasattr(idx, "strftime"):
                date_str = idx.strftime("%Y-%m-%d %H:%M" if period in MINUTE_PERIODS else "%Y-%m-%d")
            entry = {
                "date": date_str,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "volume": _safe_int(row.get("volume")),
                "amount": _safe_float(row.get("amount")),
                "ma5": _safe_float(row.get("ma5")),
                "ma10": _safe_float(row.get("ma10")),
                "ma20": _safe_float(row.get("ma20")),
                "macd_dif": _safe_float(row.get("macd_dif")),
                "macd_dea": _safe_float(row.get("macd_dea")),
                "macd_hist": _safe_float(row.get("macd_hist")),
                "rsi6": _safe_float(row.get("rsi6")),
                "rsi12": _safe_float(row.get("rsi12")),
                "rsi24": _safe_float(row.get("rsi24")),
                "kdj_k": _safe_float(row.get("kdj_k")),
                "kdj_d": _safe_float(row.get("kdj_d")),
                "kdj_j": _safe_float(row.get("kdj_j")),
                "atr14": _safe_float(row.get("atr14")),
                "ema13": _safe_float(row.get("ema13")),
                "impulse": _safe_int(row.get("impulse")),
            }
            klines.append(entry)

        # 把溢价率写回 klines (加上长度保护断言)
        if premium_data:
            prem_vals = premium_data["values"]
            for i, k in enumerate(klines):
                if i < len(prem_vals) and prem_vals[i] is not None:
                    k["premium"] = prem_vals[i]
                else:
                    k["premium"] = None

        resp = {
            "symbol": symbol,
            "name": name,
            "period": period,
            "count": len(klines),
            "is_etf": is_etf,
            "is_index": _is_index_symbol(symbol),
            "macd_params": indicators["macd"]["params"],
            "klines": klines,
            "meta": {
                "cached": False,
                "server_time": str(market_hours.now()),
                "last_trade_date": klines[-1]["date"] if klines else None,
            }
        }

        # 缓存
        cache.set(cache_key, resp)

        # 同时获取快照
        try:
            quote = fetch_quote(symbol)
            if quote:
                resp["quote"] = quote
        except Exception:
            pass

        self._send_json(resp)

    # ── API: 实时快照 ──
    def _handle_quote(self, params):
        symbol_raw = params.get("symbol", [None])[0]
        if not symbol_raw:
            return self._send_error("缺少 symbol 参数")
        symbol = normalize_symbol(symbol_raw)
        try:
            quote = fetch_quote(symbol)
            if quote is None:
                return self._send_error(f"无法获取 {symbol} 的快照", 404)
            self._send_json(quote)
        except Exception as e:
            self._send_error(f"获取快照失败: {_sanitize_error(e)}", 500)

    # ── API: 批量实时快照 ──
    def _handle_quotes(self, params):
        raw = params.get("symbols", [""])[0]
        symbols = [s.strip() for s in raw.split(",") if s.strip()]
        if not symbols:
            return self._send_error("缺少 symbols 参数")
        fresh = params.get("fresh", ["0"])[0].lower() in ("1", "true", "yes")
        try:
            quotes = fetch_quotes(symbols, fresh=fresh)
            self._send_json(quotes)
        except Exception as e:
            self._send_error(f"获取快照失败: {_sanitize_error(e)}", 500)

    # ── API: 日内分钟线 (用 batch 绕过单点权限限制) ──
    def _handle_intraday(self, params):
        symbol_raw = params.get("symbol", [None])[0]
        if not symbol_raw:
            return self._send_error("缺少 symbol 参数")
        symbol = normalize_symbol(symbol_raw)
        period = params.get("period", ["5m"])[0]
        count = min(int(params.get("count", ["120"])[0]), 250)
        try:
            af = get_af()
            dfs = af.klines.batch([symbol], period=period, count=count, adjust="none", to_dataframe=True)
            df = dfs.get(symbol)
            if df is None or len(df) == 0:
                return self._send_error(f"无法获取 {symbol} 的分钟线", 404)
            # 保存原始时间列再标准化
            time_col = "trade_time" if "trade_time" in df.columns else "trade_date"
            raw_times = df[time_col].astype(str).str[-8:-3] if time_col in df.columns else None
            df = _normalize(df, prefer_time=True)
            if df is None:
                return self._send_error("数据不足", 404)
            # 分时图显示最近一个交易日: 非交易日(周末/节假日)回退到上一交易日
            last_day = df.index.normalize().max()
            mask = df.index.normalize() == last_day
            df = df[mask]
            if raw_times is not None:
                raw_times = raw_times[mask]
            if len(df) == 0:
                return self._send_error("无分时数据", 404)
            # 基于过滤后的数据重新计算指标
            df, indicators = compute_all_indicators(df, period="1d", with_atr_val=True)
            bars = []
            for i, (idx, row) in enumerate(df.iterrows()):
                ts = raw_times.iloc[i] if raw_times is not None and i < len(raw_times) else str(idx)
                if ts and not ts[0].isdigit(): ts = str(idx)[-8:-3]
                bars.append({
                    "time": ts,
                    "open": _safe_float(row.get("open")),
                    "high": _safe_float(row.get("high")),
                    "low": _safe_float(row.get("low")),
                    "close": _safe_float(row.get("close")),
                    "volume": _safe_int(row.get("volume")),
                    "macd_dif": indicators["macd"]["dif"][i],
                    "macd_dea": indicators["macd"]["dea"][i],
                    "macd_hist": indicators["macd"]["hist"][i],
                    "kdj_k": indicators["kdj"]["k"][i] if "kdj" in indicators else None,
                    "kdj_d": indicators["kdj"]["d"][i] if "kdj" in indicators else None,
                    "kdj_j": indicators["kdj"]["j"][i] if "kdj" in indicators else None,
                    "rsi6": indicators["rsi"]["rsi6"][i] if "rsi" in indicators else None,
                    "rsi12": indicators["rsi"]["rsi12"][i] if "rsi" in indicators else None,
                    "rsi24": indicators["rsi"]["rsi24"][i] if "rsi" in indicators else None,
                    "atr14": indicators["atr"]["values"][i] if "atr" in indicators else None,
                })
            self._send_json({"symbol": symbol, "period": period, "bars": bars})
        except Exception as e:
            self._send_error(f"获取分钟线失败: {_sanitize_error(e)}", 500)

    # ── API: 质押数据 ──
    def _handle_pledge(self, params):
        symbol_raw = params.get("symbol", [None])[0]
        if not symbol_raw:
            return self._send_error("缺少 symbol 参数")
        symbol = normalize_symbol(symbol_raw)
        if _is_index_symbol(symbol):
            # 指数无质押数据, 避免与同号个股撞码 (000001.SH 上证指数 vs 000001.SZ 平安银行)
            return self._send_json({"symbol": symbol, "pledge": None})
        code = symbol.split(".")[0]
        pledge = _load_pledge()
        data = pledge.get(code)
        if data:
            self._send_json({"symbol": symbol, "pledge": data})
        else:
            self._send_json({"symbol": symbol, "pledge": None})

    # ── API: 搜索 ──
    def _handle_search(self, params):
        q = params.get("q", [""])[0].strip()
        if not q:
            return self._send_json({"results": []})
        results = _search_stocks(q)
        self._send_json({"results": results})


# ── 入口 ──
def main():
    parser = argparse.ArgumentParser(description="Visual K线图 HTTP 服务器")
    parser.add_argument("--port", type=int, default=8888, help="监听端口 (default: 8888)")
    parser.add_argument("--host", type=str, default="localhost", help="监听地址 (default: localhost)")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), VisualHandler)

    # 初始化交易记录数据库
    try:
        trades.init_db()
        log.info(f"交易记录数据库已就绪: {trades._db_path}")
    except Exception as e:
        log.warning(f"交易记录数据库初始化失败: {e}")

    # 管理员引导: .env 为口令权威来源。无管理员则创建, 已有则同步 (改 .env 密码后重启即生效)
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
                else:  # not_found: 已有其他管理员, 忽略 .env 中的该用户名
                    log.warning(f"已存在其他管理员, 忽略 .env 的 {admin_user}")
        except Exception as e:
            log.warning(f"管理员账号同步失败: {e}")
    elif admin_user or admin_pass:
        log.warning("ADMIN_USERNAME 与 ADMIN_PASSWORD 需同时设置")
    else:
        log.warning("未设置 ADMIN_USERNAME/ADMIN_PASSWORD，无管理员时无法创建用户")

    # 启动时清理磁盘缓存
    try:
        _disk_cache.cleanup()
        log.info(f"磁盘缓存已清理 (上限 {CACHE_MAX_MB}MB)")
    except Exception:
        pass

    print(f"""
╔══════════════════════════════════════════╗
║   📈 Visual K线图 股票可视化              ║
║   数据源: 麦蕊(日K) + AlphaFeed(分时/分钟) ║
║   地址: http://{args.host}:{args.port}             ║
║   速率限制: {RATE_LIMIT_PER_MIN} 次/分钟           ║
╚══════════════════════════════════════════╝
""", flush=True)

    # 后台预热: 异步加载全量股票列表
    def _warmup():
        import threading
        t = threading.Thread(target=_load_stock_list, daemon=True)
        t.start()
        return t
    _warmup()

    # 每日 15:30 定时刷新全市场质押数据
    threading.Thread(target=_pledge_scheduler, daemon=True).start()

    # 持仓监控: 交易时段按代码快照轮询, 仅管理员/授权用户
    try:
        import monitor as _mon
        _mon.start_background(get_af, fallback_quotes=fetch_quotes)
        log.info("持仓监控线程已启动")
    except Exception as e:
        log.warning(f"持仓监控启动失败: {e}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("服务器已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
