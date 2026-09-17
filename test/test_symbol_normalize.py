# -*- coding: utf-8 -*-
"""market.normalize_symbol 口径回归测试 (离线, 无网络)。

行情/K线/五档等路由共用这一个入口, 它决定了「用户输入 → 上游 symbol」。
覆盖三类写法:
- 裸代码 / 裸字母代码 (600000 / 000070 / 00700 / AAPL)
- 后缀式 (000070.SZ / 600000.sh / 00700.HK / AAPL.US)
- 前缀式 (SH600000 / SZ000070 / sh.600000 / HK00700 / US.AAPL)

前缀式曾经直接 `return raw` (把 SZ000070 原样当 symbol 送去上游, 必然失败);
现在归一成后缀式, 但不得把 SHOP/SHEL 这类美股字母代码当成 SH 前缀拆掉。

运行:
    venv/Scripts/python.exe -u visual/test/test_symbol_normalize.py
"""

import os
import sys
import unittest

_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import market  # noqa: E402


class NormalizeSymbolTest(unittest.TestCase):
    def _cases(self, expected_map):
        for raw, want in expected_map.items():
            self.assertEqual(market.normalize_symbol(raw), want, raw)

    def test_bare_codes(self):
        self._cases({
            "600000": "600000.SH",
            "000070": "000070.SZ",
            "300750": "300750.SZ",
            "688981": "688981.SH",
            "00700": "00700.HK",
            "AAPL": "AAPL",
            "BRK": "BRK",
        })

    def test_suffix_forms(self):
        self._cases({
            "000070.SZ": "000070.SZ",
            "000070.sz": "000070.SZ",
            "600000.sh": "600000.SH",
            "00700.hk": "00700.HK",
            "430047.BJ": "430047.BJ",
            "AAPL.US": "AAPL",
        })

    def test_prefix_forms(self):
        self._cases({
            "SH600000": "600000.SH",
            "SZ000070": "000070.SZ",
            "sh.600000": "600000.SH",
            "sz-000070": "000070.SZ",
            "HK00700": "00700.HK",
            "US.AAPL": "AAPL",
        })

    def test_letter_tickers_keep_prefix_like_letters(self):
        """SHOP/SHEL/BRK 是美股字母代码, 不能被当作 SH 前缀 / BRK+? 拆开。"""
        self._cases({
            "SHOP": "SHOP",
            "SHEL": "SHEL",
            "SHW": "SHW",
            "SZ": "SZ",
        })

    def test_whitespace_and_case(self):
        self._cases({
            " 000070.sz ": "000070.SZ",
            " sh600000": "600000.SH",
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
