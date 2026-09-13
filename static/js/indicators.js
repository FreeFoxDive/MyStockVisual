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

  function obv(close, volume) {
    const n = close.length;
    const out = new Array(n).fill(0);
    for (let i = 1; i < n; i++) {
      const c = close[i];
      const pc = close[i - 1];
      const v = volume[i] || 0;
      if (c > pc) out[i] = out[i - 1] + v;
      else if (c < pc) out[i] = out[i - 1] - v;
      else out[i] = out[i - 1];
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

  /** 布林带 BOLL(N,K): MID=SMA(C,N), STD=样本标准差(ddof=1), UP/LOW=MID±K*STD */
  function boll(close, n, k) {
    n = n || 20;
    k = k == null ? 2 : k;
    const mid = sma(close, n);
    const up = new Array(close.length).fill(null);
    const low = new Array(close.length).fill(null);
    for (let i = n - 1; i < close.length; i++) {
      let acc = 0;
      for (let j = i - n + 1; j <= i; j++) {
        const d = close[j] - mid[i];
        acc += d * d;
      }
      const std = Math.sqrt(acc / (n - 1));
      up[i] = mid[i] + k * std;
      low[i] = mid[i] - k * std;
    }
    return { mid, up, low };
  }

  /** 威廉指标 WR(N): 100*(HHV(H,N)-C)/(HHV-LLV); 区间为 0 → null (与后端同口径) */
  function wr(high, low, close, n) {
    n = n || 14;
    const out = new Array(close.length).fill(null);
    for (let i = n - 1; i < close.length; i++) {
      let hhv = -Infinity;
      let llv = Infinity;
      for (let j = i - n + 1; j <= i; j++) {
        if (high[j] > hhv) hhv = high[j];
        if (low[j] < llv) llv = low[j];
      }
      const rng = hhv - llv;
      out[i] = rng > 0 ? 100 * (hhv - close[i]) / rng : null;
    }
    return out;
  }

  /** 顺势指标 CCI(N): (TP-MA(TP))/(0.015*MD), MD=N期内 |TP_i-MA_i| 均值; MD=0 → null */
  function cci(high, low, close, n) {
    n = n || 14;
    const len = close.length;
    const tp = new Array(len);
    for (let i = 0; i < len; i++) tp[i] = (high[i] + low[i] + close[i]) / 3;
    const maTp = sma(tp, n);
    // 与 pandas rolling(n, min_periods=n).mean() 一致: 窗口内每根都要有 MA(TP) → i >= 2n-2
    const md = new Array(len).fill(null);
    for (let i = 2 * n - 2; i < len; i++) {
      let s = 0;
      for (let j = i - n + 1; j <= i; j++) s += Math.abs(tp[j] - maTp[j]);
      md[i] = s / n;
    }
    const out = new Array(len).fill(null);
    for (let i = n - 1; i < len; i++) {
      if (md[i] == null || md[i] === 0) continue;
      out[i] = (tp[i] - maTp[i]) / (0.015 * md[i]);
    }
    return out;
  }

  /** 乖离率 BIAS(N): (C-MA(C,N))/MA(C,N)*100; 返回 {biasN: 数组} */
  function bias(close, periods) {
    periods = periods || [6, 12, 24];
    const out = {};
    periods.forEach((n) => {
      const ma = sma(close, n);
      const arr = new Array(close.length).fill(null);
      for (let i = n - 1; i < close.length; i++) {
        if (ma[i] == null) continue;
        arr[i] = (close[i] - ma[i]) / ma[i] * 100;
      }
      out['bias' + n] = arr;
    });
    return out;
  }

  /** 动向指标 DMI(N,M) (SUM 平滑口径): +DI/-DI/ADX; 返回 {pdi, mdi, adx} */
  function dmi(high, low, close, n, m) {
    n = n || 14;
    m = m || 6;
    const len = close.length;
    const tr = new Array(len);
    const pdm = new Array(len).fill(0);
    const mdm = new Array(len).fill(0);
    for (let i = 0; i < len; i++) {
      if (i === 0) {
        tr[i] = high[i] - low[i];   // 首根无前收, ~ pandas max 跳过 NaN
        continue;
      }
      tr[i] = Math.max(
        high[i] - low[i],
        Math.abs(high[i] - close[i - 1]),
        Math.abs(low[i] - close[i - 1]),
      );
      const hd = high[i] - high[i - 1];
      const ld = low[i - 1] - low[i];
      pdm[i] = (hd > 0 && hd > ld) ? hd : 0;
      mdm[i] = (ld > 0 && ld > hd) ? ld : 0;
    }
    const rollingSum = (arr) => {
      const out = new Array(len).fill(null);
      for (let i = n - 1; i < len; i++) {
        let s = 0;
        for (let j = i - n + 1; j <= i; j++) s += arr[j];
        out[i] = s;
      }
      return out;
    };
    const trN = rollingSum(tr);
    const pdmN = rollingSum(pdm);
    const mdmN = rollingSum(mdm);
    const pdi = new Array(len).fill(null);
    const mdi = new Array(len).fill(null);
    const dx = new Array(len).fill(null);
    for (let i = 0; i < len; i++) {
      if (trN[i] == null || trN[i] === 0) continue;
      pdi[i] = pdmN[i] / trN[i] * 100;
      mdi[i] = mdmN[i] / trN[i] * 100;
      const denom = pdi[i] + mdi[i];
      dx[i] = denom > 0 ? Math.abs(pdi[i] - mdi[i]) / denom * 100 : null;
    }
    const adx = new Array(len).fill(null);
    for (let i = m - 1; i < len; i++) {
      let s = 0;
      let bad = false;
      for (let j = i - m + 1; j <= i; j++) {
        if (dx[j] == null) { bad = true; break; }
        s += dx[j];
      }
      if (!bad) adx[i] = s / m;
    }
    return { pdi, mdi, adx };
  }

  function recalcTailIndicators(klines, period) {
    if (!klines || !klines.length) return;
    const start = Math.max(0, klines.length - TAIL_WINDOW);
    const slice = klines.slice(start);
    const h = slice.map((k) => k.high);
    const l = slice.map((k) => k.low);
    const c = slice.map((k) => k.close);

    const vv = slice.map((k) => k.volume);
    const ma5 = sma(c, 5);
    const ma10 = sma(c, 10);
    const ma20 = sma(c, 20);
    const volMa5 = sma(vv, 5);
    const volMa10 = sma(vv, 10);
    const volMa20 = sma(vv, 20);

    // OBV 为全量累加 (只用末段会从 0 起算而失真); MAOBV 取全量 OBV 末段
    const obvFull = obv(klines.map((k) => k.close), klines.map((k) => k.volume));
    const obvTail = obvFull.slice(Math.max(0, obvFull.length - TAIL_WINDOW));
    const maobvTail = sma(obvTail, 30);

    const mp = getMacdParams(period || '1d');
    const { dif, dea, hist } = macd(c, mp.fast, mp.slow, mp.signal);
    const e13 = ema(c, 13);
    const { k: kdjK, d: kdjD, j: kdjJ } = kdj(h, l, c, 9);
    const rsi6 = rsi(c, 6);
    const rsi12 = rsi(c, 12);
    const rsi24 = rsi(c, 24);
    const atr14 = atr(h, l, c, 14);
    const bollVals = boll(c, 20, 2);
    const wr14 = wr(h, l, c, 14);
    const cci14 = cci(h, l, c, 14);
    const biasMap = bias(c, [6, 12, 24]);
    const dmiVals = dmi(h, l, c, 14, 6);

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
    last.vol_ma5 = volMa5[i];
    last.vol_ma10 = volMa10[i];
    last.vol_ma20 = volMa20[i];
    last.obv = obvFull[obvFull.length - 1];
    last.maobv = maobvTail[maobvTail.length - 1];
    // 主图叠加与扩展面板: 追加/快照更新末根后一并补齐, 否则图例/面板末点显示 "—"
    last.boll_mid = bollVals.mid[i];
    last.boll_up = bollVals.up[i];
    last.boll_low = bollVals.low[i];
    last.wr14 = wr14[i];
    last.cci14 = cci14[i];
    last.bias6 = biasMap.bias6[i];
    last.bias12 = biasMap.bias12[i];
    last.bias24 = biasMap.bias24[i];
    last.dmi_pdi = dmiVals.pdi[i];
    last.dmi_mdi = dmiVals.mdi[i];
    last.dmi_adx = dmiVals.adx[i];
  }

  return {
    getMacdParams,
    sma,
    ema,
    macd,
    kdj,
    rsi,
    atr,
    obv,
    boll,
    wr,
    cci,
    bias,
    dmi,
    recalcTailIndicators,
  };
});
