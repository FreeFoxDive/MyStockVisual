# -*- coding: utf-8 -*-
"""水平线悬停预览 + 数值输入回归测试。

背景 (两个都要防回来):
1. `DRAW_POINT_NEEDS.hline = 1` —— 单击即 finalizeDraft, 草稿存活的零长度时间让
   paintDraft 里那条 hline 预览分支永远执行不到, 鼠标移动时屏幕上没有任何跟随反馈。
   现在改为独立的悬停预览 paintHoverGhost (单点工具通用)。
2. 水平线原本没有任何数值入口, 改价只能切选择工具拖手柄。现在落线即弹数值框,
   迷你菜单「数值」与双击水平线也能改价。

行为镜像: Node 里跑真实的 paintHoverGhost / ghostToolActive, 断言跟手虚线与价格签。
静态断言: 落线弹框的接线顺序、菜单按钮的类型门、Esc/双击的让路关系。

运行:
    venv/Scripts/python.exe -u visual/test/test_draw_hline_price_js.py
"""

import json
import re
import unittest
from pathlib import Path

from js_test_util import require_node, run_node

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


# 真实 paintHoverGhost + ghostToolActive + tagColumnOf; 画布/标签以记录桩替代。
_HARNESS = r"""
const STATE = { draw: { tool: 'hline', _hover: null, _priceEdit: null } };
function strokeSeg(c2, x1, y1, x2, y2, color, width, dash) {
  log.segs.push({ x1: x1, y1: y1, x2: x2, y2: y2, color: color, width: width, dash: dash });
}
function paintTag(c2, text, xRight, y, bg) { log.tags.push({ text: text, x: xRight, y: y, bg: bg }); }
function fmtPrice3(v) { return String(Number(Number(v).toFixed(3))); }
const log = { segs: [], tags: [] };
const P = { grid: { left: 40, right: 600, top: 10, bottom: 300 },
            x: (i) => 40 + i * 10, y: (p) => 300 - p * 10 };
const plan = JSON.parse(process.argv[1]);
const out = [];
for (const step of plan) {
  if (step.tool !== undefined) STATE.draw.tool = step.tool;
  if (step.hover !== undefined) STATE.draw._hover = step.hover;
  if (step.priceEdit !== undefined) STATE.draw._priceEdit = step.priceEdit;
  log.segs = []; log.tags = [];
  const tags = [];
  paintHoverGhost(null, P, null, tags);
  out.push({ segs: log.segs, tags: tags, loose: log.tags });
}
process.stdout.write(JSON.stringify(out));
"""


class HoverGhostBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, plan):
        script = (
            _extract_fn(self.src, "tagColumnOf") + "\n"
            + _extract_fn(self.src, "ghostToolActive") + "\n"
            + _extract_fn(self.src, "paintHoverGhost") + "\n"
            + _HARNESS
        )
        return json.loads(run_node(script, json.dumps(plan)))

    def test_hline_hover_draws_full_width_line_and_tag(self):
        out = self._run([{"tool": "hline", "hover": {"idx": 3, "price": 12.5}}])[0]
        self.assertEqual(len(out["segs"]), 1, "水平线预览就是一条线")
        seg = out["segs"][0]
        self.assertEqual((seg["x1"], seg["x2"]), (40, 600), "全宽 (网格左右缘)")
        self.assertEqual(seg["y1"], 300 - 12.5 * 10, "线画在悬停价投影上")
        self.assertEqual(seg["y2"], seg["y1"])
        self.assertEqual(seg["dash"], [4, 4], "预览用虚线, 与已落线区分")
        self.assertEqual(len(out["tags"]), 1, "价格签必须进 tags 参与同列避让")
        tag = out["tags"][0]
        self.assertEqual(tag["id"], "ghost")
        self.assertEqual(tag["text"], "12.5")
        self.assertEqual(tag["x"], 40, "价签贴网格左缘 (与已落线价签同列)")
        self.assertEqual(tag["col"], "left")
        self.assertEqual(tag["y"], seg["y1"])

    def test_vline_hover_draws_vertical_line_without_tag(self):
        out = self._run([{"tool": "vline", "hover": {"idx": 3, "price": 12.5}}])[0]
        self.assertEqual(len(out["segs"]), 1)
        seg = out["segs"][0]
        self.assertEqual((seg["y1"], seg["y2"]), (10, 300), "全高 (网格上下缘)")
        self.assertEqual(seg["x1"], 40 + 3 * 10, "竖线画在悬停 bar 的投影上")
        self.assertEqual(out["tags"], [], "竖线定位的是日期, 不挂价格签")

    def test_no_ghost_for_other_tools(self):
        out = self._run([
            {"tool": "select", "hover": {"idx": 3, "price": 12.5}},
            {"tool": "trend", "hover": {"idx": 3, "price": 12.5}},
            {"tool": "poly", "hover": {"idx": 3, "price": 12.5}},
        ])
        for step in out:
            self.assertEqual(step["segs"], [], "非单点工具靠草稿预览, 不该有跟手线")
            self.assertEqual(step["tags"], [])

    def test_no_ghost_without_hover_point(self):
        out = self._run([{"tool": "hline", "hover": None}])[0]
        self.assertEqual(out["segs"], [])
        self.assertEqual(out["tags"], [])

    def test_no_ghost_while_editing_price(self):
        """数值框开着时屏幕上那条线已经是编辑结果, 不能再叠一条虚线。"""
        out = self._run([{"tool": "hline", "hover": {"idx": 3, "price": 12.5},
                          "priceEdit": {"id": "d1", "before": []}}])[0]
        self.assertEqual(out["segs"], [])
        self.assertEqual(out["tags"], [])

    def test_price_tag_falls_back_to_direct_paint(self):
        """没有 tags 容器时直接落笔 (与已落线价签的兜底分支一致)。"""
        script = (
            _extract_fn(self.src, "tagColumnOf") + "\n"
            + _extract_fn(self.src, "ghostToolActive") + "\n"
            + _extract_fn(self.src, "paintHoverGhost") + "\n"
            + _HARNESS.replace("paintHoverGhost(null, P, null, tags);",
                               "paintHoverGhost(null, P, null, null);")
        )
        out = json.loads(run_node(script, json.dumps([{"tool": "hline", "hover": {"idx": 1, "price": 8}}])))[0]
        self.assertEqual(len(out["loose"]), 1, "无 tags 容器时要用 paintTag 直接画")
        self.assertEqual(out["loose"][0]["text"], "8")


class HlineWiringStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_render_calls_ghost_after_draft_before_tags(self):
        body = _extract_fn(self.src, "_renderDrawingsNow")
        self.assertIn("else paintHoverGhost(c2, P, dctx, tags);", body)
        self.assertLess(body.index("paintDraft(c2, P, dctx)"), body.index("paintHoverGhost"),
                        "草稿优先: 有草稿就不画悬停预览")
        self.assertLess(body.index("paintHoverGhost"), body.index("paintTagLayer(c2, tags, P)"),
                        "价格签要先收集再统一落笔, 否则与已落线价签无法避让")
        self.assertLess(body.index("!STATE.draw.visible"), body.index("paintHoverGhost"),
                        "👁 隐藏画线时预览也要一起消失")

    def test_hover_point_uses_magnet_snapping(self):
        """预览坐标与落子同源: 都取 snappedPoint 的结果, 所见即所得。"""
        move = _extract_fn(self.src, "onDrawMouseMove")
        self.assertRegex(move, r"_hover = snappedPoint\(info\)")
        down = _extract_fn(self.src, "onDrawMouseDown")
        self.assertRegex(down, r"const pt = snappedPoint\(info\)")
        self.assertIn("ghostToolActive()", move, "移出主图/无悬停点时要收掉预览")
        self.assertIn("_hover = null", move)

    def test_leaving_grid_keeps_draft_pointer(self):
        """移出主图只收悬停预览: _pointer 是草稿端点预览的坐标, 别的工具靠它冻结在最后位置。"""
        move = _extract_fn(self.src, "onDrawMouseMove")
        branch = move[move.index("if (!info) {"):move.index("STATE.draw._pointer = {")]
        self.assertIn("_hover = null", branch)
        self.assertNotIn("_pointer = null", branch, "别改其他工具草稿预览的既有行为")
        gout = self.src[self.src.index("on('globalout'"):][:600]
        self.assertIn("_hover = null", gout, "指针离开整个图表时预览同样要收掉")
        self.assertNotIn("_pointer = null", gout, "globalout 同理")

    def test_single_point_tools_have_no_draft_preview(self):
        """水平线/垂直线 need=1, 草稿不可能存在 —— 预览只走 paintHoverGhost。"""
        draft = _extract_fn(self.src, "paintDraft")
        self.assertNotIn("draft.tool === 'hline'", draft)
        self.assertNotIn("draft.tool === 'vline'", draft)
        self.assertIn("paintHoverGhost", draft, "留一句指路, 免得以后又把预览塞回草稿分支")

    def test_click_opens_price_input_for_hline_only(self):
        down = _extract_fn(self.src, "onDrawMouseDown")
        self.assertLess(down.index("finalizeDraft()"), down.index("openPriceInput("),
                        "线必须先落袋再弹框: Esc/取消 不会丢掉这条线")
        self.assertIn("const isHline = dr.tool === 'hline';", down)
        self.assertRegex(down, r"if \(isHline\) openPriceInput\(dr\.drawings\[dr\.drawings\.length - 1\]")
        self.assertIn("e.event.preventDefault()", down, "否则数值框刚 focus 就被 mousedown 抢走")

    def test_price_box_anchored_and_stepped_by_price_scale(self):
        body = _extract_fn(self.src, "openPriceInput")
        self.assertIn("dr._priceEdit = { id: d.id, before: JSON.parse(JSON.stringify(d.points)) };", body,
                      "打开前要留快照, Esc 才有得还原")
        self.assertIn("field.value = fmtPrice3(price);", body)
        self.assertIn("field.dataset.step =", body, "低价股/指数要用不同的 ↑↓ 步长")
        self.assertIn("setTimeout(() => { field.focus(); field.select(); }, 0);", body)
        # type=number 下 select() 无效 (预填值会被追加), 必须用 text + 自己实现步进
        self.assertIn('id="dpi-value" inputmode="decimal"', self.src)
        self.assertNotIn('id="dpi-value" type="number"', self.src)
        nudge = _extract_fn(self.src, "nudgePriceInput")
        self.assertIn("field.dataset.step", nudge)
        self.assertIn("onPriceInput();", nudge, "微调后同样要走实时写回")

    def test_live_preview_writes_through_pure_logic(self):
        body = _extract_fn(self.src, "onPriceInput")
        self.assertIn("Drawings.setPointValue(d, value, 0)", body)
        self.assertIn("redrawDrawings()", body, "输入时线要跟着数字移动")

    def test_commit_and_revert_semantics(self):
        commit = _extract_fn(self.src, "commitPriceInput")
        self.assertIn("if (!explicit) { closePriceInput(true); return false; }", commit,
                      "失焦遇非法值直接还原, 不能把焦点抢回来")
        self.assertIn("showToast('请输入大于 0 的价格'", commit)
        close = _extract_fn(self.src, "closePriceInput")
        self.assertIn("if (d && revert && dirty) d.points = pe.before;", close, "Esc/取消 还原落点价")
        self.assertIn("op: 'move'", close, "改价要可撤销 (Ctrl+Z)")
        # 落线那次防抖保存被 clearTimeout 取消, 收尾必须补一次 (新线要入库 / 还原要覆盖中间态)
        self.assertRegex(close, r"if \(d\) \{\s*\n(?:.*\n)*?\s*scheduleSave\(\);")
        self.assertIn("clearTimeout(_saveTimer);", _extract_fn(self.src, "openPriceInput"),
                      "打开编辑要先掐掉落线那次防抖保存, 否则敲进去的中间值会被顺手写库")

    def test_menu_button_only_for_hline(self):
        show = _extract_fn(self.src, "showDrawMenu")
        self.assertIn("document.getElementById('dm-value').style.display = d.type === 'hline' ? '' : 'none';", show)
        self.assertIn("openPriceInput(d)", _extract_fn(self.src, "buildDrawMenu"))
        self.assertIn('id="dm-value"', self.src)

    def test_double_click_edits_hline_but_keeps_poly_finish(self):
        dbl = self.src[self.src.index("getZr().on('dblclick'"):][:1200]
        self.assertIn("finishPolyDraft(); return;", dbl)
        self.assertLess(dbl.index("finishPolyDraft(); return;"), dbl.index("hitTest("),
                        "折线的双击结束必须先判定, 不能被水平线改价抢走")
        self.assertIn("d.type !== 'hline'", dbl)
        self.assertIn("openPriceInput(d,", dbl)

    def test_escape_and_global_keys_yield_to_price_field(self):
        esc = _extract_fn(self.src, "onDrawEscape")
        self.assertLess(esc.index("_priceEdit"), esc.index("draw-monitor-input"),
                        "Esc 先处理数值框 (还原), 再轮到监控输入")
        self.assertIn("e.target.id === 'dpi-value'", self.src,
                      "全局快捷键必须给数值框让路, 否则 Esc 会被画线抢走")

    def test_state_and_listener_declared(self):
        self.assertIn("_hover: null,", self.src[self.src.index("draw: {"):])
        self.assertIn("_priceEdit: null,", self.src[self.src.index("draw: {"):])
        self.assertIn("buildPriceInput();", self.src)

    def test_sessions_closed_on_context_switch(self):
        self.assertIn("closePriceInput(false);", _extract_fn(self.src, "loadDrawings"),
                      "换股/换周期会整批替换画线, 编辑会话必须收掉")
        sel = _extract_fn(self.src, "selectDrawing")
        self.assertIn("_priceEdit.id !== id", sel)
        toggle = _extract_fn(self.src, "toggleDrawMode")
        self.assertIn("closePriceInput(false);", toggle, "退出画线模式不该丢掉已输入的数值")
        # 浏览器返回/前进换标的: 也必须先收会话 + flush, 否则收尾的保存会把上一只
        # 的画线写到新标的名下 (drawings 的 fetch 还没回来或请求失败时)
        pop = self.src[self.src.index("addEventListener('popstate'"):][:900]
        self.assertLess(pop.index("closePriceInput(false)"), pop.index("STATE.symbol = s"),
                        "popstate 换股分支要在改 STATE.symbol 之前收会话")
        self.assertLess(pop.index("flushPendingSave()"), pop.index("STATE.symbol = s"))


class DrawSaveKeyTest(unittest.TestCase):
    """画线保存的归属键: 数值编辑会话在换股/换周期时才收尾, 保存必须记在当时的标的下。

    回归: openPriceInput 会掐掉落线那次防抖保存 (否则中途敲进去的值会被顺手写库), 收尾时
    再排一次。若这次收尾发生在换股之后, 保存就会把上一只的画线写进新标的名下 —— 收尾前
    要 flushPendingSave, 保存本身也带排期时的 {symbol, period} 兜底。
    """

    HARNESS = r"""
const STATE = { symbol: 'A.SZ', period: '1d', draw: { drawings: [{ id: 'd1' }] } };
const calls = [];
let seq = 0;
const timers = new Map();
globalThis.fetch = async (url, opts) => { calls.push(JSON.parse(opts.body)); return { ok: true }; };
globalThis.setTimeout = (fn) => { timers.set(++seq, fn); return seq; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
const actions = {
  schedule: () => scheduleSave(),
  flush: () => flushPendingSave(),
  symbol: (v) => { STATE.symbol = v; },
  period: (v) => { STATE.period = v; },
  drawing: (id) => { STATE.draw.drawings = [{ id: id }]; },
  fire: async () => {
    const fns = [...timers.values()];
    timers.clear();
    for (const f of fns) await f();
  },
};
(async () => {
  const out = [];
  for (const [name, arg] of JSON.parse(process.argv[1])) {
    await actions[name](arg);
    out.push({ calls: calls.length, last: calls[calls.length - 1] || null, pending: timers.size });
  }
  process.stdout.write(JSON.stringify(out));
})();
"""

    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, plan):
        script = (
            "let _saveTimer = null;\nlet _saveKey = null;\n"   # 源码里是模块级 let, 不在函数体内
            + _extract_fn(self.src, "scheduleSave") + "\n"
            + _extract_fn(self.src, "flushPendingSave") + "\n"
            + _extract_fn(self.src, "saveDrawings") + "\n"
            + self.HARNESS
        )
        return json.loads(run_node(script, json.dumps(plan)))

    def test_switch_during_debounce_drops_the_stale_write(self):
        """排期后被换股: 到点写库要放弃, 否则把上一只的画线写进新标的。"""
        out = self._run([["schedule"], ["symbol", "B.SZ"], ["fire"]])
        self.assertEqual(out[0]["pending"], 1)
        self.assertEqual(out[2]["calls"], 0, "换股后到点的保存必须放弃")

    def test_flush_before_switch_writes_under_the_old_symbol(self):
        """换股前的 flush 要用旧的 symbol 落库 (画线随标的隔离)。"""
        out = self._run([
            ["schedule"], ["drawing", "d2"], ["symbol", "B.SZ"],   # 先换股再 flush 已经晚了
            ["symbol", "A.SZ"], ["schedule"], ["drawing", "d3"], ["flush"],
        ])
        self.assertEqual(out[-1]["last"]["symbol"], "A.SZ")
        self.assertEqual(out[-1]["last"]["drawings"], [{"id": "d3"}])
        self.assertEqual(out[-1]["pending"], 0, "flush 之后不应还留着定时器")

    def test_intraday_key_is_never_saved(self):
        out = self._run([["symbol", "300070.SZ"], ["period", "intraday"], ["schedule"], ["fire"]])
        self.assertEqual(out[-1]["calls"], 0, "分时没有画线, 不该发 PUT")
        self.assertEqual(out[-1]["pending"], 0)

    def test_switch_symbol_and_period_flush_before_mutating_state(self):
        sym = _extract_fn(self.src, "switchSymbol")
        self.assertLess(sym.index("closePriceInput(false)"), sym.index("STATE.symbol = symbol"),
                        "收尾必须在改 STATE.symbol 之前, 否则保存会记到新标的名下")
        self.assertLess(sym.index("flushPendingSave()"), sym.index("STATE.symbol = symbol"))
        per = _extract_fn(self.src, "switchPeriod")
        self.assertLess(per.index("flushPendingSave()"), per.index("applyPeriodUI(period)"),
                        "换周期同理: 画线按 代码+周期 分库 (STATE.period 在 applyPeriodUI 里才改)")
        self.assertIn("closePriceInput(false)", per)


if __name__ == "__main__":
    unittest.main(verbosity=2)
