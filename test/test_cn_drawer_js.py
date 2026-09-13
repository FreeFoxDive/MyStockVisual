# -*- coding: utf-8 -*-
"""市场数据抽屉宽度自适应测试 (fitCnDrawer)。

需求: 抽屉不再因内容宽而必须横向滑动 —— 默认更宽, 并按表格 scrollWidth 自适应 (上限 92vw)。
从 index.html 抽取 fitCnDrawer **真实源码**在 Node 里注入假 DOM 执行 (抽不到即失败)。

运行:
    venv/Scripts/python.exe -u visual/test/test_cn_drawer_js.py
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
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
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


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class CnDrawerFitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        cls.script = (_extract_fn(src, "fitCnDrawer") + "\n"
                      + "const c = JSON.parse(process.argv[1]);"
                      # 假 DOM: drawer 收集写入的 width; table 提供 scrollWidth
                      + "globalThis.document = {getElementById: () => c.drawer,"
                      + " querySelector: () => c.table};"
                      + "globalThis.window = {innerWidth: c.innerWidth};"
                      + "fitCnDrawer();"
                      + "process.stdout.write(JSON.stringify(c.drawer.style.width));")

    def _width(self, scroll_width, inner_width, has_table=True):
        drawer = {"style": {"width": "initial"}}
        table = {"scrollWidth": scroll_width} if has_table else None
        proc = subprocess.run(
            ["node", "-e", self.script,
             json.dumps({"drawer": drawer, "table": table, "innerWidth": inner_width})],
            capture_output=True, check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    def test_widens_to_content(self):
        # 700 + 26 = 726, 1920 视口下上限 1766 → 726
        self.assertEqual(self._width(700, 1920), "726px")

    def test_min_width_floor(self):
        # 内容很窄也不小于 440
        self.assertEqual(self._width(100, 1920), "440px")

    def test_capped_by_viewport(self):
        # 视口 400 → 上限 round(400*0.92)=368, 不能超过
        self.assertEqual(self._width(700, 400), "368px")

    def test_no_table_resets_to_css(self):
        # 无表格 (加载中/空/失败) → 清掉内联宽度, 回落 CSS clamp
        self.assertEqual(self._width(0, 1920, has_table=False), "")

    def test_drawer_not_hardcoded_400(self):
        src = INDEX_HTML.read_text(encoding="utf-8")
        start = src.index('id="cn-drawer"')
        style = src[start:start + 320]
        self.assertNotIn("width:400px", style.replace(" ", ""))
        self.assertIn("clamp(", style, "抽屉默认宽度应为 clamp(...) 自适应")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class ShowCnTabRaceTest(unittest.TestCase):
    """tab 切换竞态: 过期响应不得覆盖新 tab 内容。

    抽取 index.html 的 showCnTab 真实源码, 用可控 api (手动 resolve/reject) 模拟乱序返回。
    """

    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        fn = _extract_fn(src, "showCnTab")
        cls.script = (
            "let _cnTab = 'seed'; let _cnSeq = 0;\n"
            + fn + "\n"
            + """
const mode = process.argv[1];
const tick = () => new Promise(r => setTimeout(r, 0));
const pending = [];
const bodyEl = { innerHTML: "" };
let fits = 0;
globalThis.STATE = { symbol: "AAA.SZ" };
globalThis.escHtml = (s) => String(s);
globalThis.fitCnDrawer = () => { fits += 1; };
globalThis.api = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
globalThis.document = {
  getElementById: (id) => (id === "cn-body" ? bodyEl : null),
  querySelectorAll: () => [],
};
(async () => {
  if (mode === "out-of-order") {
    showCnTab("fund-flow");
    showCnTab("unlock");
    await tick();
    pending[1].resolve({ rows: [{ "解禁日期": "2026-01-01" }] });   // 新 tab 先返回
    await tick();
    pending[0].resolve({ rows: [{ "主力净流入额(亿)": 1 }] });      // 旧 tab 晚到
    await tick();
  } else if (mode === "normal") {
    showCnTab("unlock");
    await tick();
    pending[0].resolve({ rows: [{ "解禁日期": "2026-01-01" }] });
    await tick();
  } else if (mode === "symbol-change") {
    showCnTab("unlock");
    STATE.symbol = "BBB.SZ";                                      // 期间切股
    await tick();
    pending[0].resolve({ rows: [{ "解禁日期": "2026-01-01" }] });
    await tick();
  } else if (mode === "stale-error") {
    showCnTab("fund-flow");
    showCnTab("unlock");
    await tick();
    pending[0].reject(new Error("boom"));                          // 旧请求报错
    await tick();
  }
  process.stdout.write(JSON.stringify({ html: bodyEl.innerHTML, fits, tab: _cnTab }));
})();
"""
        )

    def _run(self, mode):
        proc = subprocess.run(["node", "-e", self.script, mode], capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def test_late_old_response_does_not_overwrite_new_tab(self):
        out = self._run("out-of-order")
        self.assertIn("解禁日期", out["html"], "新 tab 内容应保留")
        self.assertNotIn("主力净流入额(亿)", out["html"], "旧 tab 晚到响应不得覆盖")
        self.assertEqual(out["fits"], 1, "只有当前请求触发 fitCnDrawer")

    def test_current_response_renders(self):
        out = self._run("normal")
        self.assertIn("解禁日期", out["html"], "当前请求应正常渲染 (守卫不误杀)")
        self.assertEqual(out["fits"], 1)

    def test_symbol_change_drops_response(self):
        out = self._run("symbol-change")
        self.assertIn("加载中", out["html"], "已切股 → 丢弃响应, 保持加载中")
        self.assertNotIn("解禁日期", out["html"])

    def test_stale_error_does_not_overwrite(self):
        out = self._run("stale-error")
        self.assertNotIn("加载失败", out["html"], "过期请求报错不得覆盖新 tab")
        self.assertIn("加载中", out["html"])


class CnDrawerSymbolSwitchTest(unittest.TestCase):
    """换股时已打开的市场数据抽屉必须重载当前 tab (而非停在上一只股票)。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_load_current_refreshes_symbol_panels(self):
        self.assertIn("refreshSymbolPanels()", _extract_fn(self.src, "loadCurrent"))

    def test_refresh_symbol_panels_guards_and_reloads(self):
        fn = _extract_fn(self.src, "refreshSymbolPanels")
        self.assertIn("cn-drawer", fn)
        self.assertIn("'block'", fn, "仅在抽屉可见时重载")
        self.assertIn("showCnTab(_cnTab)", fn, "重载当前 tab")
        self.assertIn("alert-symbol", fn, "预警弹窗标的标签同步")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class CnDrawerSymbolRefreshBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        fn = _extract_fn(src, "refreshSymbolPanels")
        cls.script = (
            "let _cnTab = 'unlock';\n"
            + fn + "\n"
            + """
const c = JSON.parse(process.argv[1]);
let reloaded = 0, lastTab = null;
globalThis.showCnTab = (t) => { reloaded++; lastTab = t; };
globalThis.STATE = { symbol: c.symbol };
const els = {
  "cn-drawer": { style: { display: c.drawer } },
  "alerts-modal": { style: { display: c.modal } },
  "alert-symbol": { textContent: "OLD.SZ" },
};
globalThis.document = { getElementById: (id) => els[id] || null };
refreshSymbolPanels();
process.stdout.write(JSON.stringify({
  reloaded, lastTab, label: els["alert-symbol"].textContent }));
"""
        )

    def _run(self, drawer, modal="none", symbol="BBB.SZ"):
        proc = subprocess.run(["node", "-e", self.script,
                               json.dumps({"drawer": drawer, "modal": modal, "symbol": symbol})],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def test_open_drawer_reloads_current_tab(self):
        out = self._run("block")
        self.assertEqual(out["reloaded"], 1, "抽屉打开 → 重载一次")
        self.assertEqual(out["lastTab"], "unlock", "重载的是当前 tab")

    def test_closed_drawer_no_reload(self):
        out = self._run("none")
        self.assertEqual(out["reloaded"], 0, "抽屉关闭 → 不发起请求")

    def test_open_alerts_modal_updates_symbol_label(self):
        out = self._run("none", modal="block", symbol="000001.SZ")
        self.assertEqual(out["label"], "000001.SZ")

    def test_closed_alerts_modal_keeps_label(self):
        out = self._run("none", modal="none", symbol="000001.SZ")
        self.assertEqual(out["label"], "OLD.SZ", "弹窗未打开不写标签")


if __name__ == "__main__":
    unittest.main(verbosity=2)
