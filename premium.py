"""ETF 溢价: 慢源派生字段的异步 provider。

溢价依赖 akshare 净值 (`market._fetch_etf_nav`, 无缓存无超时) + 二次未复权日K,
原先内联在 `/api/kline` 主路径上, 使 ETF 切换显著慢于股票/指数。这里改为
stale-while-revalidate (同 `market._load_pledge` 的写法):

- `/api/kline` 只调 `request()` 预热, 立即返回占位 (`meta.deferred=["premium"]`);
- 客户端随后拉 `/api/kline/deferred`, 未就绪返回 ready=False, 前端退避重试;
- 后台单飞线程算完后长缓存 (NAV 日频, 盘中 30min / 盘后 6h 足够)。

当日 bar 契约不受影响: 本模块只产出溢价值, 不参与任何 OHLCV/指标口径。
"""
from __future__ import annotations

import logging
import os
import threading
import time

import pandas as pd

import market
import market_hours
from indicators import _safe_list

log = logging.getLogger("premium")

# 溢价长缓存: NAV 日频变化, 盘中 30min / 盘后 6h; 失败短负缓存避免反复打 akshare。
PREMIUM_TTL_SEC = float(os.environ.get("PREMIUM_TTL_SEC", "1800"))
PREMIUM_TTL_OFF_SEC = float(os.environ.get("PREMIUM_TTL_OFF_SEC", "21600"))
PREMIUM_FAIL_TTL = 300.0

_lock = threading.Lock()
_cache: dict = {}        # key -> (ts, payload)
_fail_at: dict = {}      # key -> ts (失败负缓存)
_inflight: set = set()   # key 单飞标记


def _key(symbol, period, count):
    return f"{symbol}:{period}:{count}"


def _ttl():
    return PREMIUM_TTL_SEC if market_hours.in_session() else PREMIUM_TTL_OFF_SEC


def _asof(series, targets):
    """按「<= 目标日期的最后一根」对齐 (原逐根布尔掩码的语义, 改为 O(n log n))。"""
    s = series.sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(targets, method="ffill")


def compute_premium(symbol, period, count, df=None):
    """算 ETF 溢价序列, 返回 {dates, values, params}; 无数据返回 None。

    df 为图表同口径的复权 df (由调用方传入以保证日期对齐); 缺省时自行取数。
    """
    if not market._is_etf(symbol):
        return None
    if df is None:
        df, _name, _src = market.fetch_kline_ex(symbol, period, count)
    if df is None or len(df) == 0:
        return None

    nav_df = market._fetch_etf_nav(symbol)
    if nav_df is None or len(nav_df) == 0:
        return None

    # 溢价必须用未复权 close 对齐单位净值 (前复权历史价与 NAV 不可比)
    raw_close = None
    try:
        raw_df = market._fetch_af_kline(symbol, "1d", count, adjust="none")
    except Exception as e:
        log.warning(f"ETF 溢价用未复权日K失败 {symbol}: {e}")
        raw_df = None
    if raw_df is not None and len(raw_df) > 0:
        raw_close = raw_df["close"].copy()
        raw_close.index = pd.to_datetime(raw_close.index).normalize()

    df_sorted = df.sort_index()
    nav_aligned = _asof(nav_df["nav"], df_sorted.index)
    if raw_close is None:
        # 无未复权对齐时不拿前复权价硬算, 避免拆分前溢价失真
        values = [None] * len(df_sorted)
    else:
        close_aligned = _asof(raw_close, df_sorted.index.normalize())
        prem = (close_aligned - nav_aligned) / nav_aligned * 100.0
        prem = prem.where(nav_aligned > 0)
        values = _safe_list(prem)

    return {
        "dates": [str(i)[:10] for i in df_sorted.index],
        "values": values,
        "params": {"source": "akshare fund_open_fund_info_em", "close": "raw"},
    }


def _get_cached(key, ttl):
    with _lock:
        ent = _cache.get(key)
    if ent and time.time() - ent[0] < ttl:
        return ent[1]
    return None


def _schedule(key, symbol, period, count, df):
    """单飞后台计算: 已有结果未过期或已在跑则不重复发起。"""
    with _lock:
        if key in _inflight:
            return
        ent = _cache.get(key)
        if ent and time.time() - ent[0] < _ttl():
            return
        fail = _fail_at.get(key)
        if fail and time.time() - fail < PREMIUM_FAIL_TTL:
            return
        _inflight.add(key)

    def _worker():
        try:
            payload = compute_premium(symbol, period, count, df=df)
            with _lock:
                if payload is None:
                    _fail_at[key] = time.time()
                else:
                    _cache[key] = (time.time(), payload)
        except Exception as e:
            log.warning(f"溢价计算失败 {symbol} {period}: {market._sanitize_error(e)}")
            with _lock:
                _fail_at[key] = time.time()
        finally:
            with _lock:
                _inflight.discard(key)

    threading.Thread(target=_worker, name="premium", daemon=True).start()


def request(symbol, period, count, df=None):
    """预热 (不阻塞): 由 /api/kline 在 ETF 非分钟周期调用。

    传 df 可复用调用方已取到的复权数据, 避免后台重复取数并保证日期对齐。
    """
    if not market._is_etf(symbol) or period in market.MINUTE_PERIODS:
        return
    _schedule(_key(symbol, period, count), symbol, period, count, df)


def get(symbol, period, count):
    """取溢价: {ready: bool, dates, values, params}。

    未就绪时 ready=False (调用方据此退避重试), 并顺带发起一次后台计算
    (服务重启后无 request 预热也能自愈)。
    """
    key = _key(symbol, period, count)
    cached = _get_cached(key, _ttl())
    if cached is not None:
        return {"ready": True, **cached}
    _schedule(key, symbol, period, count, None)
    return {"ready": False}


def clear():
    """测试用: 清空缓存与单飞状态。"""
    with _lock:
        _cache.clear()
        _fail_at.clear()
        _inflight.clear()
