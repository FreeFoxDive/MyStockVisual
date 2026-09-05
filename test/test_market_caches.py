# -*- coding: utf-8 -*-
"""market.py 缓存健壮性回归测试 (离线, 全 mock)。

覆盖:
- _load_index_cache 加载失败不缓存, 下次重试 (避免进程生命周期内指数被当股票路由)
- _lookup_name 跟随股票列表刷新重建 (新股/更名不再固化到进程退出)

运行:
    venv/Scripts/python.exe -u visual/test/test_market_caches.py
"""

import os
import sys
import unittest
from unittest import mock

_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import market  # noqa: E402


class IndexCacheTest(unittest.TestCase):
    def setUp(self):
        self._orig = (market._index_symbols, market._index_names)
        market._index_symbols = None
        market._index_names = {}
        self.addCleanup(self._restore)

    def _restore(self):
        market._index_symbols, market._index_names = self._orig

    def test_failure_not_cached_and_retries(self):
        calls = []

        class _Api:
            def index_list(self):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("网络抖动")
                return [{"dm": "000001.sh", "mc": "上证指数"}]

        with mock.patch.object(market, "get_mr", return_value=_Api()):
            # 第一次失败: 返回空集合, 且不缓存 (下次调用重试)
            self.assertEqual(market._load_index_cache(), set())
            self.assertIsNone(market._index_symbols)
            # 第二次成功: 缓存
            self.assertEqual(market._load_index_cache(), {"000001.SH"})
            # 第三次: 走缓存, 不再调 API
            self.assertEqual(market._load_index_cache(), {"000001.SH"})
        self.assertEqual(len(calls), 2)


class NameMapRefreshTest(unittest.TestCase):
    def setUp(self):
        self._orig = (market._name_map, market._name_map_ts, market._stock_list_time)
        market._name_map = None
        market._name_map_ts = 0.0
        market._stock_list_time = 100.0
        self.addCleanup(self._restore)

    def _restore(self):
        market._name_map, market._name_map_ts, market._stock_list_time = self._orig

    def test_follows_stock_list_refresh(self):
        with mock.patch.object(
            market, "_load_stock_list", side_effect=[
                [{"symbol": "600000.SH", "name": "浦发银行"}],
                [{"symbol": "600000.SH", "name": "浦发新名"}],
            ],
        ), mock.patch.object(market, "_is_index_symbol", return_value=False):
            self.assertEqual(market._lookup_name("600000.SH"), "浦发银行")
            self.assertEqual(market._name_map_ts, 100.0)
            # 股票列表后台刷新 (时间戳前进) → 名称映射重建
            market._stock_list_time = 200.0
            self.assertEqual(market._lookup_name("600000.SH"), "浦发新名")
            self.assertEqual(market._name_map_ts, 200.0)

    def test_empty_list_does_not_cache(self):
        with mock.patch.object(
            market, "_load_stock_list", side_effect=[
                [],
                [{"symbol": "600000.SH", "name": "浦发银行"}],
            ],
        ), mock.patch.object(market, "_is_index_symbol", return_value=False):
            # 首拉失败 (空列表): 不缓存, 后续调用重试直到成功
            self.assertEqual(market._lookup_name("600000.SH"), "600000.SH")
            self.assertIsNone(market._name_map)
            self.assertEqual(market._lookup_name("600000.SH"), "浦发银行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
