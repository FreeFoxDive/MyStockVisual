/**
 * 技术指标末根重算（与 stock_indicators_cn / visual/indicators.py 口径对齐）
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.IndicatorCalc = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MACD_PARAMS = {
    '1d': { fast: 12, slow: 26, signal: 9 },
    '1w': { fast: 6, slow: 13, signal: 5 },
    '1M': { fast: 6, slow: 13, signal: 5 },
  };

  function getMacdParams(period) {
    return MACD_PARAMS[period] || MACD_PARAMS['1d'];
  }

  function sma(arr, window) {
    const out = new Array(arr.length).fill(null);
    for (let i = window - 1; i < arr.length; i++) {
      let s = 0;
      for (let j = i - window + 1; j <= i; j++) s += arr[j];
      out[i] = s / window;
    }
    return out;
  }

  function ema(arr, span) {
    const out = new Array(arr.length).fill(null);
    if (!arr.length) return out;
    const alpha = 2 / (span + 1);
    out[0] = arr[0];
    for (let i = 1; i < arr.length; i++) {
      out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1];
    }
    return out;
  }

  function macd(close, fast, slow, signal) {
    const ef = ema(close, fast);
    const es = ema(close, slow);
    const dif = close.map((_, i) => ef[i] - es[i]);
    const dea = ema(dif, signal);
    const hist = dif.map((d, i) => 2 * (d - dea[i]));
    return { dif, dea, hist };
  }

  function kdj(high, low, close, period) {
    const n = close.length;
    const kVals = new Array(n).fill(null);
    const dVals = new Array(n).fill(null);
    let prevK = 50;
    let prevD = 50;
    let hasValid = false;

    for (let i = 0; i < n; i++) {
      if (i < period - 1) continue;
      let hh = -Infinity;
      let ll = Infinity;
      for (let j = i - period + 1; j <= i; j++) {
        if (high[j] > hh) hh = high[j];
        if (low[j] < ll) ll = low[j];
      }
      const denom = hh - ll;
      let rsv;
      if (denom < 1e-8) {
        rsv = null;
      } else {
        rsv = 100 * (close[i] - ll) / denom;
      }

      if (rsv == null || Number.isNaN(rsv)) {
        if (hasValid) {
          kVals[i] = prevK;
          dVals[i] = prevD;
        }
        continue;
      }
      const k = (2 / 3) * prevK + (1 / 3) * rsv;
      const d = (2 / 3) * prevD + (1 / 3) * k;
      kVals[i] = k;
      dVals[i] = d;
      prevK = k;
      prevD = d;
      hasValid = true;
    }

    const jVals = kVals.map((k, i) => (k == null || dVals[i] == null ? null : 3 * k - 2 * dVals[i]));
    return { k: kVals, d: dVals, j: jVals };
  }

  function rsi(close, period) {
    const n = close.length;
    const out = new Array(n).fill(null);
    if (n <= period) return out;

    const gains = new Array(n).fill(0);
    const losses = new Array(n).fill(0);
    for (let i = 1; i < n; i++) {
      const d = close[i] - close[i - 1];
      if (d > 0) gains[i] = d;
      else losses[i] = -d;
    }

    const valid = [];
    for (let i = 1; i < n; i++) {
      if (!Number.isNaN(close[i]) && !Number.isNaN(close[i - 1])) valid.push(i);
    }
    if (valid.length < period) return out;

    const initIdx = valid.slice(0, period);
    let avgG = initIdx.reduce((s, i) => s + gains[i], 0) / period;
    let avgL = initIdx.reduce((s, i) => s + losses[i], 0) / period;
    const firstValidIdx = initIdx[period - 1];
    let rs = avgL > 0 ? avgG / avgL : (avgG > 0 ? Infinity : 0);
    out[firstValidIdx] = 100 - 100 / (1 + rs);

    const startPos = valid.indexOf(firstValidIdx);
    for (let vi = startPos + 1; vi < valid.length; vi++) {
      const i = valid[vi];
      avgG = (avgG * (period - 1) + gains[i]) / period;
      avgL = (avgL * (period - 1) + losses[i]) / period;
      rs = avgL > 0 ? avgG / avgL : (avgG > 0 ? Infinity : 0);
      out[i] = 100 - 100 / (1 + rs);
    }
    return out;
  }

  function atr(high, low, close, period) {
    const tr = new Array(close.length).fill(null);
    for (let i = 0; i < close.length; i++) {
      if (i === 0) {
        tr[i] = high[i] - low[i];
      } else {
        tr[i] = Math.max(
          high[i] - low[i],
          Math.abs(high[i] - close[i - 1]),
          Math.abs(low[i] - close[i - 1]),
        );
      }
    }
    return sma(tr, period);
  }

  const TAIL_WINDOW = 60;

  function recalcTailIndicators(klines, period) {
    if (!klines || !klines.length) return;
    const start = Math.max(0, klines.length - TAIL_WINDOW);
    const slice = klines.slice(start);
    const h = slice.map((k) => k.high);
    const l = slice.map((k) => k.low);
    const c = slice.map((k) => k.close);

    const ma5 = sma(c, 5);
    const ma10 = sma(c, 10);
    const ma20 = sma(c, 20);

    const mp = getMacdParams(period || '1d');
    const { dif, dea, hist } = macd(c, mp.fast, mp.slow, mp.signal);
    const e13 = ema(c, 13);
    const { k: kdjK, d: kdjD, j: kdjJ } = kdj(h, l, c, 9);
    const rsi6 = rsi(c, 6);
    const rsi12 = rsi(c, 12);
    const rsi24 = rsi(c, 24);
    const atr14 = atr(h, l, c, 14);

    const i = slice.length - 1;
    const last = klines[klines.length - 1];
    last.ma5 = ma5[i];
    last.ma10 = ma10[i];
    last.ma20 = ma20[i];
    last.macd_dif = dif[i];
    last.macd_dea = dea[i];
    last.macd_hist = hist[i];
    last.ema13 = e13[i];
    last.kdj_k = kdjK[i];
    last.kdj_d = kdjD[i];
    last.kdj_j = kdjJ[i];
    last.rsi6 = rsi6[i];
    last.rsi12 = rsi12[i];
    last.rsi24 = rsi24[i];
    last.atr14 = atr14[i];
  }

  return {
    getMacdParams,
    sma,
    ema,
    macd,
    kdj,
    rsi,
    atr,
    recalcTailIndicators,
  };
});
