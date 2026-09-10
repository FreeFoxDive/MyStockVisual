#!/usr/bin/env python3
"""
Technical indicator calculation module - based on stock-indicators-cn package
=============================================================================
All indicators align with East Money/Tonghuashun/TDX standard algorithms.

Thin wrapper around stock_indicators_cn — atr, macd, get_macd_params
are imported directly from the library. Only compute_all_indicators, compute_impulse
and _safe_list are local (convenience aggregator + JSON-safe serialization).
"""

import numpy as np
import pandas as pd
from stock_indicators_cn import (
    ema, sma, kdj, rsi, force_index,
    atr, macd, get_macd_params,
)


def obv(close, volume):
    """能量潮 OBV (通达信/东财口径): 首根为 0, 涨累加量、跌累减量、平不变。"""
    c = pd.Series(close)
    v = pd.Series(volume)
    diff = c.diff()
    sign = pd.Series(0.0, index=c.index)
    sign[diff > 0] = 1.0
    sign[diff < 0] = -1.0
    return (sign * v).cumsum()


def compute_impulse(close, macd_params=None):
    """Elder Impulse System: 1=bullish(红), -1=bearish(绿), 0=neutral(蓝)。

    用 EMA13 方向 + MACD 柱方向决定蜡烛颜色 (与 v7 动力管线口径一致)。
    macd_params: {"fast": 12, "slow": 26, "signal": 9}, 缺省取日线标准参数。
    """
    if macd_params is None:
        macd_params = {"fast": 12, "slow": 26, "signal": 9}
    e13 = ema(close, 13)
    dif, dea, hist = macd(close, macd_params["fast"], macd_params["slow"], macd_params["signal"])
    impulse = pd.Series(0, index=close.index, dtype=int)
    for i in range(1, len(close)):
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
    return impulse


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

    # Volume MA (东财/通达信默认 5/10/20)
    result_df["vol_ma5"] = sma(v, 5)
    result_df["vol_ma10"] = sma(v, 10)
    result_df["vol_ma20"] = sma(v, 20)

    # OBV (能量潮) + MAOBV (默认 30)
    obv_series = obv(c, v)
    maobv = sma(obv_series, 30)
    result_df["obv"] = obv_series
    result_df["maobv"] = maobv

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
        },
        "obv": {
            "params": {"ma_period": 30},
            "obv": _safe_list(obv_series),
            "maobv": _safe_list(maobv),
        },
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
    impulse = compute_impulse(c, mp)
    result_df["impulse"] = impulse
    indicators["impulse"] = {
        "params": {"ema_period": 13},
        "values": _safe_list(impulse),
    }

    return result_df, indicators


def _safe_list(series):
    """Convert Series to list, NaN -> None (JSON null)"""
    return [None if pd.isna(x) else float(x) for x in series.values]
