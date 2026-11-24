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
    halfKelly: false,
    hkSource: 'manual',
    hkWinRate: null,
    hkPayoff: null,
    hkModelId: null,
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
    const optNum = (v, lo, hi, openLo) => {
      if (v == null || v === '' || typeof v === 'boolean') return null;
      const n = Number(v);
      if (!Number.isFinite(n) || n > hi || (openLo ? n <= lo : n < lo)) return null;
      return n;
    };
    const period = typeof src.atrPeriod === 'boolean' ? NaN : Number(src.atrPeriod);
    const hkSource = src.hkSource === 'model' ? 'model' : 'manual';
    let hkModelId = null;
    if (src.hkModelId === 'none') {
      hkModelId = 'none';
    } else if (typeof src.hkModelId === 'number' && Number.isInteger(src.hkModelId) && src.hkModelId > 0) {
      hkModelId = src.hkModelId;
    } else if (typeof src.hkModelId === 'string' && /^[1-9]\d*$/.test(src.hkModelId)) {
      hkModelId = Number(src.hkModelId);
    }
    return {
      capital: num(src.capital, DEFAULT_POS_CALC.capital, 1000, 1e9),
      riskPct: num(src.riskPct, DEFAULT_POS_CALC.riskPct, 0.05, 50),
      atrMult: num(src.atrMult, DEFAULT_POS_CALC.atrMult, 0.1, 20),
      atrPeriod: period === 20 ? 20 : (period === 14 ? 14 : DEFAULT_POS_CALC.atrPeriod),
      halfKelly: src.halfKelly === true,
      hkSource: hkSource,
      // 手填值一直留着。模型源计算不读它们, 切回手动时还在。
      hkWinRate: optNum(src.hkWinRate, 0, 100, false),
      hkPayoff: optNum(src.hkPayoff, 0, 1000, true),
      hkModelId: hkModelId,
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

  /**
   * 半凯利: f* = p - (1-p)/b, 取一半后按标的买入规则向下取整。
   * @returns {object|null} 买入价或本金无效时 null
   */
  function calcHalfKelly(input) {
    const entry = Number(input && input.entryPrice);
    const capital = Number(input && input.capital);
    if (!Number.isFinite(entry) || !Number.isFinite(capital) || !(entry > 0) || !(capital > 0)) return null;
    const winRate = Number(input && input.winRate);
    const payoff = Number(input && input.payoff);
    const isStar = /^68\d{4}(?:\.SH)?$/i.test(String(input && input.symbol));
    const minShares = isStar ? 200 : 100;
    const step = isStar ? 1 : 100;
    const base = {
      ok: false, entryPrice: entry, capital: capital,
      isStar: isStar,
      winRate: Number.isFinite(winRate) ? winRate : null,
      payoff: Number.isFinite(payoff) ? payoff : null,
      kelly: null, half: null, shares: 0, lots: 0, cost: 0, posPct: 0, note: null,
    };
    if (input.winRate == null || !(winRate >= 0) || winRate > 100 || !Number.isFinite(payoff) || !(payoff > 0)) {
      base.reason = 'params';
      return base;
    }
    const p = winRate / 100;
    const kelly = p - (1 - p) / payoff;
    const half = Math.max(0, kelly / 2);
    const units = (capital * half / entry) / step;
    const nearest = Math.round(units);
    // 抵消整手/整股边界上的浮点误差，不把真正不足的数量进位。
    const rounded = (Number.isSafeInteger(nearest) && nearest - units <= Number.EPSILON * Math.max(1, units) * 4)
      ? nearest : Math.floor(units);
    const candidate = rounded * step;
    const shares = Number.isSafeInteger(candidate) && candidate >= minShares ? candidate : 0;
    const cost = shares * entry;
    let reason = null;
    let note = null;
    if (!(half > 0)) {
      reason = 'non_positive';
      note = '期望非正，仓位为 0';
    } else if (!shares) {
      reason = 'below_lot';
      note = isStar ? '不足200股' : '不足1手';
    }
    return Object.assign(base, {
      ok: shares > 0,
      reason: reason,
      kelly: kelly,
      half: half,
      shares: shares,
      lots: isStar ? null : shares / 100,
      cost: cost,
      posPct: cost / capital * 100,
      note: note,
    });
  }

  /** 量化分析 summary → 内存态。不改配置; 换一组成交结果就会得到新的胜率/盈亏比。 */
  function hkLiveFromQuant(modelId, payload, now) {
    const summary = payload && payload.summary && typeof payload.summary === 'object'
      ? payload.summary : null;
    const read = (v) => (v == null || v === '' || typeof v === 'boolean' ? NaN : Number(v));
    const win = summary ? read(summary.win_rate) : NaN;
    const payoff = summary ? read(summary.payoff_ratio) : NaN;
    const closed = summary ? read(summary.closed_count) : NaN;
    const winRate = Number.isFinite(win) && win >= 0 && win <= 100 ? win : null;
    const payoffOk = Number.isFinite(payoff) && payoff > 0 ? payoff : null;
    return {
      modelId: modelId,
      winRate: winRate,
      payoff: payoffOk,
      closedCount: Number.isFinite(closed) ? closed : null,
      fetchedAt: now == null ? null : now,
      error: (winRate == null || payoffOk == null) ? '样本不足' : null,
    };
  }

  /** 已选的模型绑定: 正整数 id, 或 'none'（交易记录未指定模型）。 */
  function hkModelBound(id) {
    return id === 'none' || (typeof id === 'number' && Number.isInteger(id) && id > 0);
  }

  function hkLiveHint(hkLive) {
    if (!hkLive) return '半凯利：正在读取模型统计…';
    if (hkLive.error === '未登录') return '半凯利：未登录';
    if (hkLive.error === '加载失败') return '半凯利：加载失败';
    if (hkLive.error || hkLive.winRate == null || !(Number(hkLive.payoff) > 0)) {
      return '半凯利：样本不足';
    }
    return '';
  }

  function pickHalfKellyInputs(cfg, hkLive) {
    const c = normalizePosCalcConfig(cfg);
    if (!c.halfKelly) return { ready: false, hint: '' };
    if (c.hkSource === 'model') {
      if (!hkModelBound(c.hkModelId)) return { ready: false, hint: '半凯利：请选择模型' };
      const hint = hkLiveHint(hkLive && hkLive.modelId === c.hkModelId ? hkLive : null);
      if (hint) return { ready: false, hint: hint };
      return {
        ready: true, winRate: Number(hkLive.winRate), payoff: Number(hkLive.payoff),
        fromModel: true, closedCount: hkLive.closedCount == null ? null : hkLive.closedCount,
        modelLabel: c.hkModelId === 'none' ? '无模型' : '模型',
      };
    }
    if (c.hkWinRate == null || !(c.hkPayoff > 0)) return { ready: false, hint: '半凯利：请填写胜率和盈亏比' };
    return { ready: true, winRate: c.hkWinRate, payoff: c.hkPayoff, fromModel: false, closedCount: null };
  }

  function formatRatio(v) {
    if (!Number.isFinite(Number(v))) return '—';
    return Number(v).toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
  }

  function halfKellyResultText(r) {
    if (!r || r.reason === 'params') return '半凯利：—';
    const head = r.reason === 'non_positive'
      ? (r.isStar ? '0 股' : '0 手') + ' · 期望非正'
      : (r.ok
        ? ((r.isStar ? '' : r.lots + ' 手 / ') + r.shares + ' 股 · 仓位 ' + formatPct(r.posPct) + '%')
        : r.note);
    return '半凯利 ' + head
      + ' · f* ' + formatPct(r.kelly * 100) + '% · ½ ' + formatPct(r.half * 100) + '%';
  }

  function halfKellyTitle(r, picked) {
    const parts = [];
    if (picked && picked.fromModel) {
      const who = picked.modelLabel || '模型';
      parts.push(who + (picked.closedCount != null ? (' ' + picked.closedCount + ' 笔') : ''));
    } else {
      parts.push('手动');
    }
    if (r && r.reason !== 'params') {
      parts.push('胜率 ' + formatPct(r.winRate) + '%');
      parts.push('盈亏比 ' + formatRatio(r.payoff));
      parts.push('f* ' + formatPct(r.kelly * 100) + '%');
      parts.push('½ ' + formatPct(r.half * 100) + '%');
      if (r.ok) parts.push(r.shares + '股');
      else if (r.note) parts.push(r.note);
    }
    parts.push('点击修改算仓参数');
    return parts.join(' · ');
  }

  function halfKellySideText(r) {
    if (!r || r.reason === 'params') return '—';
    if (r.reason === 'non_positive') return r.isStar ? '0股' : '0手';
    if (!r.ok) return r.note;
    return (r.isStar ? r.shares + '股' : r.lots + '手') + ' (' + formatPct(r.posPct) + '%)';
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

  function halfKellySideRow(cfg, ctx, entry) {
    const picked = pickHalfKellyInputs(cfg, ctx && ctx.hkLive);
    if (!picked.ready) {
      return {
        label: '半凯利',
        text: '—',
        color: null,
        title: (picked.hint || '半凯利') + ' · 点击修改算仓参数',
        action: 'poscalc',
      };
    }
    const hk = calcHalfKelly({
      symbol: ctx.symbol,
      entryPrice: entry,
      capital: cfg.capital,
      winRate: picked.winRate,
      payoff: picked.payoff,
    });
    return {
      label: '半凯利',
      text: halfKellySideText(hk),
      color: null,
      title: halfKellyTitle(hk, picked),
      action: 'poscalc',
    };
  }

  /**
   * 侧栏行。指数或缺价格时返回 []。
   * 半凯利默认关; 打开后接在止损参考后面（没有 ATR 时单独成行）。
   * 模型源读 ctx.hkLive, 不读配置里的胜率。
   * @returns {Array<{label:string,text:string,color:*,title:string,action:string}>}
   */
  function formatSideRows(ctx) {
    if (!ctx || ctx.isIndex) return [];
    const cfg = normalizePosCalcConfig(ctx.config);
    const atr = Number(cfg.atrPeriod === 20 ? ctx.atr20 : ctx.atr14);
    const entry = Number(ctx.entryPrice);
    if (!(entry > 0)) return [];
    const rows = [];
    if (atr > 0) {
      const r = calcPositionSizing({
        entryPrice: entry,
        atr: atr,
        isEtf: !!ctx.isEtf,
        capital: cfg.capital,
        riskPct: cfg.riskPct,
        atrMult: cfg.atrMult,
        atrPeriod: cfg.atrPeriod,
      });
      if (r && r.reason !== 'stop_non_positive' && r.stopPrice > 0) {
        rows.push(
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
        );
      }
    }
    if (cfg.halfKelly) rows.push(halfKellySideRow(cfg, ctx, entry));
    return rows;
  }

  return {
    DEFAULT_POS_CALC: DEFAULT_POS_CALC,
    normalizePosCalcConfig: normalizePosCalcConfig,
    calcPositionSizing: calcPositionSizing,
    calcHalfKelly: calcHalfKelly,
    hkLiveFromQuant: hkLiveFromQuant,
    hkModelBound: hkModelBound,
    hkLiveHint: hkLiveHint,
    pickHalfKellyInputs: pickHalfKellyInputs,
    halfKellyResultText: halfKellyResultText,
    halfKellyTitle: halfKellyTitle,
    formatSideRows: formatSideRows,
    formatPrice: formatPrice,
    formatPct: formatPct,
    formatMoney: formatMoney,
    sharesText: sharesText,
    positionTitle: positionTitle,
    stopTitle: stopTitle,
  };
});
