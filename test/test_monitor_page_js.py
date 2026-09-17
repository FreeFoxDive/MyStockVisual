# -*- coding: utf-8 -*-
"""监控页 (monitor.html) 前端测试: 告警标签覆盖 + 价格监控区块结构 + condText 行为。

关键回归点:
  * ALERT_LABELS 必须覆盖 monitor.py 会写入 monitor_alerts 的全部 alert_type,
    否则「最近告警」表/持仓「最近触发」列直接显示英文原文
    (trendline_break / price_alert / hold_exit_am / hold_exit_pm 曾漏标);
  * 监控页必须有价格监控区块 (趋势线跌破列表 + 条件预警列表) 及其渲染与管理函数;
  * condText 把规则数组渲染为「现价 ≥ 10.5 且 涨跌幅% ≤ -3」。

运行:
    venv/Scripts/python.exe -u visual/test/test_monitor_page_js.py
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
MONITOR_HTML = _VISUAL_DIR / "static" / "monitor.html"
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
MONITOR_PY = _VISUAL_DIR / "monitor.py"

# monitor.py 写入 monitor_alerts.alert_type 的全部取值:
# DAILY_ONCE ∪ ACCEL_TYPES ∪ {price_alert, trendline_break}。
# 到期提醒的 alert_type 是时段槽 hold_exit_am/pm, 而非 hold_expire (后者从未落库)。
EXPECTED_ALERT_TYPES = {
    "sl_breached", "tp_reached", "breakeven_hit", "be_broken", "limit_up_sealed",
    "hold_exit_am", "hold_exit_pm", "accel_down", "accel_up",
    "trendline_break", "price_alert",
}
# 历史遗留标签 (从未落库的旧类型), 允许保留以示兼容
LEGACY_LABEL_KEYS = {"hold_expire"}

# 监控页价格监控区块的两个表格体与渲染/操作函数
PRICE_SECTION_TOKENS = ('id="price-section"', 'id="tl-tbody"', 'id="pa-tbody"')
PRICE_SECTION_FNS = ("condText", "renderTrendlineMonitors", "renderPriceAlerts",
                     "stopTrendlineMonitor", "togglePriceAlert", "deletePriceAlert")


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"monitor.html 中找不到 function {name}")
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
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\{.*?\}\s*;", src, re.S)
    if not m:
        raise AssertionError(f"monitor.html 中找不到 const {name}")
    return m.group(0)


def _object_keys(body: str) -> set:
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", body))


class MonitorPageStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = MONITOR_HTML.read_text(encoding="utf-8")

    def test_title_renamed_to_monitor_center(self):
        self.assertIn("<title>🛡 监控中心 — Visual</title>", self.src)
        self.assertIn('<span class="title">🛡 监控中心</span>', self.src)

    def test_index_entry_button_renamed(self):
        idx = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('id="monitor-entry"', idx, "index.html 应保留 monitor-entry 入口")
        # 顶栏按钮文案收短为「监控」; 全称留在 title 里 (悬浮可见)
        i = idx.index('id="monitor-entry"')
        open_tag = idx[i:idx.index(">", i)]
        body = idx[idx.index(">", i) + 1:idx.index("</button>", i)]
        self.assertIn('<span class="nav-txt">监控</span>', body, "文字层收短为「监控」")
        self.assertNotIn("监控中心", body, "顶栏文案应收短 (全称只在 title/aria-label)")
        self.assertIn("监控中心", open_tag, "monitor-entry 的 title 应保留全称")
        # 图标/文字两层: 平板/手机只显示图标层 (见 test_home_toolbar_js 的平板口径),
        # 文字层一藏按钮就没有可读文字了 —— 无障碍名必须写在标签上
        self.assertIn('<span class="nav-ico"', body)
        self.assertIn('aria-label="监控中心"', open_tag)

    def test_alert_labels_cover_backend_types(self):
        keys = _object_keys(_extract_const(self.src, "ALERT_LABELS"))
        missing = EXPECTED_ALERT_TYPES - keys
        self.assertFalse(missing, f"ALERT_LABELS 缺少后端会落库的告警类型: {sorted(missing)}")
        self.assertLessEqual(keys - EXPECTED_ALERT_TYPES, LEGACY_LABEL_KEYS,
                             "ALERT_LABELS 出现未知类型 (拼写错误?)")

    def test_expected_types_actually_exist_in_monitor_py(self):
        # 防止标签表与后端实现漂移: 每个预期类型都必须出现在 monitor.py 源码中
        py = MONITOR_PY.read_text(encoding="utf-8")
        for t in EXPECTED_ALERT_TYPES:
            self.assertIn(f'"{t}"', py, f"monitor.py 中找不到告警类型 {t}")

    def test_price_monitoring_sections_present(self):
        for token in PRICE_SECTION_TOKENS:
            self.assertIn(token, self.src, token)
        # 两张表的表头文案
        for head in ("跌破幅度", "复权", "条件 (AND)", "状态"):
            self.assertIn(head, self.src, head)
        for fn in PRICE_SECTION_FNS:
            self.assertIn(f"function {fn}", self.src, fn)

    def test_stat_cards_include_price_monitoring(self):
        for token in ('id="st-tl"', 'id="st-pa"', "趋势线监控", "条件预警"):
            self.assertIn(token, self.src, token)
        render = _extract_fn(self.src, "render")
        self.assertIn("st-tl", render)
        self.assertIn("st-pa", render)

    def test_render_wires_both_lists(self):
        render = _extract_fn(self.src, "render")
        self.assertIn("trendline_monitors", render)
        self.assertIn("price_alerts", render)
        # 价格监控区块独立于持仓区块, 空持仓时也应渲染
        self.assertNotIn("price-section", render, "价格监控不应被持仓空态逻辑隐藏")

    def test_trendline_disable_calls_put_endpoint(self):
        body = _extract_fn(self.src, "stopTrendlineMonitor")
        self.assertIn("/api/trendline-monitors/", body)
        self.assertIn("enabled: false", body)

    def test_price_alert_management_endpoints(self):
        toggle = _extract_fn(self.src, "togglePriceAlert")
        self.assertIn('"/api/alerts/"', toggle)
        self.assertIn('"PUT"', toggle)
        delete = _extract_fn(self.src, "deletePriceAlert")
        self.assertIn('"/api/alerts/"', delete)
        self.assertIn('"DELETE"', delete)

    def test_escape_html_used_for_server_text(self):
        # 监控名称/备注/详情等来自用户输入, 必须转义
        tl = _extract_fn(self.src, "renderTrendlineMonitors")
        pa = _extract_fn(self.src, "renderPriceAlerts")
        for body in (tl, pa):
            self.assertIn("escHtml(", body)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class TrendlineLinkBehaviorTest(unittest.TestCase):
    """趋势线监控的图表链接必须带 period: 画线与监控配置按 代码+周期 分库。"""

    @classmethod
    def setUpClass(cls):
        src = MONITOR_HTML.read_text(encoding="utf-8")
        cls.script = "\n".join([
            _extract_const(src, "TL_TYPE_NAMES"),
            _extract_const(src, "ADJUST_LABELS"),
            _extract_fn(src, "escHtml"),
            _extract_fn(src, "escAttr"),
            _extract_fn(src, "fmtTs"),
            _extract_fn(src, "renderTrendlineMonitors"),
            "const boxes = {};",
            "globalThis.document = {getElementById: (id) => "
            "(boxes[id] = boxes[id] || {innerHTML: '', textContent: ''})};",
            "renderTrendlineMonitors(JSON.parse(process.argv[1]));",
            "process.stdout.write(boxes['tl-tbody'].innerHTML);",
        ])

    @staticmethod
    def _mon(**over):
        m = {"drawing_id": "d1", "symbol": "000001.SH", "period": "1w",
             "name": "周线支撑", "line_type": "trend", "pct": 2.0,
             "adjust": "forward", "last_fired_at": None}
        m.update(over)
        return m

    def _render(self, monitors):
        proc = subprocess.run(["node", "-e", self.script, json.dumps(monitors)],
                              capture_output=True, check=True)
        return proc.stdout.decode("utf-8")

    def test_weekly_monitor_links_to_weekly_chart(self):
        html = self._render([self._mon()])
        self.assertIn("/?symbol=000001.SH&period=1w", html)
        self.assertEqual(html.count("&period=1w"), 2, "标的与去图表都应带周期")
        self.assertNotIn('href="/?symbol=000001.SH"', html)

    def test_monthly_monitor_links_to_monthly_chart(self):
        html = self._render([self._mon(period="1M")])
        self.assertIn("/?symbol=000001.SH&period=1M", html)

    def test_daily_monitor_links_to_daily_chart(self):
        html = self._render([self._mon(period="1d")])
        self.assertIn("/?symbol=000001.SH&period=1d", html)

    def test_missing_period_falls_back_to_daily(self):
        html = self._render([self._mon(period=None)])
        self.assertIn("/?symbol=000001.SH&period=1d", html)

    def test_symbol_is_url_encoded(self):
        html = self._render([self._mon(symbol="00700.HK")])
        self.assertIn("/?symbol=00700.HK&period=1w", html)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class CondTextBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = MONITOR_HTML.read_text(encoding="utf-8")
        cls.script = (
            (MONITOR_HTML.parent / "js/live-market.js").read_text(encoding="utf-8") + "\n"
            + _extract_fn(src, "fmt") + "\n"
            + _extract_const(src, "METRIC_LABELS") + "\n"
            + _extract_fn(src, "condText") + "\n"
            + "process.stdout.write(condText(JSON.parse(process.argv[1])));"
        )

    def _run(self, rule):
        proc = subprocess.run(["node", "-e", self.script, json.dumps(rule)],
                              capture_output=True, check=True)
        return proc.stdout.decode("utf-8")

    def test_and_combination(self):
        rule = [{"metric": "price", "op": ">=", "value": 10.5},
                {"metric": "change_pct", "op": "<=", "value": -3}]
        self.assertEqual(self._run(rule), "现价 ≥ 10.5 且 涨跌幅% ≤ -3")

    def test_single_condition(self):
        self.assertEqual(self._run([{"metric": "price", "op": "<=", "value": 9}]),
                         "现价 ≤ 9")

    def test_unknown_metric_falls_back_to_raw(self):
        self.assertEqual(self._run([{"metric": "pe", "op": ">=", "value": 1}]), "pe ≥ 1")

    def test_empty_rule(self):
        self.assertEqual(self._run([]), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
