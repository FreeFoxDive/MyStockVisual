# -*- coding: utf-8 -*-
"""market.py 缓存健壮性回归测试 (离线, 全 mock)。

覆盖:
- _load_index_cache: 24h 磁盘缓存命中零联网; 过期同步刷新并落盘; 失败不固化空集合并退避
- _lookup_name 跟随股票列表刷新重建 (新股/更名不再固化到进程退出)

运行:
    venv/Scripts/python.exe -u visual/test/test_market_caches.py
"""

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import market  # noqa: E402


class IndexCacheTest(unittest.TestCase):
    def setUp(self):
        self._orig = (
            market._index_symbols, market._index_names,
            market._index_derived_ts, market._index_cache,
        )
        market._index_symbols = None
        market._index_names = {}
        market._index_derived_ts = None
        # 缓存实例在构造时捕获 path, 故换文件必须重建实例 (隔离内存/磁盘状态)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "index_list.json"
        self.addCleanup(self._restore)

    def _restore(self):
        (market._index_symbols, market._index_names,
         market._index_derived_ts, market._index_cache) = self._orig

    def _new_cache(self):
        market._index_cache = market._StaticListCache(
            "指数列表", self._path, lambda: market.get_mr().index_list())

    def test_disk_hit_needs_no_network(self):
        rows = [{"dm": "000001.sh", "mc": "上证指数"}]
        self._path.write_text(json.dumps({"rows": rows, "ts": time.time()}),
                              encoding="utf-8")
        self._new_cache()
        with mock.patch.object(market, "get_mr") as get_mr:
            self.assertEqual(market._load_index_cache(), {"000001.SH"})
            self.assertEqual(market._index_names["000001.SH"], "上证指数")
        get_mr.assert_not_called()

    def test_stale_refreshes_and_persists(self):
        rows = [{"dm": "000001.sh", "mc": "上证指数"}]
        self._new_cache()
        fake = types.SimpleNamespace(index_list=lambda: rows)
        with mock.patch.object(market, "get_mr", return_value=fake):
            self.assertEqual(market._load_index_cache(), {"000001.SH"})
        saved = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertEqual(saved["rows"], rows)
        self.assertGreater(saved["ts"], 0)

    def test_failure_returns_empty_and_backs_off(self):
        calls = []

        class _Api:
            def index_list(self):
                calls.append(1)
                raise RuntimeError("网络抖动")

        self._new_cache()
        with mock.patch.object(market, "get_mr", return_value=_Api()):
            # 失败: 返回空集合, 且不固化 (否则指数会被当股票路由)
            self.assertEqual(market._load_index_cache(), set())
            self.assertIsNone(market._index_symbols)
            # 退避期内不再重打接口 (此前失败不缓存 → 每个热路径都击穿一次)
            self.assertEqual(market._load_index_cache(), set())
        self.assertEqual(len(calls), 1)


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
