#!/usr/bin/env python3
"""
技术指标计算模块 — 从 triple_screen_v5.py 提取
==============================================
全部对标东方财富/同花顺/通达信标准算法，已逐项与东财PC端验证通过。

指标列表:
  EMA   — 指数移动平均
  ATR   — 平均真实波幅 (period=14)
  MACD  — 指数平滑异同移动平均线 (日:12/26/9, 周:6/13/5)
  KDJ   — 随机指标 (period=9, EMA平滑)
  RSI   — 相对强弱指标 (Wilder原始算法)
  ForceIndex — 强力指数 (period=2)
"""

import numpy as np
import pandas as pd


def ema(s, span):
    """EMA 指数移动平均: ewm(span, adjust=False)"""
    return s.ewm(span=span, adjust=False).mean()


def atr(high, low, close, period=14):
    """ATR 平均真实波幅 (period=14)
    TR = max(H-L, |H-C₋₁|, |L-C₋₁|), 然后 TR 的 SMA(period)"""
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def macd(close, fast=12, slow=26, signal=9):
    """MACD 指数平滑异同移动平均线
    DIF = EMA(close, fast) - EMA(close, slow)
    DEA = EMA(DIF, signal)
    柱  = 2 × (DIF - DEA)  ← 国内平台通用2倍显示
    返回: (dif, dea, histogram)"""
    ef, es = ema(close, fast), ema(close, slow)
    ml = ef - es
    return ml, ema(ml, signal), 2 * (ml - ema(ml, signal))


def kdj(high, low, close, period=9):
    """KDJ 随机指标 (period=9)
    算法:
      1. RSV(n) = (C - Ln) / (Hn - Ln) × 100
      2. K = 2/3 × Kₜ₋₁ + 1/3 × RSV  (EMA平滑, K₀=50)
      3. D = 2/3 × Dₜ₋₁ + 1/3 × K    (EMA平滑, D₀=50)
      4. J = 3×K - 2×D
    返回: (k, d, j)"""
    ll, hh = low.rolling(period).min(), high.rolling(period).max()
    rsv = 100 * (close - ll) / (hh - ll).mask((hh - ll).abs() < 1e-8, other=np.nan)

    k_vals, d_vals, prev_k, prev_d = [], [], 50.0, 50.0
    for r in rsv:
        if pd.isna(r):
            k_vals.append(np.nan)
            d_vals.append(np.nan)
        else:
            k = 2 / 3 * prev_k + 1 / 3 * r
            d = 2 / 3 * prev_d + 1 / 3 * k
            k_vals.append(k)
            d_vals.append(d)
            prev_k, prev_d = k, d

    k_series = pd.Series(k_vals, index=close.index)
    d_series = pd.Series(d_vals, index=close.index)
    return k_series, d_series, 3 * k_series - 2 * d_series


def rsi(close, period=6):
    """RSI 相对强弱指标 — Wilder 原始算法
    前 period 根: SMA 初始化涨均值/跌均值
    第 period+1 根起: Wilder EMA (alpha=1/period)
    RSI = 100 - 100 / (1 + RS)"""
    d = close.diff()
    g, l = d.clip(lower=0), (-d).clip(lower=0)

    result = pd.Series(np.nan, index=close.index)
    first_valid_diffs = period
    if len(close) <= first_valid_diffs:
        return result

    # Initial SMA over first 'period' diffs
    avg_g = float(g.iloc[1:first_valid_diffs + 1].mean())
    avg_l = float(l.iloc[1:first_valid_diffs + 1].mean())
    rs = avg_g / avg_l if avg_l > 0 else (np.inf if avg_g > 0 else 0)
    result.iloc[first_valid_diffs] = 100 - 100 / (1 + rs)

    # Subsequent bars: Wilder EMA
    for i in range(first_valid_diffs + 1, len(close)):
        avg_g = (avg_g * (period - 1) + g.iloc[i]) / period
        avg_l = (avg_l * (period - 1) + l.iloc[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else (np.inf if avg_g > 0 else 0)
        result.iloc[i] = 100 - 100 / (1 + rs)

    return result


def force_index(close, volume, period=2):
    """Force Index 强力指数 (period=2)
    FI = (Cₜ - Cₜ₋₁) × Volume, EMA(span=2) 平滑"""
    return (close.diff() * volume).ewm(span=period, adjust=False).mean()


# ── MACD 参数配置 ──

MACD_PARAMS = {
    "1d": {"fast": 12, "slow": 26, "signal": 9},
    "1w": {"fast": 6, "slow": 13, "signal": 5},
    "1M": {"fast": 6, "slow": 13, "signal": 5},
    "macd13": {"fast": 13, "slow": 30, "signal": 10},
}


def get_macd_params(period, use_macd13=False):
    """根据周期和选项返回 MACD 参数"""
    if use_macd13:
        return MACD_PARAMS["macd13"]
    return MACD_PARAMS.get(period, MACD_PARAMS["1d"])


def compute_all_indicators(df, period="1d", use_macd13=False,
                           with_rsi=True, with_kdj=True, with_atr_val=True):
    """对 OHLCV DataFrame 计算全部指标
    返回: (enhanced_df, indicators_dict)
    """
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]

    # MA
    result_df = df.copy()
    result_df["ma5"] = ema(c, 5)
    result_df["ma10"] = ema(c, 10)
    result_df["ma20"] = ema(c, 20)

    # 成交量MA
    result_df["vol_ma5"] = ema(v, 5)
    result_df["vol_ma10"] = ema(v, 10)

    # MACD
    mp = get_macd_params(period, use_macd13)
    dif, dea, hist = macd(c, mp["fast"], mp["slow"], mp["signal"])
    result_df["macd_dif"] = dif
    result_df["macd_dea"] = dea
    result_df["macd_hist"] = hist

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

    return result_df, indicators


def _safe_list(series):
    """将 Series 转为 list, NaN → None (JSON null)"""
    return [None if pd.isna(x) else float(x) for x in series.values]
