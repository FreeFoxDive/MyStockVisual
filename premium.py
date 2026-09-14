"""ETF 溢价: 慢源派生字段的异步 provider。

溢价依赖 akshare 净值 (`market._fetch_etf_nav`, 无缓存无超时) + 二次未复权日K,
原先内联在 `/api/kline` 主路径上, 使 ETF 切换显著慢于股票/指数。这里改为
stale-while-revalidate (同 `market._load_pledge` 的写法):

- `/api/kline` 只调 `request()` 预热, 立即返回占位 (`meta.deferred=["premium"]`);
- 客户端随后拉 `/api/kline/deferred`, 未就绪返回 ready=False, 前端退避重试;
- NAV 长缓存，溢价盘中短周期刷新，返回计算时间；工作数量和等待预算有界。

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

# 历史结果可保留作 stale 回填；盘中有效期还受价格刷新间隔约束。
PREMIUM_TTL_SEC = float(os.environ.get("PREMIUM_TTL_SEC", "1800"))
PREMIUM_TTL_OFF_SEC = float(os.environ.get("PREMIUM_TTL_OFF_SEC", "21600"))
PREMIUM_FAIL_TTL = 300.0
PREMIUM_REFRESH_SEC = max(1.0, float(os.environ.get("PREMIUM_REFRESH_SEC", "60")))
PREMIUM_TIMEOUT_SEC = max(1.0, float(os.environ.get("PREMIUM_TIMEOUT_SEC", "30")))
PREMIUM_MAX_INFLIGHT = max(1, int(os.environ.get("PREMIUM_MAX_INFLIGHT", "4")))

_lock = threading.Lock()
_cache: dict = {}        # key -> (ts, payload)
_fail_at: dict = {}      # key -> monotonic ts (失败负缓存)
_inflight: dict = {}     # key -> monotonic 开始时间; 超时后仍占实际工作槽
_fail_kind: dict = {}


def _key(symbol, period, count):
    return f"{symbol}:{period}:{count}"


def _ttl():
    # NAV 日频但价格不是日频；盘中更新 raw close，不改变旧溢价公式。
    return min(PREMIUM_TTL_SEC, PREMIUM_REFRESH_SEC) if market_hours.in_session() else PREMIUM_TTL_OFF_SEC


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
        # 两个目标序列按位置对应，避免非午夜索引经 normalize 后被 pandas 再对齐。
        close_aligned.index = nav_aligned.index
        prem = (close_aligned - nav_aligned) / nav_aligned * 100.0
        prem = prem.where(nav_aligned > 0)
        values = _safe_list(prem)

    return {
        "dates": [str(i)[:10] for i in df_sorted.index],
        "values": values,
        "params": {"source": "akshare fund_open_fund_info_em", "close": "raw"},
    }


def _fresh(ent):
    phase = market_hours.session_phase()
    return bool(ent and time.time() - ent[0] < _ttl()
                and ent[1].get("generated_date") == market_hours.now().date().isoformat()
                and ent[1].get("generated_phase", phase) == phase)


def _schedule(key, symbol, period, count, df):
    """单飞后台计算: 已有结果未过期或已在跑则不重复发起。"""
    with _lock:
        if key in _inflight:
            return
        ent = _cache.get(key)
        if _fresh(ent):
            return
        fail = _fail_at.get(key)
        if fail is not None and time.monotonic() - fail < PREMIUM_FAIL_TTL:
            return
        if len(_inflight) >= PREMIUM_MAX_INFLIGHT:
            return
        started = time.monotonic()
        generated_date = market_hours.now().date().isoformat()
        generated_phase = market_hours.session_phase()
        _inflight[key] = started

    def _worker():
        try:
            payload = compute_premium(symbol, period, count, df=df)
            with _lock:
                if time.monotonic() - started >= PREMIUM_TIMEOUT_SEC:
                    _fail_at[key] = time.monotonic()
                    _fail_kind[key] = "failed"
                elif payload is None:
                    _fail_at[key] = time.monotonic()
                    _fail_kind[key] = "no_data"
                else:
                    stamp = time.time()
                    _cache[key] = (stamp, {**payload, "generated_at": stamp,
                                          "generated_date": generated_date,
                                          "generated_phase": generated_phase})
                    _fail_at.pop(key, None)
                    _fail_kind.pop(key, None)
        except Exception as e:
            log.warning(f"溢价计算失败 {symbol} {period}: {market._sanitize_error(e)}")
            with _lock:
                _fail_at[key] = time.monotonic()
                _fail_kind[key] = "failed"
        finally:
            with _lock:
                _inflight.pop(key, None)

    try:
        threading.Thread(target=_worker, name="premium", daemon=True).start()
    except Exception:
        with _lock:
            _inflight.pop(key, None)
            _fail_at[key] = time.monotonic()
            _fail_kind[key] = "failed"


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
    if not market._is_etf(symbol) or period not in ("1d", "1w", "1M"):
        return {"ready": False, "status": "no_data", "retry_after_sec": None}
    _schedule(key, symbol, period, count, None)
    with _lock:
        ent = _cache.get(key)
        fresh = _fresh(ent)
        start = _inflight.get(key)
        failed = _fail_at.get(key)
        status, retry = "pending", 1.0
        if start is not None and time.monotonic() - start >= PREMIUM_TIMEOUT_SEC:
            status, retry = "failed", PREMIUM_FAIL_TTL
        elif failed is not None and time.monotonic() - failed < PREMIUM_FAIL_TTL:
            status = _fail_kind.get(key, "failed")
            retry = max(1.0, PREMIUM_FAIL_TTL - (time.monotonic() - failed))
        elif start is None and not fresh:
            retry = 10.0  # 容量满，无队列，客户端稍后重试
        if fresh:
            status = "ready"
        payload = dict(ent[1]) if ent else {}
        # 客户端至多 60s 后重新确认；盘前长 TTL 不能延续到开盘/收盘后。
        age_left = min(60.0, max(0.0, _ttl() - (time.time() - ent[0]))) if fresh else 0.0
    return {**payload, "ready": ent is not None, "status": status,
            "stale": ent is not None and not fresh, "max_age_sec": age_left,
            "retry_after_sec": age_left if fresh else retry}


def clear():
    """清空缓存；存活工作仍须保留容量占用。测试应等待其工作结束。"""
    with _lock:
        _cache.clear()
        _fail_at.clear()
        _fail_kind.clear()
