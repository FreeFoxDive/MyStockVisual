# -*- coding: utf-8 -*-
"""market._StaticListCache 单测 (离线, 不联网)。

覆盖静态列表的缓存契约: 内存/磁盘命中零联网、24h TTL、过期后台刷新并落盘、
失败保留旧数据并退避、原子写无残留临时文件。

运行:
    venv/Scripts/python.exe -u visual/test/test_static_list_cache.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import market  # noqa: E402


class StaticListCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "list.json"

    def _cache(self, fetcher, **kw):
        return market._StaticListCache("测试列表", self.path, fetcher, **kw)

    def _write(self, rows, ts):
        self.path.write_text(json.dumps({"rows": rows, "ts": ts}), encoding="utf-8")

    def test_disk_hit_within_ttl_needs_no_fetch(self):
        rows = [{"a": 1}]
        self._write(rows, time.time())
        fetcher = mock.Mock(return_value=[{"b": 2}])
        self.assertEqual(self._cache(fetcher).get(), rows)
        fetcher.assert_not_called()

    def test_memory_hit_within_ttl(self):
        fetcher = mock.Mock(return_value=[{"a": 1}])
        c = self._cache(fetcher)
        self.assertEqual(c.get(), [{"a": 1}])
        # 第二次仍在 TTL 内 → 不再读盘/联网 (删掉磁盘文件也不受影响)
        self.path.unlink()
        self.assertEqual(c.get(), [{"a": 1}])
        self.assertEqual(fetcher.call_count, 1)

    def test_missing_cache_fetches_and_persists(self):
        fetcher = mock.Mock(return_value=[{"a": 1}])
        c = self._cache(fetcher)
        self.assertEqual(c.get(), [{"a": 1}])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["rows"], [{"a": 1}])
        self.assertGreater(saved["ts"], 0)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists(), "临时文件应已 replace")

    def test_stale_disk_returns_immediately_then_refreshes_and_persists(self):
        self._write([{"old": 1}], 0)
        fetcher = mock.Mock(return_value=[{"new": 1}])
        c = self._cache(fetcher)
        self.assertEqual(c.get(), [{"old": 1}])
        for _ in range(50):
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                saved = {}
            if saved.get("rows") == [{"new": 1}]:
                break
            time.sleep(0.01)
        self.assertEqual(saved["rows"], [{"new": 1}])
        fetcher.assert_called_once()

    def test_failure_keeps_stale_and_backs_off(self):
        self._write([{"old": 1}], 0)
        fetcher = mock.Mock(side_effect=RuntimeError("boom"))
        c = self._cache(fetcher)
        self.assertEqual(c.get(), [{"old": 1}])   # 刷新失败 → 保留旧数据
        for _ in range(50):
            if fetcher.called:
                break
            time.sleep(0.01)
        self.assertEqual(c.get(), [{"old": 1}])   # 退避期内不再重试
        self.assertEqual(fetcher.call_count, 1)

    def test_failure_without_cache_returns_empty_and_backs_off(self):
        fetcher = mock.Mock(return_value=[])
        c = self._cache(fetcher)
        self.assertEqual(c.get(), [])
        self.assertEqual(c.get(), [])
        self.assertEqual(fetcher.call_count, 1)

    def test_retry_resumes_after_backoff_expires(self):
        fetcher = mock.Mock(return_value=[])
        c = self._cache(fetcher, retry_delay=0.01)
        c.get()
        time.sleep(0.02)
        c.get()
        self.assertEqual(fetcher.call_count, 2)

    def test_ts_reports_loaded_version(self):
        ts = time.time() - 5
        self._write([{"a": 1}], ts)
        c = self._cache(mock.Mock())
        c.get()
        self.assertEqual(c.ts, ts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
