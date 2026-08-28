"""行情/缓存/质押等数据层 (从 server 拆出, 无 HTTP 依赖)。"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

log = logging.getLogger("market")

from logger import sanitize_error as _sanitize_error  # noqa: E402

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
        return {"ok": False, "error": _sanitize_error(e)}

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


def _daily_bar_from_quote(symbol, target):
    """历史日K尚无当天 bar 时, 用实时快照拼一根 (仅今天 + 交易日 + 成交量>0)。"""
    from datetime import date as _date

    today = market_hours.now().date()
    if target != today:
        return None
    if not market_hours.is_trading_day(today.strftime("%Y-%m-%d")):
        return None
    q = fetch_quote(symbol)
    if not q:
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
    # 回溯交易日约 = 自然日*1.6 + 缓冲；下限 30、上限 1500
    natural = max((today - target).days + 10, 30)
    count = min(max(int(natural * 1.6) + 20, 30), 1500)

    df = None
    try:
        df, _ = fetch_kline(symbol, "1d", count)
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

