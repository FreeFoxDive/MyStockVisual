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


class DiskCacheAtomicWriteTest(unittest.TestCase):
    """DiskCache.set 原子写: 成功无 .tmp 残留; 写失败不破坏旧文件且清理临时文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_cache_dir = market.CACHE_DIR
        market.CACHE_DIR = Path(self._tmp.name)
        self.addCleanup(self._restore)

    def _restore(self):
        market.CACHE_DIR = self._orig_cache_dir

    def test_set_get_roundtrip_no_tmp_leftover(self):
        cache = market.DiskCache()
        cache.set("600519.SH", "1d", 100, {"rows": [1, 2, 3]})
        out = cache.get("600519.SH", "1d", 100, ttl_seconds=300)
        self.assertEqual(out, {"rows": [1, 2, 3]})
        self.assertEqual(list(Path(self._tmp.name).glob("*.tmp-*")), [])

    def test_set_failure_keeps_old_file_and_no_tmp(self):
        cache = market.DiskCache()
        cache.set("600519.SH", "1d", 100, {"good": True})
        fp = list(Path(self._tmp.name).glob("600519*.json.gz"))[0]
        before = fp.read_bytes()
        with mock.patch.object(market, "json") as j:
            j.dump.side_effect = OSError("disk full")
            cache.set("600519.SH", "1d", 100, {"bad": True})
        self.assertEqual(fp.read_bytes(), before)
        self.assertEqual(list(Path(self._tmp.name).glob("*.tmp-*")), [])


class CleanupTmpDirsTest(unittest.TestCase):
    """cleanup_cache_tmp_dirs: 只删 .cache 根下超龄的空 tmp 目录, 其他一律不碰。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_script_dir = market.SCRIPT_DIR
        market.SCRIPT_DIR = Path(self._tmp.name)
        self.addCleanup(self._restore)

    def _restore(self):
        market.SCRIPT_DIR = self._orig_script_dir

    def _mk(self, name, age_sec=0, empty=True):
        p = Path(self._tmp.name) / ".cache" / name
        p.mkdir(parents=True, exist_ok=True)
        if not empty:
            (p / "x.txt").write_text("x", encoding="utf-8")
        old = time.time() - age_sec
        os.utime(p, (old, old))
        return p

    def test_removes_old_empty_tmp_dir(self):
        old = self._mk("tmpabc", age_sec=90000)
        self.assertEqual(market.cleanup_cache_tmp_dirs(), 1)
        self.assertFalse(old.exists())

    def test_keeps_fresh_or_nonempty_or_nontmp(self):
        fresh = self._mk("tmpfresh", age_sec=100)
        nonempty = self._mk("tmpfull", age_sec=90000, empty=False)
        nontmp = self._mk("keepme", age_sec=90000)
        self.assertEqual(market.cleanup_cache_tmp_dirs(), 0)
        self.assertTrue(fresh.exists() and nonempty.exists() and nontmp.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
