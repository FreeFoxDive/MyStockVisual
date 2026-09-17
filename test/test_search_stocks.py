# -*- coding: utf-8 -*-
"""market._search_stocks 排序与返回上限回归测试 (离线, 全 mock)。

覆盖:
- 分组顺序 股票(含港/美) > ETF > 指数, 类型分档优先于匹配分
- 精确代码命中时 股票 排在 指数 之前 (回归: 移除指数 +60 加权)
- 股票组内同分 tie-break: A股 > 港股 > 美股
- 返回上限 SEARCH_MAX_RESULTS
- 同类型内仍按匹配分 (名称前缀 > 名称包含)
- 完整代码格式输入 (000070.SZ / sz000070 / 全角 ００００７０.ＳＺ) 的命中与归一

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
def _env(stocks, indices=None, hk=None, us=None, indexed=None):
    """隔离 _search_stocks 的外部依赖: 股票列表/指数/港美股/本地索引。

    indexed 为 None 时索引返回空 (走内存兜底扫描); 传列表则模拟索引命中
    —— 真实链路里 search_index 对完整代码 (000070.SZ) 会精确命中并只返回那一行。
    """

    def _universe(attr):
        return list(hk or []) if attr == "hk" else list(us or [])

    with mock.patch.object(market, "_load_stock_list", return_value=list(stocks)), \
         mock.patch.object(market.search_index, "search", return_value=list(indexed or [])), \
         mock.patch.object(market, "_load_index_cache", return_value=set(indices or {})), \
         mock.patch.object(market, "_index_names", dict(indices or {})), \
         mock.patch.object(market, "_load_universe", side_effect=_universe):
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


class FullCodeFormatTest(unittest.TestCase):
    """完整代码输入 (000070.SZ / sz000070 / 全角) 必须能搜到。

    回归: 索引层用 lower(symbol) 精确命中 000070.SZ 并返回该行 (这是对的), 但
    _search_stocks 随后拿原串 "000070.sz" 再比 name/code (code 是不带后缀的 000070),
    三种匹配档全不成立 → score 0 → 把索引给的正确结果又丢掉, 前端显示"未找到"。
    """

    def test_suffixed_code_hits_indexed_row(self):
        row = _stock("000070.SZ", "特发信息")
        with _env([row], indexed=[row]):
            rows = market._search_stocks("000070.SZ")
        self.assertEqual(_symbols(rows), ["000070.SZ"])

    def test_suffixed_code_lowercase(self):
        row = _stock("000070.SZ", "特发信息")
        with _env([row], indexed=[row]):
            self.assertEqual(_symbols(market._search_stocks("000070.sz")), ["000070.SZ"])

    def test_suffixed_code_without_index(self):
        """索引不可用 (兜底扫描内存列表) 时同样命中。"""
        with _env([_stock("000070.SZ", "特发信息"), _stock("000071.SZ", "其他")]):
            self.assertEqual(_symbols(market._search_stocks("000070.SZ")), ["000070.SZ"])

    def test_exchange_prefixed_code(self):
        rows = [_stock("000070.SZ", "特发信息"), _stock("600000.SH", "浦发银行")]
        with _env(rows):
            self.assertEqual(_symbols(market._search_stocks("sz000070")), ["000070.SZ"])
            self.assertEqual(_symbols(market._search_stocks("SH600000")), ["600000.SH"])
            self.assertEqual(_symbols(market._search_stocks("sh.600000")), ["600000.SH"])

    def test_fullwidth_code_from_ime(self):
        with _env([_stock("000070.SZ", "特发信息")]):
            self.assertEqual(_symbols(market._search_stocks("００００７０.ＳＺ")), ["000070.SZ"])

    def test_us_suffix_keeps_bare_code_row(self):
        us = [_stock("AAPL", "苹果")]
        with _env([], us=us):
            self.assertEqual(_symbols(market._search_stocks("AAPL.US")), ["AAPL"])

    def test_short_numeric_code_is_not_substring_matched(self):
        """5 位港码不当裸代码键: 00700 是 000700 的子串, 会误命中 6 位 A 股代码。"""
        us = []
        with _env([_stock("000700.SZ", "模塑科技")], us=us):
            self.assertEqual(market._search_stocks("00700.HK"), [])

    def test_letter_ticker_not_split_as_prefix(self):
        """SHOP/SHEL 这类美股字母代码不得被当成 SH 前缀拆掉。"""
        self.assertEqual(market._search_query_keys("SHOP"), ["shop"])
        self.assertEqual(market._search_query_keys("SHEL"), ["shel"])

    def test_bare_code_keys_unchanged(self):
        """裸代码/名称查询的比对键不变 (既有命中口径不能动)。"""
        self.assertEqual(market._search_query_keys("000070"), ["000070"])
        self.assertEqual(market._search_query_keys("银行"), ["银行"])


class FullWidthNameTest(unittest.TestCase):
    """名称里的全角字母: 候选字段与查询必须同一口径归一。

    回归: 只把查询做 NFKC 时, "万科Ａ" 会变成 "万科a", 而库里名称是全角 "万科Ａ" →
    索引精确命中的行反被打成 0 分丢掉 (改动前用原串键是能命中的)。两边都归一后,
    全角 "万科Ａ" 与半角 "万科A" 都能搜到同一只。
    """

    ROW = {"symbol": "000002.SZ", "name": "万科Ａ", "code": "000002", "type": "stock"}

    def test_fullwidth_query_hits_indexed_row(self):
        with _env([self.ROW], indexed=[self.ROW]):
            self.assertEqual(_symbols(market._search_stocks("万科Ａ")), ["000002.SZ"])

    def test_halfwidth_query_hits_fullwidth_name(self):
        with _env([self.ROW]):
            self.assertEqual(_symbols(market._search_stocks("万科A")), ["000002.SZ"])
            self.assertEqual(_symbols(market._search_stocks("万科a")), ["000002.SZ"])

    def test_fullwidth_filtered_by_scoring(self):
        """索引给出行之后, 打分这一关不能把它丢掉。"""
        with _env([self.ROW], indexed=[self.ROW]):
            rows = market._search_stocks("万科Ａ")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "万科Ａ")

    def test_ascii_candidates_keep_lowercase_match(self):
        """ASCII 候选走低开销分支, 命中口径不变。"""
        with _env([_stock("000001.SZ", "PingAnBank")]):
            self.assertEqual(_symbols(market._search_stocks("pingan")), ["000001.SZ"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
