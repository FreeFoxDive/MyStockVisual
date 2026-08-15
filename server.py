#!/usr/bin/env python3
"""
Visual K线图 HTTP 服务器
========================
基于 Python 标准库 http.server，零额外依赖。
代理 AlphaFeed API + 服务端指标计算，为前端 ECharts 提供 JSON 数据。

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
import shutil
import sys
import time
import threading
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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

# ── AlphaFeed ──
AF_API_KEY = os.environ.get("AF_API_KEY", "")
if not AF_API_KEY:
    print("[Visual] ⚠️  未设置 AF_API_KEY 环境变量", flush=True)
else:
    print("[Visual] AF_API_KEY 已加载", flush=True)

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


# ── 指标计算 ──
from indicators import compute_all_indicators, _safe_list, force_index

# ── 交易记录 ──
import trades

SESSION_COOKIE = "session"
SESSION_MAX_AGE = trades.SESSION_TTL_DAYS * 24 * 3600

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
CSP_HEADER = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"

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


# 缓存: 日K 120s, 周/月K 300s, 快照 30s, 上限 500 条目
kline_cache = TTLCache(ttl_seconds=120)
kline_cache_long = TTLCache(ttl_seconds=300)
quote_cache = TTLCache(ttl_seconds=30)


# ── 质押数据缓存 ──
_pledge_cache = None
_pledge_lock = threading.Lock()


def _load_pledge():
    global _pledge_cache
    if _pledge_cache is not None:
        return _pledge_cache
    with _pledge_lock:
        if _pledge_cache is not None:  # double-check within lock
            return _pledge_cache
        try:
            import akshare as ak
            from datetime import date, timedelta
            df = None
            for offset in range(5):
                d = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
                try:
                    df = ak.stock_gpzy_pledge_ratio_em(date=d)
                    if df is not None and len(df) > 0: break
                except Exception: continue
            if df is None:
                df = ak.stock_gpzy_pledge_ratio_em()
            pledge = {}
            for _, r in df.iterrows():
                code = str(r["股票代码"]).strip()
                pledge[code] = {
                    "ratio": float(r.get("质押比例", 0) or 0),
                    "shares": float(r.get("质押股数", 0) or 0),
                    "market_value": float(r.get("质押市值", 0) or 0),
                    "count": int(r.get("质押笔数", 0) or 0),
                }
            _pledge_cache = pledge
            print(f"[Visual] 已加载 {len(pledge)} 条质押数据", flush=True)
        except Exception as e:
            print(f"[Visual] 质押数据加载失败: {e}", flush=True)
            _pledge_cache = {}
    return _pledge_cache


# ── ETF 溢价 ──

def _is_etf(symbol):
    """判断是否为境内ETF (代码前缀: 51, 58, 15, 16, 56, 11, 18等)"""
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
        print(f"[Visual] 获取 {symbol} 净值失败: {e}", flush=True)
        return None


# ── 数据获取 ──
def fetch_kline(symbol, period, count):
    """从 AlphaFeed 获取 K 线数据（优先磁盘缓存），返回标准化 DataFrame"""
    # 检查磁盘缓存 (日K 60s/非交易 300s, 周月K 600s)
    now = datetime.now()
    in_trading = 9 <= now.hour < 15 and now.weekday() < 5
    ttl = 60 if (period == "1d" and in_trading) else (300 if period == "1d" else 600)
    cached = _disk_cache.get(symbol, period, count, ttl)
    if cached:
        df = pd.DataFrame(cached["data"])
        # trade_date/trade_time 在 data 行内, 不在顶层
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("trade_date")
        elif "trade_time" in df.columns:
            df["trade_time"] = pd.to_datetime(df["trade_time"])
            df = df.set_index("trade_time")
        df = df.sort_index()
        return df, cached.get("name", symbol)

    # AlphaFeed 拉取
    af = get_af()
    raw = af.klines.get(symbol=symbol, period=period, count=count,
                        adjust="forward", to_dataframe=True)
    if raw is None or len(raw) == 0:
        return None, None

    df = _normalize(raw)
    if df is None:
        return None, None

    name = raw.iloc[0].get("name", symbol) if "name" in raw.columns else symbol

    # 存入磁盘缓存
    cache_data = {"name": name, "data": json.loads(df.reset_index().to_json(orient="records", date_format="iso"))}
    try:
        _disk_cache.set(symbol, period, count, cache_data)
    except Exception:
        pass

    return df, name


def _quote_from_row(q, symbol):
    """把 AlphaFeed quotes DataFrame 的一行转成标准 quote dict"""
    return {
        "last_price": _safe_float(q.get("last_price")),
        "prev_close": _safe_float(q.get("prev_close")),
        "open": _safe_float(q.get("open")),
        "high": _safe_float(q.get("high")),
        "low": _safe_float(q.get("low")),
        "volume": _safe_int(q.get("volume")),
        "amount": _safe_float(q.get("amount")),
        "change_pct": _safe_float(q.get("ext.change_pct")),
        "amplitude": _safe_float(q.get("ext.amplitude")),
        "turnover_rate": _safe_float(q.get("ext.turnover_rate")),
        "name": q.get("ext.name", symbol),
    }


def fetch_quotes(symbols, fresh=False):
    """批量获取实时快照（一次 AlphaFeed 调用查多只）。

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

    if to_fetch:
        try:
            af = get_af()
            quotes = af.quotes.get(symbols=to_fetch, to_dataframe=True)
            if quotes is not None and len(quotes) > 0:
                for _, q in quotes.iterrows():
                    s = normalize_symbol(str(q.get("symbol", "")))
                    if s in to_fetch and s not in result:
                        result[s] = _quote_from_row(q, s)
                        quote_cache.set(s, result[s])
        except Exception as e:
            print(f"[Visual] 批量获取快照失败 {to_fetch}: {e}", flush=True)
            # 拉取失败：回退缓存（fresh 模式下缓存此前被跳过，此处补读）
            for s in to_fetch:
                cached = quote_cache.get(s)
                if cached:
                    result[s] = cached

    return result


def fetch_quote(symbol):
    """从 AlphaFeed 获取实时快照（单只，缓存优先）"""
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
        print(f"[Visual] 读取股票列表缓存失败: {e}", flush=True)
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
        print(f"[Visual] 写入股票列表缓存失败: {e}", flush=True)


def _fetch_stock_list():
    """从 AlphaFeed 拉取 symbol+name 列表 (跳过 DataFrame, 直接读 dict)"""
    af = get_af()
    stocks = []
    for universe in ("CN_Stock", "CN_ETF"):
        try:
            quotes = af.quotes.get(universes=universe, to_dataframe=False)
            for q in quotes:
                sym = q.get("symbol", "")
                name = (q.get("ext") or {}).get("name", "")
                if not sym or not name or pd.isna(name):
                    continue
                code = sym.split(".")[0] if "." in sym else sym
                stocks.append({"symbol": sym, "name": str(name), "code": code})
        except Exception as e:
            print(f"[Visual] {universe} 加载失败: {e}", flush=True)
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
                print(f"[Visual] 后台刷新股票列表完成: {len(stocks)} 只标的", flush=True)
        except Exception as e:
            print(f"[Visual] 后台刷新股票列表失败: {e}", flush=True)
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
        print("[Visual] 首次加载全量A股+ETF列表...", flush=True)
        stocks = []
        for attempt in range(2):
            try:
                stocks = _fetch_stock_list()
                if stocks:
                    break
            except Exception as e:
                print(f"[Visual] 加载列表({attempt+1}/2)失败: {e}", flush=True)
            if attempt == 0:
                time.sleep(3)
        if stocks:
            ts = time.time()
            _stock_list, _stock_list_time = stocks, ts
            _stock_list_to_disk(stocks, ts)
            print(f"[Visual] 已加载 {len(stocks)} 只标的 (A股+ETF)", flush=True)
        else:
            _stock_list = []
        return _stock_list


def _search_stocks(query):
    """模糊搜索: 代码前缀 || 名称包含"""
    stocks = _load_stock_list()
    if not stocks:
        return []
    q = query.strip().lower()
    results = []
    for s in stocks:
        score = 0
        if s["code"].startswith(q):
            score = 100  # 代码前缀匹配最高优先级
        elif q in s["name"].lower():
            score = 50   # 名称包含
        elif q in s["code"]:
            score = 30   # 代码包含
        if score > 0:
            results.append({**s, "score": score})
    results.sort(key=lambda x: -x["score"])
    return [{"symbol": r["symbol"], "name": r["name"], "code": r["code"]}
            for r in results[:15]]


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
    """将用户输入标准化为 AlphaFeed symbol 格式"""
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
            print(f"[Visual] {fmt % args[:3]}", flush=True)
        else:
            print(f"[Visual] {fmt}", flush=True)

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
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, code=400):
        self._send_json({"error": msg}, code)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ── API 路由: 所有 /api/* 做速率限制 ──
        if path.startswith("/api/"):
            if not _check_rate_limit():
                return self._send_json({"error": "请求过于频繁，请稍后重试"}, 429)

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
            return self._send_json({"ok": True, "time": str(datetime.now())})
        elif path == "/api/auth/me":
            return self._handle_auth_me()
        elif path == "/api/admin/users":
            return self._handle_admin_users_list()
        elif path == "/api/trades/stats":
            return self._handle_trades_stats(params)
        elif path == "/api/trades":
            return self._handle_trades_list(params)
        elif path == "/api/trade-reasons":
            return self._send_json({"entry": trades.ENTRY_REASONS, "exit": trades.EXIT_REASONS})
        elif path == "/api/models":
            return self._handle_models_list()
        elif path == "/" or path == "/index.html":
            return self._serve_static("index.html")
        elif path.endswith(".html") or path.endswith(".js") or path.endswith(".css"):
            return self._serve_static(path.lstrip("/"))
        else:
            # 默认返回 index.html (SPA fallback)
            return self._serve_static("index.html")

    # ── 鉴权与 body 解析助手 ──
    def _client_ip(self):
        """识别真实客户端 IP：Cloudflare/反代头优先，回退到 socket 对端。"""
        cf = self.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip().split(",")[0].strip()
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
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
        if clear:
            return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"

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
        if path == "/api/auth/login":
            return self._handle_login()
        elif path == "/api/auth/logout":
            return self._handle_logout()
        elif path == "/api/admin/users":
            return self._handle_admin_users_create()
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
        return self._send_json({"username": user["username"], "is_admin": user["is_admin"]})

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
            mid = trades.create_model(body.get("name"), body.get("description", ""))
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
            updated = trades.update_model(mid, body.get("name"), body.get("description", ""))
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
            "limit": params.get("limit", [None])[0],
            "offset": params.get("offset", [None])[0],
        }
        records, total = trades.list_trades(user["id"], filters)
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
        return self._send_json(trades.compute_stats(user["id"], start, end))

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
        count = min(int(params.get("count", ["200"])[0]), 1000)
        use_macd13 = params.get("macd13", ["false"])[0].lower() == "true"

        # 检查缓存
        cache_key = f"{symbol}:{period}:{count}:{use_macd13}"
        cache = kline_cache_long if period in ("1w", "1M") else kline_cache
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
        df, indicators = compute_all_indicators(df, period, use_macd13)


        # ETF 溢价率
        premium_data = None
        is_etf = _is_etf(symbol)
        if is_etf:
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
                date_str = idx.strftime("%Y-%m-%d %H:%M" if ":" in period else "%Y-%m-%d")
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
            "macd_params": indicators["macd"]["params"],
            "klines": klines,
            "meta": {
                "cached": False,
                "server_time": str(datetime.now()),
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
        use_macd13 = params.get("macd13", ["false"])[0].lower() == "true"
        try:
            af = get_af()
            dfs = af.klines.batch([symbol], period=period, count=count, adjust="forward", to_dataframe=True)
            df = dfs.get(symbol)
            if df is None or len(df) == 0:
                return self._send_error(f"无法获取 {symbol} 的分钟线", 404)
            # 保存原始时间列再标准化
            time_col = "trade_time" if "trade_time" in df.columns else "trade_date"
            raw_times = df[time_col].astype(str).str[-8:-3] if time_col in df.columns else None
            df = _normalize(df, prefer_time=True)
            if df is None:
                return self._send_error("数据不足", 404)
            # 过滤非当天数据（分时图只显示当日）
            today = pd.Timestamp.now().normalize()
            mask = df.index.normalize() == today
            df = df[mask]
            if raw_times is not None:
                raw_times = raw_times[mask]
            if len(df) == 0:
                return self._send_error("无当日分时数据", 404)
            # 基于过滤后的数据重新计算指标
            df, indicators = compute_all_indicators(df, period="1d", use_macd13=use_macd13, with_atr_val=True)
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
        print(f"[Visual] 交易记录数据库已就绪: {trades._db_path}", flush=True)
    except Exception as e:
        print(f"[Visual] ⚠️  交易记录数据库初始化失败: {e}", flush=True)

    # 引导首个管理员 (仅当库中无 admin 时创建，绝不覆盖已有口令)
    admin_user = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_pass = os.environ.get("ADMIN_PASSWORD", "")
    if admin_user and admin_pass:
        try:
            if trades.count_admins() == 0:
                trades.create_user(admin_user, admin_pass, is_admin=True)
                print(f"[Visual] 已创建管理员账号: {admin_user}", flush=True)
            else:
                print("[Visual] 管理员已存在，跳过自动创建", flush=True)
        except Exception as e:
            print(f"[Visual] ⚠️  管理员账号创建失败: {e}", flush=True)
    elif admin_user or admin_pass:
        print("[Visual] ⚠️  ADMIN_USERNAME 与 ADMIN_PASSWORD 需同时设置", flush=True)
    else:
        print("[Visual] ⚠️  未设置 ADMIN_USERNAME/ADMIN_PASSWORD，无管理员时无法创建用户", flush=True)

    # 启动时清理磁盘缓存
    try:
        _disk_cache.cleanup()
        print(f"[Visual] 磁盘缓存已清理 (上限 {CACHE_MAX_MB}MB)", flush=True)
    except Exception:
        pass

    print(f"""
╔══════════════════════════════════════════╗
║   📈 Visual K线图 股票可视化              ║
║   数据源: AlphaFeed 实时接口              ║
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

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Visual] 服务器已停止", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
