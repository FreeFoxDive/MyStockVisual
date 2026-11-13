/**
 * 以损定量仓位计算（纯函数，页面与 Node 测试共用）。
 *
 * 股数 = min(本金×风险% / (止损倍数×ATR), 本金/买入价)，再按 100 股向下取整，
 * 保证实际亏损不超过风险预算，也不会算出超过本金的仓位。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.VisualPosCalc = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const DEFAULT_POS_CALC = Object.freeze({
    capital: 100000,
    riskPct: 2,
    atrMult: 2,
    atrPeriod: 14,
  });

  function normalizePosCalcConfig(raw) {
    const src = raw && typeof raw === 'object' ? raw : {};
    const num = (v, fallback, lo, hi) => {
      // 与服务端 _clean_pos_calc 一致: true 不能当成 1
      if (typeof v === 'boolean') return fallback;
      const n = Number(v);
      if (!Number.isFinite(n) || n < lo || n > hi) return fallback;
      return n;
    };
    const period = typeof src.atrPeriod === 'boolean' ? NaN : Number(src.atrPeriod);
    return {
      capital: num(src.capital, DEFAULT_POS_CALC.capital, 1000, 1e9),
      riskPct: num(src.riskPct, DEFAULT_POS_CALC.riskPct, 0.05, 50),
      atrMult: num(src.atrMult, DEFAULT_POS_CALC.atrMult, 0.1, 20),
      atrPeriod: period === 20 ? 20 : (period === 14 ? 14 : DEFAULT_POS_CALC.atrPeriod),
    };
  }

  function roundTo(v, dp) {
    const f = Math.pow(10, dp);
    return Math.round(v * f) / f;
  }

  function formatPrice(v, dp) {
    return roundTo(v, dp).toFixed(dp);
  }

  function formatPct(v) {
    if (!Number.isFinite(v)) return '—';
    const s = Number(v).toFixed(1);
    return s.endsWith('.0') ? s.slice(0, -2) : s;
  }

  /** 金额: 满 1 万用「万」, 否则取整加「元」。 */
  function formatMoney(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '—';
    const sign = n < 0 ? '-' : '';
    const a = Math.abs(n);
    if (a >= 10000) {
      const w = a / 10000;
      const s = Math.abs(w - Math.round(w)) < 1e-9
        ? String(Math.round(w))
        : w.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
      return sign + s + '万';
    }
    return sign + String(Math.round(a)) + '元';
  }

  /**
   * @param {{entryPrice:number, atr:number, capital?:number, riskPct?:number,
   *   atrMult?:number, atrPeriod?:number, isEtf?:boolean}} input
   * @returns {object|null} 价格或 ATR 无效时 null; 止损价≤0 时 reason='stop_non_positive'
   */
  function calcPositionSizing(input) {
    const cfg = normalizePosCalcConfig(input || {});
    const entry = Number(input && input.entryPrice);
    const atrVal = Number(input && input.atr);
    const isEtf = !!(input && input.isEtf);
    if (!(entry > 0) || !(atrVal > 0)) return null;
    const stopDist = cfg.atrMult * atrVal;
    if (!(stopDist > 0) || !(cfg.capital > 0) || !(cfg.riskPct > 0)) return null;
    const dp = isEtf ? 3 : 2;
    const stopPrice = roundTo(entry - stopDist, dp);
    const base = {
      ok: false, cfg: cfg, entryPrice: entry, atr: atrVal, isEtf: isEtf, dp: dp,
      stopDist: stopDist, stopPrice: stopPrice,
      riskBudget: cfg.capital * cfg.riskPct / 100,
    };
    if (!(stopPrice > 0)) {
      base.reason = 'stop_non_positive';
      return base;
    }
    const byRiskShares = Math.floor(base.riskBudget / stopDist / 100) * 100;
    const byCapShares = Math.floor(cfg.capital / entry / 100) * 100;
    const shares = Math.max(0, Math.min(byRiskShares, byCapShares));
    const cost = shares * entry;
    let note = null;
    if (shares < 100) {
      note = (byCapShares < 100 && byCapShares <= byRiskShares)
        ? '不足1手（超本金）' : '不足1手（超风险预算）';
    } else if (byRiskShares > byCapShares) {
      note = '受本金上限约束';
    }
    return Object.assign(base, {
      ok: shares >= 100,
      reason: null,
      shares: shares,
      lots: shares / 100,
      byRiskShares: byRiskShares,
      byCapShares: byCapShares,
      cost: cost,
      posPct: cost / cfg.capital * 100,
      actualRisk: shares * stopDist,
      actualRiskPct: shares * stopDist / cfg.capital * 100,
      note: note,
    });
  }

  function sharesText(r) {
    if (!r || r.shares < 100) return '不足1手';
    return r.lots + '手 (' + formatPct(r.posPct) + '%)';
  }

  function positionTitle(r) {
    if (!r || r.reason === 'stop_non_positive') return '';
    const cfg = r.cfg;
    const parts = [
      formatPct(cfg.riskPct) + '% × ' + formatMoney(cfg.capital),
      '止损 ' + cfg.atrMult + '×ATR' + cfg.atrPeriod + ' ' + r.atr.toFixed(3),
      r.shares >= 100 ? (r.shares + '股') : (r.note || '不足1手'),
      '建仓 ' + formatMoney(r.cost),
      '最大亏损 ' + formatMoney(r.actualRisk) + ' (' + formatPct(r.actualRiskPct) + '%)',
    ];
    if (r.note && r.shares >= 100) parts.push(r.note);
    parts.push('点击修改算仓参数');
    return parts.join(' · ');
  }

  function stopTitle(r) {
    if (!r || !(r.stopPrice > 0)) return '';
    const cfg = r.cfg;
    const pct = (r.stopPrice / r.entryPrice - 1) * 100;
    return cfg.atrMult + '×ATR' + cfg.atrPeriod
      + ' 止损 ' + formatPrice(r.stopPrice, r.dp)
      + ' (' + formatPct(pct) + '%)'
      + ' · 每股风险 ' + r.stopDist.toFixed(3)
      + ' · 点击修改算仓参数';
  }

  /**
   * 侧栏两行。指数、缺价格或缺对应 ATR 时返回 []（调用方滤掉空行）。
   * @returns {Array<{label:string,text:string,color:*,title:string,action:string}>}
   */
  function formatSideRows(ctx) {
    if (!ctx || ctx.isIndex) return [];
    const cfg = normalizePosCalcConfig(ctx.config);
    const atr = Number(cfg.atrPeriod === 20 ? ctx.atr20 : ctx.atr14);
    const entry = Number(ctx.entryPrice);
    if (!(atr > 0) || !(entry > 0)) return [];
    const r = calcPositionSizing({
      entryPrice: entry,
      atr: atr,
      isEtf: !!ctx.isEtf,
      capital: cfg.capital,
      riskPct: cfg.riskPct,
      atrMult: cfg.atrMult,
      atrPeriod: cfg.atrPeriod,
    });
    if (!r || r.reason === 'stop_non_positive' || !(r.stopPrice > 0)) return [];
    return [
      {
        label: '建议仓位',
        text: sharesText(r),
        color: null,
        title: positionTitle(r),
        action: 'poscalc',
      },
      {
        label: '止损参考',
        text: formatPrice(r.stopPrice, r.dp),
        color: ctx.downColor || null,
        title: stopTitle(r),
        action: 'poscalc',
      },
    ];
  }

  return {
    DEFAULT_POS_CALC: DEFAULT_POS_CALC,
    normalizePosCalcConfig: normalizePosCalcConfig,
    calcPositionSizing: calcPositionSizing,
    formatSideRows: formatSideRows,
    formatPrice: formatPrice,
    formatPct: formatPct,
    formatMoney: formatMoney,
    sharesText: sharesText,
    positionTitle: positionTitle,
    stopTitle: stopTitle,
  };
});
