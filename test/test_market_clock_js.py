# -*- coding: utf-8 -*-
"""market-clock.js 边界用例 (Node 驱动, 无浏览器)。

覆盖: 精确唤醒与安全网的关系、连接期零探测、隐藏期零探测、休眠/节流的
间隙检测、三个恢复事件、服务端不可达时的状态保留与退避、日历降级提示、
状态文案里的下一次开盘时间。
"""
import subprocess
import unittest
from pathlib import Path

from js_test_util import require_node


class MarketClockTest(unittest.TestCase):
    def test_clock_boundaries(self):
        require_node()
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', str(root / 'test/market_clock_cases.js'),
             str(root / 'static/js/market-clock.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class MarketClockScopeTest(unittest.TestCase):
    """模块必须自包含: 只依赖注入的 fetch/timers/now, 便于 Node 直接跑。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / 'static/js/market-clock.js').read_text(encoding='utf-8')

    def test_does_not_reach_for_globals_at_load_time(self):
        for banned in ('document.hidden', 'document.addEventListener', 'window.fetch'):
            self.assertNotIn(banned, self.js, f'{banned}: 必须走 options/doc 注入')

    def test_declares_the_contract_the_pages_rely_on(self):
        for name in ('MarketClock', 'localSessionFallback', 'countdownText',
                     'openHintText', 'countdownTargetSec', 'wakeMs', 'safetyMs'):
            self.assertIn(name, self.js)

    def test_module_is_syntactically_valid(self):
        """模块语法错时 vm/runInContext 会连测试一起拖垮; 单独查一次, 报错更直接。"""
        require_node()
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', '--check', str(root / 'static/js/market-clock.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
