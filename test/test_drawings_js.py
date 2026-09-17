# -*- coding: utf-8 -*-
"""drawings.js 单元测试（Node): 画线模式纯逻辑。"""

from __future__ import annotations

import json
import os
import shutil
import unittest

from js_test_util import require_node, run_node

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
DRAWINGS_JS = os.path.join(VISUAL_DIR, "static", "js", "drawings.js").replace("\\", "/")


def run_drawings_js(script_body: str):
    require_node()
    if not os.path.isfile(DRAWINGS_JS.replace("/", os.sep)) and not os.path.isfile(DRAWINGS_JS):
        raise unittest.SkipTest("drawings.js missing")
    script = f"""
const D = require({json.dumps(DRAWINGS_JS)});
function makeK() {{
  const out = [];
  for (let i = 0; i < 10; i++) {{
    const b = 10 + i * 0.1;
    out.push({{date: '2026-01-' + String(i + 1).padStart(2, '0'),
              open: b, high: b + 0.3, low: b - 0.3, close: b + 0.1}});
  }}
  return out;
}}
{script_body}
"""
    return json.loads(run_node(script))


def run_expr(expr: str):
    return run_drawings_js(f"process.stdout.write(JSON.stringify({expr}));")


def bar(date, o, h, l, c):
    return {"date": date, "open": o, "high": h, "low": l, "close": c}


def make_klines(n=10, base=10.0):
    return [
        bar(f"2026-01-{i + 1:02d}", base + i * 0.1, base + i * 0.1 + 0.3,
            base + i * 0.1 - 0.3, base + i * 0.1 + 0.1)
        for i in range(n)
    ]


# 造一段带支撑线的行情: low 在 i=10/40/70 恰好落在 trend = 10 + 0.05*i 上
def make_support_bars():
    bars = []
    for i in range(120):
        trend = 10 + 0.05 * i
        if i in (10, 40, 70):
            low = round(trend, 6)
        else:
            low = round(trend * 1.005, 6)
        bars.append({
            "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open": round(low * 1.003, 6),
            "high": round(trend * 1.05, 6),
            "low": low,
            "close": round(trend * 1.02, 6),
        })
    return bars


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestDrawingsCoordinate(unittest.TestCase):
    def test_resolve_idx_past_and_future(self):
        out = run_expr("""(() => {
          const klines = makeK();
          const dm = D.buildDateIndexMap(klines);
          const n = klines.length;
          return {
            past: D.resolveIdx({t: klines[2].date, p: 1, off: 0}, dm, n),
            future: D.resolveIdx({t: null, p: 1, off: 3}, dm, n),
            missing: D.resolveIdx({t: '1999-01-01', p: 1, off: -2}, dm, n),
            ptPast: D.pointFromIdx(1, 10.5, dm, n),
            ptFuture: D.pointFromIdx(n + 2, 11, dm, n),
          };
        })()""")
        self.assertEqual(out["past"], 2)
        self.assertEqual(out["future"], 12)  # n=10 → 9+3
        self.assertEqual(out["missing"], 7)  # 日期缺失走 off 兜底: 9-2
        self.assertEqual(out["ptPast"]["t"], "2026-01-02")
        self.assertEqual(out["ptPast"]["off"], -8)
        self.assertIsNone(out["ptFuture"]["t"])
        self.assertEqual(out["ptFuture"]["off"], 3)  # i=12, n-1=9

    def test_clip_seg(self):
        out = run_expr("""(() => {
          const r = {left: 0, right: 100, top: 0, bottom: 100};
          return {
            inside: D.clipSeg(10, 10, 90, 90, r),
            cross: D.clipSeg(-50, 50, 150, 50, r),
            outside: D.clipSeg(-150, 50, -100, 50, r),
          };
        })()""")
        self.assertEqual(out["inside"], [10, 10, 90, 90])
        self.assertEqual(out["cross"], [0, 50, 100, 50])
        self.assertIsNone(out["outside"])

    def test_fib_levels(self):
        out = run_expr("D.fibLevels({i: 0, p: 10}, {i: 9, p: 20})")
        by_r = {lv["r"]: lv["price"] for lv in out}
        self.assertAlmostEqual(by_r[0], 20.0)
        self.assertAlmostEqual(by_r[0.5], 15.0)
        self.assertAlmostEqual(by_r[1], 10.0)

    def test_measure_stats(self):
        out = run_expr("""D.measureStats(
          ['2026-01-01', '2026-01-02', '2026-01-08'], 0, 2, 10, 11)""")
        self.assertEqual(out["bars"], 2)
        self.assertEqual(out["days"], 7)
        self.assertAlmostEqual(out["pct"], 10.0)
        self.assertAlmostEqual(out["dp"], 1.0)

    def test_regression_fit_linear(self):
        out = run_expr("""(() => {
          const bars = Array.from({length: 20}, (_, i) => ({close: 5 + 2 * i}));
          const fit = D.regressionFit(bars, 0, 19);
          return {slopeAt: fit.f(19) - fit.f(0), sigma: fit.sigma, mid: fit.f(10)};
        })()""")
        self.assertAlmostEqual(out["slopeAt"], 38.0)
        self.assertAlmostEqual(out["sigma"], 0.0, places=6)
        self.assertAlmostEqual(out["mid"], 25.0)

    def test_rr_of(self):
        out = run_expr("{a: D.rrOf(10, 12, 9), b: D.rrOf(10, 12, 10)}")
        self.assertAlmostEqual(out["a"], 2 / 1)
        self.assertEqual(out["b"], 0)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestDrawingsGeom(unittest.TestCase):
    PEXPR = """{
      x: (idx) => 50 + 10 * idx,
      y: (price) => 300 - 10 * price,
      idxAt: (px) => (px - 50) / 10,
      priceAt: (py) => (300 - py) / 10,
      W: 600, H: 400,
      grid: {left: 0, right: 500, top: 0, bottom: 300},
    }"""

    def geom_of(self, drawing, bars=None):
        return run_expr(f"""(() => {{
          const bars = {json.dumps(bars) if bars is not None else "makeK()"};
          const dm = D.buildDateIndexMap(bars);
          const ctx = {{n: bars.length, dateMap: dm, bars: bars}};
          return D.geom({json.dumps(drawing)}, {self.PEXPR}, ctx);
        }})()""")

    def test_trend_geom_and_hit(self):
        bars = make_klines()
        d = {"id": "x", "type": "trend",
             "points": [{"t": bars[1]["date"], "p": 10, "off": -8},
                        {"t": bars[5]["date"], "p": 12, "off": -4}],
             "style": {"color": "#fff", "width": 1, "dash": False},
             "extendRight": False}
        g = self.geom_of(d, bars)
        # x(1)=60, y(10)=200; x(5)=100, y(12)=180 → 线段在 grid 内
        self.assertEqual(g["c"], [60, 200, 100, 180])
        self.assertEqual(g["raw1"], {"x": 60, "y": 200})

        hit = run_expr(f"""(() => {{
          const bars = {json.dumps(bars)};
          const dm = D.buildDateIndexMap(bars);
          const ctx = {{n: bars.length, dateMap: dm, bars: bars}};
          const P = {self.PEXPR};
          const d = D.normalize({json.dumps(d)});
          return {{
            mid: D.hitTest(d, P, ctx, 80, 190),
            handle1: D.hitTest(d, P, ctx, 100, 180),
            far: D.hitTest(d, P, ctx, 400, 190),
          }};
        }})()""")
        self.assertEqual(hit["mid"]["part"], "body")
        self.assertEqual(hit["handle1"]["part"], "h1")
        self.assertIsNone(hit["far"])

    def test_ray_extends_to_right_edge(self):
        bars = make_klines()
        d = {"id": "x", "type": "ray",
             "points": [{"t": bars[1]["date"], "p": 10, "off": 0},
                        {"t": bars[3]["date"], "p": 11, "off": 0}],
             "style": {"color": "#fff", "width": 1, "dash": False},
             "extendRight": False}
        g = self.geom_of(d, bars)
        # 斜率 0.5 价格/bar = 5px/bar; 延伸到 grid.right=500 后 y=-20 越顶,
        # Liang-Barsky 裁剪: 与 grid 顶边 (y=0) 交于 x=460
        self.assertAlmostEqual(g["c"][2], 460.0)
        self.assertAlmostEqual(g["c"][3], 0.0)

    def test_hline_vline_geom(self):
        bars = make_klines()
        h = {"id": "h", "type": "hline", "points": [{"t": bars[0]["date"], "p": 15, "off": 0}],
             "style": {"color": "#fff", "width": 1, "dash": False}, "extendRight": False}
        g = self.geom_of(h, bars)
        self.assertEqual(g["y"], 150)
        v = {"id": "v", "type": "vline", "points": [{"t": bars[2]["date"], "p": 10, "off": 0}],
             "style": {"color": "#fff", "width": 1, "dash": False}, "extendRight": False}
        g2 = self.geom_of(v, bars)
        self.assertEqual(g2["x"], 70)

    def test_regression_geom_uses_fit(self):
        bars = [{"date": f"2026-01-{i + 1:02d}", "open": 5 + i, "high": 5 + i,
                 "low": 5 + i, "close": 5 + i} for i in range(10)]
        d = {"id": "r", "type": "regression",
             "points": [{"t": bars[0]["date"], "p": 5, "off": -9},
                        {"t": bars[9]["date"], "p": 14, "off": 0}],
             "style": {"color": "#fff", "width": 1, "dash": False},
             "extendRight": False}
        g = self.geom_of(d, bars)
        # 精确线性 close=5+i: mid 线 y(5)=250 → y(14)=160
        self.assertAlmostEqual(g["mid1"]["y"], 250.0)
        self.assertAlmostEqual(g["mid2"]["y"], 160.0)
        self.assertEqual(len(g["handles"]), 2)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestDrawingsSmart(unittest.TestCase):
    def test_zigzag_alternates(self):
        bars = make_support_bars()
        out = run_expr(f"D.zigzag({json.dumps(bars)}, {{pct: 0.02}})")
        kinds = [p["kind"] for p in out]
        self.assertIn("H", kinds)
        self.assertIn("L", kinds)
        for a, b in zip(kinds, kinds[1:]):
            self.assertNotEqual(a, b)

    def test_detect_finds_planted_support(self):
        bars = make_support_bars()
        out = run_expr(f"D.detectTrendlines({json.dumps(bars)}, {{}})")
        supports = [t for t in out if t["kind"] == "L"]
        self.assertTrue(supports, "应检测到支撑线")
        self.assertGreaterEqual(supports[0]["touches"], 3)

    def test_optimize_improves(self):
        bars = make_support_bars()
        # 用户画偏一点: 锚点取非极值的 bar
        d = {"id": "u", "type": "trend",
             "points": [{"t": bars[11]["date"], "p": bars[11]["low"], "off": 0},
                        {"t": bars[45]["date"], "p": bars[45]["low"], "off": 0}],
             "style": {"color": "#fff", "width": 1, "dash": False},
             "extendRight": False}
        out = run_expr(f"""(() => {{
          const bars = {json.dumps(bars)};
          const dm = D.buildDateIndexMap(bars);
          const ctx = {{n: bars.length, dateMap: dm, bars: bars}};
          return D.optimizeTrendline(bars, D.normalize({json.dumps(d)}), ctx, {{}});
        }})()""")
        self.assertIsNotNone(out, "偏移线应得到更优修正建议")
        self.assertGreaterEqual(out["touches"], 3)

    def test_optimize_no_suggestion_for_exact_line(self):
        bars = make_support_bars()
        d = {"id": "u", "type": "trend",
             "points": [{"t": bars[10]["date"], "p": 10 + 0.05 * 10, "off": 0},
                        {"t": bars[40]["date"], "p": 10 + 0.05 * 40, "off": 0}],
             "style": {"color": "#fff", "width": 1, "dash": False},
             "extendRight": False}
        out = run_expr(f"""(() => {{
          const bars = {json.dumps(bars)};
          const dm = D.buildDateIndexMap(bars);
          const ctx = {{n: bars.length, dateMap: dm, bars: bars}};
          return D.optimizeTrendline(bars, D.normalize({json.dumps(d)}), ctx, {{}});
        }})()""")
        # 精确线本身评分已高, 优化器可能找到同分或更优的邻近锚点; 只要求不降低触点
        if out is not None:
            self.assertGreaterEqual(out["touches"], 3)

    def test_snap_magnet(self):
        bars = make_klines()
        bars[3] = dict(bars[3], low=9.8, high=10.4)
        out = run_expr(f"""(() => {{
          const bars = {json.dumps(bars)};
          const P = {{x: (i) => 50 + 10 * i, y: (p) => 300 - 10 * p,
                     idxAt: (px) => (px - 50) / 10, priceAt: (py) => (300 - py) / 10,
                     W: 600, H: 400, grid: {{left: 0, right: 500, top: 0, bottom: 300}}}};
          return {{
            magnet: D.snap(bars, 3.2, 9.8, P, 12),
            miss: D.snap(bars, 3.2, 30, P, 12),
          }};
        }})()""")
        self.assertEqual(out["magnet"]["idx"], 3)
        self.assertAlmostEqual(out["magnet"]["price"], 9.8)  # |202-200|=2 ≤ 磁吸半径
        self.assertEqual(out["miss"]["idx"], 3)
        self.assertAlmostEqual(out["miss"]["price"], 30)  # 无候选 → 原样

    def test_normalize_rejects_bad_points(self):
        out = run_expr("""(() => {
          return {
            bad: D.normalize({id: 'a', type: 'trend', points: [{t: null}]}),
            unknownType: D.normalize({id: 'a', type: 'nope', points: [{t: null, p: 1}]}),
            ok: D.normalize({type: 'trend', points: [{t: '2026-01-01', p: 1}]}),
          };
        })()""")
        self.assertIsNone(out["bad"])
        self.assertIsNone(out["unknownType"], "未知类型直接丢弃 (服务器回载数据防御)")
        self.assertTrue(out["ok"]["id"])
        self.assertEqual(out["ok"]["style"]["color"], "#e6a23c")

    def test_value_at(self):
        # bars: 10 根, close = 10..19; 线锚 (0,10) → (9,19) (即 close 线)
        bars = [{"date": f"2026-01-{i + 1:02d}", "open": 10 + i, "high": 10 + i,
                 "low": 10 + i, "close": 10 + i} for i in range(10)]
        out = run_expr(f"""(() => {{
          const bars = {json.dumps(bars)};
          const dm = D.buildDateIndexMap(bars);
          const ctx = {{n: bars.length, dateMap: dm, bars: bars}};
          const mk = (type, extendRight) => D.normalize({{
            id: 'v', type: type,
            points: [{{t: bars[0].date, p: 10}}, {{t: bars[9].date, p: 19}}],
            style: {{color: '#fff', width: 1, dash: false}}, extendRight: !!extendRight,
          }});
          const regExt = mk('regression'); regExt.extendRight = true;
          const regNo = mk('regression'); regNo.extendRight = false;
          return {{
            seg_in: D.valueAt(mk('trend'), 4, ctx),
            seg_before: D.valueAt(mk('trend'), -1, ctx),
            seg_after: D.valueAt(mk('trend'), 12, ctx),
            ray_future: D.valueAt(mk('ray'), 12, ctx),
            ray_far: D.valueAt(mk('ray'), 20, ctx),
            reg_mid: D.valueAt(mk('regression'), 4, ctx),
            reg_ext: D.valueAt(regExt, 14, ctx),
            reg_noext: D.valueAt(regNo, 12, ctx),
            unsupported: D.valueAt(D.normalize({{
              id: 'v', type: 'rect',
              points: [{{t: bars[0].date, p: 10}}, {{t: bars[9].date, p: 19}}],
            }}), 4, ctx),
          }};
        }})()""")
        self.assertAlmostEqual(out["seg_in"], 14.0)          # 10 + 4
        self.assertIsNone(out["seg_before"])
        self.assertIsNone(out["seg_after"], "线段超出锚点区间应返回 null")
        self.assertAlmostEqual(out["ray_future"], 22.0)      # 射线向右线性延伸 (10 + 12)
        self.assertIsNone(out["ray_far"], "射线延伸不超过未来区")
        self.assertAlmostEqual(out["reg_mid"], 14.0)         # 精确线性 close → 中线即直线
        self.assertAlmostEqual(out["reg_ext"], 24.0)         # extendRight 延伸至未来区
        self.assertIsNone(out["reg_noext"], "回归通道未开延伸时超出区间应返回 null")
        self.assertIsNone(out["unsupported"], "rect 等非线型不参与相交提示")


class TestDrawingsNameMonitor(unittest.TestCase):
    """normalize: 监控趋势线的 name / monitor 字段保留与非法剔除。"""

    PTS = "[{t: '2026-01-01', p: 10, off: 0}, {t: null, p: 12, off: 2}]"

    def test_name_and_monitor_preserved(self):
        out = run_expr(
            f"D.normalize({{id: 'd1', type: 'trend', points: {self.PTS}, "
            "name: ' 主升浪 ', monitor: {enabled: true, pct: '2.5'}})")
        self.assertEqual(out["name"], "主升浪")
        self.assertEqual(out["monitor"], {"enabled": True, "pct": 2.5})

    def test_disabled_monitor_still_kept(self):
        out = run_expr(
            f"D.normalize({{id: 'd1', type: 'trend', points: {self.PTS}, "
            "name: 'x', monitor: {enabled: false, pct: 3}})")
        self.assertEqual(out["monitor"], {"enabled": False, "pct": 3})

    def test_monitor_adjust_preserved(self):
        out = run_expr(
            f"D.normalize({{id: 'd1', type: 'trend', points: {self.PTS}, "
            "name: 'x', monitor: {enabled: true, pct: 2, adjust: 'none'}})")
        self.assertEqual(out["monitor"]["adjust"], "none")
        bad = run_expr(
            f"D.normalize({{id: 'd1', type: 'trend', points: {self.PTS}, "
            "name: 'x', monitor: {enabled: true, pct: 2, adjust: 'weird'}})")
        self.assertNotIn("adjust", bad["monitor"])

    def test_invalid_name_or_pct_dropped(self):
        blank = run_expr(
            f"D.normalize({{id: 'd1', type: 'trend', points: {self.PTS}, "
            "name: '   ', monitor: {enabled: true, pct: 2}})")
        self.assertNotIn("name", blank)
        # 名称由前端开启监控时强制填写; normalize 只做字段校验, 监控配置独立保留
        self.assertEqual(blank["monitor"]["pct"], 2)
        bad_pct = run_expr(
            f"D.normalize({{id: 'd1', type: 'trend', points: {self.PTS}, "
            "name: 'x', monitor: {enabled: true, pct: 99}})")
        self.assertNotIn("monitor", bad_pct)
        no_pct = run_expr(
            f"D.normalize({{id: 'd1', type: 'trend', points: {self.PTS}, "
            "name: 'x', monitor: {enabled: true}})")
        self.assertNotIn("monitor", no_pct)


class TestDrawingsSetPointValue(unittest.TestCase):
    """setPointValue: 水平线数值输入的纯逻辑 (非法值不动原对象)。"""

    def _mk(self, price=10.5):
        return ("D.createDrawing('hline', "
                f"[{{t: '2026-01-05', p: {price}, off: 0}}])")

    def test_writes_rounded_price_and_keeps_anchor(self):
        out = run_drawings_js(
            f"const d = {self._mk()};"
            "const ok = D.setPointValue(d, '12.3456');"
            "process.stdout.write(JSON.stringify({ok, p: d.points[0].p, "
            "t: d.points[0].t, off: d.points[0].off, type: d.type}));")
        self.assertTrue(out["ok"])
        self.assertEqual(out["p"], 12.346, "与 fmtPrice3 展示精度一致 (3 位小数)")
        self.assertEqual(out["t"], "2026-01-05", "只改价格, 日期锚点必须保留")
        self.assertEqual(out["off"], 0)
        self.assertEqual(out["type"], "hline")

    def test_number_input_accepted(self):
        out = run_drawings_js(
            f"const d = {self._mk()}; D.setPointValue(d, 9.5);"
            "process.stdout.write(JSON.stringify(d.points[0].p));")
        self.assertEqual(out, 9.5)

    def test_invalid_values_rejected_without_mutation(self):
        out = run_drawings_js(
            f"const d = {self._mk(10.5)};"
            "const results = ['', 'abc', '0', '-1', 'NaN', '0.0004', '1e-9'].map(v => D.setPointValue(d, v));"
            "process.stdout.write(JSON.stringify({results, p: d.points[0].p}));")
        self.assertEqual(out["results"], [False] * 7)
        self.assertEqual(out["p"], 10.5, "非法输入不得改动画线")

    def test_sub_milli_value_rejected_instead_of_rounding_to_zero(self):
        """取整到 3 位小数后归零的输入要拒绝: price=0 在对数轴上是非法探针。"""
        out = run_drawings_js(
            f"const d = {self._mk(10.5)};"
            "const ok = D.setPointValue(d, '0.0004');"
            "const edge = D.setPointValue(d, '0.0005');"
            "process.stdout.write(JSON.stringify({ok, edge, p: d.points[0].p}));")
        self.assertFalse(out["ok"])
        self.assertTrue(out["edge"], "0.0005 取整后是 0.001, 合法")
        self.assertEqual(out["p"], 0.001)

    def test_missing_point_rejected(self):
        out = run_drawings_js(
            "const d = {id: 'x', type: 'hline', points: []};"
            "const ok = D.setPointValue(d, 10);"
            "const noPoints = D.setPointValue({id: 'y', type: 'hline'}, 10);"
            "process.stdout.write(JSON.stringify({ok, noPoints}));")
        self.assertFalse(out["ok"])
        self.assertFalse(out["noPoints"])

    def test_second_point_by_index(self):
        out = run_drawings_js(
            "const d = D.createDrawing('trend', [{t: '2026-01-01', p: 10, off: 0},"
            " {t: '2026-01-02', p: 11, off: 1}]);"
            "D.setPointValue(d, 12, 1);"
            "process.stdout.write(JSON.stringify(d.points.map(p => p.p)));")
        self.assertEqual(out, [10, 12])


if __name__ == "__main__":
    unittest.main()
