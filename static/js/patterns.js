/**
 * K 线形态识别 (经典单根/双根/三根形态) — 纯前端, 与后端无关。
 * scanPatterns(klines) → [{idx, dir(+1看涨/-1看跌), name}]
 * patternsAt(klines, idx) → 该 bar 的形态 (tooltip 注入) 或 null。
 * 口径: 通达信/东财常见定义的简化版, 实体/影线以「幅度阈值」容忍浮点噪声。
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.PatternScanner = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var EPS = 1e-9;

  function body(k) { return Math.abs(k.close - k.open); }
  function upperShadow(k) { return k.high - Math.max(k.open, k.close); }
  function lowerShadow(k) { return Math.min(k.open, k.close) - k.low; }
  function isBull(k) { return k.close > k.open + EPS; }
  function isBear(k) { return k.close < k.open - EPS; }
  function trendBefore(klines, i, lookback) {
    // i 前 lookback 根的涨跌方向 (收-开 累计), >0 上涨
    var s = 0, from = Math.max(0, i - lookback);
    for (var j = from; j < i; j++) s += klines[j].close - klines[j].open;
    return s;
  }

  /** 单根形态: 返回 null 或 {dir, name, note}。锤头/上吊先于十字星判定 (更特异)。 */
  function single(k, prevTrend) {
    var range = k.high - k.low;
    if (range <= EPS) return null;
    var b = body(k);
    var up = upperShadow(k), low = lowerShadow(k);

    // 锤头 (下跌趋势末端): 下影 ≥ 实体2倍, 上影极短, 实体位于全幅上 1/3
    if (low >= b * 2 && up <= b * 0.3 + range * 0.02 && prevTrend < 0
        && Math.min(k.open, k.close) >= k.low + range * 0.6) {
      return { dir: 1, name: '锤头', note: '下影长, 下跌中见承接' };
    }
    // 上吊 (上涨趋势末端): 形态同锤头但出现在高位
    if (low >= b * 2 && up <= b * 0.3 + range * 0.02 && prevTrend > 0
        && Math.min(k.open, k.close) >= k.low + range * 0.6) {
      return { dir: -1, name: '上吊线', note: '高位长下影, 警惕见顶' };
    }
    // 十字星: 实体极小 (≤ 全幅 10%)
    if (b <= range * 0.1) {
      return { dir: 0, name: '十字星', note: '多空拉锯, 观望变盘' };
    }
    return null;
  }

  /** 双根形态: k=当前, p=前一根 */
  function double_(k, p, prevTrend) {
    var b1 = body(k), b0 = body(p);
    if (b1 <= EPS || b0 <= EPS) return null;
    // 看涨吞没: 前阴今阳, 阳线实体包住阴线实体
    if (isBear(p) && isBull(k) && k.close >= p.open && k.open <= p.close && b1 > b0) {
      return { dir: 1, name: '看涨吞没', note: '阳包阴, 多方反攻' };
    }
    // 看跌吞没: 前阳今阴, 阴线实体包住阳线实体
    if (isBull(p) && isBear(k) && k.close <= p.open && k.open >= p.close && b1 > b0) {
      return { dir: -1, name: '看跌吞没', note: '阴包阳, 空方反攻' };
    }
    // 曙光初现: 下跌中, 前长阴, 今低开阳线且收盘过前阴线实体中点
    if (prevTrend < 0 && isBear(p) && isBull(k)
        && k.open <= p.close && k.close > (p.open + p.close) / 2 && k.close < p.open) {
      return { dir: 1, name: '曙光初现', note: '低开高走, 见底信号' };
    }
    // 乌云盖顶: 上涨中, 前长阳, 今高开阴线且收盘破前阳线实体中点
    if (prevTrend > 0 && isBull(p) && isBear(k)
        && k.open >= p.close && k.close < (p.open + p.close) / 2 && k.close > p.open) {
      return { dir: -1, name: '乌云盖顶', note: '高开低走, 见顶信号' };
    }
    return null;
  }

  /** 三根形态: c=当前, b=前1, a=前2 */
  function triple(a, b, c) {
    // 红三兵: 连续三阳, 逐级抬升
    if (isBull(a) && isBull(b) && isBull(c)
        && b.close > a.close && c.close > b.close
        && body(b) < body(a) * 2.5 && body(c) < body(b) * 2.5) {
      return { dir: 1, name: '红三兵', note: '三连阳温和抬升' };
    }
    // 三只乌鸦: 连续三阴, 逐级下探
    if (isBear(a) && isBear(b) && isBear(c)
        && b.close < a.close && c.close < b.close) {
      return { dir: -1, name: '三只乌鸦', note: '三连阴连续下探' };
    }
    return null;
  }

  function scanPatterns(klines) {
    var out = [];
    if (!Array.isArray(klines)) return out;
    for (var i = 0; i < klines.length; i++) {
      var prevTrend = trendBefore(klines, i, 5);
      var hit = single(klines[i], prevTrend) || null;
      if (!hit && i >= 1) hit = double_(klines[i], klines[i - 1], prevTrend);
      if (!hit && i >= 2) hit = triple(klines[i - 2], klines[i - 1], klines[i]);
      if (hit) out.push({ idx: i, dir: hit.dir, name: hit.name, note: hit.note });
    }
    return out;
  }

  function patternsAt(klines, idx) {
    var all = scanPatterns(klines);
    for (var i = all.length - 1; i >= 0; i--) {
      if (all[i].idx === idx) return all[i];
    }
    return null;
  }

  return {
    scanPatterns: scanPatterns,
    patternsAt: patternsAt,
  };
});
