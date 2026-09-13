# -*- coding: utf-8 -*-
"""Elder 通道/动力系统 的周期闸门测试。

需求: 通道 ±1/2/3ATR 与动力系统在 日/周/月K 可用 (分时与 1/5/15/30/60 分不适用)。
从 index.html 抽取 isElderPeriod **真实源码**执行 (抽不到即失败), 并静态防止闸门被改回写死日K。

运行:
    venv/Scripts/python.exe -u visual/test/test_elder_period_js.py
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


class ElderPeriodTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.script = (_extract_fn(cls.src, "isElderPeriod") + "\n"
                      + "const ps = JSON.parse(process.argv[1]);"
                      + "process.stdout.write(JSON.stringify(ps.map(p => isElderPeriod(p))));")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_applies_to_daily_weekly_monthly_only(self):
        periods = ["1d", "1w", "1M", "1m", "5m", "15m", "30m", "60m", "intraday", ""]
        proc = subprocess.run(["node", "-e", self.script, json.dumps(periods)],
                              capture_output=True, check=True)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(out, [True, True, True] + [False] * 7)

    def test_gates_not_hardcoded_to_daily(self):
        """channelEnabled / impulseEnabled 的引用行不得再出现 '1d' 字面量。"""
        offenders = [
            (i, line.strip())
            for i, line in enumerate(self.src.splitlines(), 1)
            if ("channelEnabled" in line or "impulseEnabled" in line) and "'1d'" in line
        ]
        self.assertEqual(offenders, [], f"这些行把 Elder 功能写死回日K: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
