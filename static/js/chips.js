/**
 * 筹码分布 (东财 CYQ) 渲染助手 — 纯前端，仅负责把服务端 /api/chips 结果
 * 叠加到 K 线主图右侧，并与主图价格轴联动。
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.ChipChart = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const TARGET_PX = 1.5;   // 每根筹码柱目标像素高度 (按面板高度自适应分桶)

  function colorFor(price, lastClose, colors) {
    if (lastClose == null || price == null) return colors.chipTrapped;
    return price <= lastClose ? colors.chipProfit : colors.chipTrapped;
  }

  function fmtPct(v) {
    return v == null ? '—' : (v * 100).toFixed(1) + '%';
  }

  function fmtNum(v) {
    return v == null ? '—' : Number(v).toFixed(2);
  }

  /** 纯文本多行摘要 (ECharts graphic text 用) */
  function summaryLines(chip) {
    if (!chip) return [];
    return [
      chip.source === 'af' ? '筹码分布·近似' : '筹码分布',
      '获利 ' + fmtPct(chip.profitRatio),
      '均本 ' + fmtNum(chip.avgCost) + (chip.avgCostMode === 'median' ? '(中位)' : ''),
      '90% ' + fmtNum(chip.pct90 && chip.pct90[0]) + '~' + fmtNum(chip.pct90 && chip.pct90[1]),
      '70% ' + fmtNum(chip.pct70 && chip.pct70[0]) + '~' + fmtNum(chip.pct70 && chip.pct70[1]),
    ];
  }

  /** HTML 摘要 (tooltip / 状态栏用) */
  function summaryHtml(chip, colors) {
    if (!chip) return '';
    const parts = [
      '获利比例 <b style="color:' + colors.chipProfit + '">' + fmtPct(chip.profitRatio) + '</b>',
      '平均成本 ' + fmtNum(chip.avgCost),
      '90%成本 ' + fmtNum(chip.pct90 && chip.pct90[0]) + '~' + fmtNum(chip.pct90 && chip.pct90[1]),
      '70%成本 ' + fmtNum(chip.pct70 && chip.pct70[0]) + '~' + fmtNum(chip.pct70 && chip.pct70[1]),
    ];
    return parts.join(' &nbsp; ');
  }

  /**
   * 在升序价格桶上按价格线性插值取权重; 落在 [chip.min, chip.max] 外返回 0。
   */
  function weightAt(buckets, price) {
    const n = buckets.length;
    if (!n) return 0;
    const first = buckets[0];
    const last = buckets[n - 1];
    if (price < first.price || price > last.price) return 0;
    if (price === first.price) return first.weight;
    if (price === last.price) return last.weight;
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (buckets[mid].price <= price) lo = mid;
      else hi = mid;
    }
    const p0 = buckets[lo].price;
    const p1 = buckets[hi].price;
    const t = p1 === p0 ? 0 : (price - p0) / (p1 - p0);
    return buckets[lo].weight + (buckets[hi].weight - buckets[lo].weight) * t;
  }

  /**
   * 把 150 桶分布按可见价格区间 [min,max] 重采样为 n 个等距点。
   * 返回 [[weight, price], ...] (价格升序), 供 ECharts custom 系列使用。
   */
  function resample(chip, min, max, n) {
    n = Math.max(2, Math.min(900, Math.round(n) || 0));
    if (!(max > min)) return [];
    const step = (max - min) / n;
    const data = new Array(n);
    for (let i = 0; i < n; i++) {
      const p = min + step * (i + 0.5);
      data[i] = [weightAt(chip.buckets, p), p];
    }
    return data;
  }

  /** 按面板像素高度推算重采样根数 (约每 TARGET_PX 一根)。 */
  function bucketCount(pxHeight) {
    return Math.max(60, Math.min(900, Math.round((pxHeight || 600) / TARGET_PX)));
  }

  /**
   * 构建筹码叠加的 ECharts option 片段。
   * cfg: { gi, left, right, top, height, colors, lastClose, min, max, pxHeight, n }
   * 返回 { grid, xAxis, yAxis, series:[barSeries, avgLine], n, pxHeight, barPx }
   */
  function buildOverlay(chip, cfg) {
    const gi = cfg.gi;
    const colors = cfg.colors;
    const min = cfg.min != null ? cfg.min : chip.min;
    const max = cfg.max != null ? cfg.max : chip.max;
    const pxHeight = cfg.pxHeight || 600;
    const n = cfg.n || bucketCount(pxHeight);
    const barPx = Math.max(1, pxHeight / n);
    const data = resample(chip, min, max, n);
    const maxW = chip.buckets.reduce((m, b) => Math.max(m, b.weight), 0) || 1;
    const xMax = maxW * 1.08;

    const barSeries = {
      name: '筹码',
      type: 'custom',
      xAxisIndex: gi,
      yAxisIndex: gi,
      silent: true,
      encode: { x: 0, y: 1 },
      data: data,
      renderItem: function (params, api) {
        const weight = api.value(0);
        const price = api.value(1);
        const zero = api.coord([0, price]);
        const pt = api.coord([weight, price]);
        const h = barPx;
        const rect = {
          x: Math.min(zero[0], pt[0]),
          y: zero[1] - h / 2,
          width: Math.abs(pt[0] - zero[0]),
          height: h,
        };
        const g = (typeof echarts !== 'undefined') ? echarts.graphic : null;
        let shape = rect;
        if (g && g.clipRectByRect) {
          shape = g.clipRectByRect(rect, {
            x: params.coordSys.x,
            y: params.coordSys.y,
            width: params.coordSys.width,
            height: params.coordSys.height,
          });
          if (!shape) return;
        }
        return {
          type: 'rect',
          shape: shape,
          style: { fill: colorFor(price, cfg.lastClose, colors), opacity: 0.85 },
        };
      },
    };

    const avgLine = {
      name: '筹码均本',
      type: 'line',
      xAxisIndex: gi,
      yAxisIndex: gi,
      silent: true,
      symbol: 'none',
      data: [[0, chip.avgCost], [xMax, chip.avgCost]],
      encode: { x: 0, y: 1 },
      lineStyle: { color: colors.chipAvg, type: 'dashed', width: 1 },
      endLabel: {
        show: true,
        formatter: function () { return Number(chip.avgCost).toFixed(2); },
        color: colors.chipAvgLabel || '#2962ff',
        fontSize: 11,
        fontWeight: 'bold',
        offset: [2, 0],
      },
    };

    return {
      grid: { left: cfg.left, right: cfg.right, top: cfg.top, height: cfg.height, show: false },
      xAxis: {
        gridIndex: gi,
        type: 'value',
        min: 0,
        max: xMax,
        show: false,
      },
      yAxis: {
        gridIndex: gi,
        type: 'value',
        min: min,
        max: max,
        show: false,
        position: 'right',
      },
      series: [barSeries, avgLine],
      n: n,
      pxHeight: pxHeight,
      barPx: barPx,
    };
  }

  /** 按 dataZoom 百分比窗口计算可见 K 线的价格范围 (用于筹码轴联动)。 */
  function visibleRange(klines, startPct, endPct) {
    if (!klines || !klines.length) return null;
    const n = klines.length;
    const s = startPct == null ? 0 : startPct;
    const e = endPct == null ? 100 : endPct;
    let i0 = Math.floor(n * s / 100);
    let i1 = Math.ceil(n * e / 100) - 1;
    i0 = Math.max(0, Math.min(n - 1, i0));
    i1 = Math.max(i0, Math.min(n - 1, i1));
    let lo = Infinity;
    let hi = -Infinity;
    for (let i = i0; i <= i1; i++) {
      const k = klines[i];
      if (k.low != null && k.low < lo) lo = k.low;
      if (k.high != null && k.high > hi) hi = k.high;
    }
    if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return null;
    return { min: lo, max: hi };
  }

  return {
    colorFor: colorFor,
    summaryLines: summaryLines,
    summaryHtml: summaryHtml,
    buildOverlay: buildOverlay,
    visibleRange: visibleRange,
    weightAt: weightAt,
    resample: resample,
    bucketCount: bucketCount,
    TARGET_PX: TARGET_PX,
  };
});
