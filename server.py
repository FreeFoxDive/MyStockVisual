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


def get_af():
    global _af
    if _af is None:
        from alphafeed import AlphaFeed
        _af = AlphaFeed(api_key=AF_API_KEY)
    return _af


# ── 指标计算 ──
from visual.indicators import compute_all_indicators, _safe_list

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
            # 限制最大条目数，超过时淘汰最旧的一半
            if len(self._cache) >= 500:
                items = sorted(self._cache.items(), key=lambda x: x[1]["time"])
                for old_key, _ in items[:250]:
                    del self._cache[old_key]
            self._cache[key] = {"data": data, "time": time.time()}

    def clear(self):
        with self._lock:
            self._cache.clear()


# 缓存: 日K 120s, 周/月K 300s, 快照 30s, 上限 500 条目
kline_cache = TTLCache(ttl_seconds=120)
kline_cache_long = TTLCache(ttl_seconds=300)
quote_cache = TTLCache(ttl_seconds=30)


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
    """加载全量A股+ETF列表 (缓存1小时, 失败重试1次)"""
    global _stock_list, _stock_list_time
    now = time.time()
    if _stock_list is not None and now - _stock_list_time < 3600:
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
            _stock_list_time = now
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


# ── HTTP Handler ──
class VisualHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, *_args):
        # 简洁日志
        print(f"[Visual] {_args[0]}", flush=True)

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
        elif path == "/api/intraday":
            return self._handle_intraday(params)
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
        filepath = (SCRIPT_DIR / filename).resolve()
        # 防止路径穿越: 确保解析后仍在 visual/ 目录内
        if not str(filepath).startswith(str(SCRIPT_DIR.resolve())):
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
            return self._send_error(f"获取K线失败: {e}", 500)

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

        # 把溢价率写回 klines
        if premium_data:
            for i, k in enumerate(klines):
                k["premium"] = premium_data["values"][i] if i < len(premium_data["values"]) else None

        resp = {
            "symbol": symbol,
            "name": name,
            "period": period,
            "count": len(klines),
            "is_etf": is_etf,
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
            self._send_error(f"获取快照失败: {e}", 500)

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
                    "atr14": indicators["atr"]["values"][i] if "atr" in indicators else None,
                })
            self._send_json({"symbol": symbol, "period": period, "bars": bars})
        except Exception as e:
            self._send_error(f"获取分钟线失败: {e}", 500)

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
