/**
 * K 线跳空缺口扫描（经典 OHLC）+ ECharts markArea 数据构建
 * 纯前端；与后端无关。
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.GapScanner = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const GAP_PERIODS = ['60m', '1d', '1w', '1M'];
  const MAX_VISIBLE = 2;
  const EPS = 1e-8;

  function isHaltBar(bar) {
    if (!bar) return true;
    if (bar.halted === true) return true;
    const v = bar.volume;
    return v == null || v <= 0;
  }

  function isGapPeriod(period) {
    return GAP_PERIODS.indexOf(period) >= 0;
  }

  /**
   * 扫描未回补缺口（含部分回补后的收缩区间）。
   * @param {Array} klines  {open,high,low,close,volume?}
   * @param {{maxVisible?: number, isHalt?: function}} [opts]
   * @returns {Array}
   */
  function scanGaps(klines, opts) {
    opts = opts || {};
    const maxVisible = opts.maxVisible != null ? opts.maxVisible : MAX_VISIBLE;
    const haltFn = typeof opts.isHalt === 'function' ? opts.isHalt : isHaltBar;
    if (!klines || klines.length < 2) return [];

    const n = klines.length;
    const lastIdx = n - 1;
    const gaps = [];

    for (let i = 1; i < n; i++) {
      const prev = klines[i - 1];
      const curr = klines[i];
      if (haltFn(prev) || haltFn(curr)) continue;
      if (prev.high == null || prev.low == null || curr.high == null || curr.low == null) continue;

      let dir = null;
      let top0 = null;
      let bottom0 = null;

      if (curr.low > prev.high + EPS) {
        dir = 'up';
        bottom0 = prev.high;
        top0 = curr.low;
      } else if (curr.high < prev.low - EPS) {
        dir = 'down';
        top0 = prev.low;
        bottom0 = curr.high;
      } else {
        continue;
      }

      let top = top0;
      let bottom = bottom0;
      let filled = false;

      for (let j = i + 1; j < n; j++) {
        const bar = klines[j];
        if (haltFn(bar)) continue;
        if (bar.high == null || bar.low == null) continue;

        if (dir === 'up') {
          if (bar.low <= bottom + EPS) {
            filled = true;
            break;
          }
          if (bar.low < top - EPS) {
            top = bar.low;
          }
        } else {
          if (bar.high >= top - EPS) {
            filled = true;
            break;
          }
          if (bar.high > bottom + EPS) {
            bottom = bar.high;
          }
        }
        if (top - bottom <= EPS) {
          filled = true;
          break;
        }
      }

      if (filled) continue;
      const spread = top - bottom;
      if (spread <= EPS) continue;

      gaps.push({
        dir: dir,
        top0: top0,
        bottom0: bottom0,
        top: top,
        bottom: bottom,
        startIdx: i,
        endIdx: lastIdx,
        spread: spread,
      });
    }

    if (gaps.length > maxVisible) {
      return gaps.slice(gaps.length - maxVisible);
    }
    return gaps;
  }

  function fmtNum(v, digits) {
    if (v == null || Number.isNaN(v)) return '—';
    return Number(v).toFixed(digits != null ? digits : 2);
  }

  function gapTooltipHtml(gap) {
    const dirLabel = gap.dir === 'up' ? '向上缺口' : '向下缺口';
    const spread = gap.spread;
    let pctPart = '';
    if (gap.bottom > EPS) {
      const pct = (spread / gap.bottom) * 100;
      pctPart = '（' + pct.toFixed(2) + '%）';
    }
    return (
      '<div style="font-weight:700;margin-bottom:2px">' + dirLabel + '</div>' +
      '<div>价差 ' + fmtNum(spread) + pctPart + '</div>' +
      '<div>区间 ' + fmtNum(gap.bottom) + ' ~ ' + fmtNum(gap.top) + '</div>'
    );
  }

  /**
   * 十字线落在缺口灰区内则返回该缺口（优先最近形成的）。
   * 必须同时命中 x（dataIndex）与 y（price）。
   */
  function findGapAt(gaps, dataIndex, price) {
    if (!gaps || !gaps.length || dataIndex == null) return null;
    if (price == null || Number.isNaN(Number(price))) return null;
    const px = Number(price);
    for (let i = gaps.length - 1; i >= 0; i--) {
      const g = gaps[i];
      if (dataIndex < g.startIdx || dataIndex > g.endIdx) continue;
      if (px >= g.bottom - EPS && px <= g.top + EPS) return g;
    }
    return null;
  }

  /**
   * 构建 ECharts markArea 配置。
   * @param {Array} gaps scanGaps 结果
   * @param {string[]} dates x 轴类目
   * @param {{gapFill: string, gapBorder: string}} colors
   */
  function buildMarkArea(gaps, dates, colors) {
    if (!gaps || !gaps.length || !dates || !dates.length) return undefined;
    const fill = (colors && colors.gapFill) || 'rgba(158,158,158,0.28)';
    const border = (colors && colors.gapBorder) || 'rgba(120,120,120,0.45)';

    const data = gaps.map(function (g) {
      const x0 = dates[g.startIdx];
      const x1 = dates[g.endIdx] != null ? dates[g.endIdx] : dates[dates.length - 1];
      return [
        {
          coord: [x0, g.top],
          gapMeta: g,
        },
        {
          coord: [x1, g.bottom],
        },
      ];
    });

    // silent:true — 不抢 axis tooltip；缺口文案由主图 axis tooltip 注入
    return {
      silent: true,
      z: 1,
      itemStyle: {
        color: fill,
        borderColor: border,
        borderWidth: 1,
      },
      emphasis: {
        disabled: true,
      },
      data: data,
    };
  }

  return {
    GAP_PERIODS: GAP_PERIODS,
    MAX_VISIBLE: MAX_VISIBLE,
    isHaltBar: isHaltBar,
    isGapPeriod: isGapPeriod,
    scanGaps: scanGaps,
    findGapAt: findGapAt,
    gapTooltipHtml: gapTooltipHtml,
    buildMarkArea: buildMarkArea,
  };
});
