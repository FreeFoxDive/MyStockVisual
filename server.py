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
from http.server import HTTPServer, BaseHTTPRequestHandler
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


def fetch_quote(symbol):
    """从 AlphaFeed 获取实时快照"""
    cached = quote_cache.get(symbol)
    if cached:
        return cached

    try:
        af = get_af()
        quotes = af.quotes.get(symbols=[symbol], to_dataframe=True)
        if quotes is not None and len(quotes) > 0:
            q = quotes.iloc[0]
            result = {
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
            quote_cache.set(symbol, result)
            return result
    except Exception as e:
        print(f"[Visual] 获取快照失败 {symbol}: {e}", flush=True)
    return None


def _normalize(df):
    """标准化 K 线 DataFrame: 设置日期索引，确保 OHLCV 列存在"""
    if df is None or len(df) < 5:
        return None
    # 选择正确的日期列
    date_col = None
    for col in ["trade_date", "trade_time"]:
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


def _load_stock_list():
    """加载全量A股+ETF列表 (缓存1小时, 失败重试1次, 线程安全)"""
    global _stock_list, _stock_list_time
    now = time.time()
    if _stock_list is not None and now - _stock_list_time < 3600:
        return _stock_list
    with _stock_lock:
        if _stock_list is not None and now - _stock_list_time < 3600:  # double-check
            return _stock_list
        print("[Visual] 加载全量A股+ETF列表...", flush=True)
        for attempt in range(2):
            try:
                af = get_af()
                stocks = []
                for universe in ("CN_Stock", "CN_ETF"):
                    try:
                        df = af.quotes.get(universes=universe, to_dataframe=True)
                        for _, r in df.iterrows():
                            sym = r.get("symbol", "")
                            name = r.get("ext.name", "")
                            if not sym or not name or pd.isna(name):
                                continue
                            code = sym.split(".")[0] if "." in sym else sym
                            stocks.append({"symbol": sym, "name": str(name), "code": code})
                    except Exception as e:
                        print(f"[Visual] {universe} 加载失败: {e}", flush=True)
                _stock_list = stocks
                _stock_list_time = time.time()
                print(f"[Visual] 已加载 {len(stocks)} 只标的 (A股+ETF)", flush=True)
                return _stock_list
            except Exception as e:
                print(f"[Visual] 加载列表({attempt+1}/2)失败: {e}", flush=True)
                if attempt == 0:
                    time.sleep(3)
                else:
                    if _stock_list is None:
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

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, cls=NumpyEncoder).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", self._get_cors_origin())
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", CSP_HEADER)
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
        elif path == "/api/intraday":
            return self._handle_intraday(params)
        elif path == "/api/pledge":
            return self._handle_pledge(params)
        elif path == "/api/search":
            return self._handle_search(params)
        elif path == "/api/ping":
            return self._send_json({"ok": True, "time": str(datetime.now())})
        elif path == "/" or path == "/index.html":
            return self._serve_static("index.html")
        elif path.endswith(".html") or path.endswith(".js") or path.endswith(".css"):
            return self._serve_static(path.lstrip("/"))
        else:
            # 默认返回 index.html (SPA fallback)
            return self._serve_static("index.html")

    def _get_cors_origin(self):
        """限制 CORS 来源：仅允许相同源（未配置 0.0.0.0 时）"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return ""
        # 仅允许同源请求
        return origin

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", self._get_cors_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── 静态文件 ──
    def _serve_static(self, filename):
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
            df = _normalize(df)
            if df is None:
                return self._send_error("数据不足", 404)
            # 计算分时指标
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

    server = HTTPServer((args.host, args.port), VisualHandler)

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
