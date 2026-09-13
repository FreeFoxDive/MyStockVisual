# -*- coding: utf-8 -*-
"""market._search_stocks 排序与返回上限回归测试 (离线, 全 mock)。

覆盖:
- 分组顺序 股票(含港/美) > ETF > 指数, 类型分档优先于匹配分
- 精确代码命中时 股票 排在 指数 之前 (回归: 移除指数 +60 加权)
- 股票组内同分 tie-break: A股 > 港股 > 美股
- 返回上限 SEARCH_MAX_RESULTS
- 同类型内仍按匹配分 (名称前缀 > 名称包含)

运行:
    venv/Scripts/python.exe -u visual/test/test_search_stocks.py
"""

import os
import sys
import unittest
from contextlib import contextmanager
from unittest import mock

_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import market  # noqa: E402


def _stock(symbol, name, type_="stock"):
    return {"symbol": symbol, "name": name, "code": symbol.split(".")[0], "type": type_}


@contextmanager
def _env(stocks, indices=None, hk=None, us=None):
    """隔离 _search_stocks 的外部依赖: 股票列表/指数/港美股/后台预热。"""

    def _universe_mem(path, attr):
        return list(hk or []) if attr == "hk" else list(us or [])

    with mock.patch.object(market, "_load_stock_list", return_value=list(stocks)), \
         mock.patch.object(market, "_load_index_cache", return_value=set(indices or {})), \
         mock.patch.object(market, "_index_names", dict(indices or {})), \
         mock.patch.object(market, "_load_universe_mem", side_effect=_universe_mem), \
         mock.patch.object(market, "_refresh_hkus_lists_async"):
        yield


def _symbols(rows):
    return [r["symbol"] for r in rows]


class SearchOrderTest(unittest.TestCase):
    def test_stock_then_etf_then_index(self):
        # ETF 名称前缀分 (150) 高于股票名称包含分 (50), 但类型分档仍让股票在前
        with _env(
            [_stock("000001.SZ", "平安银行"), _stock("512800.SH", "银行ETF")],
            indices={"399986.SZ": "中证银行指数"},
        ):
            rows = market._search_stocks("银行")
        self.assertEqual(_symbols(rows), ["000001.SZ", "512800.SH", "399986.SZ"])
        self.assertEqual([r["type"] for r in rows], ["stock", "etf", "index"])

    def test_exact_code_stock_beats_index(self):
        # 回归: 旧逻辑给指数 +60, 000001 会先出上证指数; 现应为平安银行
        with _env(
            [_stock("000001.SZ", "平安银行")],
            indices={"000001.SH": "上证指数"},
        ):
            rows = market._search_stocks("000001")
        self.assertEqual(_symbols(rows), ["000001.SZ", "000001.SH"])
        self.assertEqual(rows[0]["type"], "stock")

    def test_market_tiebreak_cn_over_hk_over_us(self):
        with _env(
            [_stock("601988.SH", "中国银行")],
            hk=[_stock("00011.HK", "恒生银行")],
            us=[_stock("BAC", "美国银行")],
        ):
            rows = market._search_stocks("银行")
        self.assertEqual(_symbols(rows), ["601988.SH", "00011.HK", "BAC"])

    def test_score_still_orders_within_group(self):
        with _env([_stock("600001.SH", "某某银行"), _stock("600002.SH", "银行某某")]):
            rows = market._search_stocks("银行")
        # 名称前缀 (150) 优先于名称包含 (50)
        self.assertEqual(_symbols(rows), ["600002.SH", "600001.SH"])

    def test_result_cap(self):
        stocks = [_stock(f"6{i:05d}.SH", f"测试股票{i}") for i in range(60)]
        with _env(stocks):
            rows = market._search_stocks("测试")
        self.assertEqual(len(rows), market.SEARCH_MAX_RESULTS)
        self.assertEqual(market.SEARCH_MAX_RESULTS, 50)

    def test_no_match_returns_empty(self):
        with _env([_stock("600001.SH", "某某银行")]):
            self.assertEqual(market._search_stocks("不存在的名字"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
