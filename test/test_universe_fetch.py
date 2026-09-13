# -*- coding: utf-8 -*-
"""港/美股池拉取与降级回归测试 (离线, 全 mock)。

覆盖:
- _fetch_universe_rows: ext.type 映射 / 缺列回退 default_type / 空行跳过 / to_dataframe 传参
- AF 套餐无 universe 权限 (403 / not available): 熔断置位, 后续调用不再触碰 AF
- akshare 回退: _fetch_hk_list / _fetch_us_list 代码归一 (港股 zfill(5)+.HK, 美股取末段大写)
- _load_universe: 24h 磁盘缓存命中零联网; 过期同步刷新并落盘; 失败保留旧数据并退避

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
            market._HK_LIST_FILE, market._US_LIST_FILE,
            market._hk_cache, market._us_cache,
        )
        market._hkus_universe_perm_denied = False
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 缓存实例在构造时捕获 path, 故换文件必须重建实例 (隔离内存/磁盘状态)
        market._HK_LIST_FILE = Path(self._tmpdir.name) / "hk_list.json"
        market._US_LIST_FILE = Path(self._tmpdir.name) / "us_list.json"
        market._hk_cache = market._StaticListCache(
            "港股列表", market._HK_LIST_FILE, lambda: market._fetch_hk_list())
        market._us_cache = market._StaticListCache(
            "美股列表", market._US_LIST_FILE, lambda: market._fetch_us_list())
        self.addCleanup(self._restore)

    def _restore(self):
        (market._hkus_universe_perm_denied,
         market._HK_LIST_FILE, market._US_LIST_FILE,
         market._hk_cache, market._us_cache) = self._orig
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

    # ── _load_universe (24h 磁盘缓存, 用到时刷新) ──
    def test_disk_hit_needs_no_network(self):
        hk = [{"symbol": "00700.HK", "name": "腾讯控股", "code": "00700", "type": "hk"}]
        market._HK_LIST_FILE.write_text(
            json.dumps({"rows": hk, "ts": time.time()}), encoding="utf-8")
        with mock.patch.object(market, "_fetch_hk_list") as fh:
            self.assertEqual(market._load_universe("hk"), hk)
        fh.assert_not_called()

    def test_stale_disk_refreshes_and_persists(self):
        old = [{"symbol": "00001.HK", "name": "旧", "code": "00001", "type": "hk"}]
        fresh = [{"symbol": "00700.HK", "name": "腾讯控股", "code": "00700", "type": "hk"}]
        market._HK_LIST_FILE.write_text(
            json.dumps({"rows": old, "ts": 0}), encoding="utf-8")
        with mock.patch.object(market, "_fetch_hk_list", return_value=fresh):
            self.assertEqual(market._load_universe("hk"), fresh)
        saved = json.loads(market._HK_LIST_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["rows"], fresh)
        self.assertGreater(saved["ts"], 0)

    def test_failure_keeps_old_and_backs_off(self):
        old = [{"symbol": "00001.HK", "name": "旧", "code": "00001", "type": "hk"}]
        market._HK_LIST_FILE.write_text(
            json.dumps({"rows": old, "ts": 0}), encoding="utf-8")
        with mock.patch.object(market, "_fetch_hk_list", return_value=[]) as fh:
            # 刷新失败 → 保留旧数据, 且退避期内不再重复拉取
            self.assertEqual(market._load_universe("hk"), old)
            self.assertEqual(market._load_universe("hk"), old)
            self.assertEqual(fh.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
