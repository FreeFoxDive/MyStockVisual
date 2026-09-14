"""Offline FTS5 index correctness and performance smoke test."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import search_index


class SearchIndexTest(unittest.TestCase):
    def test_build_and_query(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "catalog.sqlite3"
            self.addCleanup(search_index.close_connection, path)
            self.addCleanup(search_index.close_connection, path)
            rows = [{"symbol": "000001.SZ", "name": "平安银行", "code": "000001", "type": "stock"},
                    {"symbol": "HSI.HK", "name": "恒生指数", "code": "HSI", "type": "index"}]
            self.assertEqual(search_index.build_index(rows, path), 2)
            self.assertEqual(search_index.search(path, "恒生")[0]["symbol"], "HSI.HK")
            self.assertEqual(search_index.search(path, "000001.SZ")[0]["symbol"], "000001.SZ")
            search_index.close_connection(path)

    def test_reuses_connection_and_switches_after_publish(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "catalog.sqlite3"
            self.addCleanup(search_index.close_connection, path)
            row = {"symbol": "000001.SZ", "name": "平安银行", "code": "000001", "type": "stock"}
            search_index.build_index([row], path)
            search_index.search(path, "平安")
            first = search_index._connections.entry[1]
            search_index.search(path, "银行")
            self.assertIs(search_index._connections.entry[1], first)
            search_index.build_index([{"symbol": "HSI.HK", "name": "恒生指数", "code": "HSI", "type": "index"}], path)
            self.assertEqual(search_index.search(path, "恒生")[0]["symbol"], "HSI.HK")
            self.assertIsNot(search_index._connections.entry[1], first)
            search_index.close_connection(path)

    def test_does_not_truncate_candidates_before_ranking(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "catalog.sqlite3"
            rows = [{"symbol": f"{i:06d}.SZ", "name": f"银行证券{i}", "code": f"{i:06d}", "type": "stock"}
                    for i in range(600)]
            search_index.build_index(rows, path)
            self.assertEqual(len(search_index.search(path, "银行")), 600)
            search_index.close_connection(path)

    def test_keeps_only_recent_published_versions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "catalog.sqlite3"
            for i in range(5):
                search_index.build_index([{"symbol": f"{i:06d}.SZ", "name": f"证券{i}", "code": f"{i:06d}", "type": "stock"}], path)
            self.assertLessEqual(len(list(Path(d).glob("catalog.*.sqlite3"))), 3)

    def test_benchmark_fts_against_scan(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "catalog.sqlite3"
            rows = [{"symbol": f"{i:06d}.SZ", "name": f"测试证券{i}", "code": f"{i:06d}", "type": "stock"}
                    for i in range(20000)]
            search_index.build_index(rows, path)
            query = "测试证券19999"
            start = time.perf_counter()
            fts = search_index.search(path, query)
            fts_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            search_index.search(path, query)
            warm_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            scan = [r for r in rows if query.lower() in r["name"].lower()]
            scan_ms = (time.perf_counter() - start) * 1000
            self.assertTrue(fts)
            self.assertTrue(scan)
            self.assertTrue(fts)
            print(f"\nsearch benchmark: FTS5 cold={fts_ms:.2f}ms warm={warm_ms:.2f}ms scan={scan_ms:.2f}ms rows={len(rows)}")
            search_index.close_connection(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
