"""Flask 路由: K线/筹码/分时 (/api/kline|chips|intraday)。"""
from __future__ import annotations

import logging

import pandas as pd
from flask import request

import kline_source
import market_hours
from api import api_bp
from api.common import _error, _json
from chips import get_chips
from indicators import compute_all_indicators, _safe_list
from logger import sanitize_error as _sanitize_error
from market import (
    MINUTE_COUNTS,
    MINUTE_PERIODS,
    _fetch_af_kline,
    _fetch_etf_nav,
    _fetch_instrument_meta,
    _is_etf,
    _is_index_symbol,
    _safe_float,
    _safe_int,
    fetch_kline_ex,
    fetch_quote,
    kline_cache,
    kline_cache_long,
    kline_cache_minute,
    normalize_symbol,
)

log = logging.getLogger("api")


def _attach_quote(resp, symbol):
    """给 kline 响应挂实时快照 (30s 缓存); 失败静默, quote 缺席即可。"""
    try:
        q = fetch_quote(symbol)
        if q:
            resp["quote"] = q
    except Exception:
        pass


@api_bp.route("/api/kline", methods=["GET"])
def kline():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")

    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    # 复权: forward(前,默认) / hfq(后) / none(不复权); 分钟周期固定前复权 (源口径)
    adjust = kline_source.normalize_adjust(request.args.get("adjust"))
    if period in MINUTE_PERIODS:
        adjust = kline_source.ADJUST_FORWARD
    # 日K: 3年可见 (3×252) + RSI 收敛 warmup 250；其余非分钟默认 200
    DAILY_COUNT = 3 * 252 + 250  # 1006
    if period == "1d":
        default_count = DAILY_COUNT
    else:
        default_count = MINUTE_COUNTS.get(period, 200)
    try:
        count = min(int(request.args.get("count") or str(default_count)), 1500)
    except ValueError:
        count = default_count

    cache_key = f"{symbol}:{period}:{count}:{kline_source.adjust_tag(adjust)}"
    skip_1d_cache = period == "1d" and market_hours.is_trading_day(
        market_hours.now().date().isoformat()
    )
    if period in MINUTE_PERIODS:
        cache = kline_cache_minute
    elif period in ("1w", "1M"):
        cache = kline_cache_long
    else:
        cache = kline_cache
    cached = None if skip_1d_cache else cache.get(cache_key)
    if cached:
        resp = cached.copy()
        resp["meta"] = dict(resp.get("meta") or {})
        resp["meta"]["cached"] = True
        _attach_quote(resp, symbol)
        return _json(resp)

    try:
        df, name, source = fetch_kline_ex(symbol, period, count, adjust=adjust)
    except Exception as e:
        log.warning("获取K线失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("获取K线失败，请稍后重试", 500)

    if df is None:
        return _error(f"无法获取 {symbol} 的K线数据", 404)

    try:
        df, indicators = compute_all_indicators(df, period)
    except Exception as e:
        log.warning("指标计算失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("指标计算失败", 500)

    # 主图已是前复权, impulse 直接用 compute_all_indicators 结果 (不再另拉 qfq)

    premium_data = None
    is_etf = _is_etf(symbol)
    if is_etf and period not in MINUTE_PERIODS:
        nav_df = _fetch_etf_nav(symbol)
        # 溢价必须用未复权 close 对齐单位净值 (前复权历史价与 NAV 不可比)
        raw_df = None
        try:
            raw_df = _fetch_af_kline(symbol, "1d", count, adjust="none")
        except Exception as e:
            log.warning(f"ETF 溢价用未复权日K失败 {symbol}: {e}")
        if nav_df is not None and len(nav_df) > 0:
            df_sorted = df.sort_index()
            raw_close = None
            if raw_df is not None and len(raw_df) > 0:
                raw_close = raw_df["close"].copy()
                raw_close.index = pd.to_datetime(raw_close.index).normalize()
            premiums = []
            for idx in df_sorted.index:
                nav_matches = nav_df[nav_df.index <= idx]
                if len(nav_matches) == 0:
                    premiums.append(None)
                    continue
                nav_val = float(nav_matches.iloc[-1]["nav"])
                close_val = None
                if raw_close is not None:
                    idx_n = pd.Timestamp(idx).normalize()
                    if idx_n in raw_close.index:
                        close_val = float(raw_close.loc[idx_n])
                    else:
                        earlier = raw_close[raw_close.index <= idx_n]
                        if len(earlier) > 0:
                            close_val = float(earlier.iloc[-1])
                if close_val is None:
                    # 无未复权对齐时不拿前复权价硬算, 避免拆分前溢价失真
                    premiums.append(None)
                    continue
                prem = (close_val - nav_val) / nav_val * 100 if nav_val > 0 else None
                premiums.append(prem)
            prem_series = pd.Series(premiums, index=df_sorted.index)
            premium_data = {
                "values": _safe_list(prem_series),
                "params": {"source": "akshare fund_open_fund_info_em", "close": "raw"},
            }

    klines = []
    for idx, row in df.iterrows():
        date_str = str(idx)
        if hasattr(idx, "strftime"):
            date_str = idx.strftime(
                "%Y-%m-%d %H:%M" if period in MINUTE_PERIODS else "%Y-%m-%d"
            )
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
            "obv": _safe_float(row.get("obv")),
            "maobv": _safe_float(row.get("maobv")),
            "vol_ma5": _safe_float(row.get("vol_ma5")),
            "vol_ma10": _safe_float(row.get("vol_ma10")),
            "vol_ma20": _safe_float(row.get("vol_ma20")),
            "boll_mid": _safe_float(row.get("boll_mid")),
            "boll_up": _safe_float(row.get("boll_up")),
            "boll_low": _safe_float(row.get("boll_low")),
            "wr14": _safe_float(row.get("wr14")),
            "cci14": _safe_float(row.get("cci14")),
            "bias6": _safe_float(row.get("bias6")),
            "bias12": _safe_float(row.get("bias12")),
            "bias24": _safe_float(row.get("bias24")),
            "dmi_pdi": _safe_float(row.get("dmi_pdi")),
            "dmi_mdi": _safe_float(row.get("dmi_mdi")),
            "dmi_adx": _safe_float(row.get("dmi_adx")),
        }
        klines.append(entry)

    if premium_data:
        prem_vals = premium_data["values"]
        for i, k in enumerate(klines):
            if i < len(prem_vals) and prem_vals[i] is not None:
                k["premium"] = prem_vals[i]
            else:
                k["premium"] = None

    inst_meta = _fetch_instrument_meta(symbol) or {}
    resp = {
        "symbol": symbol,
        "name": name,
        "period": period,
        "count": len(klines),
        "is_etf": is_etf,
        "is_index": _is_index_symbol(symbol),
        "float_shares": inst_meta.get("float_shares"),
        "total_shares": inst_meta.get("total_shares"),
        "instrument_type": inst_meta.get("type"),
        "obv_params": indicators.get("obv", {}).get("params"),
        "macd_params": indicators["macd"]["params"],
        "boll_params": indicators.get("boll", {}).get("params"),
        "klines": klines,
        "meta": {
            "cached": False,
            "server_time": str(market_hours.now()),
            "last_trade_date": klines[-1]["date"] if klines else None,
            "source": source,
            "adjust": adjust,
        },
    }

    # 缓存里不存 quote: TTLCache 存引用, 事后挂 quote 会污染缓存条目,
    # 让后续命中拿到最长 TTL 前的旧快照。改为存副本 (不含 quote), 每次返回前现挂。
    _attach_quote(resp, symbol)
    if not skip_1d_cache:
        cache.set(cache_key, {k: v for k, v in resp.items() if k != "quote"})

    return _json(resp)


@api_bp.route("/api/chips", methods=["GET"])
def chips():
    """东财筹码分布 (仅股票); ETF/指数或无数据返回 chips=null。"""
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    if _is_etf(symbol) or _is_index_symbol(symbol):
        return _json({"symbol": symbol, "chips": None})
    try:
        data = get_chips(symbol)
    except Exception as e:
        log.warning("获取筹码分布异常 %s: %s", symbol, _sanitize_error(e))
        return _error("获取筹码分布失败", 500)
    return _json({"symbol": symbol, "chips": data})


@api_bp.route("/api/intraday", methods=["GET"])
def intraday():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "5m"
    try:
        count = min(int(request.args.get("count") or "120"), 250)
    except ValueError:
        count = 120
    try:
        # 分钟源经 kline_source 路由 (默认 alphafeed, 失败自动回退), df 已标准化前复权
        df, _src = kline_source.fetch_kline_df(
            "minute", symbol, period, count, adjust="forward"
        )
        if df is None or len(df) == 0:
            return _error(f"无法获取 {symbol} 的分钟线", 404)
        last_day = df.index.normalize().max()
        mask = df.index.normalize() == last_day
        df = df[mask]
        if len(df) == 0:
            return _error("无分时数据", 404)
        df, indicators = compute_all_indicators(df, period="1d", with_atr_val=True)
        bars = []
        for i, (idx, row) in enumerate(df.iterrows()):
            ts = idx.strftime("%H:%M") if hasattr(idx, "strftime") else str(idx)[-8:-3]
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
        return _json({"symbol": symbol, "period": period, "bars": bars})
    except Exception as e:
        log.warning("获取分钟线失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("获取分钟线失败，请稍后重试", 500)
