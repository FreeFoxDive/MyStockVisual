# -*- coding: utf-8 -*-
"""贴线标签避让测试: 现价/风控签(主图 markLine) + 画线框签(overlay)共用的错位算法。

背景: 同一列里的贴线标签只有"自己那条线的 y"一个自由度, ECharts 不做避让, 两条线
间距小于标签高度就必然重叠。现价签每 1.25s 随快照移动, 扫过保本/止损时必现 —— 所以
每次快照、建图、缩放都要重算 (调用点的接线另有静态断言)。

这里只测两份纯逻辑:
  * stackTags   —— 同列最小位移错位 (含占位、边界钳制、超限丢弃);
  * tagLabelPos —— 盒中心 → markLine 的 (position, distance) 换算。

运行:
    venv/Scripts/python.exe -u visual/test/test_tag_layout_js.py
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


def _extract_const(src: str, name: str) -> str:
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*([^;]+);", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name}")
    return f"const {name} = {m.group(1)};"


class TagLayoutStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_tick_path_layouts_before_setoption(self):
        """现价每 tick 移动 → 必须先算签位再建线, 一次 setOption 同时更新位置。"""
        body = _extract_fn(self.src, "updateLivePriceLine")
        self.assertLess(body.index("layoutPriceTagSlots"), body.index("setOption"),
                        "签位必须先于 setOption 算好")
        self.assertIn("redrawDrawings()", body, "画线层框签要跟随现价移动重绘")

    def test_all_layout_trigger_points_wired(self):
        # 建图后 (新刻度建图前拿不到) / 缩放 / 分时 / 尾盘 bar patch
        self.assertIn("refreshPriceTagLayout()", _extract_fn(self.src, "updateChart"))
        self.assertIn("cacheGridRects(grids)", _extract_fn(self.src, "updateChart"))
        self.assertIn("refreshPriceTagLayout()", _extract_fn(self.src, "renderIntraday"))
        self.assertIn("refreshPriceTagLayout()", self.src[self.src.index("STATE.chart.on('dataZoom'"):][:600])
        self.assertIn("layoutPriceTagSlots(klines)", _extract_fn(self.src, "patchLastBarOnChart"))

    def test_overlay_tags_collected_then_painted(self):
        body = _extract_fn(self.src, "_renderDrawingsNow")
        self.assertLess(body.index("paintDrawing(c2, d, P, dctx"), body.index("paintTagLayer(c2, tags, P)"),
                        "框签要等所有图形画完再统一落笔, 否则同列之间无法互相避让")
        # 就地的 paintTag 只剩兜底分支与落笔函数本身
        self.assertNotIn("paintTag(c2, fmtPrice3(g.price)", body)
        self.assertIn("paintTagLayer(c2, tags, P)", body)

    def test_left_column_reserves_price_tags(self):
        body = _extract_fn(self.src, "paintTagLayer")
        self.assertIn("col === 'left' ? reserve : []", body,
                      "左缘列要把现价/风控签当占位, 画线注解让位给行情签")
        self.assertIn("STATE._tagSlots", body)


class TagLayoutBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, payload):
        script = (
            _extract_const(self.src, "TAG_PITCH") + "\n"
            + _extract_const(self.src, "TAG_HUG") + "\n"
            + _extract_const(self.src, "TAG_MAX_SHIFT") + "\n"
            + _extract_const(self.src, "TAG_BOX_H") + "\n"
            + _extract_fn(self.src, "stackTags") + "\n"
            + _extract_fn(self.src, "tagLabelPos") + "\n"
            + "const p = JSON.parse(process.argv[1]);\n"
            + "const out = { stacks: [], pos: [], consts: { PITCH: TAG_PITCH, HUG: TAG_HUG,"
            + " MAX: TAG_MAX_SHIFT, BOX: TAG_BOX_H } };\n"
            + "for (const c of p.stacks) out.stacks.push(stackTags(c.items, c.bounds, c.fixed));\n"
            + "for (const c of p.pos) out.pos.push(tagLabelPos(c.lineY, c.centerY));\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(["node", "-e", script, json.dumps(payload)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def _stack(self, items, bounds=None, fixed=None):
        out = self._run({"stacks": [{"items": items, "bounds": bounds or {"top": 0, "bottom": 400},
                                     "fixed": fixed or []}], "pos": []})
        return out["stacks"][0]

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_four_tags_on_the_same_y_are_separated(self):
        items = [{"key": "last", "y": 200, "lineY": 213, "priority": 0},
                 {"key": "stop_loss", "y": 200, "lineY": 187, "priority": 1},
                 {"key": "breakeven", "y": 202, "lineY": 189, "priority": 2},
                 {"key": "take_profit", "y": 204, "lineY": 191, "priority": 3}]
        out = self._stack(items)
        ys = [r["y"] for r in out]
        self.assertEqual(len(ys), 4)
        self.assertFalse(any(r["dropped"] for r in out), "还有空间就不该丢签")
        ordered = sorted(ys)
        for a, b in zip(ordered, ordered[1:]):
            self.assertGreaterEqual(round(b - a, 3), 20, f"间距不足: {ordered}")
        # 最高优先级的现价签不能被挪动 (它钉在自己的线上)
        self.assertEqual(out[0]["key"], "last")
        self.assertAlmostEqual(out[0]["shift"], 0, places=3)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_spread_tags_keep_their_place(self):
        items = [{"key": "last", "y": 100, "lineY": 113, "priority": 0},
                 {"key": "stop_loss", "y": 250, "lineY": 237, "priority": 1},
                 {"key": "take_profit", "y": 300, "lineY": 287, "priority": 3}]
        out = self._stack(items)
        for it, r in zip(items, out):
            self.assertEqual(r["shift"], 0, "不挤就不动")
            self.assertFalse(r["dropped"])

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_minimal_displacement_and_bounds_clamp(self):
        # 两个签相距 6px: 低优先级那个让位, 位移取最小 (20-6=14)
        out = self._stack([{"key": "last", "y": 200, "lineY": 213, "priority": 0},
                           {"key": "stop_loss", "y": 206, "lineY": 193, "priority": 1}])
        self.assertEqual(out[0]["shift"], 0)
        self.assertAlmostEqual(out[1]["y"], 220, places=3)
        self.assertAlmostEqual(out[1]["shift"], 14, places=3)
        # 贴顶/贴底时不得越界 (bounds 内各留半个盒高; 边界值从源码常量取, 免得改盒高后失效)
        consts = self._run({"stacks": [], "pos": []})["consts"]
        half = consts["BOX"] / 2
        out2 = self._stack([{"key": "last", "y": 5, "lineY": 40, "priority": 0},
                            {"key": "stop_loss", "y": 6, "lineY": -7, "priority": 1}],
                           {"top": 0, "bottom": 400})
        for r in out2:
            self.assertGreaterEqual(r["y"], half - 0.001, "越界到顶外")
            self.assertLessEqual(r["y"], 400 - half + 0.001, "越界到底外")
        for r in out2:
            self.assertFalse(r["dropped"], "贴边但有空间时不该丢签")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_too_crowded_tag_is_dropped(self):
        # 一条 60px 高的窄列塞 6 个签: 超限的不画字 (数值在徽章悬停里), 其余照常错开
        items = [{"key": f"t{i}", "y": 100 + i, "lineY": None, "priority": i} for i in range(6)]
        out = self._stack(items, {"top": 70, "bottom": 130})
        self.assertTrue(any(r["dropped"] for r in out), "挤不下就该丢, 而不是叠着")
        kept = [r["y"] for r in out if not r["dropped"]]
        self.assertTrue(kept)
        for a, b in zip(sorted(kept), sorted(kept)[1:]):
            self.assertGreaterEqual(round(b - a, 3), 20)
        self.assertLessEqual(len(kept), 4, "70~130 的窄列最多容 4 个间距 20px 的签")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_fixed_slots_are_respected(self):
        # 现价签已占位 (画线层看不到 ECharts 的签, 只能当占位传进来)
        out = self._stack([{"key": "hline:1", "y": 202, "priority": 10}], None, [200])
        self.assertNotAlmostEqual(out[0]["y"], 200, places=3)
        self.assertGreaterEqual(abs(out[0]["y"] - 200), 20)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_slot_near_its_own_line_is_rejected(self):
        # 槽位离**自己那条线**不足 TAG_HUG (13) 会让标签盒压线 → 不能要 (拿线比, 不是 0)
        items = [{"key": "last", "y": 208, "lineY": 213, "priority": 0},      # 距自己线 5px
                 {"key": "stop_loss", "y": 205, "lineY": 200, "priority": 1}]  # 距自己线 5px
        out = self._stack(items)
        for it, r in zip(items, out):
            if r["dropped"]:
                continue
            self.assertGreaterEqual(abs(r["y"] - it["lineY"]), 13 - 0.001,
                                    f"{it['key']} 的槽位压在自己的线上")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_edge_hugging_tags_are_not_dropped(self):
        """回归: 线贴网格上/下缘时, base 被 clamp 到边界后离自己的线不足 TAG_HUG,
        而候选集里没有"线的两侧"就一个可用槽位都没有 → 标签整个消失 (现价在区间高点
        时必现)。补上线的两侧最小合法位后应改到另一侧, 而不是丢签。"""
        out = self._stack([{"key": "last", "y": 108, "lineY": 115, "priority": 0}],
                          {"top": 100, "bottom": 500})
        self.assertFalse(out[0]["dropped"], "贴顶单签不该丢")
        self.assertGreaterEqual(abs(out[0]["y"] - 115), 13 - 0.001, "仍须离自己的线一个间距")
        out2 = self._stack([{"key": "stop_loss", "y": 492, "lineY": 485, "priority": 1}],
                           {"top": 100, "bottom": 500})
        self.assertFalse(out2[0]["dropped"], "贴底单签不该丢")
        self.assertGreaterEqual(abs(out2[0]["y"] - 485), 13 - 0.001)
        # 真正的"挤不下"仍然要丢: 窄列塞 6 个
        crowded = self._stack([{"key": f"t{i}", "y": 100 + i, "lineY": None, "priority": i}
                               for i in range(6)], {"top": 70, "bottom": 130})
        self.assertTrue(any(r["dropped"] for r in crowded))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_tag_label_pos_matches_echarts_anchor_semantics(self):
        out = self._run({"stacks": [], "pos": [
            {"lineY": 100, "centerY": 86},   # 线上方
            {"lineY": 100, "centerY": 114},  # 线下方
            {"lineY": 100, "centerY": 100},  # 压在线上
        ]})["pos"]
        # 盒高 14 (TAG_BOX_H): 上方 → 盒底贴 (线 y − distance), distance = 100-86-7 = 7
        self.assertEqual(out[0]["position"], "insideStartTop")
        self.assertEqual(out[0]["distance"][1], 7)
        # 下方 → 盒顶贴 (线 y + distance), distance = 114-100-7 = 7
        self.assertEqual(out[1]["position"], "insideStartBottom")
        self.assertEqual(out[1]["distance"][1], 7)
        # 压在线上: 退到最小间隙 (distance 至少 1), 不让标签贴上那条线
        self.assertEqual(out[2]["distance"][1], 1)
        self.assertEqual(out[0]["distance"][0], 2, "横向缩进 2px")
        self.assertEqual([out[0]["position"], out[1]["position"], out[2]["position"]],
                         ["insideStartTop", "insideStartBottom", "insideStartTop"])

    def test_box_height_matches_measured_svg(self):
        """盒高必须与 ECharts 实际渲染的 14px 一致, 否则 distance 每签偏 1px。"""
        self.assertIn("const TAG_BOX_H = 14;", self.src)


class TagRefreshDeferralTest(unittest.TestCase):
    """建图后重排签位必须等这一帧渲染完再算。

    setOption 用 lazyUpdate 提交, 提交完 convertToPixel 返回的还是**旧刻度** —— 若同步
    算签位, 换股/切周期后的第一版签位会按上一只股票的刻度摆放 (错几十像素)。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, plan):
        script = (
            _extract_fn(self.src, "refreshPriceTagLayout") + "\n"
            + "const handlers = {}; const timers = [];\n"
            + "globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };\n"
            + "const log = { layout: 0, setOption: 0, redraw: 0 };\n"
            + "const STATE = { chart: null, _tagKeys: '', _tagRefreshPending: false };\n"
            + "function tagBars() { return [{}]; }\n"
            + "function layoutPriceTagSlots() { log.layout++; STATE._tagKeys = 'k'; return true; }\n"
            + "function buildKlineMarkLines() { return { data: [1] }; }\n"
            + "function redrawDrawings() { log.redraw++; }\n"
            + "STATE.chart = { on: (n, f) => { handlers[n] = f; }, off: () => {},"
            + " setOption: () => { log.setOption++; } };\n"
            + "const out = {};\n"
            + "refreshPriceTagLayout();\n"
            + "out.sync = { layout: log.layout, setOption: log.setOption, pending: STATE._tagRefreshPending };\n"
            + "refreshPriceTagLayout(); refreshPriceTagLayout();   // 重复调用只排一次\n"
            + "out.dedup = timers.length;\n"
            + "handlers['rendered']();\n"
            + "out.afterRender = { layout: log.layout, setOption: log.setOption,"
            + " pending: STATE._tagRefreshPending, redraw: log.redraw };\n"
            + "timers.forEach(f => f());                        // 兜底定时器: 事件没来也要排\n"
            + "out.afterTimers = { layout: log.layout, setOption: log.setOption };\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_layout_waits_for_render(self):
        out = self._run(None)
        self.assertEqual(out["sync"]["layout"], 0, "提交后立刻算 = 读到旧刻度")
        self.assertEqual(out["sync"]["setOption"], 0)
        self.assertTrue(out["sync"]["pending"])
        self.assertEqual(out["dedup"], 1, "重复触发只排一次")
        self.assertEqual(out["afterRender"]["layout"], 1, "渲染完成后才算签位")
        self.assertEqual(out["afterRender"]["setOption"], 1, "只推一次 markLine")
        self.assertEqual(out["afterRender"]["redraw"], 1, "签位变了要重绘画线层")
        self.assertFalse(out["afterRender"]["pending"], "排队标记要复位")
        self.assertEqual(out["afterTimers"]["layout"], 1, "兜底定时器不得重复算")


if __name__ == "__main__":
    unittest.main(verbosity=2)
