# -*- coding: utf-8 -*-
"""ECharts 实例就绪时序回归: model 未建时不许取像素, 全量重建不许 lazyUpdate。

线上两个报错 (都用真实 echarts-5.5.0 在下面复现, 报错文案逐字一致):

1. `TypeError: Cannot read properties of undefined (reading 'queryComponents')`
   `echarts.init()` 之后、首次 `setOption` 之前 `chart._model` 是 undefined, 而 ECharts 内部
   `convertToPixel` 只挡了 `_disposed` 没挡 `_model` → `queryComponents` 抛错。可达路径:
   * 日/周/月 K 冷启动: `fetchData` 先写 `STATE.klineData = data` 再 `await loadTrades(...)`,
     这个窗口里任何一个行情快照 tick 都会走 flush:'sync' 的 watcher → `updateLivePriceLine`
     → `tagColumnCtx` → `priceYMapper` → `convertToPixel` 抛错 (用户贴的栈就是这个);
   * 分时冷启动: `fetchIntraday` 写 `STATE.intradayData` 后先 `applyQuoteData(quote)` 同步触发
     同一个 watcher, `renderIntraday` 还没跑 → 抛错被外层 catch 吞掉, 分时图永久白屏。

2. `TypeError: Cannot read properties of undefined (reading 'getRawIndex')`
   `setOption(option, { notMerge: true, lazyUpdate: true })` 会同步换掉 model 与全部 seriesModel,
   但数据管线要等下一帧; 画布上仍是带旧 seriesIndex 的旧元素, 这中间 mousemove 会让 ECharts
   内部事件桥接拿旧索引去查新 series 的 `getDataParams` → `getData()` 为空 → `getRawIndex` 抛错。

两道不变量各有测试:
  * 探针: 静态顺序断言 + 假 chart 行为 (整只 tick 让路) + 真实 echarts 复现 (未建 model 时
    `priceYMapper`/`drawMapper` 返回 null, 建图后必须恢复正常投影);
  * 同步提交: 全量重建的两个 `setOption` 选项对象从源码里取出来喂给真实 echarts, 断言同一 tick
    内新 series 已有数据 —— 谁把 lazyUpdate 加回去, 这里就会复现出 `getRawIndex` 报错。

运行:
    venv/Scripts/python.exe -u visual/test/test_chart_ready_js.py
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
ECHARTS_JS = _VISUAL_DIR / "static" / "vendor" / "echarts-5.5.0.min.js"

# 全量重建: updateChart / renderIntraday 各一处
_REBUILD_OPTS = re.compile(r"chart\.setOption\(option,\s*(\{[^{}]*\})\)")

_BARS_JS = "[{ open: 10, close: 10.5, low: 9.7, high: 12.2 },\n\
            { open: 10.5, close: 11.2, low: 9.6, high: 12.4 }]"


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


class ChartReadyStaticTest(unittest.TestCase):
    """就绪探针与 setOption 提交方式的静态不变量。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_probe_uses_get_model(self):
        body = _extract_fn(self.src, "chartModel")
        self.assertIn("typeof chart.getModel !== 'function'", body)
        self.assertIn("return chart.getModel() || null", body,
                      "探针要便宜: getOption() 每次深拷贝整份 option, 不能进逐帧路径")

    def test_pixel_projection_gated_on_model(self):
        for fn in ("priceYMapper", "drawMapper"):
            body = _extract_fn(self.src, fn)
            self.assertLess(body.index("chartModel()"), body.index("convertToPixel"),
                            f"{fn} 必须先探 model 再调 convertToPixel, 否则未建图时抛 TypeError")

    def test_live_tick_path_fully_gated(self):
        body = _extract_fn(self.src, "updateLivePriceLine")
        gate = body.index("chartModel()")
        self.assertLess(gate, body.index("layoutPriceTagSlots"), "整只 tick 都要让路")
        self.assertLess(gate, body.index("setOption"))
        self.assertIn("lazyUpdate: true", body,
                      "merge 语义的增量补丁 (模型实例不变) 保留 lazyUpdate, 别顺手改掉")

    def test_refresh_layout_gated_twice(self):
        body = _extract_fn(self.src, "refreshPriceTagLayout")
        self.assertGreaterEqual(body.count("chartModel()"), 2,
                                "入口与 80ms 兜底各要一次: 兜底可能早于首次 setOption")
        self.assertLess(body.index("chartModel()"), body.index("layoutPriceTagSlots"))
        self.assertIn("if (!chartModel() || !tagBars()) return;", body)

    def test_chip_axis_gated_before_get_option(self):
        body = _extract_fn(self.src, "syncChipAxis")
        self.assertLess(body.index("chartModel()"), body.index("chart.getOption()"),
                        "getOption() 返回 undefined 时 opt.dataZoom 同样会抛 TypeError")

    def test_full_rebuild_submits_synchronously(self):
        seen = 0
        for fn in ("updateChart", "renderIntraday"):
            for o in _REBUILD_OPTS.findall(_extract_fn(self.src, fn)):
                seen += 1
                self.assertIn("notMerge: true", o)
                self.assertIn("silent: true", o)
                self.assertNotIn("lazyUpdate", o,
                                 "全量重建带 lazyUpdate 会留下「model 已换、数据管线没跑」的空窗, "
                                 "鼠标事件撞上就抛 getRawIndex")
        self.assertEqual(seen, 2, "updateChart / renderIntraday 各要一次原子替换整图")

    def test_incremental_patch_keeps_lazy(self):
        self.assertIn("lazyUpdate: true", _extract_fn(self.src, "patchLastBarOnChart"))


class FreshChartMappingTest(unittest.TestCase):
    """真实 echarts: init 之后没 setOption 时, 投影必须返回 null 而不是抛错。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self):
        script = (
            _extract_fn(self.src, "chartModel") + "\n"
            + _extract_fn(self.src, "priceYMapper") + "\n"
            + _extract_fn(self.src, "drawMapper") + "\n"
            + "const echarts = require(process.argv[1]);\n"
            + "const BARS = " + _BARS_JS + ";\n"
            + "const STATE = { logScale: false, _gridRects: null, chart: null,\n"
            + "  klineData: { klines: BARS } };\n"
            + "STATE.chart = echarts.init(null, null, { renderer: 'svg', ssr: true,"
            + " width: 600, height: 400 });\n"
            + "const out = {};\n"
            + "// 机制: 未建 model 时 convertToPixel 自己就会抛 (所以调用方必须让路)\n"
            + "try { STATE.chart.convertToPixel({ gridIndex: 0 }, [1, 12]); out.rawErr = null; }\n"
            + "catch (e) { out.rawErr = e.message; }\n"
            + "out.mapNull = priceYMapper(BARS) === null;\n"
            + "out.drawNull = drawMapper() === null;\n"
            + "// 建图之后必须恢复正常 (探针不能把正常路径一起挡住)\n"
            + "STATE.chart.setOption({ grid: [{}], xAxis: { type: 'category', data: ['a', 'b'] },\n"
            + "  yAxis: { type: 'value', scale: true },\n"
            + "  series: [{ name: 'K线', type: 'candlestick',"
            + " data: BARS.map(b => [b.low, b.high, b.low, b.high]) }] });\n"
            + "const m = priceYMapper(BARS);\n"
            + "out.y10 = m && Number(m.yOf(10).toFixed(3));\n"
            + "out.yOfNaN = m && Number.isNaN(m.yOf(10));\n"
            + "out.drawOk = !!drawMapper();\n"
            + "STATE.chart.dispose();   // 不然动画循环会挂住 node\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(["node", "-e", script, str(ECHARTS_JS)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_mapper_returns_null_before_first_setoption(self):
        out = self._run()
        self.assertIn("queryComponents", out["rawErr"] or "",
                      "未建 model 时 convertToPixel 抛的就是线上那一条 (机制复现)")
        self.assertTrue(out["mapNull"], "priceYMapper 未建 model 时必须返回 null")
        self.assertTrue(out["drawNull"], "drawMapper 未建 model 时必须返回 null")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_mapper_recovers_after_setoption(self):
        out = self._run()
        self.assertFalse(out["yOfNaN"], "建图后投影必须可用")
        self.assertTrue(out["drawOk"], "建图后画线层投影必须可用")


class RebuildWindowTest(unittest.TestCase):
    """真实 echarts: 全量重建的 setOption 选项直接取自源码, 同一 tick 内新 series 必须有数据。

    对照组用线上曾经的组合 (notMerge + lazyUpdate) 复现 `getRawIndex` 报错 —— 若有人把
    lazyUpdate 加回 updateChart, 被测组就会跟对照组一样抛错。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self):
        opts = _REBUILD_OPTS.findall(self.src)
        self.assertTrue(opts, "找不到全量重建的 setOption 选项对象")
        script = (
            "const echarts = require(process.argv[2]);\n"
            + "const p = JSON.parse(process.argv[1]);\n"
            + "const BARS = " + _BARS_JS + ";\n"
            + "const mk = (n, name) => ({ grid: [{}],"
            + " xAxis: { type: 'category', data: BARS.map((_, i) => 'd' + i) },"
            + " yAxis: { type: 'value', scale: true },\n"
            + "  series: Array.from({ length: n }, (_, i) => ({ name: name + i, type: 'line',"
            + " data: BARS.map(b => b.close + i) })) });\n"
            + "function probe(second) {\n"
            + "  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true,"
            + " width: 600, height: 400 });\n"
            + "  chart.setOption(mk(2, 'A'), { notMerge: true, silent: true });\n"
            + "  chart.setOption(mk(3, 'B'), second);\n"
            + "  const s = chart.getModel().getSeriesByIndex(0);\n"
            + "  const out = { err: null, params: false, pixels: null };\n"
            + "  try { out.params = !!s.getDataParams(0); } catch (e) { out.err = e.message; }\n"
            + "  const px = chart.convertToPixel({ gridIndex: 0 }, [1, 12]);\n"
            + "  out.pixels = px === undefined ? null : px;\n"
            + "  chart.dispose();\n"
            + "  return out;\n"
            + "}\n"
            + "const appOpts = new Function('return (' + p.appOpts + ')')();\n"
            + "const out = { app: probe(appOpts),\n"
            + "  lazy: probe({ notMerge: true, lazyUpdate: true, silent: true }) };\n"
            + "// lazyUpdate 会挂一个 zrender 帧, 写完 stdout 再退出, 别让 node 悬着\n"
            + "process.stdout.write(JSON.stringify(out), () => process.exit(0));\n"
        )
        proc = subprocess.run(["node", "-e", script, json.dumps({"appOpts": opts[0]}), str(ECHARTS_JS)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_lazy_rebuild_reproduces_online_error(self):
        out = self._run()
        self.assertIn("getRawIndex", out["lazy"]["err"] or "",
                      "对照组必须复现线上报错, 否则这条测试就不再证明什么")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_app_rebuild_has_no_dataless_window(self):
        out = self._run()
        self.assertIsNone(out["app"]["err"],
                          "全量重建后同一 tick 内 new series 必须已可 getDataParams")
        self.assertTrue(out["app"]["params"])
        self.assertIsNotNone(out["app"]["pixels"], "重建后同一 tick 内投影也要可用")


class GateBehaviorTest(unittest.TestCase):
    """假 chart: model 未建时整只 tick / 整次签位重排都要让路, 建好后不能少做。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, model_ready):
        script = (
            _extract_fn(self.src, "chartModel") + "\n"
            + _extract_fn(self.src, "updateLivePriceLine") + "\n"
            + _extract_fn(self.src, "refreshPriceTagLayout") + "\n"
            + "const BARS = " + _BARS_JS + ";\n"
            + "const STATE = { symbol: 'X', period: '1d', _tagKeys: '', _tagRefreshPending: false,\n"
            + "  klineData: { symbol: 'X', klines: BARS }, chart: null };\n"
            + "const log = { layout: 0, setOption: 0, redraw: 0, on: 0, timers: 0 };\n"
            + "globalThis.setTimeout = () => { log.timers += 1; return 1; };\n"
            + "function layoutPriceTagSlots() { log.layout += 1; return true; }\n"
            + "function buildKlineMarkLines() { return { data: [1] }; }\n"
            + "function tagBars() { return STATE.klineData.klines; }\n"
            + "function redrawDrawings() { log.redraw += 1; }\n"
            + "STATE.chart = { getModel: () => " + ("({})" if model_ready else "undefined") + ",\n"
            + "  setOption: () => { log.setOption += 1; }, on: () => { log.on += 1; },\n"
            + "  off: () => {} };\n"
            + "updateLivePriceLine({ last_price: 12 }, 'X');\n"
            + "refreshPriceTagLayout();\n"
            + "process.stdout.write(JSON.stringify(log));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_tick_and_relayout_skip_when_model_missing(self):
        out = self._run(model_ready=False)
        self.assertEqual(out, {"layout": 0, "setOption": 0, "redraw": 0, "on": 0, "timers": 0},
                         "model 未建时行情 tick 与签位重排都要整体让路 (不抛错也不推 option)")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_tick_and_relayout_run_when_ready(self):
        out = self._run(model_ready=True)
        self.assertEqual(out["layout"], 1, "建图后行情 tick 照旧算签位")
        self.assertEqual(out["setOption"], 1, "建图后行情 tick 照旧推一次 markLine")
        self.assertEqual(out["on"], 1, "建图后签位重排照旧等 rendered")
        self.assertEqual(out["timers"], 1, "80ms 兜底照旧排上")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_mapper_survives_throwing_convert_to_pixel(self):
        script = (
            _extract_fn(self.src, "chartModel") + "\n"
            + _extract_fn(self.src, "priceYMapper") + "\n"
            + "const STATE = { logScale: false, chart: null };\n"
            + "STATE.chart = { getModel: () => undefined,\n"
            + "  convertToPixel: () => { throw new TypeError('boom: queryComponents'); } };\n"
            + "let out;\n"
            + "try { out = priceYMapper(" + _BARS_JS + ") === null; }\n"
            + "catch (e) { out = 'threw: ' + e.message; }\n"
            + "process.stdout.write(JSON.stringify({ null: out }));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, check=True)
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["null"], True,
                         "model 探针必须在 convertToPixel 之前拦下, 而不是靠 catch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
