# -*- coding: utf-8 -*-
"""筹码分布 的周期闸门测试。

需求: 筹码分布在 日/周/月K 可用 (分时与 1/5/15/30/60 分不适用)。
周/月K 不是换算法, 而是把日线回看窗口加长 (见 chips.WINDOW_BY_PERIOD);
前端必须把 period 带进 /api/chips 请求, 且本地缓存按 (symbol, period) 隔离。

从 index.html 抽取 isChipPeriod **真实源码**执行 (抽不到即失败),
并静态防止闸门被改回写死日K、防止 fetchChips 丢失 period。

运行:
    venv/Scripts/python.exe -u visual/test/test_chip_period_js.py
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


class ChipPeriodTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.script = (_extract_fn(cls.src, "isChipPeriod") + "\n"
                      + "const ps = JSON.parse(process.argv[1]);"
                      + "process.stdout.write(JSON.stringify(ps.map(p => isChipPeriod(p))));")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_applies_to_daily_weekly_monthly_only(self):
        periods = ["1d", "1w", "1M", "1m", "5m", "15m", "30m", "60m", "intraday", ""]
        proc = subprocess.run(["node", "-e", self.script, json.dumps(periods)],
                              capture_output=True, check=True)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(out, [True, True, True] + [False] * 7)

    def test_gate_not_hardcoded_to_daily(self):
        """isChipPeriod 必须含周/月, 不得退回只认 '1d'。"""
        fn = _extract_fn(self.src, "isChipPeriod")
        for p in ("'1w'", "'1M'"):
            self.assertIn(p, fn, f"isChipPeriod 缺少 {p}")

    def test_fetch_chips_carries_period(self):
        """请求与本地缓存都必须按周期区分, 否则周/月K 会串日K 的筹码。"""
        fn = _extract_fn(self.src, "fetchChips")
        self.assertIn("const period = STATE.period", fn)
        self.assertIn("&period=${period}", fn)
        self.assertIn("`vc_${symbol}_${period}`", fn)
        # 竞态守卫: 切周期后旧响应必须丢弃
        self.assertIn("STATE.period !== period", fn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
