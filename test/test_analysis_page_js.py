# -*- coding: utf-8 -*-
"""量化分析页 (analysis.html) 静态回归。

覆盖点:
  * 控制栏、图表容器、月度/逐笔表都在，且图表只引用自托管 ECharts；
  * 主题在阻塞的 vendor 脚本之前上色；
  * 主页顶栏与交易页有入口，login.html 回跳白名单含本页（量化管理页不能被挤掉）；
  * parseCapital / presetRange / fmtMoney / fmtPct 的边界。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
if str(_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_DIR))

from js_test_util import run_node  # noqa: E402

_VISUAL_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = _VISUAL_DIR / "static"
ANALYSIS_HTML = STATIC_DIR / "analysis.html"


def _inline_scripts(src: str) -> list[str]:
    """取出没有 src 的 script 正文。

    按 `<script` / `</script` 的字面边界截，认属性、大小写和 `</script ...>`。
    不用正则切标签。
    """
    blocks = []
    lower = src.lower()
    pos = 0
    while True:
        start = lower.find("<script", pos)
        if start < 0:
            break
        tag_end = src.find(">", start)
        if tag_end < 0:
            break
        opener = lower[start:tag_end]
        close = lower.find("</script", tag_end + 1)
        if close < 0:
            break
        if "src" not in opener:
            blocks.append(src[tag_end + 1:close])
        pos = close + len("</script")
    return blocks


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"analysis.html 中找不到 function {name}")
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


class AnalysisPageStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = ANALYSIS_HTML.read_text(encoding="utf-8")
        cls.index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.trades = (STATIC_DIR / "trades.html").read_text(encoding="utf-8")
        cls.login = (STATIC_DIR / "login.html").read_text(encoding="utf-8")

    def test_inline_scripts_parse(self):
        blocks = _inline_scripts(self.src)
        self.assertGreaterEqual(len(blocks), 2)
        for block in blocks:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
                fh.write(block)
                path = fh.name
            try:
                proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
            finally:
                Path(path).unlink(missing_ok=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_controls_charts_and_tables(self):
        for token in (
            'id="model-select"', 'id="capital-input"', 'id="date-from"', 'id="date-to"',
            'id="mode-realized"', 'id="mode-mtm"', 'id="deduct-fees"',
            'id="nav-chart"', 'id="dd-chart"', 'id="month-chart"', 'id="hist-chart"',
            'id="reason-chart"', 'id="scatter-chart"', 'id="rolling-chart"',
            'id="symbol-chart"', 'id="compare-chart"',
            'id="month-tbody"', 'id="compare-tbody"', 'id="trade-tbody"',
            'id="warning-banner"', 'id="note-banner"', 'id="load-state"', 'id="stat-cards"',
            "setCapital(50000)", "setCapital(100000)", "setCapital(200000)", "setCapital(500000)",
            "setRange('all')", "setRange('ytd')", "setRange('1m')", "setRange('3m')",
            "setRange('6m')", "setRange('1y')", "toggleLegs",
        ):
            self.assertIn(token, self.src, token)
        self.assertIn('value="100000"', self.src)
        self.assertIn('id="mode-realized" class="active"', self.src)
        self.assertIn('id="deduct-fees" type="checkbox" checked', self.src)
        self.assertIn("params.set('compare', '1')", self.src)
        self.assertIn("visual-analysis-capital", self.src)

    def test_no_external_cdn_and_theme_paints_first(self):
        self.assertIn('src="/vendor/echarts-5.5.0.min.js"', self.src)
        self.assertNotRegex(self.src, r'<script[^>]+src="https?://')
        self.assertNotIn("cdn.jsdelivr.net", self.src)
        self.assertNotIn('src="http', self.src)
        theme = self.src.index('href="/css/theme.css"')
        theme_js = self.src.index('src="/js/theme.js"')
        paint = self.src.index("document.documentElement.setAttribute('data-theme'")
        body = self.src.index("<body>")
        vendor = self.src.index("/vendor/echarts-5.5.0.min.js")
        self.assertLess(theme, theme_js)
        self.assertLess(theme_js, paint)
        self.assertLess(body, vendor, "ECharts 不能堵在首帧主题上色之前")
        self.assertIn("VisualTheme.init({ buttonId: 'btn-theme' })", self.src)
        toggle = _extract_fn(self.src, "toggleTheme")
        self.assertIn("VisualTheme.toggle", toggle)
        self.assertIn("renderCharts()", toggle)

    def test_all_users_not_admin_only(self):
        auth = _extract_fn(self.src, "checkAuth")
        self.assertIn("/api/auth/me", auth)
        self.assertIn("loadAnalysis()", auth)
        self.assertNotIn("is_admin", auth, "分析页对所有登录用户开放")
        self.assertNotIn("/trades.html", auth)
        menu = _extract_fn(self.src, "toggleUserMenu")
        self.assertIn("is_admin", menu, "用户管理入口仍只给管理员")
        self.assertIn('href="/admin.html#users"', self.src)
        self.assertIn('href="/quant.html"', self.src)

    def test_home_and_trades_entries_and_login_allowlist(self):
        i = self.index.index('id="analysis-entry"')
        open_tag = self.index[i:self.index.index(">", i)]
        body = self.index[self.index.index(">", i) + 1:self.index.index("</button>", i)]
        self.assertIn('class="nav-btn"', open_tag)
        self.assertIn('aria-label="量化分析"', open_tag)
        self.assertIn('title="量化分析: 账户模型净值曲线与绩效指标"', open_tag)
        self.assertIn("location.href='/analysis.html'", open_tag)
        self.assertIn('<span class="nav-ico" aria-hidden="true">📈</span>', body)
        self.assertIn('<span class="nav-txt">分析</span>', body)
        self.assertIn(
            '<button onclick="location.href=\'/analysis.html\'" title="量化分析: 净值曲线与绩效">📈 量化分析</button>',
            self.trades,
        )
        self.assertIn("if (next === '/analysis.html') return '/analysis.html';", self.login)
        self.assertIn("if (next === '/quant.html') return '/quant.html';", self.login)


class AnalysisPageHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = ANALYSIS_HTML.read_text(encoding="utf-8")
        cls.fns = "\n".join(_extract_fn(src, name) for name in (
            "parseCapital", "presetRange", "fmtMoney", "fmtPct", "symbolSlice",
        ))

    def _run(self, body: str):
        raw = run_node(self.fns + "\n" + body)
        return json.loads(raw)

    def test_parse_capital_clamps(self):
        out = self._run("""
        console.log(JSON.stringify({
          empty: parseCapital(''),
          text: parseCapital('abc'),
          low: parseCapital('10'),
          round: parseCapital('50000.6'),
          high: parseCapital('2000000000'),
          ok: parseCapital('100000')
        }));
        """)
        self.assertEqual(out, {
            "empty": 100000, "text": 100000, "low": 1000,
            "round": 50001, "high": 1000000000, "ok": 100000,
        })

    def test_preset_range_uses_calendar_days(self):
        today = date(2026, 6, 15)
        out = self._run("""
        console.log(JSON.stringify({
          all: presetRange('all', '2026-06-15'),
          ytd: presetRange('ytd', '2026-06-15'),
          m1: presetRange('1m', '2026-06-15'),
          m3: presetRange('3m', '2026-06-15'),
          m6: presetRange('6m', '2026-06-15'),
          y1: presetRange('1y', '2026-06-15'),
          bad: presetRange('nope', '2026-06-15')
        }));
        """)
        self.assertEqual(out["all"], {"from": "", "to": ""})
        self.assertEqual(out["ytd"], {"from": "2026-01-01", "to": ""})
        self.assertEqual(out["bad"], {"from": "", "to": ""})
        for key, days in (("m1", 30), ("m3", 91), ("m6", 182), ("y1", 365)):
            self.assertEqual(out[key]["to"], "")
            self.assertEqual(out[key]["from"], (today - timedelta(days=days)).isoformat())

    def test_formatters(self):
        out = self._run("""
        console.log(JSON.stringify({
          nil: fmtMoney(null, true),
          zero: fmtMoney(0, true),
          neg: fmtMoney(-12.5, true),
          pos: fmtMoney(1234.5, true),
          pct: fmtPct(1.2),
          pctNeg: fmtPct(-0.5),
          pctZero: fmtPct(0),
          pctNil: fmtPct(null)
        }));
        """)
        self.assertEqual(out["nil"], "—")
        self.assertEqual(out["zero"], "0.00")
        self.assertTrue(out["neg"].startswith("-"))
        self.assertIn("12.50", out["neg"])
        self.assertTrue(out["pos"].startswith("+"))
        self.assertIn("234.50", out["pos"])
        self.assertEqual(out["pct"], "+1.20%")
        self.assertEqual(out["pctNeg"], "-0.50%")
        self.assertEqual(out["pctZero"], "0.00%")
        self.assertEqual(out["pctNil"], "—")

    def test_symbol_slice_keeps_extremes_and_dedupes(self):
        out = self._run("""
        const small = symbolSlice([{symbol:'a', pnl:2},{symbol:'b', pnl:-1}]);
        const rows = [];
        for (let i = 0; i < 10; i++) {
          rows.push({symbol: 'A' + i, pnl: 100 - i});
          rows.push({symbol: 'A' + i, pnl: -i});
        }
        const big = symbolSlice(rows);
        console.log(JSON.stringify({
          small: small.map(r => r.symbol),
          big: big.map(r => [r.symbol, r.pnl])
        }));
        """)
        self.assertEqual(out["small"], ["b", "a"])
        self.assertEqual(len(out["big"]), 10)
        self.assertEqual(out["big"][0], ["A9", -9])
        self.assertEqual(out["big"][-1], ["A0", 100])
        self.assertEqual(len({row[0] for row in out["big"]}), 10)


if __name__ == "__main__":
    unittest.main()
