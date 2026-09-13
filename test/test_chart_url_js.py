# -*- coding: utf-8 -*-
"""图表 URL 的周期参数测试: chartUrl 纯函数 + 入口 ?period= 生效 + 返回键周期跟随。

背景: 趋势线监控配置按 代码+周期 分库 (chart_drawings), 监控中心的图表链接若只带
?symbol= 会落到日K, 看不到建在周K/月K上的被监控线 (例: 000001.SH 周K)。

运行:
    venv/Scripts/python.exe -u visual/test/test_chart_url_js.py
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


class ChartUrlStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_chart_url_helper_exists(self):
        fn = _extract_fn(self.src, "chartUrl")
        self.assertIn("period", fn)
        self.assertIn("encodeURIComponent", fn)

    def test_symbol_switching_carries_period(self):
        self.assertIn("chartUrl(", _extract_fn(self.src, "switchSymbol"))
        self.assertIn("chartUrl(", _extract_fn(self.src, "syncSymbolUrl"))

    def test_period_switch_syncs_url(self):
        self.assertIn("syncSymbolUrl(true)", _extract_fn(self.src, "switchPeriod"))

    def test_boot_reads_period_param_before_first_load(self):
        self.assertIn("const jumpPeriod = bootParams.get('period')", self.src)
        self.assertIn("ALL_PERIODS.includes(jumpPeriod)", self.src)
        boot = self.src.index("const jumpPeriod")
        first_load = self.src.index("const scheduleInit")
        self.assertLess(boot, first_load, "周期须在首帧加载前生效, 否则先拉日K再切")

    def test_popstate_follows_period(self):
        self.assertIn("params.get('period')", self.src)
        self.assertIn("ALL_PERIODS.includes(per)", self.src)
        self.assertIn("switchPeriod(nextPeriod)", self.src)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
class ChartUrlBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        cls.script = (
            _extract_fn(src, "chartUrl") + "\n"
            + "const p = JSON.parse(process.argv[1]);"
            + "process.stdout.write(chartUrl(p.symbol, p.period));"
        )

    def _run(self, symbol, period):
        proc = subprocess.run(["node", "-e", self.script,
                               json.dumps({"symbol": symbol, "period": period})],
                              capture_output=True, check=True)
        return proc.stdout.decode("utf-8")

    def test_daily_omits_period(self):
        self.assertEqual(self._run("000001.SH", "1d"), "/?symbol=000001.SH")

    def test_weekly_keeps_period(self):
        self.assertEqual(self._run("000001.SH", "1w"), "/?symbol=000001.SH&period=1w")

    def test_monthly_keeps_period(self):
        self.assertEqual(self._run("000001.SH", "1M"), "/?symbol=000001.SH&period=1M")

    def test_minute_period_kept(self):
        self.assertEqual(self._run("600519.SH", "30m"), "/?symbol=600519.SH&period=30m")

    def test_missing_period_yields_bare_url(self):
        self.assertEqual(self._run("600519.SH", ""), "/?symbol=600519.SH")

    def test_hk_symbol_encoded(self):
        self.assertEqual(self._run("00700.HK", "1w"), "/?symbol=00700.HK&period=1w")


if __name__ == "__main__":
    unittest.main(verbosity=2)
