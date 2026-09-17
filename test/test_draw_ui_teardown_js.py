# -*- coding: utf-8 -*-
"""画线控件随对象收起 (控件残留回归测试)。

背景: 依附于「选中画线」的控件 (迷你菜单 #draw-menu 含换色/线宽/数值/延伸/🔔监控/删除、
监控输入框、数值框、修正建议浮框) 原本只有 selectDrawing(id) 一处负责收起。删除/撤销/清空/
换标的这四条路径直接写 `STATE.draw.selectedId = null` 绕过了它 → 对象没了控件还浮在屏上。
另外「采纳」建议后 suggest 置空但浮框不隐藏, 且清空压下的 'del-all' 撤销记录无人处理。

两道防线:
  1. positionDrawMenu 与 positionSuggestBox 同一条规矩: 状态没了就自己隐藏, 重绘每帧兜底;
  2. 会移除/替换画线的路径统一走 selectDrawing(null) (状态 + 立即收起, 不必等下一帧)。
行为镜像驱动真实函数 (假 DOM), 静态断言守住接线。

运行:
    venv/Scripts/python.exe -u visual/test/test_draw_ui_teardown_js.py
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


_EXTRACT = [
    "getSelected", "hideMonitorInput", "hideSuggestBox", "hideDrawMenu",
    "positionDrawMenu", "positionSuggestBox", "showDrawMenu", "selectDrawing",
    "runSuggest", "deleteSelectedDrawing", "drawUndo", "onPaletteAction",
    "adoptSuggest", "loadDrawings",
    # selectDrawing 会顺带重算拖拽平移锁 (真身, 别用桩: 见 test_draw_pan_lock_js.py)
    "drawPanLocked", "applyDrawPanLock",
]

# 假 DOM: 每个 id 一个元素, 只带这些控件真正用到的字段。
# redrawDrawings 走重绘路径里与控件相关的那两支 (positionDrawMenu 兜底 + 建议浮框定位)。
_HARNESS = r"""
const el = (id) => (els[id] || (els[id] = {
  style: {}, textContent: '', clientWidth: 900, clientHeight: 420, offsetWidth: 120, offsetHeight: 40,
  classList: { toggle() {}, add() {}, remove() {} },
  getBoundingClientRect: () => ({ left: 10, top: 10, bottom: 50, right: 130, width: 120, height: 40 }),
}));
const els = {};
globalThis.document = {
  getElementById: (id) => el(id),
  querySelectorAll: () => [],
};
globalThis.confirm = () => true;
globalThis.fetch = async () => ({ ok: fetchOk, json: async () => ({ drawings: reloaded }) });
let fetchOk = true, reloaded = [];
const Drawings = {
  normalize: (d) => d,
  pointFromIdx: (i, p) => ({ t: null, p: p, off: 0 }),
  handlesOf: () => [],                      // 控件定位到此为止, 不碰真实几何
  // 只有趋势线能出修正建议 (真实实现里也就是 trend/ray 两种线型)
  optimizeTrendline: (bars, d) => (d && d.type === 'trend'
    ? { points: [{ i: 0, p: 9 }, { i: 5, p: 13 }], touches: 4 } : null),
};
// 控件定位只需要「有投影」: 建议浮框因此会真的显示出来 (drawCtx/drawMapper 为 null 时会提前 return)
function drawCtx() { return { n: 10, dateMap: { map: {}, dates: [] }, bars: [] }; }
function drawMapper() {
  return { x: () => 100, y: () => 200, idxAt: () => 0, priceAt: () => 10,
           W: 900, H: 420, grid: { left: 0, right: 800, top: 0, bottom: 400 } };
}
function scheduleSave() {}
function showToast() {}
function updateChart() {}
function closePriceInput() {                  // 真身会隐藏浮框并清掉会话
  el('draw-price-input').style.display = 'none';
  STATE.draw._priceEdit = null;
}
function redrawDrawings() { positionDrawMenu(); positionSuggestBox(); }

const hline = (id) => ({ id: id, type: 'hline', points: [{ t: null, p: 10, off: 0 }],
  style: { color: '#e6a23c', width: 1, dash: false }, extendRight: false });
const trend = (id) => ({ id: id, type: 'trend', points: [{ t: null, p: 10, off: 0 }, { t: null, p: 12, off: 1 }],
  style: { color: '#e6a23c', width: 1, dash: false }, extendRight: false });
const STATE = { symbol: '000001.SZ', period: '1d', adjust: 'forward', chart: null,
  draw: { enabled: true, visible: true, tool: 'select', magnet: true, autoLines: false,
          drawings: [], selectedId: null, undo: [], suggest: null, draft: null, drag: null,
          _priceEdit: null, _hover: null, _loadedKey: null } };
// 平移锁: 只要有个能收 setOption 的 chart 即可 (锁本身在 test_draw_pan_lock_js.py 里细测)
STATE.chart = { setOption() {}, dispatchAction() {} };
const snap = () => ({
  selected: STATE.draw.selectedId,
  ids: STATE.draw.drawings.map(d => d.id),
  menu: el('draw-menu').style.display || 'none',
  monitor: el('draw-monitor-input').style.display || 'none',
  suggest: el('draw-suggest').style.display || 'none',
  price: el('draw-price-input').style.display || 'none',
  suggestState: STATE.draw.suggest,
  priceEdit: STATE.draw._priceEdit,
  visible: STATE.draw.visible,
  undo: STATE.draw.undo.length,
});
const actions = {
  add: (id) => { STATE.draw.drawings.push(hline(id)); },
  addTrend: (id) => { STATE.draw.drawings.push(trend(id)); },
  select: (id) => selectDrawing(id),
  delete: () => deleteSelectedDrawing(),
  undo: () => drawUndo(),
  clear: () => onPaletteAction('clear', { classList: { toggle() {} } }),
  hide: () => onPaletteAction('hide', { classList: { toggle() {} } }),
  // 建议总是属于当前选中那条 (真实链路里 runSuggest 只对刚定稿/拖拽的那条跑)
  suggest: (on) => {
    STATE.draw.suggest = on ? { forId: STATE.draw.selectedId, points: [{ i: 1, p: 10 }] } : null;
    redrawDrawings();
  },
  adopt: () => adoptSuggest(),
  priceEdit: (id) => { STATE.draw._priceEdit = { id: id, before: [] }; el('draw-price-input').style.display = 'flex'; },
  pushUndo: (op) => { STATE.draw.undo.push(op); },
  staleSelect: () => { STATE.draw.selectedId = 'ghost'; },   // 选中 id 指向已不在列表里的对象
  positionOnly: () => positionDrawMenu(),
  reload: async () => { await loadDrawings(); },
  failFetch: () => { fetchOk = false; },
  okFetch: () => { fetchOk = true; },
};
(async () => {
  const out = [];
  for (const [name, arg] of JSON.parse(process.argv[1])) {
    await actions[name](arg);
    out.push(snap());
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


class UiTeardownBehaviorTest(unittest.TestCase):
    """逐条驱动真实源码: 对象消失后依附控件必须收起。"""

    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, plan):
        script = "\n".join(_extract_fn(self.src, name) for name in _EXTRACT) + "\n" + _HARNESS
        return json.loads(run_node(script, json.dumps(plan)))

    def _assert_clean(self, step, msg):
        self.assertIsNone(step["selected"], msg)
        self.assertEqual(step["menu"], "none", f"{msg}: 迷你菜单 (换色/监控) 残留")
        self.assertEqual(step["monitor"], "none", f"{msg}: 监控输入框残留")
        self.assertEqual(step["suggest"], "none", f"{msg}: 修正建议浮框残留")
        self.assertEqual(step["price"], "none", f"{msg}: 数值框残留")

    def test_delete_takes_down_dependent_controls(self):
        out = self._run([["add", "d1"], ["select", "d1"], ["delete"]])
        self.assertEqual(out[1]["menu"], "flex", "选中后菜单要出来 (前置条件)")
        self.assertEqual(out[2]["ids"], [])
        self._assert_clean(out[2], "删除选中画线后")

    def test_delete_closes_price_input_session(self):
        out = self._run([["add", "d1"], ["select", "d1"], ["priceEdit", "d1"], ["delete"]])
        self.assertEqual(out[2]["price"], "flex", "前置条件: 数值框已打开")
        self.assertEqual(out[3]["price"], "none")
        self.assertIsNone(out[3]["priceEdit"], "数值编辑会话要一并清掉")

    def test_undo_of_add_takes_down_controls(self):
        out = self._run([
            ["add", "d1"], ["select", "d1"], ["pushUndo", {"op": "add", "id": "d1"}], ["undo"],
        ])
        self.assertEqual(out[3]["ids"], [], "撤销把刚画的线撤掉了")
        self._assert_clean(out[3], "撤销新增后")

    def test_undo_of_move_keeps_selection_and_menu(self):
        """反向护栏: 撤销拖动没让对象消失, 选区和菜单要原样保留。"""
        out = self._run([
            ["add", "d1"], ["select", "d1"],
            ["pushUndo", {"op": "move", "id": "d1", "before": [{"t": None, "p": 9, "off": 0}]}], ["undo"],
        ])
        self.assertEqual(out[3]["selected"], "d1")
        self.assertEqual(out[3]["menu"], "flex")

    def test_clear_takes_down_controls_and_is_undoable(self):
        out = self._run([
            ["add", "d1"], ["add", "d2"], ["select", "d2"], ["clear"], ["undo"],
        ])
        self.assertEqual(out[3]["ids"], [])
        self._assert_clean(out[3], "清空后")
        self.assertEqual(out[4]["ids"], ["d1", "d2"], "清空要能用 Ctrl+Z 恢复 (del-all)")

    def test_visibility_toggle_deselects_when_hidden(self):
        out = self._run([["add", "d1"], ["select", "d1"], ["hide"], ["hide"]])
        self.assertFalse(out[2]["visible"])
        self._assert_clean(out[2], "👁 隐藏画线时")
        self.assertTrue(out[3]["visible"], "再点一次恢复显示")
        self.assertIsNone(out[3]["selected"], "隐藏时已清掉的选区不会自己回来")

    def test_position_menu_hides_without_selection(self):
        """兜底那道: 只跑重绘 (不经过 selectDrawing) 也要把残留菜单收掉。"""
        out = self._run([["add", "d1"], ["select", "d1"], ["staleSelect"], ["positionOnly"]])
        self.assertEqual(out[2]["menu"], "flex", "前置条件: 菜单还开着但选中对象已不存在")
        self.assertEqual(out[3]["menu"], "none")
        self.assertEqual(out[3]["monitor"], "none")

    def test_reload_takes_down_controls_even_when_fetch_fails(self):
        """换股/换周期会整批替换画线: 收尾要在请求之前, 失败/竞态的提前 return 也不能留残留。"""
        ok = self._run([["add", "d1"], ["select", "d1"], ["reload"]])
        self._assert_clean(ok[2], "换标的重载后")
        bad = self._run([["add", "d1"], ["select", "d1"], ["failFetch"], ["reload"]])
        self._assert_clean(bad[3], "换标的后请求失败时")

    def test_suggest_box_follows_state(self):
        """「采纳」把 suggest 置空, 浮框由重绘路径自愈收起 (与「忽略」按钮一致)。"""
        out = self._run([["add", "d1"], ["select", "d1"], ["suggest", True], ["adopt"]])
        self.assertEqual(out[2]["suggest"], "flex", "前置条件: 建议浮框已显示")
        self.assertEqual(out[3]["suggest"], "none")
        self.assertIsNone(out[3]["suggestState"])

    def test_switch_retargets_suggestion_to_new_selection(self):
        """换选另一条线: 建议不销毁, 而是改指向新选中对象 (不能留一条指向别人的幽灵线)。"""
        out = self._run([
            ["addTrend", "t1"], ["addTrend", "t2"], ["select", "t1"], ["suggest", True],
            ["select", "t2"],
        ])
        self.assertEqual(out[3]["suggestState"]["forId"], "t1", "前置条件: 建议属于 t1")
        self.assertEqual(out[4]["suggest"], "flex", "换选后仍要显示")
        self.assertEqual(out[4]["suggestState"]["forId"], "t2", "建议必须改指向新选中的那条线")

    def test_switch_to_ineligible_type_clears_suggestion(self):
        """新选中对象不适配 (水平线没有修正建议): 清空并收起, 不留上一条线的浮框。"""
        out = self._run([
            ["add", "d1"], ["addTrend", "t1"], ["select", "t1"], ["suggest", True],
            ["select", "d1"],
        ])
        self.assertEqual(out[3]["suggest"], "flex", "前置条件: 建议浮框已显示")
        self.assertEqual(out[4]["suggest"], "none")
        self.assertIsNone(out[4]["suggestState"])

    def test_deselect_destroys_suggestion(self):
        out = self._run([["addTrend", "t1"], ["select", "t1"], ["suggest", True], ["select", None]])
        self.assertEqual(out[2]["suggest"], "flex", "前置条件: 建议浮框已显示")
        self.assertEqual(out[3]["suggest"], "none")
        self.assertIsNone(out[3]["suggestState"])

    def test_delete_destroys_suggestion_and_settings_ui(self):
        """删除: 修正线 (幽灵线+浮框) 与设置 UI 一起销毁。"""
        out = self._run([["addTrend", "t1"], ["select", "t1"], ["suggest", True], ["delete"]])
        self.assertEqual(out[2]["suggest"], "flex", "前置条件: 建议浮框已显示")
        self.assertEqual(out[3]["ids"], [])
        self.assertEqual(out[3]["suggest"], "none")
        self.assertIsNone(out[3]["suggestState"])
        self._assert_clean(out[3], "删除带修正建议的画线后")


class UiTeardownStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_position_menu_hides_when_nothing_selected(self):
        body = _extract_fn(self.src, "positionDrawMenu")
        self.assertIn("if (!d) { hideDrawMenu(); return; }", body,
                      "与 positionSuggestBox 同规矩: 没有选中对象就别留着菜单")
        self.assertLess(body.index("if (!d) { hideDrawMenu(); return; }"),
                        body.index("menu.style.display === 'none'"),
                        "兜底要放在「已显示」判断之前, 否则菜单开着时直接返回")

    def test_render_path_positions_both_boxes(self):
        body = _extract_fn(self.src, "_renderDrawingsNow")
        self.assertIn("positionDrawMenu();", body)
        self.assertIn("positionSuggestBox();", body)
        self.assertLess(body.index("positionDrawMenu();"), body.index("positionSuggestBox();"))

    def test_mutating_paths_route_through_select_drawing(self):
        for fn in ("deleteSelectedDrawing", "loadDrawings"):
            body = _extract_fn(self.src, fn)
            self.assertIn("selectDrawing(null)", body, f"{fn} 要走统一收尾")
            self.assertNotIn("selectedId = null", body, f"{fn} 不能再直接置空选中态")
        clear = _extract_fn(self.src, "onPaletteAction")
        clear_branch = clear[clear.index("id === 'clear'"):]
        self.assertIn("selectDrawing(null);", clear_branch)
        self.assertNotIn("dr.selectedId = null", clear_branch)
        reload_body = _extract_fn(self.src, "loadDrawings")
        self.assertLess(reload_body.index("selectDrawing(null);"),
                        reload_body.index("STATE.draw._loadedKey = key;"),
                        "收尾必须在请求与 _loadedKey 之前, 提前 return 的两条支路才干净")
        self.assertIn("closePriceInput(false);", reload_body, "数值编辑会话仍要先收")

    def test_undo_handles_del_all_and_only_clears_dead_selection(self):
        body = _extract_fn(self.src, "drawUndo")
        self.assertIn("op.op === 'del-all'", body, "清空压的撤销记录必须有人处理")
        self.assertIn("dr.drawings = op.list.slice();", body)
        self.assertIn("!dr.drawings.some(x => x.id === dr.selectedId)", body,
                      "只有选中的那条真的没了才清选区 (move/del 不能误清)")
        self.assertNotIn("dr.selectedId = null", body)

    def test_visibility_toggle_deselects_on_hide(self):
        body = _extract_fn(self.src, "onPaletteAction")
        hide = body[body.index("id === 'hide'"):body.index("id === 'undo'")]
        self.assertIn("if (dr.visible) redrawDrawings(); else selectDrawing(null);", hide)

    def test_select_drawing_is_the_single_teardown(self):
        body = _extract_fn(self.src, "selectDrawing")
        for call in ("hideMonitorInput()", "closePriceInput(false)", "hideSuggestBox()", "hideDrawMenu()"):
            self.assertIn(call, body, f"收尾口应覆盖 {call}")

    def test_select_drawing_retargets_suggestion_on_switch(self):
        body = _extract_fn(self.src, "selectDrawing")
        self.assertIn("const changed = id !== STATE.draw.selectedId;", body)
        self.assertIn("else if (changed && STATE.draw.suggest)", body,
                      "只有换选到另一条线时才重算建议 (点同一条不必重算)")
        self.assertIn("runSuggest(nd);", body, "建议要改指向新选中对象, 不能留着上一条的")
        self.assertLess(body.index("runSuggest(nd);"), body.index("positionSuggestBox();"),
                        "先重算再定位: 新对象没有更好的方案时由定位函数收起")
        deselect = body[body.index("if (!id) {"):body.index("} else if")]
        self.assertIn("STATE.draw.suggest = null;", deselect, "取消选中要销毁建议")
        self.assertIn("hideSuggestBox();", deselect)

    def test_harness_stubs_do_not_shadow_extracted_functions(self):
        """桩函数不得与抽取的真身重名: 同名时后声明的桩会静默顶掉真身。

        (runSuggest 踩过一次 —— 桩把真身顶成空函数, 用例看着"建议没重指向"其实压根没跑。)
        """
        script = "\n".join(_extract_fn(self.src, name) for name in _EXTRACT) + "\n" + _HARNESS
        for name in _EXTRACT:
            self.assertEqual(script.count(f"function {name}("), 1,
                             f"{name} 被重复声明 (harness 桩覆盖了真身)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
