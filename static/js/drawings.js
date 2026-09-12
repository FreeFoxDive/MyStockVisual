/**
 * 画线模式纯逻辑: 数据模型 / 坐标解析 / 命中测试 / 磁吸 / ZigZag 摆动点 /
 * 趋势线触点评分与修正建议 / 线性回归 / 斐波那契 / 线段裁剪 / 测量统计。
 * 无 DOM 依赖; Node 可测 (test/test_drawings_js.py)。
 *
 * 坐标约定: 画线锚点存 {t, p, off} —— t=K线日期字符串(过去锚点, 优先按日期粘附),
 * off=距最后一根bar的偏移(未来锚点 t=null; 亦作日期缺失时的兜底), p=价格。
 * 渲染时由宿主注入像素投影 P = {x(idx), y(price), idxAt(px), priceAt(py), W, H, grid}。
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.Drawings = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TYPES = ['trend', 'ray', 'hline', 'vline', 'poly', 'rect', 'channel',
               'fib', 'regression', 'arrow', 'text', 'position'];
  var FUTURE_BARS = 8;    // 画线模式下右侧预留的未来 bar 数
  var MAGNET_PX = 12;     // 磁吸半径 (像素)
  var HIT_PX = 8;         // 选中/命中半径 (像素)
  var HANDLE_R = 5;       // 端点手柄半径
  var PALETTE = ['#e6a23c', '#409eff', '#f56c6c', '#67c23a', '#b26bde'];
  var REG_SIGMA = 2;      // 回归通道上下轨 σ 倍数

  function uid() {
    return 'd' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  }

  function createDrawing(type, points, extra) {
    var d = {
      id: uid(),
      type: TYPES.indexOf(type) >= 0 ? type : 'trend',
      points: points,
      style: { color: PALETTE[0], width: 1, dash: false },
      extendRight: false,
      createdAt: Date.now(),
    };
    if (extra) {
      for (var k in extra) if (extra[k] !== undefined) d[k] = extra[k];
    }
    return normalize(d);
  }

  /** 补全/修正字段; 服务器回载的数据也过一遍, 保证渲染端字段齐全。 */
  function normalize(d) {
    if (!d || typeof d !== 'object') return null;
    if (TYPES.indexOf(d.type) < 0) return null;
    if (!Array.isArray(d.points) || d.points.length < 1) return null;
    var pts = [];
    for (var i = 0; i < d.points.length; i++) {
      var p = d.points[i];
      if (!p || typeof p !== 'object' || typeof p.p !== 'number' || !isFinite(p.p)) return null;
      pts.push({
        t: (typeof p.t === 'string' && p.t) ? p.t : null,
        p: p.p,
        off: (typeof p.off === 'number' && isFinite(p.off)) ? p.off : 0,
      });
    }
    var st = d.style || {};
    return {
      id: typeof d.id === 'string' && d.id ? d.id : uid(),
      type: d.type,
      points: pts,
      style: {
        color: typeof st.color === 'string' && st.color ? st.color : PALETTE[0],
        width: (st.width === 2) ? 2 : 1,
        dash: !!st.dash,
      },
      extendRight: !!d.extendRight,
      text: (typeof d.text === 'string') ? d.text : undefined,
      dir: (d.dir === 'long' || d.dir === 'short') ? d.dir : undefined,
      createdAt: d.createdAt || Date.now(),
    };
  }

  /** klines → {map: {date: idx}, dates: [date...]} */
  function buildDateIndexMap(klines) {
    var map = {}, dates = [];
    for (var i = 0; i < klines.length; i++) {
      var t = klines[i].date;
      dates.push(t);
      if (map[t] === undefined) map[t] = i;
    }
    return { map: map, dates: dates };
  }

  function resolveIdx(pt, dateMap, n) {
    if (pt.t != null && dateMap.map[pt.t] !== undefined) return dateMap.map[pt.t];
    return n - 1 + (pt.off || 0);
  }

  /** 屏幕位置 → 锚点: 过去锚点记日期(优先粘附), 未来锚点记 off。 */
  function pointFromIdx(idx, price, dateMap, n) {
    var i = Math.round(idx);
    var t = (i >= 0 && i <= n - 1) ? dateMap.dates[i] : null;
    return { t: t, p: price, off: i - (n - 1) };
  }

  // ── 几何工具 ──

  function pointSegDist(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    var t = len2 > 0 ? ((px - x1) * dx + (py - y1) * dy) / len2 : 0;
    t = Math.max(0, Math.min(1, t));
    var qx = x1 + t * dx, qy = y1 + t * dy;
    return Math.hypot(px - qx, py - qy);
  }

  /** Liang-Barsky 线段裁剪到矩形; 完全在外返回 null。 */
  function clipSeg(x1, y1, x2, y2, r) {
    var t0 = 0, t1 = 1, dx = x2 - x1, dy = y2 - y1;
    var p = [-dx, dx, -dy, dy];
    var q = [x1 - r.left, r.right - x1, y1 - r.top, r.bottom - y1];
    for (var i = 0; i < 4; i++) {
      if (p[i] === 0) { if (q[i] < 0) return null; continue; }
      var t = q[i] / p[i];
      if (p[i] < 0) { if (t > t1) return null; if (t > t0) t0 = t; }
      else { if (t < t0) return null; if (t < t1) t1 = t; }
    }
    return [x1 + t0 * dx, y1 + t0 * dy, x1 + t1 * dx, y1 + t1 * dy];
  }

  /** 线段末端箭头两翼坐标 */
  function arrowHead(x1, y1, x2, y2, len) {
    var ang = Math.atan2(y2 - y1, x2 - x1);
    var a1 = ang + Math.PI * 0.85, a2 = ang - Math.PI * 0.85;
    return {
      lx: x2 + len * Math.cos(a1), ly: y2 + len * Math.sin(a1),
      rx: x2 + len * Math.cos(a2), ry: y2 + len * Math.sin(a2),
    };
  }

  // ── 数据域: 趋势线触点评分 ──

  /** 直线在 idx 处的价格 (数据域) */
  function lineValue(p1, p2, idx) {
    if (p2.i === p1.i) return p1.p;
    return p1.p + (p2.p - p1.p) * (idx - p1.i) / (p2.i - p1.i);
  }

  /**
   * ZigZag 摆动点 (高低价, 百分比阈值)。
   * 返回 [{i, p, kind:'H'|'L'}]; 尾部未确认极值不输出。
   */
  function zigzag(bars, opts) {
    var pct = (opts && opts.pct) || 0.02;
    var n = bars.length, out = [];
    if (n < 2) return out;
    var dir = 0, hi = bars[0].high, hiIdx = 0, lo = bars[0].low, loIdx = 0;
    for (var i = 1; i < n; i++) {
      var h = bars[i].high, l = bars[i].low;
      if (dir === 1) {
        if (h >= hi) { hi = h; hiIdx = i; }
        if (l <= hi * (1 - pct)) {
          out.push({ i: hiIdx, p: hi, kind: 'H' });
          dir = -1; lo = l; loIdx = i;
        }
      } else if (dir === -1) {
        if (l <= lo) { lo = l; loIdx = i; }
        if (h >= lo * (1 + pct)) {
          out.push({ i: loIdx, p: lo, kind: 'L' });
          dir = 1; hi = h; hiIdx = i;
        }
      } else {
        if (h >= hi) { hi = h; hiIdx = i; }
        if (l <= lo) { lo = l; loIdx = i; }
        if (hi >= lo * (1 + pct)) {
          if (hiIdx <= loIdx) {
            out.push({ i: hiIdx, p: hi, kind: 'H' });
            dir = -1; lo = l; loIdx = i;
          } else {
            out.push({ i: loIdx, p: lo, kind: 'L' });
            dir = 1; hi = h; hiIdx = i;
          }
        }
      }
    }
    return out;
  }

  /**
   * 给定两个锚点 (i,p) 与方向 kind('L'=支撑锚低点 / 'H'=压力锚高点'),
   * 统计第二锚点之后的触点数与破位数; 破位 >1 判无效, 返回 null。
   */
  function scoreLine(bars, kind, p1, p2, opts) {
    var tolPct = (opts && opts.tolPct) || 0.005;
    var n = bars.length, touches = 2, violations = 0;
    for (var i = p2.i + 1; i < n; i++) {
      var v = lineValue(p1, p2, i);
      var tol = tolPct * v;
      var extreme = kind === 'L' ? bars[i].low : bars[i].high;
      var close = bars[i].close;
      if (kind === 'L') {
        if (extreme >= v - tol && extreme <= v + tol * 2) touches++;
        if (close < v - tol * 2) violations++;
      } else {
        if (extreme <= v + tol && extreme >= v - tol * 2) touches++;
        if (close > v + tol * 2) violations++;
      }
      if (violations > 1) return null;
    }
    var span = n - 1 - p1.i;
    var steep = Math.abs(slopeOf(p1, p2) * span) / Math.max(1e-9, Math.abs(p1.p));
    var score = touches * 10 + span * 0.02 + (p2.i / n) * 5 - Math.min(10, steep * 20);
    return { touches: touches, violations: violations, score: score };
  }

  function slopeOf(p1, p2) {
    if (p2.i === p1.i) return 0;
    return (p2.p - p1.p) / (p2.i - p1.i);
  }

  /**
   * 自动建议线: 最近 window 根内, 各取触点最多的一条支撑(L-L)与压力(H-H)。
   * 返回 [{kind, points:[{i,p},{i,p}], touches, score}] (最多 2 条)。
   */
  function detectTrendlines(bars, opts) {
    opts = opts || {};
    var pct = opts.pct || 0.02, window = opts.window || 150, minTouches = opts.minTouches || 3;
    var n = bars.length;
    if (n < 20) return [];
    var pivots = zigzag(bars, { pct: pct }).filter(function (p) { return p.i >= n - window; });
    var best = {};
    ['L', 'H'].forEach(function (kind) {
      var pts = pivots.filter(function (p) { return p.kind === kind; });
      for (var a = 0; a < pts.length; a++) {
        for (var b = a + 1; b < pts.length; b++) {
          if (pts[b].i - pts[a].i < 5) continue;
          var r = scoreLine(bars, kind, pts[a], pts[b], opts);
          if (!r || r.touches < minTouches) continue;
          if (!best[kind] || r.score > best[kind].score) {
            best[kind] = {
              kind: kind,
              points: [{ i: pts[a].i, p: pts[a].p }, { i: pts[b].i, p: pts[b].p }],
              touches: r.touches, score: r.score,
            };
          }
        }
      }
    });
    var out = [];
    if (best.L) out.push(best.L);
    if (best.H) out.push(best.H);
    return out;
  }

  /**
   * 画线修正建议: 在原锚点 ±k 根内搜索更优锚点 (吸附到该 bar 的极值)。
   * 返回 {points:[{i,p},{i,p}], touches, score} | null (无更优)。
   */
  function optimizeTrendline(bars, d, ctx, opts) {
    opts = opts || {};
    var k = opts.k || 4;
    var n = bars.length;
    var i1 = Math.round(resolveIdx(d.points[0], ctx.dateMap, n));
    var i2 = Math.round(resolveIdx(d.points[1], ctx.dateMap, n));
    if (i2 - i1 < 3) return null;
    var kind = anchorKind(bars, i1, d.points[0].p) === 'low'
      && anchorKind(bars, i2, d.points[1].p) === 'low' ? 'L' : 'H';
    var orig = scoreLine(bars, kind, { i: i1, p: d.points[0].p }, { i: i2, p: d.points[1].p }, opts);
    if (!orig) return null;
    var best = null;
    for (var a = Math.max(0, i1 - k); a <= Math.min(n - 1, i1 + k); a++) {
      for (var b = Math.max(a + 3, i2 - k); b <= Math.min(n - 1, i2 + k); b++) {
        var pa = extremeOf(bars, a, kind), pb = extremeOf(bars, b, kind);
        var r = scoreLine(bars, kind, { i: a, p: pa }, { i: b, p: pb }, opts);
        if (!r) continue;
        if (!best || r.score > best.score) {
          best = { points: [{ i: a, p: pa }, { i: b, p: pb }], touches: r.touches, score: r.score };
        }
      }
    }
    if (!best || best.score <= orig.score + 1e-9) return null;
    return best;
  }

  function anchorKind(bars, i, price) {
    var bar = bars[i];
    if (!bar) return 'low';
    return Math.abs(price - bar.low) <= Math.abs(price - bar.high) ? 'low' : 'high';
  }

  function extremeOf(bars, i, kind) {
    var bar = bars[i];
    return kind === 'L' ? bar.low : bar.high;
  }

  // ── 斐波那契 / 回归 / 测量 / 盈亏比 ──

  /** 回撤位: 0% 在终点 p2, 100% 在起点 p1 */
  function fibLevels(p1, p2) {
    var rs = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    return rs.map(function (r) {
      return { r: r, price: p2.p - (p2.p - p1.p) * r };
    });
  }

  /** 最小二乘拟合 closes[i1..i2]; 返回 {i1, i2, f(idx), sigma} | null */
  function regressionFit(bars, i1, i2) {
    var sx = 0, sy = 0, sxx = 0, sxy = 0, cnt = 0;
    for (var i = i1; i <= i2; i++) {
      var c = bars[i] ? bars[i].close : null;
      if (c == null || !isFinite(c)) continue;
      sx += i; sy += c; sxx += i * i; sxy += i * c; cnt++;
    }
    if (cnt < 2) return null;
    var xm = sx / cnt, ym = sy / cnt;
    var varx = sxx / cnt - xm * xm;
    if (Math.abs(varx) < 1e-12) return null;
    var slope = (sxy / cnt - xm * ym) / varx;
    var b0 = ym - slope * xm;
    var sse = 0;
    for (var j = i1; j <= i2; j++) {
      var cc = bars[j] ? bars[j].close : null;
      if (cc == null || !isFinite(cc)) continue;
      var res = cc - (slope * j + b0);
      sse += res * res;
    }
    var sigma = Math.sqrt(sse / cnt);
    return {
      i1: i1, i2: i2, sigma: sigma,
      f: function (idx) { return b0 + slope * idx; },
    };
  }

  /** 测量统计: bar 根数 / 自然日数 / 涨跌幅% / 价差 */
  function measureStats(dates, i1, i2, p1, p2) {
    var bars = Math.abs(i2 - i1);
    var d1 = dates[i1] ? String(dates[i1]).slice(0, 10) : null;
    var d2 = dates[i2] ? String(dates[i2]).slice(0, 10) : null;
    var days = 0;
    if (d1 && d2) {
      var t1 = Date.parse(d1), t2 = Date.parse(d2);
      if (!isNaN(t1) && !isNaN(t2)) days = Math.round((t2 - t1) / 86400000);
    }
    var pct = p1 ? (p2 - p1) / Math.abs(p1) * 100 : 0;
    return { bars: bars, days: days, pct: pct, dp: p2 - p1 };
  }

  /** 多空仓位盒盈亏比: |目标-入场| / |入场-止损| */
  function rrOf(entry, target, stop) {
    var risk = Math.abs(entry - stop);
    return risk > 1e-9 ? Math.abs(target - entry) / risk : 0;
  }

  // ── 像素域: 几何投影 (渲染与命中共用) ──

  function segGeom(rp0, rp1, P, extend, arrow) {
    var x1 = P.x(rp0.idx), y1 = P.y(rp0.p);
    var x2 = P.x(rp1.idx), y2 = P.y(rp1.p);
    if (extend) {
      if (x2 !== x1) {
        var t = (P.grid.right - x1) / (x2 - x1);
        if (t > 1) { y2 = y1 + (y2 - y1) * t; x2 = P.grid.right; }
      } else {
        x2 = x1;
        y2 = y2 > y1 ? P.grid.bottom : P.grid.top;
      }
    }
    var raw1 = { x: P.x(rp0.idx), y: P.y(rp0.p) };
    var raw2 = { x: P.x(rp1.idx), y: P.y(rp1.p) };
    var clipped = clipSeg(x1, y1, x2, y2, P.grid);
    return {
      kind: 'seg', arrow: !!arrow,
      x1: x1, y1: y1, x2: x2, y2: y2,
      c: clipped, raw1: raw1, raw2: raw2,
    };
  }

  function geom(d, P, ctx) {
    var n = ctx.n;
    var rp = d.points.map(function (pt) {
      return { idx: resolveIdx(pt, ctx.dateMap, n), p: pt.p };
    });
    switch (d.type) {
      case 'trend': return segGeom(rp[0], rp[1], P, false, false);
      case 'ray': return segGeom(rp[0], rp[1], P, true, false);
      case 'arrow': return segGeom(rp[0], rp[1], P, false, true);
      case 'hline':
        return { kind: 'hline', y: P.y(rp[0].p), price: rp[0].p, raw: { x: P.grid.left + (P.grid.right - P.grid.left) * 0.7, y: P.y(rp[0].p) } };
      case 'vline':
        return { kind: 'vline', x: P.x(rp[0].idx), raw: { x: P.x(rp[0].idx), y: P.grid.top + (P.grid.bottom - P.grid.top) * 0.3 } };
      case 'poly':
        return { kind: 'poly', pts: rp.map(function (r) { return { x: P.x(r.idx), y: P.y(r.p) }; }) };
      case 'rect': {
        var xa = P.x(rp[0].idx), ya = P.y(rp[0].p);
        var xb = P.x(rp[1].idx), yb = P.y(rp[1].p);
        return {
          kind: 'rect',
          x: Math.min(xa, xb), y: Math.min(ya, yb),
          w: Math.abs(xb - xa), h: Math.abs(yb - ya),
          raw1: { x: xa, y: ya }, raw2: { x: xb, y: yb },
        };
      }
      case 'channel': {
        var a = { x: P.x(rp[0].idx), y: P.y(rp[0].p) };
        var b = { x: P.x(rp[1].idx), y: P.y(rp[1].p) };
        var dx = P.x(rp[2].idx) - a.x, dy = P.y(rp[2].p) - a.y;
        return { kind: 'channel', a: a, b: b, dx: dx, dy: dy, raw: [a, b, { x: a.x + dx, y: a.y + dy }] };
      }
      case 'fib': {
        var xa2 = P.x(rp[0].idx), xb2 = P.x(rp[1].idx);
        var xStart = Math.min(xa2, xb2);
        var xEnd = d.extendRight ? P.grid.right : Math.max(xa2, xb2);
        var levels = fibLevels(rp[0], rp[1]).map(function (lv) {
          return { r: lv.r, price: lv.price, y: P.y(lv.price) };
        });
        return {
          kind: 'fib', xStart: xStart, xEnd: Math.max(xEnd, xStart + 2),
          levels: levels, raw1: { x: xa2, y: P.y(rp[0].p) }, raw2: { x: xb2, y: P.y(rp[1].p) },
        };
      }
      case 'regression': {
        var lo = Math.max(0, Math.min(Math.round(rp[0].idx), Math.round(rp[1].idx)));
        var hi = Math.min(n - 1, Math.max(Math.round(rp[0].idx), Math.round(rp[1].idx)));
        var ext = hi;
        if (d.extendRight) ext = n - 1 + FUTURE_BARS;
        var fit = regressionFit(ctx.bars, lo, hi);
        if (!fit) return null;
        function pt(idx, price) { return { x: P.x(idx), y: P.y(price) }; }
        return {
          kind: 'regression',
          x1: P.x(lo), x2: P.x(ext),
          mid1: pt(lo, fit.f(lo)), mid2: pt(ext, fit.f(ext)),
          up1: pt(lo, fit.f(lo) + REG_SIGMA * fit.sigma), up2: pt(ext, fit.f(ext) + REG_SIGMA * fit.sigma),
          lo1: pt(lo, fit.f(lo) - REG_SIGMA * fit.sigma), lo2: pt(ext, fit.f(ext) - REG_SIGMA * fit.sigma),
          handles: [pt(lo, fit.f(lo)), pt(hi, fit.f(hi))],
        };
      }
      case 'text':
        return { kind: 'text', x: P.x(rp[0].idx), y: P.y(rp[0].p), text: d.text || '' };
      case 'position': {
        var entry = rp[0].p, target = rp[1].p, stop = rp[2].p;
        var x1 = P.x(rp[0].idx);
        var x2 = d.extendRight ? P.grid.right : Math.max(P.x(rp[1].idx), x1 + 24);
        return {
          kind: 'position',
          dir: target >= entry ? 'long' : 'short',
          x1: x1, x2: x2,
          entryY: P.y(entry), targetY: P.y(target), stopY: P.y(stop),
          entry: entry, target: target, stop: stop,
          rr: rrOf(entry, target, stop),
          handles: [
            { x: x1, y: P.y(entry) }, { x: x1, y: P.y(target) }, { x: x1, y: P.y(stop) },
          ],
        };
      }
    }
    return null;
  }

  /** 可编辑锚点手柄 (像素) */
  function handlesOf(d, P, ctx) {
    var g = geom(d, P, ctx);
    if (!g) return [];
    switch (d.type) {
      case 'trend': case 'ray': case 'arrow':
        return [g.raw1, g.raw2];
      case 'hline': return [g.raw];
      case 'vline': return [g.raw];
      case 'poly': return g.pts;
      case 'rect': return [g.raw1, g.raw2];
      case 'channel': return g.raw;
      case 'fib': return [g.raw1, g.raw2];
      case 'regression': return g.handles;
      case 'text': return [{ x: g.x, y: g.y }];
      case 'position': return g.handles;
    }
    return [];
  }

  /** 命中测试: 返回 null 或 {part:'h0'|'h1'|...|'body', dist} */
  function hitTest(d, P, ctx, px, py) {
    var handles = handlesOf(d, P, ctx);
    var bestH = null;
    for (var i = 0; i < handles.length; i++) {
      var dist = Math.hypot(px - handles[i].x, py - handles[i].y);
      if (dist <= HIT_PX && (bestH === null || dist < bestH.dist)) {
        bestH = { part: 'h' + i, dist: dist };
      }
    }
    if (bestH) return bestH;
    var g = geom(d, P, ctx);
    if (!g) return null;
    var bd = null;
    function seg(x1, y1, x2, y2) {
      var v = pointSegDist(px, py, x1, y1, x2, y2);
      if (bd === null || v < bd) bd = v;
    }
    switch (d.type) {
      case 'trend': case 'ray': case 'arrow':
        if (g.c) seg(g.c[0], g.c[1], g.c[2], g.c[3]);
        break;
      case 'hline':
        if (py >= P.grid.top && py <= P.grid.bottom) bd = Math.abs(py - g.y);
        break;
      case 'vline':
        if (px >= P.grid.left && px <= P.grid.right) bd = Math.abs(px - g.x);
        break;
      case 'poly':
        for (var i = 0; i < g.pts.length - 1; i++) {
          seg(g.pts[i].x, g.pts[i].y, g.pts[i + 1].x, g.pts[i + 1].y);
        }
        break;
      case 'rect': {
        var inX = px >= g.x && px <= g.x + g.w, inY = py >= g.y && py <= g.y + g.h;
        if (inX && inY) bd = 0;
        break;
      }
      case 'channel': {
        var c1 = clipSeg(g.a.x, g.a.y, g.b.x, g.b.y, P.grid);
        var c2 = clipSeg(g.a.x + g.dx, g.a.y + g.dy, g.b.x + g.dx, g.b.y + g.dy, P.grid);
        if (c1) seg(c1[0], c1[1], c1[2], c1[3]);
        if (c2) seg(c2[0], c2[1], c2[2], c2[3]);
        break;
      }
      case 'fib':
        for (var j = 0; j < g.levels.length; j++) {
          if (px >= g.xStart && px <= g.xEnd && Math.abs(py - g.levels[j].y) <= HIT_PX) bd = 0;
        }
        break;
      case 'regression':
        seg(g.mid1.x, g.mid1.y, g.mid2.x, g.mid2.y);
        seg(g.up1.x, g.up1.y, g.up2.x, g.up2.y);
        seg(g.lo1.x, g.lo1.y, g.lo2.x, g.lo2.y);
        break;
      case 'text': {
        var w = (g.text || '').length * 13 + 10, h = 20;
        if (px >= g.x && px <= g.x + w && py >= g.y - h && py <= g.y + 4) bd = 0;
        break;
      }
      case 'position':
        if (px >= g.x1 && px <= g.x2) {
          bd = Math.min(Math.abs(py - g.entryY), Math.abs(py - g.targetY), Math.abs(py - g.stopY));
        }
        break;
    }
    if (bd !== null && bd <= HIT_PX) return { part: 'body', dist: bd };
    return null;
  }

  /**
   * 线型画线在 bar idx 处的价位 (tooltip 相交提示用); 不在可见跨度内返回 null。
   * 跨度语义与渲染一致: trend/arrow/channel=锚点区间, ray=向右延伸, hline=全宽,
   * regression=拟合区间 (extendRight 时向右延伸至未来区)。
   */
  function valueAt(d, idx, ctx) {
    var n = ctx.n;
    if (idx < 0 || idx > n - 1 + FUTURE_BARS) return null;
    var rp = d.points.map(function (pt) {
      return { i: Math.round(resolveIdx(pt, ctx.dateMap, n)), p: pt.p };
    });
    var lo, hi;
    switch (d.type) {
      case 'trend': case 'arrow': case 'channel':
        lo = Math.min(rp[0].i, rp[1].i);
        hi = Math.max(rp[0].i, rp[1].i);
        if (idx < lo || idx > hi) return null;
        return lineValue(rp[0], rp[1], idx);
      case 'ray':
        lo = Math.min(rp[0].i, rp[1].i);
        hi = Math.max(rp[0].i, rp[1].i);
        if (idx < lo) return null;
        // 第二锚点在右 → 向右延伸 (含未来区); 否则与渲染一致仅到第二锚点
        if (rp[1].i > rp[0].i && idx > hi + FUTURE_BARS) return null;
        if (rp[1].i <= rp[0].i && idx > hi) return null;
        return lineValue(rp[0], rp[1], idx);
      case 'hline':
        return rp[0].p;
      case 'regression':
        lo = Math.max(0, Math.min(rp[0].i, rp[1].i));
        hi = Math.min(n - 1, Math.max(rp[0].i, rp[1].i));
        if (idx < lo) return null;
        if (idx > (d.extendRight ? n - 1 + FUTURE_BARS : hi)) return null;
        var fit = regressionFit(ctx.bars, lo, hi);
        return fit ? fit.f(idx) : null;
    }
    return null;
  }

  /** 磁吸: 在指针附近的 bar 极值 (开高低收) 中找 ≤tolPx 的最近价格 */
  function snap(klines, idxF, price, P, tolPx) {    var n = klines.length;
    var base = Math.round(idxF);
    var best = null;
    for (var di = -1; di <= 1; di++) {
      var i = base + di;
      if (i < 0 || i > n - 1) continue;
      var bar = klines[i];
      var cands = [bar.open, bar.high, bar.low, bar.close];
      for (var c = 0; c < cands.length; c++) {
        var v = cands[c];
        if (v == null || !isFinite(v)) continue;
        var dist = Math.abs(P.y(v) - P.y(price));
        if (dist <= tolPx && (best === null || dist < best.dist)) {
          best = { idx: i, price: v, dist: dist };
        }
      }
    }
    if (best) return { idx: best.idx, price: best.price };
    return { idx: Math.max(0, Math.min(n - 1 + FUTURE_BARS, base)), price: price };
  }

  return {
    TYPES: TYPES,
    FUTURE_BARS: FUTURE_BARS,
    MAGNET_PX: MAGNET_PX,
    HIT_PX: HIT_PX,
    HANDLE_R: HANDLE_R,
    PALETTE: PALETTE,
    REG_SIGMA: REG_SIGMA,
    uid: uid,
    createDrawing: createDrawing,
    normalize: normalize,
    buildDateIndexMap: buildDateIndexMap,
    resolveIdx: resolveIdx,
    pointFromIdx: pointFromIdx,
    pointSegDist: pointSegDist,
    clipSeg: clipSeg,
    arrowHead: arrowHead,
    lineValue: lineValue,
    valueAt: valueAt,
    zigzag: zigzag,
    scoreLine: scoreLine,
    detectTrendlines: detectTrendlines,
    optimizeTrendline: optimizeTrendline,
    fibLevels: fibLevels,
    regressionFit: regressionFit,
    measureStats: measureStats,
    rrOf: rrOf,
    geom: geom,
    handlesOf: handlesOf,
    hitTest: hitTest,
    snap: snap,
  };
});
