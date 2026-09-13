# -*- coding: utf-8 -*-
"""港/美股池拉取与降级回归测试 (离线, 全 mock)。

覆盖:
- _fetch_universe_rows: ext.type 映射 / 缺列回退 default_type / 空行跳过 / to_dataframe 传参
- AF 套餐无 universe 权限 (403 / not available): 熔断置位, 后续调用不再触碰 AF
- akshare 回退: _fetch_hk_list / _fetch_us_list 代码归一 (港股 zfill(5)+.HK, 美股取末段大写)
- _refresh_hkus_lists_async: 拉取成功写入内存并落盘; 失败后 60s 内不重试

运行:
    venv/Scripts/python.exe -u visual/test/test_universe_fetch.py
"""

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import market  # noqa: E402


class _SyncThread:
    """把 Thread(target=...).start() 变成同步立即执行, 免跨线程抖动。"""

    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _af_quotes(get_impl):
    """构造只用到 quotes.get 的假 AF; get_impl(**kwargs) 返回 DataFrame 或抛异常。"""
    class _Quotes:
        def get(self, **kw):
            return get_impl(**kw)

    af = types.SimpleNamespace()
    af.quotes = _Quotes()
    return af


def _universe_df(rows):
    """rows: [(symbol, name, ext_type|None)] → AF 风格 DataFrame (index=symbol)。"""
    idx, names, types_ = [], [], []
    for sym, name, itype in rows:
        idx.append(sym)
        names.append(name)
        types_.append(itype)
    df = pd.DataFrame({"name": names}, index=idx)
    if any(t is not None for t in types_):
        df["ext.type"] = types_
    return df


class UniverseFetchTest(unittest.TestCase):
    def setUp(self):
        self._orig = (
            market._hkus_universe_perm_denied,
            market._hk_list, market._us_list,
            market._hk_list_failed_at, market._us_list_failed_at,
            market._hkus_refreshing,
            market._HK_LIST_FILE, market._US_LIST_FILE,
        )
        market._hkus_universe_perm_denied = False
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._restore)

    def _restore(self):
        (market._hkus_universe_perm_denied,
         market._hk_list, market._us_list,
         market._hk_list_failed_at, market._us_list_failed_at,
         market._hkus_refreshing,
         market._HK_LIST_FILE, market._US_LIST_FILE) = self._orig
        self._tmpdir.cleanup()

    # ── _fetch_universe_rows ──
    def test_type_mapping_and_default(self):
        calls = []

        def get_impl(**kw):
            calls.append(kw)
            return _universe_df([
                ("600000.SH", "浦发银行", "stock"),
                ("512800.SH", "银行ETF", "etf"),
                ("000001.SH", "上证指数", "index"),
                ("161725.SZ", "白酒基金", "fund"),
                ("XYZ", "未知类型", "warrant"),   # 未知 → default_type
                ("", "空代码", "stock"),           # 空 symbol 跳过
                ("600001.SH", "", "stock"),        # 空 name 跳过
            ])

        with mock.patch.object(market, "get_af", return_value=_af_quotes(get_impl)):
            rows = market._fetch_universe_rows("CN_Stock", "cn", "stock")
        self.assertEqual(calls, [{"universes": ["CN_Stock"], "to_dataframe": True}])
        by_sym = {r["symbol"]: r for r in rows}
        self.assertEqual(by_sym["600000.SH"]["type"], "stock")
        self.assertEqual(by_sym["512800.SH"]["type"], "etf")
        self.assertEqual(by_sym["000001.SH"]["type"], "index")
        self.assertEqual(by_sym["161725.SZ"]["type"], "fund")
        self.assertEqual(by_sym["XYZ"]["type"], "stock")   # default_type
        self.assertNotIn("", by_sym)
        self.assertNotIn("600001.SH", by_sym)
        self.assertEqual(by_sym["600000.SH"]["code"], "600000")

    def test_df_without_ext_type_uses_default(self):
        with mock.patch.object(market, "get_af",
                               return_value=_af_quotes(lambda **kw: _universe_df(
                                   [("00700.HK", "腾讯控股", None)]))):
            rows = market._fetch_universe_rows("HK_Stock", "hk", "stock")
        self.assertEqual(rows, [{"symbol": "00700.HK", "name": "腾讯控股",
                                 "code": "00700", "type": "stock"}])

    def test_empty_df_returns_empty(self):
        with mock.patch.object(market, "get_af",
                               return_value=_af_quotes(lambda **kw: pd.DataFrame())):
            self.assertEqual(market._fetch_universe_rows("HK_Stock", "hk", "hk"), [])

    # ── 403 熔断 ──
    def test_permission_denied_trips_breaker(self):
        calls = []

        def get_impl(**kw):
            calls.append(kw)
            raise RuntimeError("universe not available: 403")

        with mock.patch.object(market, "get_af", return_value=_af_quotes(get_impl)):
            self.assertEqual(market._fetch_universe_rows("HK_Stock", "hk", "hk"), [])
            self.assertTrue(market._hkus_universe_perm_denied)
            n = len(calls)
            # 熔断后再次调用不再触碰 AF
            self.assertEqual(market._fetch_universe_rows("HK_Stock", "hk", "hk"), [])
        self.assertEqual(len(calls), n)

    def test_other_error_does_not_trip_breaker(self):
        with mock.patch.object(market, "get_af",
                               return_value=_af_quotes(mock.Mock(
                                   side_effect=RuntimeError("网络抖动")))):
            self.assertEqual(market._fetch_universe_rows("HK_Stock", "hk", "hk"), [])
        self.assertFalse(market._hkus_universe_perm_denied)

    # ── akshare 回退 ──
    def test_hk_falls_back_to_akshare(self):
        hk_df = pd.DataFrame({"代码": ["700", "9988"], "名称": ["腾讯控股", "阿里巴巴-W"]})
        fake_ak = types.SimpleNamespace(stock_hk_spot_em=lambda: hk_df)
        with mock.patch.object(market, "_fetch_universe_rows", return_value=[]), \
             mock.patch.dict(sys.modules, {"akshare": fake_ak}):
            rows = market._fetch_hk_list()
        self.assertEqual(rows[0], {"symbol": "00700.HK", "name": "腾讯控股",
                                   "code": "00700", "type": "hk"})
        self.assertEqual(rows[1]["symbol"], "09988.HK")

    def test_us_falls_back_to_akshare(self):
        us_df = pd.DataFrame({"代码": ["105.AAPL", "106.tsla"], "名称": ["苹果", "特斯拉"]})
        fake_ak = types.SimpleNamespace(stock_us_spot_em=lambda: us_df)
        with mock.patch.object(market, "_fetch_universe_rows", return_value=[]), \
             mock.patch.dict(sys.modules, {"akshare": fake_ak}):
            rows = market._fetch_us_list()
        self.assertEqual(rows[0], {"symbol": "AAPL", "name": "苹果",
                                   "code": "AAPL", "type": "us"})
        self.assertEqual(rows[1]["symbol"], "TSLA")

    # ── _refresh_hkus_lists_async ──
    def test_refresh_writes_mem_and_disk(self):
        market._hk_list, market._us_list = None, None
        market._hk_list_failed_at = market._us_list_failed_at = 0.0
        market._HK_LIST_FILE = Path(self._tmpdir.name) / "hk_list.json"
        market._US_LIST_FILE = Path(self._tmpdir.name) / "us_list.json"
        hk = [{"symbol": "00700.HK", "name": "腾讯控股", "code": "00700", "type": "hk"}]
        us = [{"symbol": "AAPL", "name": "苹果", "code": "AAPL", "type": "us"}]
        with mock.patch.object(market, "_fetch_hk_list", return_value=hk), \
             mock.patch.object(market, "_fetch_us_list", return_value=us), \
             mock.patch.object(market, "threading", types.SimpleNamespace(Thread=_SyncThread)):
            market._refresh_hkus_lists_async()
        self.assertEqual(market._hk_list, hk)
        self.assertEqual(market._us_list, us)
        self.assertEqual(json.loads(market._HK_LIST_FILE.read_text(encoding="utf-8"))["rows"], hk)
        self.assertEqual(json.loads(market._US_LIST_FILE.read_text(encoding="utf-8"))["rows"], us)

    def test_refresh_skips_within_retry_delay(self):
        market._hk_list, market._us_list = None, None
        now = time.time()
        market._hk_list_failed_at = market._us_list_failed_at = now
        with mock.patch.object(market, "_fetch_hk_list") as fh, \
             mock.patch.object(market, "_fetch_us_list") as fu, \
             mock.patch.object(market, "threading", types.SimpleNamespace(Thread=_SyncThread)):
            market._refresh_hkus_lists_async()
        fh.assert_not_called()
        fu.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
