# -*- coding: utf-8 -*-
"""visual/market.py 质押数据缓存与定时更新的单元测试。

覆盖范围（纯标准库 + mock，无网络依赖，可离线运行）：
- 文件名自描述 + 日期戳
- gzip 磁盘缓存写读回环 + 仅保留最近 N 个文件
- _fetch_pledge 拉取解析 + 最近交易日回溯 + 无数据兜底（mock akshare）
- _load_pledge 状态机：内存新鲜/过期、磁盘兜底、首跑同步拉取、拉取失败兜底
- _refresh_pledge_async 失败保留旧缓存 / 日期未更新跳过写入
- _next_schedule_delay 定时点计算（15:30 前后 / 恰好 15:30）

运行：
    venv/Scripts/python.exe -u visual/test/test_pledge.py
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# visual/ 不是包（无 __init__.py），把其目录加入 sys.path 后直接 import market
_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import market  # noqa: E402


def _mk_pledge(ratio=12.3, shares=100.0, market_value=5000.0, count=2):
    return {"000001": {"ratio": ratio, "shares": shares,
                       "market_value": market_value, "count": count}}


def _mk_df(rows):
    import pandas as pd
    return pd.DataFrame(rows)


class PledgeTestCase(unittest.TestCase):
    """每个测试用独立临时目录当 .cache, 并把全局状态复位, 保证互相隔离。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_script_dir = market.SCRIPT_DIR
        market.SCRIPT_DIR = Path(self._tmp.name)
        # 复位模块级质押全局状态
        market._pledge_cache = None
        market._pledge_ts = 0
        market._pledge_date = None
        market._refreshing_pledge = False

    def tearDown(self):
        market.SCRIPT_DIR = self._orig_script_dir
        market._pledge_cache = None
        market._pledge_ts = 0
        market._pledge_date = None
        market._refreshing_pledge = False
        self._tmp.cleanup()

    def _wait_refresh(self, timeout=5.0):
        """等待后台刷新线程跑完（mock 下极快），防止断言读到中间状态。"""
        deadline = time.time() + timeout
        while market._refreshing_pledge and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(market._refreshing_pledge, "后台刷新超时未完成")

    # ---- 文件名 / 磁盘缓存 ----

    def test_file_path_self_descriptive(self):
        fp = market._pledge_file_path("20260818")
        self.assertIn("pledge_ratio_", fp.name)
        self.assertIn("20260818", fp.name)
        self.assertTrue(fp.name.endswith(".json.gz"))

    def test_disk_roundtrip(self):
        pledge = _mk_pledge()
        market._pledge_to_disk("20260818", pledge)
        date, got, ts = market._pledge_from_disk()
        self.assertEqual(date, "20260818")
        self.assertEqual(got, pledge)
        self.assertGreater(ts, 0)
        # 确实是 gzip 文件
        self.assertTrue(market._pledge_file_path("20260818").exists())

    def test_disk_retention_keeps_latest(self):
        # 写 8 天, 应只保留最近 7 天, 删最旧 1 天
        dates = [f"202608{i:02d}" for i in range(11, 19)]  # 11..18
        for i, d in enumerate(dates):
            market._pledge_to_disk(d, {"000001": {"ratio": float(i)}})
        files = list((market.SCRIPT_DIR / ".cache").glob("pledge_ratio_*.json.gz"))
        self.assertEqual(len(files), market.PLEDGE_KEEP_FILES)
        self.assertTrue(market._pledge_file_path("20260818").exists())
        self.assertFalse(market._pledge_file_path("20260811").exists())

    def test_disk_reads_latest_by_date(self):
        # 多个文件时应取日期最新 (字符串序 = 日期序)
        market._pledge_to_disk("20260816", {"000001": {"ratio": 1.0}})
        market._pledge_to_disk("20260818", {"000001": {"ratio": 2.0}})
        date, got, _ = market._pledge_from_disk()
        self.assertEqual(date, "20260818")
        self.assertEqual(got["000001"]["ratio"], 2.0)

    # ---- _fetch_pledge (mock akshare) ----

    def test_fetch_parses_and_backtracks(self):
        from datetime import date, timedelta
        d3 = (date.today() - timedelta(days=3)).strftime("%Y%m%d")
        df = _mk_df([
            {"股票代码": "000001", "质押比例": 12.34, "质押股数": 100,
             "质押市值": 5000, "质押笔数": 2},
            {"股票代码": "600000", "质押比例": 0.5, "质押股数": 50,
             "质押市值": 300, "质押笔数": 1},
        ])
        fake = mock.MagicMock()

        def _em(date=None):
            if date == d3 or date is None:
                return df
            raise RuntimeError("no data for %s" % date)

        fake.stock_gpzy_pledge_ratio_em.side_effect = _em
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            got_date, pledge = market._fetch_pledge()
        self.assertEqual(got_date, d3)          # 回溯到 3 天前有数据的交易日
        self.assertEqual(len(pledge), 2)
        self.assertEqual(pledge["000001"]["ratio"], 12.34)
        self.assertEqual(pledge["600000"]["count"], 1)

    def test_fetch_fallback_to_noarg(self):
        df = _mk_df([{"股票代码": "000001", "质押比例": 1.0, "质押股数": 1,
                      "质押市值": 1, "质押笔数": 1}])
        fake = mock.MagicMock()

        def _em(date=None):
            if date is None:
                return df
            raise RuntimeError("all dated queries fail")

        fake.stock_gpzy_pledge_ratio_em.side_effect = _em
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            got_date, pledge = market._fetch_pledge()
        self.assertIsNotNone(got_date)
        self.assertEqual(len(pledge), 1)

    def test_fetch_failure_returns_none(self):
        fake = mock.MagicMock()

        def _em(date=None):
            raise RuntimeError("network down")

        fake.stock_gpzy_pledge_ratio_em.side_effect = _em
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            got_date, pledge = market._fetch_pledge()
        self.assertIsNone(got_date)
        self.assertIsNone(pledge)

    # ---- _load_pledge 状态机 ----

    def test_load_first_run_fetches_and_persists(self):
        pledge = _mk_pledge()
        with mock.patch.object(market, "_fetch_pledge",
                               return_value=("20260818", pledge)):
            result = market._load_pledge()
        self.assertEqual(result, pledge)
        self.assertEqual(market._pledge_cache, pledge)
        self.assertEqual(market._pledge_date, "20260818")
        # 同时落盘
        date, got, _ = market._pledge_from_disk()
        self.assertEqual(date, "20260818")
        self.assertEqual(got, pledge)

    def test_load_fresh_memory_no_fetch(self):
        pledge = _mk_pledge()
        market._pledge_cache = pledge
        market._pledge_ts = time.time()
        market._pledge_date = "20260818"
        with mock.patch.object(market, "_fetch_pledge") as m, \
             mock.patch.object(market, "_refresh_pledge_async") as r:
            result = market._load_pledge()
        self.assertEqual(result, pledge)
        m.assert_not_called()
        r.assert_not_called()

    def test_load_stale_memory_returns_old_and_refreshes(self):
        pledge = _mk_pledge()
        market._pledge_cache = pledge
        market._pledge_ts = time.time() - 100000  # 过期
        market._pledge_date = "20260817"
        with mock.patch.object(market, "_refresh_pledge_async") as r:
            result = market._load_pledge()
        self.assertEqual(result, pledge)          # 立即返回旧值
        r.assert_called_once()                     # 并触发后台刷新

    def test_load_disk_fallback(self):
        pledge = _mk_pledge()
        market._pledge_to_disk("20260818", pledge)  # 内存空, 磁盘有(新鲜)
        with mock.patch.object(market, "_fetch_pledge") as m, \
             mock.patch.object(market, "_refresh_pledge_async") as r:
            result = market._load_pledge()
        self.assertEqual(result, pledge)
        m.assert_not_called()
        r.assert_not_called()

    def test_load_fetch_failure_returns_empty(self):
        with mock.patch.object(market, "_fetch_pledge", return_value=(None, None)):
            result = market._load_pledge()
        self.assertEqual(result, {})
        self.assertEqual(market._pledge_cache, {})

    # ---- _refresh_pledge_async 失败处理 ----

    def test_refresh_failure_keeps_old_cache(self):
        old = _mk_pledge(ratio=9.9)
        market._pledge_cache = old
        market._pledge_ts = time.time()
        market._pledge_date = "20260817"
        with mock.patch.object(market, "_fetch_pledge", return_value=(None, None)):
            market._refresh_pledge_async()
            self._wait_refresh()
        self.assertEqual(market._pledge_cache, old)      # 旧缓存保留
        self.assertEqual(market._pledge_date, "20260817")

    def test_refresh_success_updates_cache_and_disk(self):
        old = _mk_pledge(ratio=9.9)
        market._pledge_cache = old
        market._pledge_ts = time.time() - 100000
        market._pledge_date = "20260817"
        new = _mk_pledge(ratio=5.5)
        new["600000"] = {"ratio": 1.0, "shares": 1.0, "market_value": 1.0, "count": 1}
        with mock.patch.object(market, "_fetch_pledge",
                               return_value=("20260818", new)):
            market._refresh_pledge_async()
            self._wait_refresh()
        self.assertEqual(market._pledge_cache, new)
        self.assertEqual(market._pledge_date, "20260818")
        date, got, _ = market._pledge_from_disk()
        self.assertEqual(date, "20260818")
        self.assertEqual(got, new)

    def test_refresh_same_date_skips_write(self):
        old = _mk_pledge(ratio=9.9)
        market._pledge_cache = old
        market._pledge_ts = time.time()
        market._pledge_date = "20260818"
        # 拉到同一交易日 → 跳过写入, 保持旧值
        with mock.patch.object(market, "_fetch_pledge",
                               return_value=("20260818", _mk_pledge(ratio=1.0))):
            market._refresh_pledge_async()
            self._wait_refresh()
        self.assertEqual(market._pledge_cache, old)

    # ---- 定时点计算 ----

    def test_schedule_delay_before_1530(self):
        from datetime import datetime
        now = datetime(2026, 8, 18, 10, 0, 0)
        self.assertEqual(market._next_schedule_delay(now), 5.5 * 3600)

    def test_schedule_delay_after_1530(self):
        from datetime import datetime
        now = datetime(2026, 8, 18, 16, 0, 0)
        self.assertEqual(market._next_schedule_delay(now), 23.5 * 3600)

    def test_schedule_delay_exactly_1530(self):
        from datetime import datetime
        now = datetime(2026, 8, 18, 15, 30, 0)
        self.assertEqual(market._next_schedule_delay(now), 24 * 3600)


if __name__ == "__main__":
    unittest.main(verbosity=2)


