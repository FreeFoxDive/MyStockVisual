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
import io
import json
import os
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
sys.path.insert(0, str(PROJECT_DIR))  # 确保能 import visual.indicators

# ── 加载 .env ──
_ENV_FILE = PROJECT_DIR / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE, "r", encoding="utf-8") as _f:
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
    print(f"[Visual] AF_API_KEY: {AF_API_KEY[:8]}...", flush=True)

_af = None


def get_af():
    global _af
    if _af is None:
        from alphafeed import AlphaFeed
        _af = AlphaFeed(api_key=AF_API_KEY)
    return _af


# ── 指标计算 ──
from visual.indicators import compute_all_indicators, MACD_PARAMS

# ── 缓存 ──
class TTLCache:
    """简单的 TTL 内存缓存"""

    def __init__(self, ttl_seconds=60):
        self._cache = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["time"] < self._ttl:
                return entry["data"]
        return None

    def set(self, key, data):
        with self._lock:
            self._cache[key] = {"data": data, "time": time.time()}

    def clear(self):
        with self._lock:
            self._cache.clear()


# 不同 TTL: 日K缓存 120s, 周/月K缓存 300s, 快照缓存 30s
kline_cache = {}
quote_cache = TTLCache(ttl_seconds=30)


def get_cache_ttl(period):
    if period in ("1w", "1M"):
        return 300
    return 120


# ── 数据获取 ──
def fetch_kline(symbol, period, count):
    """从 AlphaFeed 获取 K 线数据，返回标准化 DataFrame"""
    af = get_af()
    raw = af.klines.get(symbol=symbol, period=period, count=count,
                        adjust="forward", to_dataframe=True)
    if raw is None or len(raw) == 0:
        return None, None

    # 标准化
    df = _normalize(raw)
    if df is None:
        return None, None

    # 获取名称
    name = raw.iloc[0].get("name", symbol) if "name" in raw.columns else symbol
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


def _load_stock_list():
    """加载全量A股列表 (缓存1小时, 失败重试1次)"""
    global _stock_list, _stock_list_time
    now = time.time()
    if _stock_list is not None and now - _stock_list_time < 3600:
        return _stock_list
    print("[Visual] 加载全量A股列表...", flush=True)
    for attempt in range(2):
        try:
            af = get_af()
            df = af.quotes.get(universes="CN_Stock", to_dataframe=True)
            stocks = []
            for _, r in df.iterrows():
                sym = r.get("symbol", "")
                name = r.get("ext.name", "")
                if not sym or not name or pd.isna(name):
                    continue
                code = sym.split(".")[0] if "." in sym else sym
                stocks.append({"symbol": sym, "name": str(name), "code": code})
            _stock_list = stocks
            _stock_list_time = now
            print(f"[Visual] 已加载 {len(stocks)} 只股票", flush=True)
            return _stock_list
        except Exception as e:
            print(f"[Visual] 加载股票列表({attempt+1}/2)失败: {e}", flush=True)
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


# ── HTTP Handler ──
class VisualHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, format, *args):
        # 简洁日志
        print(f"[Visual] {args[0]}", flush=True)

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, cls=NumpyEncoder).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content, code=200):
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, code=400):
        self._send_json({"error": msg}, code)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ── API 路由 ──
        if path == "/api/kline":
            return self._handle_kline(params)
        elif path == "/api/quote":
            return self._handle_quote(params)
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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── 静态文件 ──
    def _serve_static(self, filename):
        filepath = SCRIPT_DIR / filename
        if not filepath.exists():
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
        ttl = get_cache_ttl(period)
        if cache_key not in kline_cache:
            kline_cache[cache_key] = {"data": None, "time": 0}
        entry = kline_cache[cache_key]
        if entry["data"] and time.time() - entry["time"] < ttl:
            resp = entry["data"].copy()
            resp["meta"]["cached"] = True
            return self._send_json(resp)

        # 获取数据
        try:
            df, name = fetch_kline(symbol, period, count)
        except Exception as e:
            return self._send_error(f"获取K线失败: {e}", 500)

        if df is None:
            return self._send_error(f"无法获取 {symbol} 的K线数据", 404)

        # 计算指标
        df, indicators = compute_all_indicators(df, period, use_macd13)

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
                "vol_ma5": _safe_float(row.get("vol_ma5")),
                "vol_ma10": _safe_float(row.get("vol_ma10")),
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

        resp = {
            "symbol": symbol,
            "name": name,
            "period": period,
            "count": len(klines),
            "macd_params": indicators["macd"]["params"],
            "klines": klines,
            "indicators": indicators,
            "meta": {
                "cached": False,
                "server_time": str(datetime.now()),
                "last_trade_date": klines[-1]["date"] if klines else None,
            }
        }

        # 缓存
        kline_cache[cache_key] = {"data": resp, "time": time.time()}

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
            self._send_error(f"获取快照失败: {e}", 500)

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

    print(f"""
╔══════════════════════════════════════════╗
║   📈 Visual K线图 股票可视化              ║
║   数据源: AlphaFeed 实时接口              ║
║   地址: http://{args.host}:{args.port}             ║
╚══════════════════════════════════════════╝
""", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Visual] 服务器已停止", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
