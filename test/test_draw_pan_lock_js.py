# -*- coding: utf-8 -*-
"""画线模式的拖拽平移锁回归测试。

规则: 画线模式下只有「选择工具 + 没选中画线」允许拖拽平移
(dataZoom.inside.moveOnMouseMove); 画线工具激活时禁, 选中画线时也禁 ——
选中那一刻交互预算属于这条线 (拖手柄改端点 / 点菜单换色监控), 图跟着鼠标跑会把线
和菜单一起带偏 (用户报告的"选中线之后整张图跟着鼠标乱动")。

关键点: 这条规则必须只有一个出处。图表 option 构建 (updateChart 里的 dzLocked) 每次
重绘都会写一遍 moveOnMouseMove, 若它还用旧规则 (只看工具不看选区), 那么任何一次重绘
(切指标/换周期/落数据) 都会把选中态的锁覆盖掉 —— 所以两处共用 drawPanLocked()。

行为镜像驱动真实函数 (假 chart 记录 setOption 载荷), 静态断言守住"单一出处"。

运行:
    venv/Scripts/python.exe -u visual/test/test_draw_pan_lock_js.py
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


_EXTRACT = ["drawPanLocked", "applyDrawPanLock", "getSelected", "selectDrawing", "setDrawTool", "toggleDrawMode"]

# 假 chart: 只记录 moveOnMouseMove 是否被写过 (以及最终值), 其余 setOption/dispatchAction 忽略。
_HARNESS = r"""
const els = {};
globalThis.document = {
  getElementById: (id) => (els[id] || (els[id] = {
    style: {}, textContent: '', clientWidth: 900, clientHeight: 420, offsetWidth: 120, offsetHeight: 40,
    classList: { toggle() {}, add() {}, remove() {} },
    getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 40, right: 120, width: 120, height: 40 }),
  })),
  querySelectorAll: () => [],
};
const STATE = { symbol: '000001.SZ', period: '1d', chart: null,
  draw: { enabled: false, tool: 'select', selectedId: null, drawings: [], suggest: null, draft: null,
          drag: null, _hover: null, _priceEdit: null } };
function hideMonitorInput() {}
function hideSuggestBox() {}
function hideDrawMenu() {}
function showDrawMenu() {}
function closePriceInput() {}
function redrawDrawings() {}
function showToast() {}
function buildPalette() {}
function drawSupported() { return true; }
function updateChart() {}                 // 真身会写 option, 这里只测运行时开关; option 侧由静态断言钉
STATE.chart = {
  __writes: [],
  setOption(o) {
    const dz = o && o.dataZoom && o.dataZoom[0];
    if (dz && Object.prototype.hasOwnProperty.call(dz, 'moveOnMouseMove')) STATE.chart.__writes.push(dz.moveOnMouseMove);
  },
  dispatchAction() {},
};
const hline = (id) => ({ id: id, type: 'hline', points: [{ t: null, p: 10, off: 0 }],
  style: { color: '#e6a23c', width: 1, dash: false }, extendRight: false });
const snap = () => ({
  locked: drawPanLocked(),
  lastWrite: STATE.chart.__writes.length ? STATE.chart.__writes[STATE.chart.__writes.length - 1] : null,
  enabled: STATE.draw.enabled,
  tool: STATE.draw.tool,
  selected: STATE.draw.selectedId,
});
const actions = {
  add: (id) => { STATE.draw.drawings.push(hline(id)); },
  enable: (on) => { STATE.draw.enabled = !!on; },
  tool: (t) => setDrawTool(t),
  select: (id) => selectDrawing(id),
  selectNone: () => selectDrawing(null),
  enterDraw: () => toggleDrawMode(),
  exitDraw: () => toggleDrawMode(),
  apply: () => applyDrawPanLock(),
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


class PanLockBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, plan):
        script = "\n".join(_extract_fn(self.src, name) for name in _EXTRACT) + "\n" + _HARNESS
        return json.loads(run_node(script, json.dumps(plan)))

    def _expect(self, step, locked, msg):
        self.assertEqual(step["locked"], locked, f"{msg}: drawPanLocked 判定")
        self.assertEqual(step["lastWrite"], not locked, f"{msg}: 下发的 moveOnMouseMove (锁住时须为 false)")

    def test_select_tool_without_selection_allows_pan(self):
        out = self._run([["enable", True], ["tool", "select"]])
        self._expect(out[1], False, "画线模式 + 选择工具 + 未选中")

    def test_selected_drawing_locks_pan(self):
        """核心回归: 选中画线后不许再拖图 (选中那一刻 mousedown 就上锁)。"""
        out = self._run([["enable", True], ["tool", "select"], ["add", "d1"], ["select", "d1"]])
        self._expect(out[2], False, "选中前仍可平移")
        self._expect(out[3], True, "选中画线后")

    def test_deselect_releases_pan(self):
        out = self._run([["enable", True], ["tool", "select"], ["add", "d1"], ["select", "d1"], ["selectNone"]])
        self._expect(out[3], True, "选中画线时锁住")
        self._expect(out[4], False, "取消选中后放开")

    def test_drawing_tool_locks_pan(self):
        out = self._run([["enable", True], ["tool", "trend"]])
        self._expect(out[1], True, "画线工具激活时")

    def test_exit_draw_mode_releases_lock(self):
        """回归: 停在画线工具上退出画线模式, 普通视图也要能拖拽平移 (以前会一直锁着)。"""
        out = self._run([["enable", True], ["tool", "trend"], ["exitDraw"]])
        self._expect(out[1], True, "画线工具激活时锁住")
        self.assertEqual(out[2]["enabled"], False)
        self._expect(out[2], False, "退出画线模式后")

    def test_enter_draw_mode_applies_current_rule(self):
        out = self._run([["enable", True], ["tool", "trend"], ["exitDraw"], ["enterDraw"], ["tool", "select"]])
        self._expect(out[3], True, "重新进入后仍是画线工具 → 锁")
        self._expect(out[4], False, "切到选择工具且未选中 → 放开")

    def test_selection_survives_tool_switch_within_select(self):
        """选择工具内部重复 setDrawTool('select') 不该影响已选中的锁。"""
        out = self._run([["enable", True], ["tool", "select"], ["add", "d1"], ["select", "d1"], ["tool", "select"]])
        self._expect(out[3], True, "选中后")
        self._expect(out[4], True, "再点一次选择工具")

    def test_apply_is_idempotent(self):
        out = self._run([["enable", True], ["tool", "select"], ["apply"], ["apply"]])
        self.assertEqual([s["lastWrite"] for s in out[-2:]], [True, True])


class PanLockStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_rule_has_a_single_source(self):
        """moveOnMouseMove 只能有两个写入口: 图表 option 构建 + applyDrawPanLock, 且共用同一判定。"""
        self.assertEqual(self.src.count("moveOnMouseMove"), 2,
                         "多出来的写入点会与选中态打架 (重绘时把锁覆盖回去就是这个原因)")
        self.assertIn("function drawPanLocked() {", self.src)
        self.assertIn("moveOnMouseMove: !drawPanLocked()", self.src)
        # option 构建侧必须调同一个判定, 且真的用上它 —— 只算不用 (写成 moveOnMouseMove: true)
        # 的话次数与两个 assertIn 都照样成立, 但重绘会把锁覆盖回去, guard 必须钉死这一行
        self.assertIn("const dzLocked = drawPanLocked();", self.src)
        self.assertIn("moveOnMouseMove: !dzLocked", self.src)
        self.assertNotIn("dzLocked = STATE.draw.enabled && STATE.draw.tool !== 'select'", self.src)

    def test_predicate_covers_selection_and_mode(self):
        body = _extract_fn(self.src, "drawPanLocked")
        self.assertIn("if (!dr.enabled) return false;", body, "非画线模式一律放开")
        self.assertIn("return !(dr.tool === 'select' && !dr.selectedId);", body)

    def test_all_state_changes_reapply_the_lock(self):
        self.assertIn("applyDrawPanLock();", _extract_fn(self.src, "selectDrawing"))
        self.assertIn("applyDrawPanLock();", _extract_fn(self.src, "setDrawTool"))
        self.assertIn("applyDrawPanLock();", _extract_fn(self.src, "onDrawMouseUp"))
        toggle = _extract_fn(self.src, "toggleDrawMode")
        self.assertEqual(toggle.count("applyDrawPanLock();"), 2, "进入与退出都要按当前状态定")

    def test_handle_drag_does_not_touch_the_switch(self):
        """手柄拖拽必然发生在已选中时 (已被锁), mousedown 里不该再碰 dataZoom。
        (别靠注释里的字样满足断言 —— 要钉的是"这一段没有任何 dataZoom 写入"。)"""
        down = _extract_fn(self.src, "onDrawMouseDown")
        self.assertNotIn("moveOnMouseMove", down)
        self.assertNotIn("dataZoom", down)


if __name__ == "__main__":
    unittest.main(verbosity=2)
