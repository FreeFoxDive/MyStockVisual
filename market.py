"""行情/缓存/质押等数据层 (从 server 拆出, 无 HTTP 依赖)。"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

log = logging.getLogger("market")

from logger import redact_message, sanitize_error as _sanitize_error  # noqa: E402
import datetime as dt_mod
import decimal as dec_mod
import feed
import kline_source  # noqa: E402  (K线数据源注册/回退路由; kline_source 惰性反向引用本模块)
import market_hours  # noqa: E402

# ── AlphaFeed ──
AF_API_KEY = os.environ.get("AF_API_KEY", "")
if not AF_API_KEY:
    log.warning("未设置 AF_API_KEY 环境变量")
else:
    log.info("AF_API_KEY 已加载")

# SDK 超时显式化(默认值与各 SDK 默认一致, 零行为变更; 可用环境变量收紧)。
# alphafeed 默认 30s; mairui 默认 (connect 5s, read 60s)。
AF_TIMEOUT_SEC = float(os.environ.get("AF_TIMEOUT_SEC", "30"))
MAIRUI_TIMEOUT = (5.0, float(os.environ.get("MAIRUI_READ_TIMEOUT_SEC", "60")))

_af = None
_af_lock = threading.Lock()


def get_af():
    global _af
    if _af is None:
        with _af_lock:
            if _af is None:
                from alphafeed import AlphaFeed
                _af = AlphaFeed(api_key=AF_API_KEY, timeout=AF_TIMEOUT_SEC)
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


# ── 麦蕊 429 熔断退避 ──
# 官方限流: 同一证书在 60s 滑动窗口内最多允许 2 个不同来源 IP, 超出返回 429
# (错误码 103, 与日额度无关 —— 白金版日额度"无限")。本机同时存在 Xray 隧道与
# WLAN 两条默认路由, 同一证书的请求会从不同出口 IP 发出, 因此极易命中。
# 命中后进程内全局退避, 窗口内不再发 HTTP, 直接走既有回退链。
MAIRUI_BACKOFF_SEC = float(os.environ.get("MAIRUI_BACKOFF_SEC", "60"))
_mr_backoff_until = 0.0
_mr_backoff_lock = threading.Lock()


class MairuiBackoff(RuntimeError):
    """429 退避窗口内主动跳过 (未发 HTTP)。"""


def _mr_backoff_remaining():
    """退避剩余秒数; 0 表示可正常请求。"""
    return max(0.0, _mr_backoff_until - time.time())


def _mr_retry_after_sec(e):
    """尽力取 Retry-After: requests 异常在 .response.headers, urllib 在 .headers。"""
    headers = getattr(getattr(e, "response", None), "headers", None) or getattr(e, "headers", None)
    try:
        ra = str(headers.get("Retry-After", "")).strip() if headers else ""
    except Exception:
        return None
    return float(ra) if ra.isdigit() else None


def _mr_is_429(e):
    """只认状态码, 不用子串匹配 (000429.SZ 这类代码会误伤)。"""
    code = getattr(e, "status_code", None)
    if code is None:
        code = getattr(e, "code", None)          # urllib.error.HTTPError
    return code == 429


def _mr_note_429(e):
    """记录 429 并开启进程级退避窗口。"""
    global _mr_backoff_until
    sec = max(1.0, _mr_retry_after_sec(e) or MAIRUI_BACKOFF_SEC)
    with _mr_backoff_lock:
        _mr_backoff_until = max(_mr_backoff_until, time.time() + sec)
    payload = getattr(e, "payload", None)
    detail = f" payload={redact_message(payload)}" if payload is not None else ""
    log.warning(f"麦蕊触发 429 限流, 退避 {sec:.0f}s (窗口内不再请求, 走回退){detail}")


class _MairuiClient:
    """mairui Client 代理: 所有 SDK 调用统一受 429 熔断退避约束。

    __getattr__ 透传 Client 的公开方法; 调用前检查退避窗口 (命中则抛
    MairuiBackoff, 不发 HTTP), 调用后识别 429 开启退避并原样抛出,
    使各调用点既有的 except → 回退语义保持不变。
    """

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def _guarded(*args, **kwargs):
            left = _mr_backoff_remaining()
            if left > 0:
                raise MairuiBackoff(f"麦蕊 429 退避中, 剩余 {left:.0f}s")
            try:
                return attr(*args, **kwargs)
            except Exception as e:
                if _mr_is_429(e):
                    _mr_note_429(e)
                raise

        return _guarded


def _mr_urlopen_json(url, timeout=8):
    """直接 HTTP 调麦蕊 (SDK 未封装接口), 同样受 429 熔断退避约束。"""
    left = _mr_backoff_remaining()
    if left > 0:
        raise MairuiBackoff(f"麦蕊 429 退避中, 剩余 {left:.0f}s")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _mr_note_429(e)
        raise


def get_mr():
    global _mr
    if _mr is None:
        with _mr_lock:
            if _mr is None:
                from mairui import Client
                _mr = _MairuiClient(Client(licence=MAIRUI_API_KEY, timeout=MAIRUI_TIMEOUT))
    return _mr


# ── 静态列表缓存 (指数/股票/港美股: 内存 + 磁盘, 24h TTL, 用到时刷新) ──
# 静态信息变化缓慢, 一天一更新即可。取用顺序: 内存新鲜 → 直接返回; 否则读磁盘
# (正常重启零联网); 磁盘也过期/缺失才在锁内同步刷新一次 (single-flight, 无后台线程)。
# 刷新失败保留旧数据并退避 STATIC_LIST_RETRY_DELAY, 避免每个请求都阻塞重试。
STATIC_LIST_TTL = 24 * 3600
STATIC_LIST_RETRY_DELAY = 300.0


class _StaticListCache:
    """静态列表的 内存+磁盘 缓存 (24h TTL, 用到时同步刷新)。

    fetcher() 返回非空 list 视为成功; 空/异常视为失败 (保留旧数据)。
    磁盘格式 {"rows": [...], "ts": ...}, 原子写 (临时文件 + replace)。
    """

    def __init__(self, name, path, fetcher, ttl=STATIC_LIST_TTL,
                 retry_delay=STATIC_LIST_RETRY_DELAY):
        self.name = name
        self.path = path
        self.fetcher = fetcher
        self.ttl = ttl
        self.retry_delay = retry_delay
        self._lock = threading.Lock()
        self._data = None
        self._ts = 0.0
        self._fail_at = 0.0

    @property
    def ts(self):
        """当前内存数据的时间戳 (0 = 无可用数据)。"""
        return self._ts

    def _read_disk(self):
        """读磁盘缓存, 返回 (rows, ts); 失败/无则 (None, 0)。"""
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("rows"):
                    return raw["rows"], raw.get("ts", 0)
        except Exception as e:
            log.warning(f"读取{self.name}缓存失败: {redact_message(e)}")
        return None, 0

    def _write_disk(self, rows, ts):
        """原子写磁盘 (临时文件 + replace)。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"rows": rows, "ts": ts}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            log.warning(f"写入{self.name}缓存失败: {redact_message(e)}")

    def _cached(self):
        """内存/磁盘里的旧值 (不联网): 内存优先, 其次磁盘。"""
        if self._data is not None:
            return self._data, self._ts
        rows, ts = self._read_disk()
        if rows:
            self._data, self._ts = rows, ts
        return rows, ts

    def get(self):
        """取静态列表; 过期则同步刷新一次。永不抛异常, 取不到返回 []。"""
        now = time.time()
        if self._data is not None and now - self._ts < self.ttl:
            return self._data

        rows, ts = self._cached()
        if rows and now - ts < self.ttl:
            return rows

        with self._lock:
            now = time.time()
            if self._data is not None and now - self._ts < self.ttl:
                return self._data
            if self._fail_at and now - self._fail_at < self.retry_delay:
                return self._data or rows or []
            try:
                fresh = self.fetcher()
            except Exception as e:
                log.warning(f"刷新{self.name}失败: {redact_message(e)}")
                fresh = None
            if not fresh:
                self._fail_at = now
                if self._data is None and rows:
                    self._data, self._ts = rows, ts
                return self._data or []
            ts = time.time()
            self._data, self._ts, self._fail_at = fresh, ts, 0.0
            self._write_disk(fresh, ts)
            log.info(f"已加载{self.name}: {len(fresh)} 条")
            return fresh


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
        log.warning("查询额度失败: %s", _sanitize_error(e))
        return {"ok": False, "error": "查询额度失败，请稍后重试"}

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


# ── 指数集合 + 名称映射 (派生缓存, 底层为 24h 磁盘缓存) ──
_index_symbols = None      # 派生: 指数 symbol 集合 (None = 尚未派生)
_index_names = {}          # 派生: symbol → 指数名称
_index_derived_ts = None   # 派生自哪一版列表 (_index_cache.ts)
_index_lock = threading.Lock()
_name_map = None
_name_map_ts = 0.0     # _name_map 构建时的股票列表时间戳, 跟随列表刷新重建
_name_map_lock = threading.Lock()

INDEX_LIST_FILE = SCRIPT_DIR / ".cache" / "index_list.json"
_index_cache = _StaticListCache("指数列表", INDEX_LIST_FILE, lambda: get_mr().index_list())


def _index_rows_to_cache(rows):
    """rows → (symbols, names); 无有效行返回 (None, None)。"""
    symbols, names = set(), {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("dm", "")).strip().upper()
        name = str(r.get("mc", "")).strip()
        if sym:
            symbols.add(sym)
            if name:
                names[sym] = name
    return (symbols, names) if symbols else (None, None)


def _load_index_cache():
    """沪深指数列表 (symbol 集合 + symbol→名称)。

    底层 _index_cache 是 24h 磁盘缓存: 正常重启零联网, 过期才同步刷新一次。
    派生结果按缓存版本 (_index_cache.ts) 记忆, _is_index_symbol 热路径零开销。
    返回空集合 = 未知 (既不能确认是指数, 也不能确认不是); 空集合不固化 ——
    固化会让进程内所有指数都被当股票路由 (index_history → stock_history)。
    """
    global _index_symbols, _index_names, _index_derived_ts
    rows = _index_cache.get()
    ts = _index_cache.ts
    if rows and ts != _index_derived_ts:
        with _index_lock:
            if ts != _index_derived_ts:
                symbols, names = _index_rows_to_cache(rows)
                if symbols:
                    _index_symbols, _index_names = symbols, names
                _index_derived_ts = ts
    return _index_symbols if _index_symbols is not None else set()


def _is_index_symbol(symbol):
    """判断 symbol 是否为沪深指数 (如 000001.SH 上证指数)"""
    return symbol in _load_index_cache()


def _lookup_name(symbol):
    """查标的名称: 指数 → 股票/ETF 列表。找不到返回 symbol 本身。

    名称映射跟随股票列表的 24h 刷新重建 (比较时间戳), 否则新股上市、
    更名、ST 戴帽/摘帽永远反映不到 quote 与 kline 的 name。
    """
    if _is_index_symbol(symbol):
        return _index_names.get(symbol, symbol)
    global _name_map, _name_map_ts
    if _name_map is None or _stock_list_time > _name_map_ts:
        with _name_map_lock:
            if _name_map is None or _stock_list_time > _name_map_ts:
                stocks = _load_stock_list()
                if stocks:
                    _name_map = {s["symbol"]: s["name"] for s in stocks}
                    _name_map_ts = _stock_list_time or time.time()
    return (_name_map or {}).get(symbol, symbol)


# ── 指标计算 ──
from indicators import compute_all_indicators, _safe_list, force_index

import market_hours

# ── 交易记录 ──
import trades


# ── 磁盘缓存 ──
CACHE_DIR = SCRIPT_DIR / ".cache" / "klines"
CACHE_MAX_MB = 50


class DiskCache:
    """K线数据磁盘缓存 (JSON.gz, TTL + 总大小限制)"""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _key(self, symbol, period, count, adjust="forward", chain_tag=""):
        tag = kline_source.adjust_tag(adjust)
        suffix = f"_{chain_tag}" if chain_tag else ""
        name = f"{symbol}_{period}_{count}_{tag}{suffix}.json.gz"
        base = os.path.realpath(CACHE_DIR)
        fp = os.path.realpath(os.path.join(base, name))
        if not fp.startswith(base + os.sep):
            raise ValueError("invalid cache key")
        return Path(fp)

    def get(self, symbol, period, count, ttl_seconds, adjust="forward", chain_tag=""):
        fp = self._key(symbol, period, count, adjust, chain_tag)
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

    def set(self, symbol, period, count, data, adjust="forward", chain_tag=""):
        fp = self._key(symbol, period, count, adjust, chain_tag)
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

    def set(self, key, data, fetched_at=None):
        at = time.time() if fetched_at is None else fetched_at
        with self._lock:
            # 如果已存在, 更新并移到末尾
            if key in self._cache:
                self._cache[key] = {"data": data, "time": at}
                self._cache.move_to_end(key)
                return
            # 超过上限: 淘汰最旧的 (OrderedDict popitem(last=False))
            if len(self._cache) >= self._max_entries:
                try:
                    self._cache.popitem(last=False)
                except KeyError:
                    pass
            self._cache[key] = {"data": data, "time": at}

    def clear(self):
        with self._lock:
            self._cache.clear()


# 分钟 K 线 (AlphaFeed 原生周期; 不做 3m 合成)
MINUTE_PERIODS = frozenset({"1m", "5m", "15m", "30m", "60m"})
MINUTE_COUNTS = {"1m": 1200, "5m": 480, "15m": 320, "30m": 320, "60m": 1000}

# 缓存: 日K 120s, 分钟K 60s, 周/月K 300s, 快照 1.25s, 上限 500 条目
kline_cache = TTLCache(ttl_seconds=120)
kline_cache_minute = TTLCache(ttl_seconds=60)
kline_cache_long = TTLCache(ttl_seconds=300)
# Shared across page connections; 60/min package, reserve 1/5 for other work.
from live_budget import PacedBudget
QUOTE_RATE_PER_MIN = 48
_quote_budget = PacedBudget(QUOTE_RATE_PER_MIN)
_quote_fetch_lock = threading.Lock()
_quote_interest_lock = threading.Lock()
_quote_interests = {}
_quote_cursor = 0
quote_cache = TTLCache(ttl_seconds=1.25)


def register_quote_interest(token, symbols):
    with _quote_interest_lock:
        _quote_interests[token] = tuple(symbols)


def unregister_quote_interest(token):
    with _quote_interest_lock:
        _quote_interests.pop(token, None)


class _QuoteResult(dict):
    """Budget deferral is not an upstream failure: do not fan out to backup APIs."""
    def __init__(self):
        super().__init__()
        self.deferred = set()


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
    from datetime import timedelta
    # 必须用北京墙钟: date.today() 在 UTC 容器上北京时间 0-8 点仍是"昨天",
    # 会晚 8 小时发现新数据、兜底日期也标错。
    today = market_hours.now().date()
    df = None
    got_date = None
    # 收盘后数据有延迟, 回溯最近 5 天找最新有数据的交易日
    for offset in range(5):
        d = (today - timedelta(days=offset)).strftime("%Y%m%d")
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
            got_date = today.strftime("%Y%m%d")
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
    if _symbol_market(symbol) != "cn":
        return False  # 港/美股不走 A 股代码前缀判定 (港股 11/15 开头会误判)
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
        rows = _mr_urlopen_json(url)
    except Exception as e:
        log.warning(f"麦蕊获取ETF K线失败 {symbol}: {redact_message(e)}")
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


def _af_adjust(adj):
    """内部复权口径 → AlphaFeed API 词汇: hfq 即后复权 = backward。"""
    adj = kline_source.normalize_adjust(adj)
    return "backward" if adj == kline_source.ADJUST_HFQ else adj


def _fetch_minute_kline(symbol, period, count, adjust="forward"):
    """从 AlphaFeed 拉取分钟 K 线, 返回标准化 DataFrame 或 None。"""
    adj = _af_adjust(adjust)
    # 港/美股 K线限频 (额度 10/min 的 4/5); 桶空返回 None 走缓存/回退
    if _symbol_market(symbol) in ("hk", "us"):
        with _hkus_lock:
            if not _hkus_kline_bucket.try_acquire():
                log.warning(f"港美股K线限频跳过 {symbol} {period}")
                return None
    try:
        af = get_af()
        dfs = af.klines.batch(
            [symbol], period=period, count=count, adjust=adj, to_dataframe=True
        )
        df = dfs.get(symbol) if dfs else None
    except Exception as e:
        log.warning(f"AlphaFeed 获取分钟K线失败 {symbol} {period}: {e}")
        return None
    if df is None or len(df) == 0:
        return None
    return _normalize(df, prefer_time=True)


def _fetch_af_kline(symbol, period, count, adjust="forward"):
    """从 AlphaFeed 拉取日/周/月K (股票/ETF), 返回标准化 DataFrame 或 None。

    AlphaFeed 原生支持 1d/1w/1M; 股票/ETF 日K为备选源, 周/月K为主源
    (周/月K不依赖抖动的 akshare)。
    """
    adj = _af_adjust(adjust)
    try:
        af = get_af()
        dfs = af.klines.batch(
            [symbol], period=period, count=count, adjust=adj, to_dataframe=True
        )
        df = dfs.get(symbol) if dfs else None
    except Exception as e:
        log.warning(f"AlphaFeed 获取K线失败 {symbol} {period}: {e}")
        return None
    if df is None or len(df) == 0:
        return None
    return _normalize(df)


MR_FSJY_PERIODS = {"5m", "15m", "30m", "60m"}


def _fetch_mr_minute_kline(symbol, period, count):
    """从麦蕊分时交易接口 (hszbl/fsjy, SDK 未封装) 拉取分钟K。

    实测 (2026-09-05): 5m/15m/30m/60m 可用 (PAID 证书),
    1m 不支持 (HTTP 422), 北交所 404; st/et/lt 查询参数无效, 返回固定窗口
    (5m 上限 1488 根), 本地 tail 截取。字段 {d:"YYYY-MM-DD HH:MM",
    o,h,l,c, v:手, e:成交额元}。数据窗口可能滞后 (当时冻结在 2026-04-30),
    路由层分钟新鲜度守卫会把末根过旧的返回视为失败继续回退。
    """
    if symbol.endswith(".BJ") or period not in MR_FSJY_PERIODS:
        return None
    # fsjy 路径用带交易所后缀的代码 (probe 实测 600519.SH 可用, 同 stock_history 风格)
    url = f"https://api.mairuiapi.com/hszbl/fsjy/{symbol}/{period}/{MAIRUI_API_KEY}"
    try:
        rows = _mr_urlopen_json(url)
    except Exception as e:
        log.warning(f"麦蕊获取分钟K线失败 {symbol} {period}: {redact_message(e)}")
        return None

    # dict = 错误响应, 空列表 = 无数据
    if not rows or isinstance(rows, dict):
        return None

    df = pd.DataFrame(rows)
    df = df.drop(columns=["zf", "hs", "zd", "zde", "ud"], errors="ignore")
    df = df.rename(columns={
        "o": "open", "h": "high", "l": "low", "c": "close",
        "e": "amount", "v": "volume", "d": "trade_time",
    })
    df = _normalize(df, prefer_time=True)
    if df is not None and count:
        df = df.tail(count)
    return df


def _fetch_mr_kline(symbol, period, count, adjust="forward"):
    """从麦蕊 SDK 拉取指数/股票 日/周/月K, 返回标准化 DataFrame 或 None。

    股票复权: forward→等比前复权 fr, none→不复权 n (与 AlphaFeed forward 对齐)。
    指数无复权参数。
    """
    api = get_mr()
    mr_period = {"1d": "d", "1w": "w", "1M": "m"}.get(period, "d")
    adj = kline_source.normalize_adjust(adjust)
    mr_div = "fr" if adj == kline_source.ADJUST_FORWARD else "n"
    try:
        if _is_index_symbol(symbol):
            rows = api.index_history(symbol, mr_period, lt=count)
        else:
            rows = api.stock_history(symbol, mr_period, mr_div, lt=count)
    except Exception as e:
        log.warning(f"麦蕊获取K线失败 {symbol}: {e}")
        return None

    # dict = 错误响应 (如 {"error": "数据不存在"}), 空列表 = 无数据
    if not rows or isinstance(rows, dict):
        return None

    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "o": "open", "h": "high", "l": "low", "c": "close",
        "a": "amount", "v": "volume", "t": "trade_date",
    })
    return _normalize(df)


def _kline_category(symbol, period):
    """K线类别路由 (kline_source 按类别选源): minute / fund(ETF) / index / stock / hk / us。"""
    if _symbol_market(symbol) in ("hk", "us"):
        return _symbol_market(symbol)
    if period in MINUTE_PERIODS:
        return "minute"
    if _is_etf(symbol):
        return "fund"
    if _is_index_symbol(symbol):
        return "index"
    return "stock"


def fetch_kline(symbol, period, count, adjust="forward"):
    """获取 K 线数据（优先磁盘缓存），返回标准化 DataFrame。

    数据源路由由 kline_source 注册表承担 (见 fetch_kline_ex):
    图表默认前复权; 成交校验等传 adjust="none"。
    """
    df, name, _source = fetch_kline_ex(symbol, period, count, adjust=adjust)
    return df, name


def fetch_kline_ex(symbol, period, count, adjust="forward"):
    """fetch_kline 完整版, 额外返回实际服务的数据源名 (观测/透传 meta 用)。

    返回 (标准化 DataFrame, 名称, 数据源名); 失败 (None, None, None)。
    """
    adj = kline_source.normalize_adjust(adjust)
    category = _kline_category(symbol, period)
    ct = kline_source.chain_tag(category)  # 缓存按数据源链隔离, 改链即失效
    # 检查磁盘缓存 (日K/分钟 盘中 60s/盘后 300s, 周月K 600s)
    now = market_hours.now()
    in_trading = market_hours.in_session(now)
    if period in MINUTE_PERIODS:
        ttl = 60 if in_trading else 300
    elif period == "1d":
        ttl = 60 if in_trading else 300
    else:
        ttl = 600
    cached = _disk_cache.get(symbol, period, count, ttl, adjust=adj, chain_tag=ct)
    if cached:
        df = pd.DataFrame(cached["data"])
        if period == "1d":
            # concat 后索引名可能丢失，落盘列名为 index；统一走 _normalize
            df = _normalize(df, prefer_time=False)
            if df is None:
                cached = None
            else:
                df = _strip_today_bar_df(df)
                df = _maybe_append_today_bar(symbol, df)
                return df, cached.get("name", symbol), cached.get("source")
        else:
            # 分钟线优先 trade_time: JSON 常同时带 trade_date(日) 与 trade_time,
            # 若先按 date 建索引, 同日多根重复 → RSI get_loc 返回 slice
            if "trade_time" in df.columns:
                df["trade_time"] = pd.to_datetime(df["trade_time"])
                df = df.set_index("trade_time")
            elif "trade_date" in df.columns:
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df = df.set_index("trade_date")
            df = df.sort_index()
            return df, cached.get("name", symbol), cached.get("source")

    df, source = kline_source.fetch_kline_df(
        category, symbol, period, count, adjust=adj
    )
    name = _lookup_name(symbol) if df is not None else None

    if df is None:
        return None, None, None

    if period == "1d":
        df = _maybe_append_today_bar(symbol, df)

    # 存入磁盘缓存 (日K 当日 bar 不写入，默认 dirty)
    cache_df = _strip_today_bar_df(df) if period == "1d" else df
    if cache_df is not None and len(cache_df) > 0:
        out = cache_df.reset_index()
        if period in MINUTE_PERIODS:
            # 分钟线索引是 trade_time，读缓存也优先 trade_time。
            # 勿把 index 列强行改成 trade_date：AF 数据常同时带日列 trade_date，会重复列名
            # → to_json(orient="records") 报 ValueError。
            if out.columns[0] != "trade_time":
                out = out.rename(columns={out.columns[0]: "trade_time"})
        elif out.columns[0] != "trade_date":
            out = out.rename(columns={out.columns[0]: "trade_date"})
        cache_data = {
            "name": name or symbol,
            "source": source,
            "data": json.loads(out.to_json(orient="records", date_format="iso")),
        }
        try:
            _disk_cache.set(symbol, period, count, cache_data, adjust=adj, chain_tag=ct)
        except Exception:
            pass

    return df, name or symbol, source


def _mr_quote_to_std(q, symbol):
    """把麦蕊实时行情 dict 转成标准 quote dict"""
    return {
        "symbol": symbol,
        "last_price": _safe_float(q.get("p")),
        "prev_close": _safe_float(q.get("yc")),
        "open": _safe_float(q.get("o")),
        "high": _safe_float(q.get("h")),
        "low": _safe_float(q.get("l")),
        "volume": _safe_int(q.get("v")),
        "amount": _safe_float(q.get("cje")),
        "change_pct": _safe_float(q.get("pc")),    # 麦蕊实时 pc = 涨跌幅%
        "amplitude": _safe_float(q.get("zf")),     # zf = 振幅%
        # 基金/股票实时接口通常用 hs，部分接口才提供 tr；两者均为百分数。
        "turnover_rate": _safe_float(q.get("tr") if q.get("tr") is not None else q.get("hs")),
        "vol_ratio": _safe_float(q.get("lb")),  # 量比 (实时接口)
        "timestamp": _safe_epoch(q.get("t")),  # 快照更新时间 (epoch 秒, 无则 None)
        "name": _lookup_name(symbol),
    }


MR_QUOTE_BATCH = 20        # 麦蕊 stock_ssjy_more 单次最多 20 只


def _af_quote_valid(q):
    if not q:
        return False
    if _safe_float(q.get("last_price")) is None:
        return False
    if _safe_float(q.get("high")) is None or _safe_float(q.get("low")) is None:
        return False
    return True


def _af_pct(v):
    """AlphaFeed ext 比率(小数) → 百分数; None 原样返回。"""
    f = _safe_float(v)
    return None if f is None else f * 100.0


def _af_quote_to_std(q, symbol):
    """把 AlphaFeed quote dict 转成标准 quote dict（与 _mr_quote_to_std 字段一致）。"""
    return {
        "symbol": symbol,
        "last_price": _safe_float(q.get("last_price")),
        "prev_close": _safe_float(q.get("prev_close")),
        "open": _safe_float(q.get("open")),
        "high": _safe_float(q.get("high")),
        "low": _safe_float(q.get("low")),
        "volume": _safe_int(q.get("volume")),
        "amount": _safe_float(q.get("amount")),
        "change_pct": _safe_float(q.get("change_pct")),
        "amplitude": _af_pct(q.get("amplitude")),        # 小数 → %
        "turnover_rate": _af_pct(q.get("turnover_rate")),  # 小数 → %
        "vol_ratio": _safe_float(q.get("vol_ratio")),
        "timestamp": _safe_epoch(q.get("timestamp")),  # epoch 秒 (交易所时间)
        "name": q.get("name") or _lookup_name(symbol),
    }


# ── 标的元数据 (AlphaFeed instruments): 流通/总股本/类型 ──
_instrument_meta_cache = {}      # symbol -> (ts, meta|None)
_instrument_meta_lock = threading.Lock()
INSTRUMENT_META_TTL = 24 * 3600  # 股本/类型变化缓慢, 24h 足够


def _fetch_instrument_meta(symbol):
    """AlphaFeed 标的元数据 (float_shares/total_shares/type), 24h 内存缓存。

    失败返回 None (静默降级: 前端隐藏流值/份额, 不影响 K 线)。
    """
    if not AF_API_KEY:
        return None
    now = time.time()
    with _instrument_meta_lock:
        ent = _instrument_meta_cache.get(symbol)
        if ent and now - ent[0] < INSTRUMENT_META_TTL:
            return ent[1]
    try:
        inst = get_af().instruments.get(symbol)
    except Exception as e:
        log.warning(f"获取标的元数据失败 {symbol}: {_sanitize_error(e)}")
        return None
    if not isinstance(inst, dict) or not inst:
        return None
    ext = inst.get("ext") or {}
    meta = {
        "type": inst.get("type") or ext.get("type"),
        "name": inst.get("name"),
        "float_shares": _safe_float(ext.get("float_shares")),
        "total_shares": _safe_float(ext.get("total_shares")),
        "listing_date": ext.get("listing_date"),
    }
    with _instrument_meta_lock:
        _instrument_meta_cache[symbol] = (now, meta)
    return meta


def _fetch_af_quotes(symbols):
    """批量拉 AlphaFeed 快照；失败或缺 OHLCV 的 symbol 不出现在返回中。"""
    global _quote_cursor
    if not AF_API_KEY:
        return {}
    symbols = list(dict.fromkeys(s for s in symbols if s))
    if not symbols:
        return {}
    from feed import QUOTES_BATCH, _chunks, _row_to_quote

    try:
        af = get_af()
        out = _QuoteResult()
        for batch in _chunks(symbols, QUOTES_BATCH):
            if not _quote_budget.try_acquire():
                out.deferred.update(s for s in symbols if s not in out)
                break
            _quote_cursor += len(batch)
            df = af.quotes.get(symbols=batch, to_dataframe=True)
            if df is None or getattr(df, "empty", True):
                continue
            for _, row in df.iterrows():
                q = _row_to_quote(row)
                if not q or not q.get("symbol"):
                    continue
                # SDK responses may omit the exchange suffix or vary casing;
                # map back to the normalized request symbol before caching.
                sym_raw = q["symbol"]
                sym = normalize_symbol(sym_raw)
                if sym not in symbols:
                    code = str(sym_raw).split(".", 1)[0]
                    sym = next((x for x in symbols if x.split(".", 1)[0] == code), sym)
                q["symbol"] = sym
                if _af_quote_valid(q):
                    out[sym] = q
        return out
    except Exception as e:
        log.warning(f"AF 快照失败: {e}")
        return {}


def fetch_quotes(symbols, fresh=False):
    global _quote_cursor
    symbols = list(dict.fromkeys(normalize_symbol(s) for s in symbols if s))
    # A concurrent HTTP fallback cannot start a second fetch or overwrite its result.
    if not _quote_fetch_lock.acquire(blocking=False):
        return {s: q for s in symbols if (q := quote_cache.get(s)) is not None}
    try:
        with _quote_interest_lock:
            watched = {s for group in _quote_interests.values() for s in group}
        # One symbol query can serve every connected page (never a universes query).
        combined = sorted(watched.union(symbols))
        if combined:
            offset = _quote_cursor % len(combined)
            combined = combined[offset:] + combined[:offset]
        result = _fetch_quotes_locked(combined, fresh=fresh)
        return {s: result[s] for s in symbols if s in result}
    finally:
        _quote_fetch_lock.release()


def _fetch_quotes_locked(symbols, fresh=False):
    """批量获取实时快照：AlphaFeed quotes.get 优先，未命中按类型回退麦蕊。

    麦蕊回退：指数 index_real_time、ETF fund_real_time、股票 ssjy_more 批量。
    fresh=True 时跳过缓存强刷，失败/空数据回退到缓存（缓存到期的 get() 返回 None）。
    返回 {symbol: quote}；未取到的 symbol 不出现在返回字典中。
    """
    symbols = [normalize_symbol(s) for s in symbols if s]
    symbols = list(dict.fromkeys(symbols))  # 去重保序
    if not symbols:
        return {}

    fetched_at = time.time()
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

    api = get_mr()

    def _emit_mr(s, q):
        result[s] = _mr_quote_to_std(q, s)
        result[s]["_live_info"] = _live_info_for_quote(s, result[s])
        result[s]["_revision"] = time.time_ns() // 1000
        quote_cache.set(s, result[s], fetched_at=fetched_at)

    def _emit_af(s, q):
        result[s] = _af_quote_to_std(q, s)
        result[s]["_live_info"] = _live_info_for_quote(s, result[s])
        result[s]["_revision"] = time.time_ns() // 1000
        quote_cache.set(s, result[s], fetched_at=fetched_at)

    # AlphaFeed 优先（指数 / ETF / 股票统一）
    af_set = list(to_fetch)
    # 港/美股走 AF 专用令牌桶 (额度 10/min 的 4/5); 桶空则本轮跳过沿用缓存
    if any(_symbol_market(s) in ("hk", "us") for s in af_set):
        with _hkus_lock:
            if not _hkus_quote_bucket.try_acquire():
                af_set = []
    af_result = _fetch_af_quotes(af_set)
    for s, q in af_result.items():
        if s in to_fetch:
            _emit_af(s, q)

    deferred = getattr(af_result, "deferred", set())
    remaining = [s for s in to_fetch if s not in result and s not in deferred]
    if not remaining:
        for s in to_fetch:
            if s not in result:
                cached = quote_cache.get(s)
                if cached:
                    result[s] = cached
        return result

    # 按类型分组麦蕊回退
    index_codes, stock_codes, etf_codes = [], [], []
    for s in remaining:
        if _is_index_symbol(s):
            index_codes.append(s)
        elif _is_etf(s):
            etf_codes.append(s)
        else:
            stock_codes.append(s)

    # 1) 指数 (单只 index_real_time)
    for s in index_codes:
        try:
            q = api.index_real_time(s)
            if isinstance(q, dict) and not q.get("error"):
                _emit_mr(s, q)
        except Exception as e:
            log.warning(f"指数快照失败 {s}: {e}")

    # 2) ETF/基金 (单只 fund_real_time, code 6 位无后缀)
    for s in etf_codes:
        try:
            q = api.fund_real_time(s.split(".")[0])
            if isinstance(q, dict) and not q.get("error"):
                _emit_mr(s, q)
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
                        _emit_mr(s, q)
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
    """获取实时快照（单只，AF 优先 + 麦蕊回退，缓存优先）"""
    return fetch_quotes([symbol]).get(normalize_symbol(symbol))


def _normalize(df, prefer_time=False):
    """标准化 K 线 DataFrame: 设置日期索引，确保 OHLCV 列存在"""
    if df is None or len(df) < 5:
        return None
    # 选择正确的日期列 (分钟线优先用 trade_time，避免同日 bar 索引重复)
    if prefer_time:
        cols = ["trade_time", "trade_date", "index"]
    else:
        cols = ["trade_date", "trade_time", "index"]
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


# ── 五档盘口 (分时用) ──
# AF depth 限额 30/min (实测 429: "Rate limit exceeded (30/min)");
# 取 4/5 = 24/min，滚动窗口无突发限速，给其它调用留余量。
AF_DEPTH_LIMIT_PER_MIN = float(os.environ.get("AF_DEPTH_RATE_PER_MIN", "30") or "30")
DEPTH_RATE_PER_MIN = max(1.0, AF_DEPTH_LIMIT_PER_MIN * 4.0 / 5.0)
_depth_cache = TTLCache(ttl_seconds=60.0 / DEPTH_RATE_PER_MIN)
_depth_fetch_lock = threading.Lock()
_depth_bucket = None
_depth_bucket_lock = threading.Lock()
_mr_depth_bucket = PacedBudget(int(DEPTH_RATE_PER_MIN))
_depth_day_cache = {}
_depth_day_cache_date = None
_depth_day_cache_lock = threading.Lock()
_DEPTH_DAY_CACHE_FILE = SCRIPT_DIR / ".cache" / "depth_day.json"


def _depth_trade_date():
    """用交易日历的北京时间取得当天日期；周末/假期返回 None。"""
    now = market_hours.now()
    return now.strftime("%Y-%m-%d") if market_hours.is_trading_day(now) else None


def _depth_day_get(symbol):
    """盘后/周末读最后交易日五档；进入下一交易日即失效。"""
    global _depth_day_cache_date
    with _depth_day_cache_lock:
        today = _depth_trade_date()
        if _depth_day_cache_date is not None:
            if today and _depth_day_cache_date != today:
                _depth_day_cache.clear()
                _depth_day_cache_date = None
                return None
            return _depth_day_cache.get(symbol)
        try:
            raw = json.loads(_DEPTH_DAY_CACHE_FILE.read_text(encoding="utf-8"))
            cache_date = raw.get("trade_date")
            if isinstance(cache_date, str) and isinstance(raw.get("rows"), dict):
                if today and cache_date != today:
                    return None
                _depth_day_cache_date = cache_date
                _depth_day_cache.update(raw["rows"])
                return _depth_day_cache.get(symbol)
        except Exception:
            pass
    return None


def _depth_day_set(symbol, trade_date, value):
    if not trade_date:
        return
    global _depth_day_cache_date
    with _depth_day_cache_lock:
        if _depth_day_cache_date != trade_date:
            _depth_day_cache.clear()
        _depth_day_cache_date = trade_date
        _depth_day_cache[symbol] = value
        try:
            _DEPTH_DAY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _DEPTH_DAY_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"trade_date": trade_date, "rows": _depth_day_cache}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_DEPTH_DAY_CACHE_FILE)
        except Exception as e:
            log.warning("写入当日五档缓存失败: %s", _sanitize_error(e))


def _depth_token_bucket():
    global _depth_bucket
    if _depth_bucket is None:
        with _depth_bucket_lock:
            if _depth_bucket is None:
                _depth_bucket = PacedBudget(int(DEPTH_RATE_PER_MIN))
    return _depth_bucket


def fetch_depth(symbol):
    if not _depth_fetch_lock.acquire(blocking=False):
        return _depth_cache.get(normalize_symbol(symbol))
    try:
        return _fetch_depth_locked(symbol)
    finally:
        _depth_fetch_lock.release()


def _fetch_depth_locked(symbol):
    """获取五档盘口 (独立滚动预算 4/5 限额 + 请求起点计时缓存); 失败/限流返回 None。

    返回 {symbol, timestamp, bid_prices, bid_volumes, ask_prices, ask_volumes}。
    """
    symbol = normalize_symbol(symbol)
    cached = _depth_cache.get(symbol)
    if cached is not None:
        return cached
    trade_date = _depth_trade_date()
    # 收盘/午休/盘前不再请求供应商，展示当天盘中最后一份五档。
    if not market_hours.in_session() or not market_hours.is_trading_day():
        day_cached = _depth_day_get(symbol)
        if day_cached is not None:
            return day_cached
    # AlphaFeed 优先；无 AF key、AF 限流或返回空时回退麦蕊五档。
    if AF_API_KEY and _depth_token_bucket().try_acquire(1):
        fetched_at = time.time()
        try:
            d = get_af().depth.get(symbol)
            # 兼容旧版 SDK 可能返回 {data: {...}} 的包装格式。
            if isinstance(d, dict) and isinstance(d.get("data"), dict):
                d = d["data"]
            if isinstance(d, dict) and d.get("bid_prices"):
                out = {
                    "symbol": symbol,
                    "timestamp": d.get("timestamp"),
                    "bid_prices": d.get("bid_prices") or [],
                    "bid_volumes": d.get("bid_volumes") or [],
                    "ask_prices": d.get("ask_prices") or [],
                    "ask_volumes": d.get("ask_volumes") or [],
                }
                out["_revision"] = time.time_ns() // 1000
                _depth_cache.set(symbol, out, fetched_at=fetched_at)
                if market_hours.in_session():
                    _depth_day_set(symbol, trade_date, out)
                return out
        except Exception as e:
            log.warning(f"AlphaFeed 获取五档失败 {symbol}: {_sanitize_error(e)}")

    if not MAIRUI_API_KEY or not _mr_depth_bucket.try_acquire(1):
        return None
    try:
        raw = get_mr().stock_real_five(symbol.split(".")[0])
    except Exception as e:
        log.warning(f"麦蕊获取五档失败 {symbol}: {_sanitize_error(e)}")
        return None
    row = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else None
    if not isinstance(row, dict):
        return None
    bids = [_safe_float(row.get(f"pb{i}") if row.get(f"pb{i}") is not None else row.get("pb")) for i in range(1, 6)]
    bid_volumes = [_safe_float(row.get(f"vb{i}") if row.get(f"vb{i}") is not None else row.get("vb")) for i in range(1, 6)]
    asks = [_safe_float(row.get(f"ps{i}") if row.get(f"ps{i}") is not None else row.get("ps")) for i in range(1, 6)]
    ask_volumes = [_safe_float(row.get(f"vs{i}") if row.get(f"vs{i}") is not None else row.get("vs")) for i in range(1, 6)]
    if not any(v is not None for v in bids + asks):
        return None
    out = {
        "symbol": symbol,
        "timestamp": _safe_epoch(row.get("t")) or time.time(),
        "bid_prices": bids,
        "bid_volumes": bid_volumes,
        "ask_prices": asks,
        "ask_volumes": ask_volumes,
        "_revision": time.time_ns() // 1000,
    }
    _depth_cache.set(symbol, out)
    if market_hours.in_session():
        _depth_day_set(symbol, trade_date, out)
    return out


# ── 全量股票搜索缓存 (内存 + 磁盘, 24h TTL, 用到时刷新) ──
_stock_list = None
_stock_list_time = 0
STOCK_LIST_FILE = SCRIPT_DIR / ".cache" / "stock_list.json"


def _fetch_stock_list():
    """从麦蕊拉取 symbol+name 列表 (沪深A股 + 北交所 + 场内基金)。

    任一来源抛异常即整体视为失败 (异常向上抛 → 缓存保留旧数据): 列表要缓存
    24h, 不能把"少了一截"的结果落盘, 否则搜索会长时间静默缺票。HTTP 200 但
    返回空只告警跳过 (多为该市场确实无数据, 不该因此整份列表不可用)。
    """
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
            rows = fn()
        except Exception as e:
            log.warning(f"{label} 列表加载失败: {redact_message(e)}")
            raise RuntimeError(f"{label} 列表加载失败") from e
        if not rows:
            log.warning(f"{label} 列表返回空, 跳过")
            continue
        _append(rows)
    return stocks


# fetcher 用 lambda 延迟取全局名: 便于测试 patch, 也避免绑定旧函数对象
_stock_cache = _StaticListCache("股票列表", STOCK_LIST_FILE, lambda: _fetch_stock_list())


def _load_stock_list():
    """全量A股+ETF列表 (内存 + 磁盘, 24h TTL, 用到时同步刷新)。

    正常重启零联网 (磁盘缓存直接命中); 过期才刷新一次, 失败保留旧数据并退避
    STATIC_LIST_RETRY_DELAY。app.py 启动时会另起线程调用本函数预热。
    """
    global _stock_list, _stock_list_time
    _stock_list = _stock_cache.get()
    _stock_list_time = _stock_cache.ts
    return _stock_list


_HK_LIST_FILE = SCRIPT_DIR / ".cache" / "hk_list.json"
_US_LIST_FILE = SCRIPT_DIR / ".cache" / "us_list.json"
# AF universes 文档确认仅 4 池: CN_Stock / US_Stock / HK_Stock / CN_ETF (无指数池)
AF_UNIVERSE_HK = os.environ.get("AF_UNIVERSE_HK", "HK_Stock")
AF_UNIVERSE_US = os.environ.get("AF_UNIVERSE_US", "US_Stock")


def _fetch_hk_list():
    """港股列表: AlphaFeed 股票池 (一次调用全市场), 回退 akshare 东财。"""
    rows = _fetch_universe_rows(AF_UNIVERSE_HK, "hk", "stock")
    if rows:
        return rows
    import akshare as ak
    df = ak.stock_hk_spot_em()
    out = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip().zfill(5)
        name = str(row.get("名称", "")).strip()
        if code and name:
            out.append({"symbol": f"{code}.HK", "name": name, "code": code, "type": "hk"})
    return out


def _fetch_us_list():
    """美股列表: AlphaFeed 股票池 (一次调用全市场), 回退 akshare 东财。"""
    rows = _fetch_universe_rows(AF_UNIVERSE_US, "us", "stock")
    if rows:
        return rows
    import akshare as ak
    df = ak.stock_us_spot_em()
    out = []
    for _, row in df.iterrows():
        raw = str(row.get("代码", "")).strip()
        ticker = raw.split(".")[-1].strip().upper()
        name = str(row.get("名称", "")).strip()
        if ticker and name:
            out.append({"symbol": ticker, "name": name, "code": ticker, "type": "us"})
    return out


_hkus_universe_perm_denied = False  # AF 套餐无 universe 查询权限时置位, 进程内不再尝试


def _fetch_universe_rows(universe, market_tag, default_type):
    """AF quotes.get(universes=...) 拉一个池的全市场快照 (代码/名称/类型)。

    行类型优先取 AF 的 ext.type (stock/etf/index...), 缺失用 default_type。
    返回 [] 表示该池不可用 (无权限/ID 不对/额度不足), 由调用方回退。
    """
    global _hkus_universe_perm_denied
    if _hkus_universe_perm_denied:
        return []  # 套餐无 universe 权限 (403 永久性), 本进程内不再尝试
    af = get_af()
    try:
        df = af.quotes.get(universes=[universe], to_dataframe=True)
    except Exception as e:
        msg = str(e)
        if "not available" in msg or "403" in msg or "Permission" in type(e).__name__:
            _hkus_universe_perm_denied = True
            log.warning("AF 套餐无 universe 查询权限, 港/美股列表改用 akshare (本进程内不再尝试 AF)")
        else:
            log.warning(f"AF universe {universe} 拉取失败: {_sanitize_error(e)}")
        return []
    if df is None or len(df) == 0:
        log.warning(f"AF universe {universe} 返回空")
        return []
    has_ext_type = "ext.type" in df.columns
    out = []
    for sym, row in df.iterrows():
        s = str(sym).strip().upper()
        name = str(row.get("name") or "").strip()
        if not s or not name:
            continue
        itype = str(row.get("ext.type") or "").strip().lower() if has_ext_type else ""
        out.append({"symbol": s, "name": name, "code": s.split(".")[0],
                    "type": itype if itype in ("stock", "etf", "index", "fund") else default_type})
    log.info(f"AF universe {universe} 列表加载成功: {len(out)} 只")
    return out


_hk_cache = _StaticListCache("港股列表", _HK_LIST_FILE, lambda: _fetch_hk_list())
_us_cache = _StaticListCache("美股列表", _US_LIST_FILE, lambda: _fetch_us_list())


def _load_universe(attr):
    """港/美股列表 (内存 + 磁盘, 24h TTL, 用到时同步刷新); 取不到返回 []。"""
    return (_hk_cache if attr == "hk" else _us_cache).get()


SEARCH_MAX_RESULTS = 50           # 下拉返回上限 (前端一屏约 18 条, 可滚动)
# 结果分组顺序: 股票(含港/美) > ETF > 指数; 组内再按匹配分, 同分 A股 > 港 > 美
_SEARCH_TYPE_RANK = {"etf": 1, "fund": 1, "index": 2}
_SEARCH_MARKET_RANK = {"cn": 0, "hk": 1, "us": 2}


def _search_stocks(query):
    """模糊搜索: 名称/代码精确 > 名称前缀 > 代码前缀 > 名称包含 > 代码包含。

    覆盖 A股/基金/指数/港股/美股; 结果带 type (stock/etf/index/hk/us)。
    排序为 股票(含港/美) > ETF > 指数, 组内按匹配分, 同分 A股 > 港 > 美。
    """
    stocks = _load_stock_list()
    universe = [{"symbol": s["symbol"], "name": s["name"], "code": s["code"],
                 "type": "etf" if _is_etf(s["symbol"]) else "stock"} for s in stocks]
    _load_index_cache()
    if _index_names:
        for sym, name in _index_names.items():
            universe.append({"symbol": sym, "name": name,
                             "code": sym.split(".")[0], "type": "index"})
    hk_rows = _load_universe("hk")
    us_rows = _load_universe("us")
    universe.extend(hk_rows)
    universe.extend(us_rows)

    q = query.strip().lower()
    results = []
    for s in universe:
        name = str(s.get("name") or "").lower()
        code = str(s.get("code") or "").lower()
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
    results.sort(key=lambda x: (_SEARCH_TYPE_RANK.get(x.get("type"), 0),
                                -x["score"],
                                _SEARCH_MARKET_RANK.get(_symbol_market(x["symbol"]), 0)))
    return [{"symbol": r["symbol"], "name": r["name"], "code": r["code"], "type": r.get("type", "stock")}
            for r in results[:SEARCH_MAX_RESULTS]]


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
        if isinstance(obj, dt_mod.datetime):
            return obj.isoformat(sep=" ", timespec="seconds")
        if isinstance(obj, dt_mod.date):
            return obj.isoformat()
        if isinstance(obj, dec_mod.Decimal):
            return float(obj)
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


def _safe_epoch(v):
    """快照时间戳规整为 epoch 秒; 毫秒自动降级, 无法解析/不在合理区间返回 None。

    防住 14 位紧凑日期串 (如 20260911150000) 被误当毫秒: 归一后若不在
    [2001, 2096] 的 epoch 区间则视为无效, 避免误判快照日期。
    """
    f = _safe_float(v)
    if f is None or f <= 0:
        return None
    if f > 1e12:            # 毫秒
        f /= 1000.0
    if not (1e9 <= f <= 4e9):
        return None
    return f


# ── 股票代码标准化 ──
def _symbol_market(symbol):
    """市场归类: cn (A股/ETF/指数, 默认) / hk (港股, 5位数字代码) / us (美股字母代码)。"""
    s = str(symbol).upper().strip()
    if s.endswith(".HK"):
        return "hk"
    if s.endswith(".US"):
        return "us"
    code = s.split(".")[0]
    if code.isalpha() and 1 <= len(code) <= 6:
        return "us"
    if code.isdigit() and len(code) == 5:
        return "hk"
    return "cn"


# 港/美股 AF 快照与 K线令牌桶: 额度 10/min 的 4/5 = 8/min (env 可调)
_hkus_quote_bucket = feed.TokenBucket(rate_per_min=int(os.environ.get("HKUS_AF_QUOTE_PER_MIN", "8")))
_hkus_kline_bucket = feed.TokenBucket(rate_per_min=int(os.environ.get("HKUS_AF_KLINE_PER_MIN", "8")))
_hkus_lock = threading.Lock()


def normalize_symbol(raw):
    """将用户输入标准化为带交易所后缀的 symbol 格式 (如 000001.SZ)"""
    raw = raw.strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ", ".HK")):
        return raw
    if raw.endswith(".US"):
        return raw[:-3]
    code0 = raw.split(".")[0]
    if code0.isalpha() and 1 <= len(code0) <= 6:
        return code0  # 美股字母代码
    if code0.isdigit() and len(code0) == 5:
        return code0 + ".HK"  # 港股 5 位数字
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


def _row_to_daily_bar(d, row):
    """把 K 线行转成 get_daily_bar 返回 dict；无效返回 None。"""
    vol = row.get("volume")
    try:
        volume = int(vol) if vol is not None and not (isinstance(vol, float) and (np.isnan(vol) or np.isinf(vol))) else 0
    except (TypeError, ValueError):
        volume = 0
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    if high is None or low is None:
        return None
    return {
        "date": d.isoformat(),
        "open": _safe_float(row.get("open")),
        "high": high,
        "low": low,
        "close": _safe_float(row.get("close")),
        "volume": volume,
    }


def _last_bar_date(df):
    """取日K DataFrame 最后一根 bar 的 date; 无效返回 None。"""
    from datetime import date as _date

    if df is None or len(df) == 0:
        return None
    last_idx = df.index[-1]
    try:
        if hasattr(last_idx, "date"):
            return last_idx.date()
        return _date.fromisoformat(str(last_idx)[:10])
    except (ValueError, TypeError):
        return None


def _strip_today_bar_df(df):
    """日K: 去掉末根「今天」bar，避免当日 OHLCV 写入缓存 (默认 dirty)。"""
    if df is None or len(df) == 0:
        return df
    today = market_hours.now().date()
    last = _last_bar_date(df)
    if last is not None and last == today:
        # 仅收盘后视为终值可落盘; 盘中/午休(BAR_READY 但非 closed)都剥掉,
        # 避免把不完整的当日 bar 当终值写进磁盘缓存 (午休 in_session=False 曾是陷阱)
        if market_hours.session_phase() == "closed":
            return df
        return df.iloc[:-1].copy()
    return df


def _apply_quote_bar_to_df(df, quote_bar):
    """把 quote_bar dict 写入 df 末行 (已有今天) 或 append 新行。"""
    ts = pd.Timestamp(str(quote_bar["date"])[:10])
    row = {
        "open": quote_bar["open"],
        "high": quote_bar["high"],
        "low": quote_bar["low"],
        "close": quote_bar["close"],
        "volume": quote_bar["volume"],
    }
    if "amount" in df.columns:
        amt = quote_bar.get("amount")
        row["amount"] = float(amt) if amt is not None else float("nan")
    last_date = _last_bar_date(df)
    if last_date is not None and last_date == ts.date():
        for col, val in row.items():
            if col in df.columns:
                df.iloc[-1, df.columns.get_loc(col)] = val
        return df
    new_row = pd.DataFrame([row], index=[ts])
    for col in df.columns:
        if col not in new_row.columns:
            new_row[col] = float("nan") if col == "amount" else 0.0
    out = pd.concat([df, new_row]).sort_index()
    out.index.name = df.index.name or "trade_date"
    return out


def _maybe_append_today_bar(symbol, df):
    """日K: 用实时快照补/刷新当日 bar。

    交易日只要快照可用就覆盖末根「今天」OHLCV (盘中/收盘后一致);
    快照不可用 (非交易日/停牌 volume=0/网络/高低缺) 时原样返回源 bar。
    """
    if df is None or len(df) == 0:
        return df
    today = market_hours.now().date()
    if not market_hours.is_trading_day(today.strftime("%Y-%m-%d")):
        return df
    if market_hours.session_phase() == "pre":
        # 盘前快照是上一交易日残留 (volume 可能 >0), 拼出来会凭空多一根"今日"bar
        return df
    last_date = _last_bar_date(df)
    if last_date is None:
        return df
    if last_date > today:
        return df
    quote_bar = _daily_bar_from_quote(symbol, today)
    if not quote_bar:
        return df
    return _apply_quote_bar_to_df(df, quote_bar)


def _daily_bar_from_quote(symbol, target):
    """历史日K尚无当天 bar 时, 用实时快照拼一根 (仅今天 + 交易日 + 成交量>0)。"""
    from datetime import date as _date

    today = market_hours.now().date()
    if target != today:
        return None
    if not market_hours.is_trading_day(today.strftime("%Y-%m-%d")):
        return None
    if market_hours.session_phase() == "pre":
        # 盘前不拼当日 bar (快照为上一交易日残留)
        return None
    q = fetch_quotes([symbol], fresh=True).get(normalize_symbol(symbol))
    if not q:
        return None
    # 快照自带交易所时间戳时校验日期: 挡住盘前/停牌等陈旧快照被当今日
    ts = q.get("timestamp")
    if ts is not None:
        try:
            ts_date = dt_mod.datetime.fromtimestamp(
                float(ts), dt_mod.timezone(dt_mod.timedelta(hours=8))
            ).date()
        except (TypeError, ValueError, OSError, OverflowError):
            ts_date = None
        if ts_date is not None and ts_date != today:
            return None
    high = _safe_float(q.get("high"))
    low = _safe_float(q.get("low"))
    if high is None or low is None:
        return None
    vol = q.get("volume")
    try:
        volume = int(vol) if vol is not None else 0
    except (TypeError, ValueError):
        volume = 0
    if volume <= 0:
        return None
    close = _safe_float(q.get("last_price"))
    if close is None:
        close = _safe_float(q.get("open"))
    return {
        "date": target.isoformat(),
        "open": _safe_float(q.get("open")),
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": _safe_float(q.get("amount")),
    }


def get_daily_bar(symbol, date_str):
    """取指定交易日日K: {date, open, high, low, close, volume}；无该日 bar 返回 None。

    历史接口 (麦蕊 stock_history / 基金日K) 盘中往往尚无当天完整 bar;
    若 date 为今天且为交易日, 回退到实时快照 open/high/low/last_price/volume。
    """
    from datetime import date as _date

    symbol = normalize_symbol(symbol)
    try:
        target = _date.fromisoformat(str(date_str)[:10])
    except (ValueError, TypeError):
        return None

    today = market_hours.now().date()
    # 当日: 历史源(如麦蕊)当日 bar 可能是盘中滞后/部分成交快照; 收盘后
    # in_session()=False, _maybe_append_today_bar 不再用实时快照覆盖, 会残留
    # 滞后 low/high (曾致 601058.SH 买入价 14.20 被 [14.30, 14.64] 误拒)。
    # 故当日一律以实时快照为准, 快照失败再退回历史 bar。
    if target == today:
        quote_bar = _daily_bar_from_quote(symbol, target)
        if quote_bar is not None:
            return quote_bar

    # 回溯交易日约 = 自然日*1.6 + 缓冲；下限 30、上限 1500
    natural = max((today - target).days + 10, 30)
    count = min(max(int(natural * 1.6) + 20, 30), 1500)

    df = None
    try:
        # 成交校验必须未复权真实价 (拆分前 OHLC 不能被 ÷N)
        df, _ = fetch_kline(symbol, "1d", count, adjust="none")
    except Exception as e:
        log.warning(f"get_daily_bar 失败 {symbol} {date_str}: {e}")

    if df is not None and len(df) > 0:
        for idx, row in df.iterrows():
            try:
                if hasattr(idx, "date"):
                    d = idx.date()
                else:
                    d = _date.fromisoformat(str(idx)[:10])
            except (ValueError, TypeError):
                continue
            if d != target:
                continue
            bar = _row_to_daily_bar(d, row)
            if bar is not None:
                return bar

    quote_bar = _daily_bar_from_quote(symbol, target)
    if quote_bar is not None:
        return quote_bar

    return None


# ═══════════════════════════════════════════════════════════════
# 麦蕊特色数据 (基本信息侧栏 / 市场数据抽屉)
# ═══════════════════════════════════════════════════════════════
# 全市场榜单 (/himk/roe、/higg/zljlr) 一次返回几 MB, 必须长缓存进程内共享;
# 逐股接口 (instrument/concepts/hscp) 按 symbol 缓存, 免费版 500 次/日额度可控。

_MR_FAIL_TTL = 300.0  # 上游失败/无数据的短负缓存, 避免反复打接口


def _mr_rows(rows):
    """麦蕊返回归一: 非空 list 正常; dict=错误响应 / 空=无数据 → None。"""
    if isinstance(rows, list) and rows:
        return rows
    return None


def _memo(key, store, lock, ttl, fetcher):
    """进程内 TTL 记忆 (成功 ttl / 失败 _MR_FAIL_TTL); 抛异常 → None。"""
    now = time.time()
    with lock:
        ent = store.get(key)
        if ent and now - ent["ts"] < (ttl if ent["ok"] else _MR_FAIL_TTL):
            return ent["data"]
    try:
        data = fetcher()
        ok = data is not None
    except Exception as e:
        log.warning(f"麦蕊数据获取失败 {key}: {_sanitize_error(e)}")
        data, ok = None, False
    with lock:
        store[key] = {"ts": now, "data": data, "ok": ok}
    return data


# ── 全市场 ROE 排行: 一次请求同时拿到 行业(hym) / 市盈率(syld) / 市净率(sjl) ──
_ROE_TTL = 12 * 3600
_roe_cache = {"ts": 0.0, "data": None, "ok": False}
_roe_lock = threading.Lock()


def _mr_roe_map():
    """{6位代码: {industry, pe, pb}}; 12h 缓存, 失败空 dict + 5min 负缓存。

    /himk/roe (ROE 降序) 单次返回全市场, 故 PE/PB/行业 三项共用一次请求。
    """
    now = time.time()
    with _roe_lock:
        if _roe_cache["data"] is not None:
            ttl = _ROE_TTL if _roe_cache["ok"] else _MR_FAIL_TTL
            if now - _roe_cache["ts"] < ttl:
                return _roe_cache["data"]

    data: dict = {}
    ok = False
    if MAIRUI_API_KEY:
        rows = None
        try:
            rows = get_mr().hsdc_himk_roe()
        except Exception as e:
            log.warning(f"麦蕊 ROE 排行失败: {_sanitize_error(e)}")
        if isinstance(rows, list) and rows:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                dm = str(r.get("dm") or "").strip()
                if not dm:
                    continue
                hy = str(r.get("hym") or "").strip() or None
                data[dm.zfill(6)] = {
                    "industry": hy,
                    "pe": _safe_float(r.get("syld")),
                    "pb": _safe_float(r.get("sjl")),
                }
            ok = True

    with _roe_lock:
        _roe_cache.update({"ts": now, "data": data, "ok": ok})
    return data


# ── 股票基础信息 (涨停/跌停/前收/市值) ──
_MR_INSTRUMENT_TTL = 24 * 3600
_mr_instrument_cache: dict = {}
_mr_instrument_lock = threading.Lock()


def _mr_instrument(symbol):
    """股票基础信息 {limit_up, limit_down, prev_close, float_value, total_value}; 24h。"""
    symbol = normalize_symbol(symbol)

    def _fetch():
        row = get_mr().stock_instrument(symbol)
        if not isinstance(row, dict) or row.get("error"):
            return None
        out = {
            "limit_up": _safe_float(row.get("up")),
            "limit_down": _safe_float(row.get("dp")),
            "prev_close": _safe_float(row.get("pc")),
            "float_value": _safe_float(row.get("fv")),
            "total_value": _safe_float(row.get("tv")),
            "listing_date": row.get("od"),
        }
        if out["limit_up"] is None and out["limit_down"] is None:
            return None
        return out

    return _memo(symbol, _mr_instrument_cache, _mr_instrument_lock, _MR_INSTRUMENT_TTL, _fetch)


# ── 所属行业 (相关指数/行业/概念) ──
_MR_INDUSTRY_TTL = 7 * 24 * 3600
_mr_industry_cache: dict = {}
_mr_industry_lock = threading.Lock()
_INDUSTRY_PREFIXES = ("A股-申万行业-", "A股-行业-", "申万行业-")


def _mr_industry(code):
    """申万行业名 (去前缀); 取不到返回 None。"""
    def _fetch():
        rows = get_mr().concepts_of_stock(code)
        if not isinstance(rows, list):
            return None
        names = [str(r.get("name") or "").strip() for r in rows if isinstance(r, dict)]
        for name in names:
            for p in _INDUSTRY_PREFIXES:
                if name.startswith(p) and name[len(p):]:
                    return name[len(p):]
        for name in names:  # 无标准前缀时退而取含「行业」的条目
            if "行业" in name:
                return name.split("-")[-1] or name
        return None

    return _memo(code, _mr_industry_cache, _mr_industry_lock, _MR_INDUSTRY_TTL, _fetch)


# ── 个股 N 日涨幅 / 量比 / 交易状态 ──
def _n_day_change(closes, n):
    """最新收盘相对 n 个交易日前收盘的涨幅%; 数据不足返回 None。"""
    if not closes or len(closes) < n + 1:
        return None
    last, prev = closes[-1], closes[-n - 1]
    if not prev:
        return None
    try:
        return (last - prev) / prev * 100.0
    except (TypeError, ZeroDivisionError):
        return None


def _volume_ratio_from_df(df):
    """量比 = 当日每分钟均量 / 前5日每分钟均量。

    盘中用已过交易分钟折算; 非盘中用末根 bar 的全天 240 分钟口径 —— 这样
    收盘后/休市/未开盘时侧栏也能看到最近一个交易日的量比, 而不是空白。
    数据不足或末根无成交返回 None。
    """
    if df is None or len(df) < 6:
        return None
    vols = [_safe_float(v) for v in df["volume"].iloc[-6:].tolist()]
    if len(vols) < 6 or vols[-1] is None or vols[-1] <= 0:
        return None
    hist = [v for v in vols[:-1] if v is not None and v > 0]
    if len(hist) < 5:
        return None
    avg5 = sum(hist) / len(hist)
    if avg5 <= 0:
        return None
    if _last_bar_date(df) == market_hours.now().date():
        elapsed = market_hours.session_elapsed_minutes()
        if elapsed <= 0:
            return None  # 今日有 bar 但尚未开盘/无成交 → 盘中量比无意义
        return (vols[-1] / elapsed) / (avg5 / 240.0)
    # 末根非今日: 按最近一个交易日全天口径
    return vols[-1] / avg5


def _trade_status(quote):
    """A 股交易状态 (code, text): 停牌/休市/未开盘/交易中/午间休市/已收盘。"""
    now = market_hours.now()
    if not market_hours.is_trading_day(now):
        return "closed", "休市"
    elapsed = market_hours.session_elapsed_minutes(now)
    vol = _safe_float((quote or {}).get("volume"))
    if elapsed > 0 and quote is not None and (vol is None or vol <= 0):
        return "halt", "停牌"
    if elapsed <= 0:
        return "pre", "未开盘"
    t = now.hour * 60 + now.minute
    if 11 * 60 + 30 < t < 13 * 60:
        return "break", "午间休市"
    if market_hours.in_session(now):
        return "trading", "交易中"
    return "closed", "已收盘"


_STOCK_INFO_TTL = 60.0
_info_quote_bases = {}


def _live_info_for_quote(symbol, quote):
    """Update quote-dependent info without refetching instruments/history per tick."""
    code, label = _trade_status(quote)
    out = {"trade_status": code, "trade_status_text": label}
    # Mairui fund/stock snapshots may already carry lb (量比); preserve it when
    # there is no daily-history basis yet, so the first live frame is useful.
    raw_ratio = _safe_float((quote or {}).get("vol_ratio"))
    if raw_ratio is not None:
        out["vol_ratio"] = raw_ratio
    with _stock_info_lock:
        basis = _info_quote_bases.get(symbol)
    if not basis:
        return out
    day = market_hours.now().date()
    if basis["day"] != day:
        return out
    price = quote.get("last_price")
    for n, base in basis["closes"].items():
        if price is not None and base and base > 0:
            out[f"chg_{n}d"] = (price / base - 1) * 100
    elapsed = market_hours.session_elapsed_minutes()
    avg = basis["avg_volume"]
    volume = quote.get("volume")
    if avg and avg > 0 and volume is not None and elapsed > 0:
        out["vol_ratio"] = (volume / elapsed) / (avg / 240.0)
    return out


_stock_info_cache: dict = {}
_stock_info_lock = threading.Lock()


def fetch_stock_info(symbol, force=False):
    """侧栏「基本信息」数据: 行业/总手/成交额/换手/量比/涨跌停/3-5-10日涨幅/PE/PB/交易状态。

    60s 进程内缓存。港股/美股跳过麦蕊专属字段 (行业/涨跌停/PE/PB 置 None)。
    上游失败静默降级 (字段 None), 不抛异常。
    """
    symbol = normalize_symbol(symbol)
    now = time.time()
    if not force:
        with _stock_info_lock:
            ent = _stock_info_cache.get(symbol)
            if ent and now - ent[0] < _STOCK_INFO_TTL:
                return ent[1]

    market = _symbol_market(symbol)
    is_cn = market == "cn"
    code = symbol.split(".")[0]
    plain = not _is_etf(symbol) and not _is_index_symbol(symbol)

    quote = None
    try:
        quote = fetch_quote(symbol)
    except Exception as e:
        log.warning(f"基本信息快照失败 {symbol}: {_sanitize_error(e)}")

    df = None
    try:
        df, _name, _src = fetch_kline_ex(symbol, "1d", 12)
    except Exception as e:
        log.warning(f"基本信息日K失败 {symbol}: {_sanitize_error(e)}")

    closes = []
    if df is not None and len(df) > 0:
        closes = [c for c in (_safe_float(x) for x in df["close"].tolist()) if c is not None]

    industry = None
    limit_up = limit_down = None
    pe = pb = None
    estimated = False

    if is_cn and plain:
        roe = _mr_roe_map().get(code)
        if roe:
            pe, pb = roe.get("pe"), roe.get("pb")
            industry = roe.get("industry")
        ind = _mr_industry(code)
        if ind:
            industry = ind
        inst = _mr_instrument(symbol)
        if inst:
            limit_up = inst.get("limit_up")
            limit_down = inst.get("limit_down")

    # 涨停/跌停回退: 前收 ±10% (仅 A 股个股; 不区分 ST / 创业板 / 科创板)
    prev_close = _safe_float((quote or {}).get("prev_close"))
    if prev_close and is_cn and plain:
        if limit_up is None:
            limit_up, estimated = round(prev_close * 1.1, 2), True
        if limit_down is None:
            limit_down, estimated = round(prev_close * 0.9, 2), True

    if is_cn:
        status_code, status_text = _trade_status(quote)
    else:
        status_code, status_text = "", "—"

    info = {
        "symbol": symbol,
        "name": (quote or {}).get("name") or _lookup_name(symbol),
        "industry": industry,
        "volume": _safe_float((quote or {}).get("volume")),
        "amount": _safe_float((quote or {}).get("amount")),
        "turnover_rate": _safe_float((quote or {}).get("turnover_rate")),
        "vol_ratio": _volume_ratio_from_df(df),
        "limit_up": limit_up,
        "limit_down": limit_down,
        "chg_3d": _n_day_change(closes, 3),
        "chg_5d": _n_day_change(closes, 5),
        "chg_10d": _n_day_change(closes, 10),
        "pe": pe,
        "pb": pb,
        "trade_status": status_code,
        "trade_status_text": status_text,
        "limit_estimated": estimated,
    }
    with _stock_info_lock:
        if df is not None and len(df) >= 6:
            _info_quote_bases[symbol] = {
                "day": _last_bar_date(df),
                "closes": {n: closes[-n - 1] for n in (3, 5, 10) if len(closes) > n},
                "avg_volume": _safe_float(df["volume"].iloc[-6:-1].mean()),
            }
        _stock_info_cache[symbol] = (now, info)
    return info


# ── 市场数据抽屉: 全市场榜单 / 公告 / 股东 / 解禁 ──
_MR_MARKET_TTL = 600.0        # 主力净流入榜单 (每日 15:40 更新)
_MR_ANNOUNCE_TTL = 1800.0
_MR_HOLDER_TTL = 6 * 3600
_MR_UNLOCK_TTL = 12 * 3600
_mr_market_cache: dict = {}
_mr_market_lock = threading.Lock()
_mr_company_cache: dict = {}
_mr_company_lock = threading.Lock()


def mr_zljlr():
    """全市场主力净流入额降序榜单 (原样 list); 失败 None。"""
    return _memo("zljlr", _mr_market_cache, _mr_market_lock, _MR_MARKET_TTL,
                 lambda: _mr_rows(get_mr().hsdc_zljlr()))


def mr_announcements(code, lt=20):
    """交易所公告 (6 位代码); 失败 None。"""
    return _memo(f"ann:{code}", _mr_company_cache, _mr_company_lock, _MR_ANNOUNCE_TTL,
                 lambda: _mr_rows(get_mr().stock_announcement(code, lt=lt)))


def mr_holder_change(code):
    """股东户数变化趋势 (按截止日期倒序); 失败 None。"""
    return _memo(f"gdbh:{code}", _mr_company_cache, _mr_company_lock, _MR_HOLDER_TTL,
                 lambda: _mr_rows(get_mr().company_holder_change(code)))


def mr_top_holders(code):
    """十大股东 (嵌套 sdgd, 按截止日期倒序); 失败 None。"""
    return _memo(f"sdgd:{code}", _mr_company_cache, _mr_company_lock, _MR_HOLDER_TTL,
                 lambda: _mr_rows(get_mr().company_top10_holders(code)))


def mr_float_holders(code):
    """十大流通股东 (嵌套 sdgd, 按截止日期倒序); 失败 None。"""
    return _memo(f"ltgd:{code}", _mr_company_cache, _mr_company_lock, _MR_HOLDER_TTL,
                 lambda: _mr_rows(get_mr().company_top10_float_holders(code)))


def mr_unlock(code):
    """解禁限售 (按解禁日期倒序); 失败 None。"""
    return _memo(f"jjxs:{code}", _mr_company_cache, _mr_company_lock, _MR_UNLOCK_TTL,
                 lambda: _mr_rows(get_mr().company_unlock(code)))

