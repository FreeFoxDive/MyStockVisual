#!/usr/bin/env python3
"""
Technical indicator calculation module - based on stock-indicators-cn package
=============================================================================
All indicators align with East Money/Tonghuashun/TDX standard algorithms.

Thin wrapper around stock_indicators_cn — atr, macd, get_macd_params
are imported directly from the library. Only compute_all_indicators and _safe_list
are local (convenience aggregator + JSON-safe serialization).
"""

import numpy as np
import pandas as pd
from stock_indicators_cn import (
    ema, sma, kdj, rsi, force_index,
    atr, macd, get_macd_params,
)


def compute_all_indicators(df, period="1d",
                           with_rsi=True, with_kdj=True, with_atr_val=True):
    """Compute all indicators on OHLCV DataFrame
    Returns: (enhanced_df, indicators_dict)
    """
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]

    # MA (SMA simple moving average)
    result_df = df.copy()
    result_df["ma5"] = sma(c, 5)
    result_df["ma10"] = sma(c, 10)
    result_df["ma20"] = sma(c, 20)

    # Volume MA
    result_df["vol_ma5"] = sma(v, 5)
    result_df["vol_ma10"] = sma(v, 10)

    # MACD
    mp = get_macd_params(period)
    dif, dea, hist = macd(c, mp["fast"], mp["slow"], mp["signal"])
    result_df["macd_dif"] = dif
    result_df["macd_dea"] = dea
    result_df["macd_hist"] = hist

    # Elder Impulse System: EMA 13
    e13 = ema(c, 13)
    result_df["ema13"] = e13

    indicators = {
        "macd": {
            "params": mp,
            "dif": _safe_list(dif),
            "dea": _safe_list(dea),
            "hist": _safe_list(hist),
        }
    }

    # RSI
    if with_rsi:
        rsi6 = rsi(c, 6)
        rsi12 = rsi(c, 12)
        rsi24 = rsi(c, 24)
        result_df["rsi6"] = rsi6
        result_df["rsi12"] = rsi12
        result_df["rsi24"] = rsi24
        indicators["rsi"] = {
            "params": {"periods": [6, 12, 24]},
            "rsi6": _safe_list(rsi6),
            "rsi12": _safe_list(rsi12),
            "rsi24": _safe_list(rsi24),
        }

    # KDJ
    if with_kdj:
        k, d, j = kdj(h, l, c, 9)
        result_df["kdj_k"] = k
        result_df["kdj_d"] = d
        result_df["kdj_j"] = j
        indicators["kdj"] = {
            "params": {"period": 9},
            "k": _safe_list(k),
            "d": _safe_list(d),
            "j": _safe_list(j),
        }

    # ATR
    if with_atr_val:
        a = atr(h, l, c, 14)
        result_df["atr14"] = a
        indicators["atr"] = {
            "params": {"period": 14},
            "values": _safe_list(a),
        }

    # Elder Impulse System: 1=bullish(红), -1=bearish(绿), 0=neutral(蓝)
    impulse = pd.Series(0, index=c.index, dtype=int)
    for i in range(1, len(c)):
        e13_i = e13.iloc[i]
        e13_prev = e13.iloc[i - 1]
        hist_i = hist.iloc[i]
        hist_prev = hist.iloc[i - 1]
        if pd.isna(e13_i) or pd.isna(e13_prev) or pd.isna(hist_i) or pd.isna(hist_prev):
            continue
        ema_up = e13_i > e13_prev
        hist_up = hist_i > hist_prev
        if ema_up and hist_up:
            impulse.iloc[i] = 1
        elif not ema_up and not hist_up:
            impulse.iloc[i] = -1
    result_df["impulse"] = impulse
    indicators["impulse"] = {
        "params": {"ema_period": 13},
        "values": _safe_list(impulse),
    }

    return result_df, indicators


def _safe_list(series):
    """Convert Series to list, NaN -> None (JSON null)"""
    return [None if pd.isna(x) else float(x) for x in series.values]
