# -*- coding: utf-8 -*-
"""画线层重绘回归测试: 退出画线模式后画线不再消失。

曾经的 bug: 退出画线模式后已画的线不见了, 重新进入画线模式才恢复。
根因是 toggleDrawMode 退出分支先 selectDrawing(null) → redrawDrawings() 排了一个
rAF, 紧接着 updateChart 用 lazyUpdate 提交新坐标轴并再次请求重绘, 被 _raf 合并吃掉。
唯一执行的那一帧里 chart.convertToPixel() 因坐标轴未 flush 返回 undefined,
drawMapper() 抛错, 而此时 overlay 已被 clearRect 清空 → 画布空白 (数据模型没丢,
所以重进画线又能画出来)。

三道防线各有测试:
  * 顺序: 退出分支先重建坐标轴再清选中态 (静态断言);
  * 防御: 投影不可用时不擦画布, 只返回 false (行为镜像);
  * 自愈: 排期期间被合并的请求与失败帧都会补一帧, 且重试有上限 (行为镜像)。

运行:
    venv/Scripts/python.exe -u visual/test/test_draw_repaint_js.py
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 function {name}")
    start = src.index("{", m.end() - 1)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    raise AssertionError(f"function {name} 大括号不配对")


# 用真实 drawMapper/_renderDrawingsNow/redrawDrawings 源码 + 逐帧可控的假 chart/DOM:
# flushed=False 模拟 setOption(lazyUpdate) 已提交但坐标轴下一帧才就绪。
_HARNESS = r"""
const STATE = {
  chart: null,
  klineData: { klines: [{ low: 9, high: 12.5 }, { low: 9.5, high: 12 }] },
  _gridRects: null,
  draw: { visible: true, autoLines: false, drafts: [], selectedId: null, suggest: null,
          draft: null, _labels: [], _raf: false, _rafAgain: false, _retry: 0,
          drawings: [{ id: 'd1' }, { id: 'd2' }] },
};
const log = { cleared: 0, painted: [], frames: 0 };
let flushed = false;
const queue = [];
globalThis.requestAnimationFrame = (cb) => { queue.push(cb); return queue.length; };
function runFrames(n) {
  for (let i = 0; i < n; i++) {
    const cb = queue.shift();
    if (!cb) break;
    log.frames += 1;
    cb(0);
  }
}
function drawCtx() { return { n: STATE.klineData.klines.length, bars: STATE.klineData.klines }; }
function drawOverlayCtx() { log.cleared += 1; return { setTransform() {}, clearRect() {} }; }
function paintLabels() {}
function paintLegend() {}
function refreshAutoLines() {}
function paintAutoLines() {}
function paintSuggest() {}
function paintDraft() {}
function positionDrawMenu() {}
function paintTagLayer() {}
function paintDrawing(c2, d) { log.painted.push(d.id); }
STATE.chart = {
  getWidth: () => 600,
  getHeight: () => 400,
  getModel: () => ({}),                          // setOption 已跑过 (model 在, 只是轴没 flush)
  convertToPixel: (finder, coord) => {
    if (!flushed) return undefined;              // 坐标轴未 flush
    return [600 + coord[0], 330 - coord[1] * 10];
  },
};
const snap = () => ({
  cleared: log.cleared, painted: log.painted.slice(), frames: log.frames, queued: queue.length,
  raf: STATE.draw._raf, rafAgain: STATE.draw._rafAgain, retry: STATE.draw._retry,
});
const plan = JSON.parse(process.argv[1]);
const out = [];
for (const step of plan) {
  if (step.flush !== undefined) flushed = step.flush;
  if (step.visible !== undefined) STATE.draw.visible = step.visible;
  if (step.redraw) redrawDrawings();
  if (step.frames) runFrames(step.frames);
  out.push(snap());
}
process.stdout.write(JSON.stringify(out));
"""


class DrawRepaintStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_exit_repaints_after_chart_rebuild(self):
        body = _extract_fn(self.src, "toggleDrawMode")
        branch = body[body.index("} else {"):]
        rebuild = branch.index("updateChart(false)")
        clear_sel = branch.index("selectDrawing(null)")
        self.assertLess(rebuild, clear_sel,
                        "退出画线须先重建坐标轴再清选中态: lazyUpdate 提交后坐标轴下一帧才就绪")
        self.assertRegex(branch[clear_sel:], r"selectDrawing\(null\);\s*return;",
                         "退出分支须提前返回, 否则末尾还会再重建一次图表")

    def test_projection_computed_before_clear(self):
        body = _extract_fn(self.src, "_renderDrawingsNow")
        self.assertLess(body.index("drawMapper()"), body.index("drawOverlayCtx()"),
                        "投影必须先于清屏算好, 否则坐标轴未就绪时会把上一帧画线擦成空白")
        self.assertIn("if (paint && !P) return false;", body)
        # 面板名/图例仍须早于可见性早退 (既有回归, 别改回去)
        gate = body.index("!STATE.draw.visible")
        self.assertLess(body.index("paintLabels"), gate)
        self.assertLess(body.index("paintLegend"), gate)

    def test_redraw_requeues_and_bounds_retries(self):
        body = _extract_fn(self.src, "redrawDrawings")
        self.assertIn("dr._rafAgain = true", body, "排期期间的请求不能被吞掉")
        self.assertIn("done === false", body)
        self.assertIn("dr._retry < 3", body, "补绘须有上限, 否则图表不可用时空转")
        self.assertIn("dr._retry = 0", body, "画出后重试计数须清零")

    def test_mapper_returns_null_when_axis_not_ready(self):
        body = _extract_fn(self.src, "drawMapper")
        self.assertIn("if (!pLast", body, "convertToPixel 可能返回 undefined")
        self.assertIn("if (!yMap) return null;", body, "价格投影取不到就别硬算")
        # 价格投影本身 (0/1 探针的退化与对数轴分支) 在 priceYMapper 里
        pmap = _extract_fn(self.src, "priceYMapper")
        self.assertIn("if (!p0 || !p1", pmap, "convertToPixel 可能返回 undefined")
        self.assertIn("p0[1] === p1[1]", pmap, "两点重合会让投影除零")
        self.assertIn("if (!a || !b", pmap, "对数轴分支同样要守卫")
        self.assertIn("Math.log", pmap, "对数轴要按对数插值, 且不能拿 price=0 当探针")
        for fn in ("drawPointer", "positionDrawMenu", "positionSuggestBox"):
            self.assertRegex(_extract_fn(self.src, fn), r"drawMapper\(\);\s*\n\s*if \(!P\) return",
                             f"{fn} 未处理 drawMapper 的 null")

    def test_state_flags_declared(self):
        draw = self.src[self.src.index("draw: {"):]
        draw = draw[:draw.index("\n  },")]
        for flag in ("_rafAgain: false", "_retry: 0"):
            self.assertIn(flag, draw, flag)


class AxisProjectionTest(unittest.TestCase):
    """价格→像素投影: 线性轴沿用 0/1 探针, 对数轴必须换探针。

    回归: 对数轴下 convertToPixel(price=0) 返回 [x, null] (log 0 无定义), 旧实现据此
    判定"投影未就绪" → 标签退回固定位置、画线层干脆不重绘。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, log_scale):
        log = "true" if log_scale else "false"
        script = (
            _extract_fn(self.src, "chartModel") + "\n"
            + _extract_fn(self.src, "priceYMapper") + "\n"
            + "const LO = 10, HI = 100;   // 末根 low/high\n"
            + "const BARS = [{ low: LO, high: HI }];\n"
            + "const STATE = { logScale: " + log + ", chart: null };\n"
            + "const calls = [];\n"
            + "STATE.chart = { getModel: () => ({}), convertToPixel: (finder, coord) => {\n"
            + "  calls.push(coord[1]);\n"
            + "  if (" + log + ") {\n"
            + "    if (coord[1] === 0) return [600, null];          // 对数轴上 price=0 非法\n"
            + "    const t = (Math.log(coord[1]) - Math.log(LO)) / (Math.log(HI) - Math.log(LO));\n"
            + "    return [600, 200 - 100 * t];                     // 10→200, 100→100\n"
            + "  }\n"
            + "  return [600, 200 - 100 * (coord[1] - LO) / (HI - LO)];\n"   # 线性轴的假刻度
            + "}};\n"
            + "const m = priceYMapper(BARS);\n"
            + "process.stdout.write(JSON.stringify({ null: m === null, calls,\n"
            + "  y10: m && +m.yOf(LO).toFixed(3), y100: m && +m.yOf(HI).toFixed(3),\n"
            + "  yMid: m && +m.yOf(Math.sqrt(LO * HI)).toFixed(3),\n"
            + "  back: m && +m.priceAt(150).toFixed(3) }));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_linear_axis_uses_value_probe(self):
        out = self._run(log_scale=False)
        self.assertFalse(out["null"])
        self.assertEqual(out["calls"], [0, 1], "线性轴仍用 0/1 两点探针")
        self.assertAlmostEqual(out["y10"], 200, places=3, msg="线性刻度下 10 就在原处")
        self.assertAlmostEqual(out["y100"], 100, places=3)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_log_axis_projects_without_price_zero(self):
        out = self._run(log_scale=True)
        self.assertFalse(out["null"], "对数轴也必须能给出投影")
        self.assertNotIn(0, out["calls"], "对数轴不得拿 price=0 当探针")
        self.assertEqual(out["calls"], [10, 100], "改用末根 low/high 探针")
        self.assertAlmostEqual(out["y10"], 200, places=3)
        self.assertAlmostEqual(out["y100"], 100, places=3)
        self.assertAlmostEqual(out["yMid"], 150, places=3, msg="对数轴按对数插值 (几何中点居中)")
        self.assertAlmostEqual(out["back"], 31.623, places=3,
                               msg="priceAt 是 yOf 的逆: y=中点 → 几何平均价")


class DrawRepaintBehaviorTest(unittest.TestCase):
    """逐帧驱动真实源码: 坐标轴先不可用再就绪, 断言画布不被擦白且最终补绘。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, plan):
        script = (
            _extract_fn(self.src, "chartModel") + "\n"
            + _extract_fn(self.src, "priceYMapper") + "\n"
            + _extract_fn(self.src, "drawMapper") + "\n"
            + _extract_fn(self.src, "_renderDrawingsNow") + "\n"
            + _extract_fn(self.src, "redrawDrawings") + "\n"
            + _HARNESS
        )
        proc = subprocess.run(
            ["node", "-e", script, json.dumps(plan)],
            capture_output=True, check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_axis_not_ready_keeps_previous_frame_then_repaints(self):
        # 第 1 帧: setOption(lazyUpdate) 已提交但坐标轴未就绪 → 不清屏, 自动补一帧
        # 第 2 帧: 坐标轴就绪 → 正常画出
        out = self._run([
            {"flush": False, "redraw": True, "frames": 1},
            {"flush": True, "frames": 1},
        ])
        self.assertEqual(out[0]["painted"], [], "投影不可用不应画 (也不应擦)")
        self.assertEqual(out[0]["cleared"], 0, "投影不可用时清屏会把上一帧画线擦成空白")
        self.assertEqual(out[0]["queued"], 1, "失败帧须再排一帧补绘")
        self.assertEqual(out[0]["retry"], 1)
        self.assertEqual(out[1]["painted"], ["d1", "d2"], "坐标轴就绪后画线须全部回来")
        self.assertEqual(out[1]["cleared"], 1)
        self.assertEqual(out[1]["queued"], 0)
        self.assertEqual(out[1]["retry"], 0, "画出后重试计数清零")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_request_during_pending_frame_is_not_swallowed(self):
        # 排期期间的追加请求 (如 setOption 之后 updateChart 里的那次) 不能被合并丢掉
        out = self._run([
            {"flush": True, "redraw": True, "frames": 1},   # 第 1 帧正常画出
            {"redraw": True},                                # 排一帧
            {"redraw": True},                                # 排期期间的追加请求 → 合并标记
            {"redraw": True, "frames": 1},                   # 又一追加 + 执行本帧 → 画出并补一帧
            {"frames": 1},                                   # 补帧执行
        ])
        self.assertEqual(out[2]["queued"], 1, "同一时刻只排一帧")
        self.assertEqual(out[2]["rafAgain"], True, "排期期间的请求须被记住")
        self.assertEqual(out[3]["rafAgain"], False, "执行后标记复位")
        self.assertEqual(out[3]["retry"], 0, "合并请求不该消耗补绘预算")
        self.assertEqual(out[3]["queued"], 1, "合并期间收到的请求须补一帧")
        self.assertEqual(out[4]["frames"], 3)
        self.assertEqual(out[4]["queued"], 0)
        self.assertEqual(out[4]["painted"], ["d1", "d2"] * 3, "三次都完整画出")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_retries_are_bounded(self):
        out = self._run([{"flush": False, "redraw": True, "frames": 20}])
        self.assertEqual(out[0]["frames"], 4, "首次 + 3 次补绘后停止, 不空转")
        self.assertEqual(out[0]["queued"], 0)
        self.assertEqual(out[0]["cleared"], 0, "全程未就绪: 画布始终保留上一帧")
        self.assertEqual(out[0]["painted"], [])

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_hidden_drawings_still_clear_overlay(self):
        # 👁 隐藏画线: overlay 仍要清掉 (面板名/图例照画), 但画线本身不画 —— 也不能被
        # 投影守卫拦住 (visible=false 时根本不求投影, 所以即使坐标轴未就绪也要清屏)
        out = self._run([
            {"flush": True, "visible": True, "redraw": True, "frames": 1},
            {"visible": False, "redraw": True, "frames": 1},
        ])
        self.assertEqual(out[0]["painted"], ["d1", "d2"])
        self.assertEqual(out[1]["painted"], ["d1", "d2"], "隐藏后不得再画新的一笔")
        self.assertEqual(out[1]["cleared"], 2, "隐藏后仍要清一次 overlay")
        self.assertEqual(out[1]["queued"], 0, "隐藏分支不该再排补帧")
        # 隐藏 + 坐标轴未就绪: 同样要清屏 (不被投影守卫拦), 且不空转补帧
        out2 = self._run([{"visible": False, "flush": False, "redraw": True, "frames": 3}])
        self.assertEqual(out2[0]["cleared"], 1, "隐藏时坐标轴未就绪也要清屏")
        self.assertEqual(out2[0]["frames"], 1)
        self.assertEqual(out2[0]["painted"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
