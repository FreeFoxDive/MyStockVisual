# -*- coding: utf-8 -*-
"""市场数据抽屉宽度自适应测试 (fitCnDrawer) + 「股东变化」折线图/明细表测试。

需求:
  * 抽屉不再因内容宽而必须横向滑动 —— 默认更宽, 并按**表格内容宽**自适应 (上限 92vw);
  * 「股东变化」: 近三年折线图 (横轴刻度 = 每期公布的截至日期) 在上, 常显的全部记录表在下。
从 index.html 抽取**真实源码**在 Node 里注入假 DOM 执行 (抽不到即失败)。

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


def _const_int(src: str, name: str) -> int:
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*(\d+)\s*;", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name}")
    return int(m.group(1))


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class CnDrawerFitTest(unittest.TestCase):
    """fitCnDrawer 的宽度公式 + cnTableContentWidth 的测量契约。

    宽度输入是**表格内容宽**(列宽之和), 不是 #cn-body table 的实时 scrollWidth ——
    后者在 width:100% 下等于抽屉当前内容宽, 代回公式会自我引用 (每次调用 +2px 涨到 92vw)。
    真实布局的量法无法在假 DOM 里复现, 这里钉住「临时切 max-content 量、量完还原」的契约,
    另在浏览器实测过修好后的宽度在连续 resize 下稳定。
    """

    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        cls.chart_min_w = _const_int(src, "CN_CHART_MIN_W")
        cls.script = (_extract_fn(src, "fitCnDrawer") + "\n"
                      + _extract_fn(src, "cnTableContentWidth") + "\n"
                      + "const c = JSON.parse(process.argv[1]);"
                      # 宽度下限取自真源码: 断言值跟着实现走, 不在测试里写死
                      + f"const CN_CHART_MIN_W = {cls.chart_min_w};"
                      + "const seen = [];"
                      + "let _cnChart = c.chartInstance ? { resize: () => seen.push('resize') } : null;"
                      # 假表格: scrollWidth 用 getter 记下「读数时的内联 width」, 以验证量法契约
                      + "const table = c.contentW == null ? null"
                      + "  : { style: { width: 'initial' },"
                      + "      get scrollWidth() { seen.push('measure@' + this.style.width); return c.contentW; } };"
                      + "const drawer = { style: { width: 'initial', display: c.drawerDisplay } };"
                      + "globalThis.document = {getElementById: () => drawer,"
                      + " querySelector: (sel) => (sel.indexOf('.cn-chart') >= 0 ? c.chart : table)};"
                      + "globalThis.window = {innerWidth: c.innerWidth};"
                      + "fitCnDrawer();"
                      + "process.stdout.write(JSON.stringify({width: drawer.style.width, seen,"
                      + " restored: table ? table.style.width : null}));")

    def _fit(self, content_w, inner_width, has_chart=False, chart_instance=False,
             drawer_display="block"):
        proc = subprocess.run(
            ["node", "-e", self.script,
             json.dumps({"contentW": content_w, "chart": {} if has_chart else None,
                         "chartInstance": chart_instance, "drawerDisplay": drawer_display,
                         "innerWidth": inner_width})],
            capture_output=True, check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    def _width(self, content_w, inner_width, has_chart=False):
        return self._fit(content_w, inner_width, has_chart=has_chart)["width"]

    def test_content_width_is_measured_under_max_content(self):
        # 契约: 量的时候表格是 max-content (否则量到抽屉内容宽), 量完还原
        out = self._fit(700, 1920)
        self.assertEqual(out["seen"], ["measure@max-content"],
                         "必须在 max-content 下读 scrollWidth, 否则量到的是抽屉当前宽度")
        self.assertEqual(out["restored"], "initial", "量完要把 width 还原, 不能污染表格样式")

    def test_fit_measures_content_not_live_table_width(self):
        # 防止回退: fitCnDrawer 自己不许直接读 table.scrollWidth (那就是自我引用)
        self.assertNotIn("table.scrollWidth", _extract_fn(INDEX_HTML.read_text(encoding="utf-8"),
                                                          "fitCnDrawer"))

    def test_widens_to_content(self):
        # 700 + 26 = 726, 1920 视口下上限 1766 → 726
        self.assertEqual(self._width(700, 1920), "726px")

    def test_min_width_floor(self):
        # 内容很窄也不小于 440
        self.assertEqual(self._width(100, 1920), "440px")

    def test_capped_by_viewport(self):
        # 视口 400 → 上限 round(400*0.92)=368, 不能超过
        self.assertEqual(self._width(700, 400), "368px")

    def test_no_table_no_chart_resets_to_css(self):
        # 无表格也无可视图表 (加载中/空/失败) → 清掉内联宽度, 回落 CSS clamp
        self.assertEqual(self._width(None, 1920), "")

    def test_drawer_not_hardcoded_400(self):
        src = INDEX_HTML.read_text(encoding="utf-8")
        start = src.index('id="cn-drawer"')
        style = src[start:start + 320]
        self.assertNotIn("width:400px", style.replace(" ", ""))
        self.assertIn("clamp(", style, "抽屉默认宽度应为 clamp(...) 自适应")

    def test_chart_floor_without_measurable_table(self):
        # 表格取不到时, 宽度仍由图表下限兜底 (12 个日期刻度不重叠)
        self.assertEqual(self._width(None, 1920, has_chart=True), f"{self.chart_min_w}px")

    def test_chart_floor_beats_narrow_content_on_small_viewport(self):
        # 窄视口 (1000px) 下 CSS clamp 只给 460, 含图表的 tab 抬到下限: 372+26=398 → 660
        self.assertEqual(self._width(372, 1000, has_chart=True), f"{self.chart_min_w}px")

    def test_chart_wider_table_still_wins(self):
        # 表格内容比图表下限更宽时按表格走 (900 + 26 = 926)
        self.assertEqual(self._width(900, 1920, has_chart=True), "926px")

    def test_chart_floor_capped_by_viewport(self):
        # 窄视口下图表下限同样受 92vw 封顶
        self.assertEqual(self._width(None, 400, has_chart=True), "368px")

    def test_resize_only_when_drawer_visible(self):
        out = self._fit(372, 1920, has_chart=True, chart_instance=True)
        self.assertEqual(out["seen"].count("resize"), 1, "抽屉打开时宽度变化要同步画布")
        hidden = self._fit(372, 1920, has_chart=True, chart_instance=True,
                           drawer_display="none")
        self.assertNotIn("resize", hidden["seen"],
                         "抽屉关着时容器宽 0, resize 会把画布清成 0×0: 必须跳过")

    def test_chart_floor_is_wide_enough_for_ticks(self):
        self.assertGreaterEqual(self.chart_min_w, 560,
                                "近三年约 12 个日期刻度需要足够宽度才不重叠")
        self.assertIn("CN_CHART_MIN_W", _extract_fn(INDEX_HTML.read_text(encoding="utf-8"),
                                                    "fitCnDrawer"))


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
            "let _cnTab = 'seed'; let _cnSeq = 0; let _cnChart = null;\n"
            + "let _cnHolderPoints = null;\n"
            + fn + "\n"
            + """
const mode = process.argv[1];
const tick = () => new Promise(r => setTimeout(r, 0));
const pending = [];
const bodyEl = { innerHTML: "" };
let fits = 0;
let disposed = 0;
globalThis.STATE = { symbol: "AAA.SZ" };
globalThis.escHtml = (s) => String(s);
globalThis.fitCnDrawer = () => { fits += 1; };
globalThis.api = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
globalThis.document = {
  getElementById: (id) => (id === "cn-body" ? bodyEl : null),
  querySelectorAll: () => [],
};
(async () => {
  if (mode === "drops-stale-chart") {
    // 换 tab 前手上还有一个图实例 + 上一只股票的点位: 两者都必须清掉
    _cnChart = { dispose: () => { disposed += 1; } };
    _cnHolderPoints = [{ date: "2000-01-01", count: 1, chg: "" }];
    showCnTab("holder-change");
    await tick();
  } else if (mode === "out-of-order") {
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
  process.stdout.write(JSON.stringify({ html: bodyEl.innerHTML, fits, tab: _cnTab,
    disposed, chart: _cnChart, points: _cnHolderPoints }));
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

    def test_switch_tab_disposes_chart_and_clears_points(self):
        # 容器马上被 innerHTML 丢掉: 实例必须 dispose, 点位不能留到新 tab/新股票
        out = self._run("drops-stale-chart")
        self.assertEqual(out["disposed"], 1, "切 tab 要销毁旧图实例")
        self.assertIsNone(out["chart"], "实例引用要置空, 否则下次会 dispose 已销毁的对象")
        self.assertIsNone(out["points"], "点位要与实例同生命周期, 不能残留上一只股票的数据")
        self.assertIn("加载中", out["html"], "销毁发生在重写内容之前 (随后进入加载态)")

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


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class HolderChangeTabTest(unittest.TestCase):
    """「股东变化」: 折线图限近三年 (刻度 = 公布的截至日期), 明细表常显且列全部记录。

    抽取 index.html 真源码, 假 echarts 记录 init/setOption/dispose; 造跨 5 年乱序数据。
    折线图窗口边界 (恰好在「今天-3年」) 必须保留、早一天的必须排除; 明细表不受该窗口限制。
    """

    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        fns = "\n".join(_extract_fn(src, n) for n in
                        ("cnHolderCutoff", "fmtHolders", "fmtHolderInt",
                         "renderHolderChart", "renderHolderChangeTab"))
        cls.script = (
            "let _cnChart = null; let _cnHolderPoints = null;\n"
            + f"const CN_HOLDER_YEARS = {_const_int(src, 'CN_HOLDER_YEARS')};"
            + f"const CN_CHART_MIN_W = {_const_int(src, 'CN_CHART_MIN_W')};"
            + fns + "\n"
            + """
globalThis.escHtml = (s) => String(s);
globalThis.C = () => ({ axisText: '#a', gridLine: '#b', tipBg: '#c', tipBorder: '#d',
                        tipText: '#e', holder: '#f' });
globalThis.window = globalThis;   // index.html 用 window.echarts 探测图表库
const calls = [];
let inst = 0;
globalThis.echarts = { init: () => {
  const id = ++inst;
  calls.push({ t: 'init', id });
  return { setOption: (o) => calls.push({ t: 'setOption', id, option: o }),
           resize: () => calls.push({ t: 'resize', id }),
           dispose: () => calls.push({ t: 'dispose', id }) };
} };
globalThis.document = { getElementById: (id) => (id === 'cn-holder-chart' ? { clientWidth: 620 } : null) };

const iso = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
                   + '-' + String(d.getDate()).padStart(2, '0');
const shift = (s, n) => { const d = new Date(s + 'T00:00:00'); d.setDate(d.getDate() + n); return iso(d); };
const cut = cnHolderCutoff();                       // 今天 - 3 年
const kept = [cut, shift(cut, 1), shift(cut, 30), shift(cut, 95)];   // 边界当天保留
const dropped = [shift(cut, -400), shift(cut, -1)];                  // 图窗口外
const old = shift(cut, -730);                                        // 更早, 表里仍要出现
const allAsc = [old, dropped[0], dropped[1], kept[0], kept[1], kept[2], kept[3]];
const count = {};                                   // 户数随日期递增: 排序错了必然对不上
allAsc.forEach((d, i) => { count[d] = 400000 + i * 1000; });
count[old] = null;                                  // 上游字段无法解析 → 表里 '—', 图上跳过
const rowOf = (d) => ({ '截止日期': d, '股东户数': count[d],
                        '增减': count[d] == null ? null : '增加1000' });
// 故意乱序: 上游顺序不作契约
const rows = [kept[2], old, dropped[0], kept[0], dropped[1], kept[3], kept[1]].map(rowOf);

const body = { innerHTML: '' };
renderHolderChangeTab({ rows, hint: '' }, body);
const first = { html: body.innerHTML, points: _cnHolderPoints.slice(),
                option: calls.find(c => c.t === 'setOption').option };
// 换股重入: 已有实例必须先销毁再重建 (仍给 >=2 期, 走建图分支)
renderHolderChangeTab({ rows: [kept[0], kept[1]].map(rowOf), hint: '' }, body);
process.stdout.write(JSON.stringify({
  html: first.html, dates: first.points.map(p => p.date), counts: first.points.map(p => p.count),
  option: first.option, calls, kept, keptCounts: kept.map(d => count[d]),
  allDates: allAsc, old,
  cutoff: [cnHolderCutoff(new Date(2029, 8, 19)), cnHolderCutoff(new Date(2028, 1, 29)),
           cnHolderCutoff(new Date(2024, 1, 29)), cnHolderCutoff(new Date(2028, 0, 1))],
  fmt: [fmtHolders(450712), fmtHolders(9800), fmtHolders(null)],
  fmtInt: [fmtHolderInt(450712), fmtHolderInt(null)],
}));
"""
        )

    @classmethod
    def _run(cls):
        proc = subprocess.run(["node", "-e", cls.script], capture_output=True)
        if proc.returncode != 0:
            raise AssertionError("node 执行失败:\n" + proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))

    def test_chart_plots_only_last_three_years(self):
        out = self._run()
        self.assertEqual(out["dates"], out["kept"],
                         "只画近三年 (含窗口边界当天), 窗口外的期数必须排除")
        self.assertEqual(out["counts"], out["keptCounts"],
                         "y 值按截止日期升序与横轴对齐")
        self.assertEqual(out["counts"], sorted(out["counts"]),
                         "乱序输入也必须升序成线")

    def test_axis_types_and_ticks(self):
        out = self._run()
        x = out["option"]["xAxis"]
        self.assertEqual(x["type"], "category", "横轴为公布的截至日期记录 (category)")
        self.assertEqual(x["data"], out["kept"])
        self.assertEqual(x["axisLabel"]["interval"], 0, "每个记录一个刻度, 不抽稀")
        self.assertEqual(out["option"]["yAxis"]["type"], "value", "纵轴为股东数")
        self.assertEqual(out["option"]["series"][0]["type"], "line")

    def test_table_lists_all_records_newest_first(self):
        out = self._run()
        self.assertNotIn("<details", out["html"], "明细表常显, 不做折叠")
        for col in ("截止日期", "股东户数", "增减"):
            self.assertIn(col, out["html"], col)
        self.assertEqual(out["html"].count("<tr><td>"), len(out["allDates"]),
                         "表里列出全部记录 (不限近三年)")
        body = out["html"].split("<tbody>")[1]
        order = [r.split("</td>")[0] for r in body.split("<tr><td>")[1:]]
        self.assertEqual(order, list(reversed(out["allDates"])), "表内最新在上")
        self.assertNotIn(out["old"], out["dates"], "三年前的期数不上图")
        self.assertIn(out["old"], out["html"], "三年前的期数仍要出现在表里")

    def test_table_marks_missing_numbers(self):
        out = self._run()
        row = out["html"].split(out["old"])[1].split("</tr>")[0]
        self.assertEqual(row.count("—"), 2, "户数/增减缺失时表里给 '—' 而不是 0")

    def test_caption_scopes_chart_and_table(self):
        out = self._run()
        self.assertIn(f"折线图（近三年 {len(out['kept'])} 期）", out["html"], "说明图只画近三年")
        self.assertIn(f"明细（全部 {len(out['allDates'])} 期）", out["html"], "说明表列全部记录")

    def test_reentry_disposes_previous_chart(self):
        out = self._run()
        seq = [c["t"] for c in out["calls"]]
        self.assertEqual(seq[:2], ["init", "setOption"], "首次渲染建图并 setOption")
        self.assertEqual(seq[2:], ["dispose", "init", "setOption"], "重入先销毁旧实例再重建")

    def test_helpers_render_human_readable_numbers(self):
        out = self._run()
        self.assertEqual(out["fmt"], ["45.07万", "9800", "—"], "y 轴刻度用万, 避免五位数挤在一起")
        self.assertEqual(out["fmtInt"], ["450,712", "—"], "明细表户数带千分位")

    def test_cutoff_is_three_years_ago(self):
        out = self._run()
        self.assertEqual(out["cutoff"], ["2026-09-19", "2025-02-28", "2021-02-28", "2025-01-01"],
                         "闰日要退到当月最后一天, 不能归一成 3/1 (那会多排掉一天)")


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
