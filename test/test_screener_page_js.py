# -*- coding: utf-8 -*-
"""选股页 (screener.html): 时段门控 + 条件锁定/编辑/回显 + 命中数(OR)/明细 + 因子库状态条。

覆盖的真实问题:
  * 结果表现价由 LiveMarket 刷新, 但本页曾没有任何 active 门控 —— 选股页开着就
    全天每 2.5s 轮询 /api/quotes (盘外服务端只返回收盘快照);
  * pollStatus 的 catch 里曾置 scanning=false, 一次 status 抖动就永久冻住进度;
  * 任务化后的行为: 扫描中锁定条件、重新登录回显上次条件与结果、0 命中/已取消也
    要显示、历史最多 3 条、取消后解锁;
  * 第二批: OR 模式「满足 N 项」筛选 + 逐条件明细 + 因子库状态条。

运行:
    python -m unittest visual/test/test_screener_page_js.py -v
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
SCREENER_HTML = _VISUAL_DIR / "static" / "screener.html"
ADMIN_HTML = _VISUAL_DIR / "static" / "admin.html"

_FNS = (
    "specOf", "metricMenuLabel", "condGuide", "condLabel", "fmtTime", "statusText",
    "readInputs", "saveCond",
    "editCond", "cancelEdit", "delCond", "renderConds", "setLocked", "syncInputs",
    "fillComposerFrom", "loadRunConditions", "fillHistory", "renderFilterRow",
    "setHitFilter", "renderResults", "renderRun", "showRun", "applyStatus",
)
_FACTOR_FNS = (
    "scheduleFactorBar", "setFactorRebuildVisible", "refreshFactorBar",
    "rebuildFactorsNow",
)


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"screener.html 中找不到 function {name}")
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


_PRELUDE = """
const METRICS = {
  above_ma20: { key: "above_ma20", label: "站上 MA20", group: "技术指标", kind: "bool",
                unit: "", ops: ["is"], hint: "" },
  rsi6: { key: "rsi6", label: "RSI6", group: "技术指标", kind: "num", unit: "",
          op: "<=", ops: [">=", "<="], hint: "<20 超卖", domain: "0~100" },
  turnover: { key: "turnover", label: "换手率", group: "量价", kind: "range", unit: "%",
              ops: ["between"], hint: "" },
  industry: { key: "industry", label: "所处行业", group: "基本面/估值", kind: "text",
              unit: "", ops: ["=="], options: ["银行", "白酒"], hint: "" },
  main_net_inflow_wan: { key: "main_net_inflow_wan", label: "主力净流入",
                         group: "资金与筹码", kind: "num", unit: "万元", op: ">=",
                         ops: [">="], hint: "" },
};
let metricGroups = [];
let conditions = [];
let editingIndex = null;
let composed = false;
let busy = false;
let pollFailures = 0;
let runs = {};
let runCache = {};
let displayId = null;
let hitFilter = null;
let factorDay = null;
let isAdmin = false;
let factorBarTimer = null;
const LIVE_QUOTE_MAX = 200;
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, value: "", textContent: "", innerHTML: "",
                            disabled: false, style: {}, classList: { add(){}, remove(){} } };
  return els[id];
}
const document = { getElementById: el, hidden: false };
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
const errors = [];
function showErr(m) { errors.push(m); }
const liveQuotes = { setSymbols: () => {}, get: () => null, refresh: () => {} };
const VisualLive = { price: v => (v == null ? "—" : String(v)) };
const VisualMarketClock = { MarketClock: function () {
  this.start = () => {}; this.live = () => false; this.streamAllowed = () => false;
  this.applyMarketFrame = () => {}; this.setStreamConnected = () => {};
} };
const VisualMarketStore = { store: { accept: () => {} }, watch: () => {} };
const marketClock = new VisualMarketClock.MarketClock();
const apiCalls = [];
const apiPosts = [];
function api(path, options) {
  apiCalls.push(path);
  apiPosts.push({ path, options: options || {} });
  if (typeof globalThis.__apiHandler === "function") {
    return Promise.resolve(globalThis.__apiHandler(path, options || {}));
  }
  return Promise.resolve(globalThis.__apiResult || {});
}
// 现价订阅要记下来: 结果可达数千行, 全量订阅会打出几十个 /api/quotes 批次
const quoteSyms = [];
liveQuotes.setSymbols = syms => { quoteSyms.push(syms); };
const confirms = [];
globalThis.confirm = (msg) => { confirms.push(msg); return globalThis.__confirm !== false; };
const timers = [];
globalThis.clearTimeout = (id) => {};
globalThis.setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
"""


def _run_page_script(extra: str) -> dict:
    src = SCREENER_HTML.read_text(encoding="utf-8")
    fns = "\n".join(_extract_fn(src, name) for name in _FNS)
    proc = subprocess.run(["node", "-e", _PRELUDE + fns + "\n" + extra],
                          capture_output=True, check=True, text=True, encoding="utf-8")
    return json.loads(proc.stdout)


class ScreenerPageStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SCREENER_HTML.read_text(encoding="utf-8")

    def test_quotes_are_session_gated(self):
        self.assertIn("/js/market-clock.js", self.src, "要用共享时段口径")
        self.assertIn("new VisualMarketClock.MarketClock(", self.src)
        self.assertIn("marketClock.start()", self.src)
        self.assertRegex(self.src, r"active: \(\) =>[^\n]*canAutoRefresh\(\)")
        self.assertRegex(self.src, r"streamActive: \(\) =>[^\n]*canStreamQuotes\(\)")
        for name in ("canAutoRefresh", "canStreamQuotes"):
            body = _extract_fn(self.src, name)
            self.assertIn("document.hidden", body, "隐藏标签页必须停机")
            self.assertIn("marketClock.", body, "时段口径只来自时钟")

    def test_poll_state_declared_before_use(self):
        decl = self.src.index("let pollFailures = 0;")
        for caller in ("async function runScan(", "(async function initResume()"):
            self.assertLess(decl, self.src.index(caller),
                            f"pollFailures 必须声明在 {caller} 之前")
        self.assertEqual(self.src.count("let pollFailures"), 1)

    def test_poll_status_keeps_task_state_on_error(self):
        body = _extract_fn(self.src, "pollStatus")
        catch = body[body.index("} catch"):]
        self.assertNotIn("busy = false", catch, "失败不能解锁条件")
        self.assertIn("setTimeout(pollStatus", catch, "要退避重试")

    def test_metrics_come_from_backend_catalog(self):
        """指标下拉必须由 /api/screener/metrics 渲染, 不能在前端硬编码。"""
        self.assertIn('/api/screener/metrics', self.src)
        self.assertIn("optgroup", self.src)
        self.assertIn("metricMenuLabel", self.src)
        self.assertIn('id="cond-unit"', self.src)
        self.assertIn('id="cond-hint"', self.src)
        self.assertNotIn("METRIC_LABELS", self.src, "旧硬编码标签表应已移除")
        body = _extract_fn(self.src, "syncInputs")
        for kind in ("bool", "num", "range", "text"):
            self.assertIn(kind, body, f"输入控件要区分 {kind}")

    def test_factor_status_bar(self):
        self.assertIn('/api/factors/status', self.src)
        self.assertIn('id="factor-bar"', self.src)
        self.assertIn('id="factor-rebuild"', self.src)
        body = _extract_fn(self.src, "refreshFactorBar")
        self.assertIn("snapshot_day", body)
        self.assertIn("构建中", body)
        self.assertIn("setFactorRebuildVisible", body)
        show = _extract_fn(self.src, "setFactorRebuildVisible")
        self.assertIn('"inline-block"', show,
                      "显示时不能写空字符串, 否则回落到 CSS 的 display:none")
        rebuild = _extract_fn(self.src, "rebuildFactorsNow")
        self.assertIn('"/api/factors/rebuild"', rebuild)
        self.assertIn("force: true", rebuild)
        self.assertIn("isAdmin", rebuild)

    def test_no_legacy_global_job_fields(self):
        self.assertNotIn("s.running", self.src)
        self.assertNotIn("s.results", self.src)
        self.assertIn("s.active", self.src)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class ScreenerComposerTest(unittest.TestCase):
    """条件框: 锁定/编辑/回显 + 四种 kind 的取值控件。"""

    def test_lock_disables_composer_and_unlocks(self):
        out = _run_page_script("""
const r = {};
conditions = [{ metric: "above_ma20", label: "站上 MA20", kind: "bool" }];
setLocked(true, "扫描中，取消后可修改条件");
r.locked = { add: el("btn-add").disabled, metric: el("cond-metric").disabled,
             mode: el("cond-mode").disabled, price: el("cond-price").disabled,
             run: el("btn-run").disabled, stop: el("btn-stop").style.display,
             hint: el("lock-hint").textContent, chip: el("cond-list").innerHTML };
setLocked(false);
r.unlocked = { add: el("btn-add").disabled, stop: el("btn-stop").style.display,
               hint: el("lock-hint").style.display };
console.log(JSON.stringify(r));
""")
        for k in ("add", "metric", "mode", "price", "run"):
            self.assertTrue(out["locked"][k], f"{k} 扫描中应禁用")
        self.assertEqual(out["locked"]["stop"], "")
        self.assertIn("取消后可修改", out["locked"]["hint"])
        self.assertIn("chip locked", out["locked"]["chip"])
        self.assertFalse(out["unlocked"]["add"])
        self.assertEqual(out["unlocked"]["stop"], "none")

    def test_edit_replaces_condition_in_place(self):
        out = _run_page_script("""
const r = {};
conditions = [{ metric: "above_ma20", label: "站上 MA20", kind: "bool" },
              { metric: "rsi6", label: "RSI6", kind: "num", op: "<=", value: 30 }];
editCond(1);
r.btn = el("btn-add").textContent;
el("cond-value").value = "25";
saveCond();
r.conditions = conditions;
r.editing = editingIndex;
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["btn"], "保存修改")
        self.assertEqual(len(out["conditions"]), 2, "编辑是替换不是追加")
        self.assertEqual(out["conditions"][1]["value"], 25)
        self.assertEqual(out["conditions"][0]["metric"], "above_ma20")
        self.assertIsNone(out["editing"])

    def test_kind_switches_inputs(self):
        out = _run_page_script("""
const r = {};
const probe = key => {
  el("cond-metric").value = key;
  syncInputs();
  return { op: el("op-wrap").style.display, value: el("cond-value").style.display,
           range: el("range-wrap").style.display, text: el("cond-text").style.display,
           options: el("cond-options").innerHTML };
};
r.bool = probe("above_ma20");
r.num = probe("rsi6");
r.range = probe("turnover");
r.text = probe("industry");
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["bool"]["value"], "none")
        self.assertEqual(out["bool"]["op"], "none")
        self.assertEqual(out["num"]["value"], "")
        self.assertEqual(out["num"]["op"], "")
        self.assertEqual(out["range"]["range"], "")
        self.assertEqual(out["text"]["text"], "")
        self.assertIn("银行", out["text"]["options"])

    def test_unit_and_guide_follow_the_metric(self):
        """选指标后: 下拉带单位, 输入框后有单位, 下方说明给出怎么填。"""
        out = _run_page_script("""
const r = {};
r.menu = metricMenuLabel(METRICS.main_net_inflow_wan);
r.plain = metricMenuLabel(METRICS.rsi6);
const probe = key => {
  el("cond-metric").value = key;
  syncInputs();
  return { unit: el("cond-unit").textContent, unitOn: el("cond-unit").style.display,
           unit2: el("cond-unit2").style.display, ph: el("cond-value").placeholder,
           ph2: el("cond-value2").placeholder, hint: el("cond-hint").textContent };
};
r.wan = probe("main_net_inflow_wan");
r.rsi = probe("rsi6");
r.range = probe("turnover");
r.bool = probe("above_ma20");
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["menu"], "主力净流入（万元）")
        self.assertEqual(out["plain"], "RSI6")
        self.assertEqual(out["wan"]["unit"], "万元")
        self.assertEqual(out["wan"]["unitOn"], "")
        self.assertEqual(out["wan"]["ph"], "数值（万元）")
        self.assertIn("1 亿填 10000", out["wan"]["hint"])
        self.assertEqual(out["rsi"]["unitOn"], "none")
        self.assertEqual(out["rsi"]["hint"], "取值范围 0~100 · <20 超卖")
        self.assertEqual(out["range"]["unit2"], "")
        self.assertEqual(out["range"]["ph2"], "上限（%）")
        self.assertIn("不是 0.05", out["range"]["hint"])
        self.assertEqual(out["bool"]["unitOn"], "none")
        self.assertEqual(out["bool"]["hint"], "")

    def test_range_condition_label_and_validation(self):
        out = _run_page_script("""
const r = {};
el("cond-metric").value = "turnover";
syncInputs();
el("cond-value").value = "1";
el("cond-value2").value = "5";
saveCond();
r.conditions = conditions;
r.label = condLabel(conditions[0]);
r.errs = errors.slice();
// 缺上限 → 报错且不入列
el("cond-value2").value = "";
saveCond();
r.afterBad = conditions.length;
r.errs = errors.slice();
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["conditions"][0]["kind"], "range")
        self.assertEqual((out["conditions"][0]["value"], out["conditions"][0]["value2"]),
                         (1.0, 5.0))
        self.assertEqual(out["label"], "换手率 1 ~ 5%")
        self.assertEqual(out["afterBad"], 1)
        self.assertIn("请填写区间上限", out["errs"])

    def test_cond_label_variants(self):
        out = _run_page_script("""
const r = {};
r.bool = condLabel({ metric: "above_ma20", kind: "bool", label: "站上 MA20" });
r.num = condLabel({ metric: "rsi6", kind: "num", op: "<=", value: 20, label: "RSI6" });
r.unit = condLabel({ metric: "main_net_inflow_wan", kind: "num", op: ">=",
                     value: 5000, label: "主力净流入" });
r.text = condLabel({ metric: "industry", kind: "text", value: "银行", label: "所处行业" });
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["bool"], "站上 MA20")
        self.assertEqual(out["num"], "RSI6 ≤ 20")
        self.assertEqual(out["unit"], "主力净流入 ≥ 5000万元")
        self.assertEqual(out["text"], "所处行业 = 银行")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class ScreenerResultsTest(unittest.TestCase):
    """结果区: 回显 / 历史 / 0 命中 / 已取消 / OR 命中数筛选与明细。"""

    def _run(self, status, results, extra="", conditions=None):
        conds = conditions or [{"metric": "above_ma20", "label": "站上 MA20", "kind": "bool"},
                              {"metric": "rsi6", "label": "RSI6", "kind": "num",
                               "op": "<=", "value": 20}]
        return _run_page_script(f"""
const r = {{}};
applyStatus({{
  active: null,
  recent: [{{ id: 7, status: "{status}", mode: "or", n_results: {len(results)},
             conditions: {json.dumps(conds, ensure_ascii=False)}, total: 10, progress: 10,
             done_at: "2026-09-18T14:30:00", price_mode: "close",
             data_version: "2026-09-18" }}],
  current: {{ id: 7, status: "{status}", mode: "or", n_results: {len(results)},
             conditions: {json.dumps(conds, ensure_ascii=False)}, total: 10, progress: 10,
             done_at: "2026-09-18T14:30:00", price_mode: "close",
             data_version: "2026-09-18",
             results: {json.dumps(results, ensure_ascii=False)} }},
}});
{extra}
console.log(JSON.stringify(r));
""")

    @staticmethod
    def _row(symbol, hits, close=10.0, chg=1.0):
        return {"symbol": symbol, "name": symbol[:6], "close": close, "change_pct": chg,
                "chip_profit": 66.6, "hit_count": sum(1 for h in hits if h["ok"]), "hits": hits}

    def test_or_results_with_hits_and_detail(self):
        hits_full = [{"metric": "above_ma20", "label": "站上 MA20", "ok": True, "value": True, "unit": ""},
                     {"metric": "rsi6", "label": "RSI6", "ok": True, "value": 15.0, "unit": ""}]
        hits_one = [{"metric": "above_ma20", "label": "站上 MA20", "ok": False, "value": False, "unit": ""},
                    {"metric": "rsi6", "label": "RSI6", "ok": True, "value": 18.0, "unit": ""}]
        out = self._run("done", [self._row("600000.SH", hits_full),
                                 self._row("000001.SZ", hits_one)],
                        extra="r.rows = el('result-tbody').innerHTML;"
                              "r.filter = el('filter-row').innerHTML;"
                              "r.conditions = conditions;"
                              "r.meta = el('run-meta').innerHTML;")
        self.assertIn("2/2", out["rows"])
        self.assertIn("1/2", out["rows"])
        self.assertIn("✅站上 MA20", out["rows"])
        self.assertIn("❌站上 MA20", out["rows"])
        self.assertIn("RSI6 18", out["rows"])
        self.assertIn("满足 2 项 1", out["filter"])
        self.assertIn("全部 2", out["filter"])
        self.assertIn("满足任一", out["meta"])
        self.assertIn("数据 2026-09-18", out["meta"])
        self.assertEqual(out["conditions"][0]["metric"], "above_ma20", "回显条件到条件框")

    def test_hit_filter_narrows_rows(self):
        hits_full = [{"metric": "above_ma20", "label": "站上 MA20", "ok": True, "value": True, "unit": ""},
                     {"metric": "rsi6", "label": "RSI6", "ok": True, "value": 15.0, "unit": ""}]
        hits_one = [{"metric": "above_ma20", "label": "站上 MA20", "ok": False, "value": False, "unit": ""},
                    {"metric": "rsi6", "label": "RSI6", "ok": True, "value": 18.0, "unit": ""}]
        out = self._run("done", [self._row("600000.SH", hits_full),
                                 self._row("000001.SZ", hits_one)],
                        extra="setHitFilter(1); r.rows = el('result-tbody').innerHTML;"
                              "r.hit = hitFilter;")
        self.assertEqual(out["hit"], 1)
        self.assertIn("000001.SZ", out["rows"])
        self.assertNotIn("600000.SH", out["rows"])

    def test_empty_and_stopped_states(self):
        out = self._run("done", [], extra="r.rows = el('result-tbody').innerHTML;")
        self.assertIn("无命中", out["rows"])
        out2 = self._run("stopped", [], extra="r.rows = el('result-tbody').innerHTML;"
                                              "r.meta = el('run-meta').innerHTML;")
        self.assertIn("已取消，无命中", out2["rows"])
        self.assertIn("已取消，保留部分结果", out2["meta"])
        out3 = self._run("running", [], extra="r.rows = el('result-tbody').innerHTML;")
        self.assertIn("扫描中", out3["rows"])

    def test_queue_and_lock_from_active_run(self):
        out = _run_page_script("""
const r = {};
applyStatus({
  active: { id: 9, status: "queued", mode: "and", progress: 0, total: 0, position: 2,
            price_mode: "live",
            conditions: [{ metric: "above_ma20", label: "站上 MA20", kind: "bool" }] },
  recent: [], current: null,
});
r.locked = busy;
r.hint = el("lock-hint").textContent;
r.scanHint = el("scan-hint").textContent;
r.conditions = conditions;
r.mode = el("cond-mode").value;
r.price = el("cond-price").value;
console.log(JSON.stringify(r));
""")
        self.assertTrue(out["locked"])
        self.assertIn("前面还有 2 个任务", out["hint"])
        self.assertIn("排队中", out["scanHint"])
        self.assertEqual(out["conditions"][0]["metric"], "above_ma20")
        self.assertEqual(out["mode"], "and")
        self.assertEqual(out["price"], "live")

    def test_history_lists_active_plus_three_recent(self):
        out = _run_page_script("""
const r = {};
const mk = (id, status) => ({ id, status, mode: "and", n_results: id, progress: 1,
                              total: 1, conditions: [], queued_at: "2026-09-18T09:00:00" });
applyStatus({ active: mk(10, "running"),
              recent: [mk(9, "done"), mk(8, "stopped"), mk(7, "done")],
              current: { ...mk(10, "running"), results: [] } });
r.options = el("hist-select").innerHTML;
r.count = (el("hist-select").innerHTML.match(/<option/g) || []).length;
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["count"], 4, "进行中 1 条 + 历史最多 3 条")
        self.assertIn("进行中", out["options"])

    def test_history_switch_fetches_details_on_demand(self):
        out = _run_page_script("""
(async () => {
  const r = {};
  globalThis.__apiResult = { id: 5, status: "done", mode: "and", n_results: 1,
                             conditions: [], done_at: "2026-09-18T14:30:00",
                             results: [{ symbol: "600519.SH", name: "贵州茅台", close: 1.0,
                                         change_pct: 0.5, hit_count: 1, hits: [] }] };
  applyStatus({ active: null,
                recent: [{ id: 5, status: "done", mode: "and", n_results: 1, conditions: [],
                           results: null },
                         { id: 4, status: "stopped", mode: "and", n_results: 2,
                           conditions: [], results: null }],
                current: { id: 6, status: "done", mode: "and", n_results: 0, conditions: [],
                           results: [] } });
  el("hist-select").value = "5";
  await showRun(el("hist-select").value);
  r.paths = apiCalls;
  r.rows = el("result-tbody").innerHTML;
  r.histVal = el("hist-select").value;
  console.log(JSON.stringify(r));
})();
""")
        detail = [p for p in out["paths"] if str(p).startswith("/api/screener/runs/5")]
        self.assertEqual(detail, ["/api/screener/runs/5?limit=3000"], "详情只取一次, 不分页循环")
        self.assertIn("600519.SH", out["rows"])
        self.assertEqual(out["histVal"], "5")

    def test_quote_subscription_is_capped(self):
        """回归: 结果全量订阅现价 → 每 tick 几十个批次, 挤占监控/其它页面额度。"""
        out = _run_page_script("""
const r = {};
const results = Array.from({ length: 500 }, (_, i) => ({
  symbol: "60" + String(i).padStart(4, "0") + ".SH", name: "X", close: 1.0,
  change_pct: 0.0, chip_profit: 1.0, hit_count: 1, hits: [] }));
renderResults(results, { mode: "or", status: "done", conditions: [], results });
r.calls = quoteSyms.map(s => s.length);
r.rows = (el("result-tbody").innerHTML.match(/<tr>/g) || []).length;
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["rows"], 500, "表格仍显示全部结果")
        self.assertTrue(out["calls"], "应当订阅现价")
        self.assertLessEqual(max(out["calls"]), 200, "订阅数要封顶")
        self.assertEqual(out["calls"], [200])

    def test_header_updated_even_when_empty(self):
        """回归: 0 命中时 early-return 曾让表头残留上一个任务的「命中 (OR)」。"""
        out = _run_page_script("""
const r = {};
renderResults([{ symbol: "600000.SH", name: "X", close: 1, change_pct: 0, hit_count: 1,
                 hits: [] }], { mode: "or", status: "done", conditions: [], results: [] });
r.or = el("th-hit").textContent;
renderResults([], { mode: "and", status: "done", conditions: [] });
r.and = el("th-hit").textContent;
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["or"], "命中 (OR)")
        self.assertEqual(out["and"], "命中")

    def test_truncated_run_shows_hint(self):
        out = _run_page_script("""
const r = {};
renderRun({ id: 1, status: "done", mode: "or", n_results: 3000, truncated: true,
            conditions: [], results: [], data_version: "2026-09-18" });
r.meta = el("run-meta").innerHTML;
console.log(JSON.stringify(r));
""")
        self.assertIn("单次上限", out["meta"])

    def test_user_edits_win_over_restore(self):
        out = _run_page_script("""
const r = {};
conditions = [{ metric: "above_ma20", label: "站上 MA20", kind: "bool" }];
composed = true;
applyStatus({ active: null, recent: [],
              current: { id: 3, status: "done", mode: "and", results: [],
                         conditions: [{ metric: "rsi6", label: "RSI6", kind: "num",
                                        op: "<=", value: 30 }] } });
r.conditions = conditions;
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["conditions"][0]["metric"], "above_ma20",
                         "用户动过条件框后不得被回显覆盖")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class ScreenerPollStatusBehaviorTest(unittest.TestCase):
    """用 stub 跑真实 pollStatus, 断言重排节奏与失败后的自愈。"""

    @classmethod
    def setUpClass(cls):
        src = SCREENER_HTML.read_text(encoding="utf-8")
        cls.fn = _extract_fn(src, "pollStatus")

    def _run(self, responses, drain=0):
        body = self.fn.replace("applyStatus(s);", "globalThis.__applied.push(s);")
        script = (
            "const results = {};\n"
            "globalThis.__scheduled = [];\n"
            "globalThis.__queue = [];\n"
            "globalThis.__applied = [];\n"
            "globalThis.setTimeout = (fn, ms) => { globalThis.__scheduled.push(ms);"
            " globalThis.__queue.push(fn); return 1; };\n"
            "const seq = " + json.dumps(responses) + ";\n"
            "let i = 0;\n"
            "globalThis.api = async () => {\n"
            "  const r = seq[Math.min(i++, seq.length - 1)];\n"
            "  if (r === 'throw') throw new Error('status boom');\n"
            "  return r;\n"
            "};\n"
            "globalThis.pollFailures = 0;\n"
            + body + "\n"
            + "(async () => {\n"
            + "  await pollStatus();\n"
            + f"  for (let k = 0; k < {drain} && globalThis.__queue.length; k++) {{\n"
            + "    await globalThis.__queue.shift()();\n"
            + "  }\n"
            + "  process.stdout.write(JSON.stringify({ scheduled: globalThis.__scheduled,"
            + " failures: globalThis.pollFailures,"
            + " applied: globalThis.__applied.length }));\n"
            + "})();"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True,
                              check=True, text=True, encoding="utf-8")
        return json.loads(proc.stdout)

    def test_active_run_reschedules_fast(self):
        out = self._run([{"active": {"id": 1, "status": "running", "progress": 3, "total": 10},
                          "recent": [], "current": None}])
        self.assertEqual(out["scheduled"], [1500])
        self.assertEqual(out["applied"], 1)

    def test_finished_run_stops_polling(self):
        out = self._run([{"active": None, "recent": [], "current": None}])
        self.assertEqual(out["scheduled"], [], "任务结束不再轮询")
        self.assertEqual(out["applied"], 1)

    def test_error_backs_off_and_recovers(self):
        out = self._run(
            ["throw", "throw",
             {"active": {"id": 1, "status": "running"}, "recent": [], "current": None}],
            drain=2)
        self.assertEqual(out["scheduled"], [1500, 3000, 1500],
                         "失败退避 1.5s→3s, 恢复后回到 1.5s")
        self.assertEqual(out["failures"], 0, "成功即复位失败计数")
        self.assertEqual(out["applied"], 1)


class ScriptOrderTest(unittest.TestCase):
    """market-store.js 在加载时就要求 Vue 和 Pinia 都在。缺 Vue 会抛错, 后面的 initResume 不执行。"""

    def test_vue_and_demi_load_before_pinia(self):
        src = SCREENER_HTML.read_text(encoding="utf-8")
        vue = src.index("/vendor/vue-3.5.13.global.prod.js")
        demi = src.index("/vendor/vue-demi-0.14.10.iife.js")
        pinia = src.index("/vendor/pinia-2.2.6.iife.prod.js")
        store = src.index("/js/market-store.js")
        self.assertLess(vue, demi)
        self.assertLess(demi, pinia)
        self.assertLess(pinia, store)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class FactorRebuildTest(unittest.TestCase):
    """选股页因子库缺失/失败时, 管理员可点「立即构建」; 普通用户看不到按钮。"""

    def _run(self, extra: str) -> dict:
        src = SCREENER_HTML.read_text(encoding="utf-8")
        fns = "\n".join(_extract_fn(src, name) for name in _FACTOR_FNS)
        proc = subprocess.run(["node", "-e", _PRELUDE + fns + "\n" + extra],
                              capture_output=True, check=True, text=True, encoding="utf-8")
        return json.loads(proc.stdout)

    def test_admin_sees_button_when_missing(self):
        out = self._run("""
(async () => {
  isAdmin = true;
  globalThis.__apiResult = { state: "idle", snapshot_day: null };
  await refreshFactorBar();
  const r = {
    display: el("factor-rebuild").style.display,
    text: el("factor-text").textContent,
    delay: timers[timers.length - 1],
  };
  console.log(JSON.stringify(r));
})();
""")
        self.assertEqual(out["display"], "inline-block")
        self.assertIn("立即构建", out["text"])
        self.assertEqual(out["delay"], 60000)

    def test_user_hides_button_when_missing(self):
        out = self._run("""
(async () => {
  isAdmin = false;
  globalThis.__apiResult = { state: "idle", snapshot_day: null };
  await refreshFactorBar();
  console.log(JSON.stringify({
    display: el("factor-rebuild").style.display,
    text: el("factor-text").textContent,
  }));
})();
""")
        self.assertEqual(out["display"], "none")
        self.assertIn("管理员", out["text"])

    def test_running_hides_button_and_polls_fast(self):
        out = self._run("""
(async () => {
  isAdmin = true;
  globalThis.__apiResult = { state: "running", percent: 40, phase: "bars",
                             snapshot_day: null };
  await refreshFactorBar();
  console.log(JSON.stringify({
    display: el("factor-rebuild").style.display,
    text: el("factor-text").textContent,
    delay: timers[timers.length - 1],
  }));
})();
""")
        self.assertEqual(out["display"], "none")
        self.assertIn("构建中 40%", out["text"])
        self.assertEqual(out["delay"], 5000)

    def test_rebuild_posts_force_true(self):
        out = self._run("""
(async () => {
  isAdmin = true;
  let rebuilt = false;
  globalThis.__apiHandler = (path) => {
    if (path === "/api/factors/rebuild") return { ok: true, started: true };
    if (path === "/api/factors/status") {
      return rebuilt
        ? { state: "running", percent: 1, phase: "bars", snapshot_day: null }
        : { state: "idle", snapshot_day: null };
    }
    return {};
  };
  await refreshFactorBar();
  rebuilt = true;
  await rebuildFactorsNow();
  const post = apiPosts.find(c => c.path === "/api/factors/rebuild");
  console.log(JSON.stringify({
    confirmed: confirms.length === 1,
    method: post && post.options.method,
    body: post && post.options.body,
    display: el("factor-rebuild").style.display,
    text: el("factor-text").textContent,
  }));
})();
""")
        self.assertTrue(out["confirmed"])
        self.assertEqual(out["method"], "POST")
        self.assertEqual(json.loads(out["body"]), {"force": True})
        self.assertEqual(out["display"], "none", "触发后进入构建中, 按钮应隐藏")
        self.assertIn("构建中", out["text"])


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class AdminCondTextTest(unittest.TestCase):
    """管理页选股队列的 condText 要和选股页 condLabel 同一套写法: OR 用「或」,
    区间带上下限, 文本带等号, 数值带运算符。旧版只显示 value、一律用「且」。"""

    @classmethod
    def setUpClass(cls):
        src = ADMIN_HTML.read_text(encoding="utf-8")
        cls.script = (
            _extract_fn(src, "condText") + "\n"
            + "process.stdout.write(condText("
            + "JSON.parse(process.argv[1]), process.argv[2]));"
        )

    def _run(self, conditions, mode):
        proc = subprocess.run(
            ["node", "-e", self.script, json.dumps(conditions, ensure_ascii=False), mode],
            capture_output=True, check=True)
        return proc.stdout.decode("utf-8")

    def test_or_range_text_and_num(self):
        conds = [
            {"kind": "bool", "label": "站上 MA20", "metric": "above_ma20"},
            {"kind": "range", "label": "换手率", "metric": "turnover",
             "value": 1, "value2": 5},
            {"kind": "text", "label": "所处行业", "metric": "industry", "value": "银行"},
            {"kind": "num", "label": "RSI6", "metric": "rsi6", "op": "<=", "value": 20},
        ]
        self.assertEqual(
            self._run(conds, "or"),
            "站上 MA20 或 换手率 1~5 或 所处行业=银行 或 RSI6 ≤ 20")
        self.assertEqual(
            self._run(conds[:2], "and"),
            "站上 MA20 且 换手率 1~5")

    def test_empty_is_dash(self):
        self.assertEqual(self._run([], "and"), "—")
        self.assertEqual(self._run(None, "or"), "—")


if __name__ == "__main__":
    unittest.main(verbosity=2)
