# -*- coding: utf-8 -*-
"""kline_source 数据源包装层测试: 默认路由 / env 切换 / 自动回退 / 新鲜度守卫 / 列映射。

契约 (README「K线数据源配置」):
- 默认链 = 券商/付费源优先, 与既有行为一致 (分钟→alphafeed, 股票/指数/基金→mairui)
- KLINE_SOURCE_{MINUTE,STOCK,INDEX,FUND} 逗号分隔链, 主源失败自动回退
- 分钟末根 bar 距今 > MINUTE_STALE_DAYS 天视为该源失败
- akshare 兜底: 中文列名映射; 基金日K volume ×100 对齐麦蕊「股」口径
- market.fetch_kline_ex 返回 (df, name, source), 磁盘缓存记录 source

运行:
    venv/Scripts/python.exe -u visual/test/test_kline_source.py
"""

import contextlib
import json
import os
import sys
import types
import unittest
from unittest import mock
from pathlib import Path

import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import kline_source
import market

ENV_KEYS = ("KLINE_SOURCE_MINUTE", "KLINE_SOURCE_STOCK",
            "KLINE_SOURCE_INDEX", "KLINE_SOURCE_FUND")


@contextlib.contextmanager
def _no_kline_env(**overrides):
    """清掉 KLINE_SOURCE_* 后按 overrides 设置, 用例间互不串扰。"""
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    env.update({k: v for k, v in overrides.items() if v is not None})
    with mock.patch.dict(os.environ, env, clear=True):
        yield


def _norm_df(n=30, start="2026-08-01"):
    idx = pd.date_range(start, periods=n, freq="D")
    base = 10.0 + pd.Series(range(n), index=idx, dtype=float) * 0.01
    return pd.DataFrame({
        "open": base, "high": base + 0.2, "low": base - 0.2,
        "close": base + 0.1, "volume": 100000.0, "amount": 1_000_000.0,
    }, index=idx)


def _mr_rows(n=10):
    return [{"t": d, "o": 10.0, "h": 10.2, "l": 9.8, "c": 10.1,
             "v": 100, "a": 1000.0}
            for d in pd.date_range("2026-08-01", periods=n).strftime("%Y-%m-%d")]


def _cn_daily_df(n=25):
    idx = pd.date_range("2026-08-01", periods=n, freq="D")
    return pd.DataFrame({
        "日期": idx.strftime("%Y-%m-%d"),
        "开盘": 10.0, "最高": 10.2, "最低": 9.8, "收盘": 10.1,
        "成交量": 8414655.0, "成交额": 3.9e9,
    })


def _cn_minute_df(n=10):
    idx = pd.date_range("2026-09-04 09:35", periods=n, freq="5min")
    return pd.DataFrame({
        "时间": idx.strftime("%Y-%m-%d %H:%M:%S"),
        "开盘": 10.0, "收盘": 10.1, "最高": 10.2, "最低": 9.9,
        "成交量": 100.0, "成交额": 1e5,
    })


def _minute_df(n=30, end=None):
    if end is None:
        # 默认末根贴着当前时间, 避免测试随日期推移变"过旧"
        end = (market.market_hours.now() - pd.Timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
    idx = pd.date_range(end=end, periods=n, freq="5min")
    return pd.DataFrame({
        "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1,
        "volume": 100.0, "amount": 1e5,
    }, index=idx)


class _FakeMr:
    def __init__(self, rows):
        self.rows = rows

    def stock_history(self, symbol, period, div, lt=None):
        return self.rows

    def index_history(self, symbol, period, lt=None):
        return self.rows


class _FakeDisk:
    def __init__(self):
        self.store = {}

    def get(self, symbol, period, count, ttl):
        return self.store.get((symbol, period, count))

    def set(self, symbol, period, count, data):
        self.store[(symbol, period, count)] = data


class _FakeUrlopenResp:
    """urlopen 上下文管理器, 返回 fsjy 格式 JSON (带 zf/hs/ud 等冗余字段)。"""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._rows).encode("utf-8")


def _fsjy_rows(n=8):
    return [{"d": "2026-02-06 13:15", "o": 1524.96, "h": 1526.0,
             "l": 1520.56, "c": 1520.56, "v": 1078, "e": 164359250.0,
             "zf": 0.36, "hs": 0.01, "zd": -0.28, "zde": -4.24,
             "ud": "2026-03-29 23:13:44"}] * n


def _fake_akshare(stock_daily=None, fund_daily=None, index_daily=None,
                  stock_min=None, fund_min=None, index_min=None):
    mod = types.ModuleType("akshare")
    mod.stock_zh_a_hist = mock.Mock(return_value=stock_daily)
    mod.fund_etf_hist_em = mock.Mock(return_value=fund_daily)
    mod.index_zh_a_hist = mock.Mock(return_value=index_daily)
    mod.stock_zh_a_hist_min_em = mock.Mock(return_value=stock_min)
    mod.fund_etf_hist_min_em = mock.Mock(return_value=fund_min)
    mod.index_zh_a_hist_min_em = mock.Mock(return_value=index_min)
    return mod


class KlineSourceTestBase(unittest.TestCase):
    def setUp(self):
        kline_source._warned_names.clear()
        # 适配器内部走 market 属性访问, 统一屏蔽分类判定与名称查询
        for p in (
            mock.patch.object(market, "_lookup_name", return_value="测试名"),
            mock.patch.object(market, "_is_etf", return_value=False),
            mock.patch.object(market, "_is_index_symbol", return_value=False),
        ):
            p.start()
            self.addCleanup(p.stop)


class TestDefaultRouting(KlineSourceTestBase):
    """默认链与既有行为一致: 分钟→alphafeed, 股票/指数/基金→mairui。"""

    def test_stock_defaults_to_mairui(self):
        with _no_kline_env(), mock.patch.object(
                market, "_fetch_mr_kline", return_value=_norm_df()) as mr:
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 100)
        self.assertEqual(src, "mairui")
        self.assertEqual(len(df), 30)
        mr.assert_called_once_with("600519.SH", "1d", 100)

    def test_minute_defaults_to_alphafeed(self):
        with _no_kline_env(), mock.patch.object(
                market, "_fetch_minute_kline", return_value=_minute_df()) as af:
            _df, src = kline_source.fetch_kline_df("minute", "600519.SH", "1m", 100)
        self.assertEqual(src, "alphafeed")
        af.assert_called_once_with("600519.SH", "1m", 100)

    def test_fund_defaults_to_mairui_jjlskx(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_is_etf", return_value=True), \
             mock.patch.object(market, "_fetch_fund_kline", return_value=_norm_df()) as f:
            _df, src = kline_source.fetch_kline_df("fund", "510300.SH", "1d", 100)
        self.assertEqual(src, "mairui")
        f.assert_called_once_with("510300.SH", "1d", 100)

    def test_index_defaults_to_mairui(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_is_index_symbol", return_value=True), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()) as mr:
            _df, src = kline_source.fetch_kline_df("index", "000300.SH", "1w", 100)
        self.assertEqual(src, "mairui")
        mr.assert_called_once_with("000300.SH", "1w", 100)

    def test_all_sources_fail_returns_none(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=None), \
             mock.patch.object(market, "_fetch_af_daily_kline", return_value=None), \
             mock.patch.object(kline_source.AkshareSource, "fetch", return_value=None):
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 100)
        self.assertIsNone(df)
        self.assertIsNone(src)


class TestEnvSwitch(KlineSourceTestBase):
    def test_stock_switch_to_akshare(self):
        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare"), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertEqual(src, "akshare")
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        for col in ("open", "high", "low", "close", "volume", "amount"):
            self.assertIn(col, df.columns)
        fake.stock_zh_a_hist.assert_called_once()
        self.assertEqual(fake.stock_zh_a_hist.call_args.kwargs["period"], "daily")
        # tail(count) 生效
        self.assertEqual(len(df), 10)

    def test_minute_period_converted_to_em_format(self):
        fake = _fake_akshare(stock_min=_cn_minute_df())
        with _no_kline_env(KLINE_SOURCE_MINUTE="akshare"), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            df, src = kline_source.fetch_kline_df("minute", "600519.SH", "5m", 5)
        self.assertEqual(src, "akshare")
        self.assertEqual(fake.stock_zh_a_hist_min_em.call_args.kwargs["period"], "5")
        self.assertEqual(len(df), 5)

    def test_describe_chains_reflect_env(self):
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare,mairui"):
            chains = kline_source.describe_chains()
        self.assertEqual(chains["stock"], "akshare,mairui")
        self.assertEqual(chains["minute"], "alphafeed,akshare")


class TestFailover(KlineSourceTestBase):
    def test_mairui_failure_falls_to_alphafeed(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=None), \
             mock.patch.object(market, "_fetch_af_daily_kline",
                               return_value=_norm_df()) as af, \
             mock.patch.object(kline_source.AkshareSource, "fetch", return_value=None) as ak:
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 100)
        self.assertEqual(src, "alphafeed")
        self.assertIsNotNone(df)
        af.assert_called_once()
        ak.assert_not_called()  # 首个成功源即止, 不再下沉

    def test_falls_through_to_akshare(self):
        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=None), \
             mock.patch.object(market, "_fetch_af_daily_kline", return_value=None), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            _df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertEqual(src, "akshare")

    def test_exception_in_source_counts_as_failure(self):
        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_mr_kline",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(market, "_fetch_af_daily_kline", return_value=None), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            _df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertEqual(src, "akshare")

    def test_unsupported_source_skipped_without_request(self):
        # alphafeed 不支持指数 -> 即使排在前位也不该发起请求
        with _no_kline_env(KLINE_SOURCE_INDEX="alphafeed,mairui"), \
             mock.patch.object(market, "_is_index_symbol", return_value=True), \
             mock.patch.object(market, "_fetch_af_daily_kline") as af, \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()):
            _df, src = kline_source.fetch_kline_df("index", "000300.SH", "1d", 100)
        self.assertEqual(src, "mairui")
        af.assert_not_called()

    def test_mairui_not_used_for_1m(self):
        # 麦蕊 fsjy 不支持 1m (实测 HTTP 422), 1m 应直接落到 alphafeed
        with _no_kline_env(KLINE_SOURCE_MINUTE="mairui,alphafeed"), \
             mock.patch.object(market, "_fetch_mr_minute_kline") as mr, \
             mock.patch.object(market, "_fetch_minute_kline", return_value=_minute_df()):
            _df, src = kline_source.fetch_kline_df("minute", "600519.SH", "1m", 100)
        self.assertEqual(src, "alphafeed")
        mr.assert_not_called()

    def test_unknown_source_name_warns_and_skips(self):
        with _no_kline_env(KLINE_SOURCE_STOCK="bogus,mairui"), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()):
            with self.assertLogs("kline_source", level="WARNING") as logs:
                _df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 100)
        self.assertEqual(src, "mairui")
        self.assertTrue(any("bogus" in line for line in logs.output))


class TestMinuteStalenessGuard(KlineSourceTestBase):
    def test_stale_minute_data_rejected(self):
        stale_end = (market.market_hours.now() - pd.Timedelta(days=40)).strftime("%Y-%m-%d %H:%M")
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_minute_kline",
                               return_value=_minute_df(end=stale_end)):
            with self.assertLogs("kline_source", level="WARNING") as logs:
                df, src = kline_source.fetch_kline_df("minute", "600519.SH", "5m", 100)
        self.assertIsNone(df)
        self.assertIsNone(src)
        self.assertTrue(any("过旧" in line for line in logs.output))

    def test_fresh_minute_data_accepted(self):
        fresh_end = (market.market_hours.now() - pd.Timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_minute_kline",
                               return_value=_minute_df(end=fresh_end)) as af, \
             mock.patch.object(kline_source.AkshareSource, "fetch", return_value=None) as ak:
            df, src = kline_source.fetch_kline_df("minute", "600519.SH", "5m", 100)
        self.assertEqual(src, "alphafeed")
        self.assertIsNotNone(df)
        ak.assert_not_called()


class TestAkshareAdapter(KlineSourceTestBase):
    def test_fund_daily_volume_x100_to_gu(self):
        # 东财基金日K volume=「手」-> ×100 对齐麦蕊 jj/lskx「股」口径
        fake = _fake_akshare(fund_daily=_cn_daily_df())
        with _no_kline_env(KLINE_SOURCE_FUND="akshare"), \
             mock.patch.object(market, "_is_etf", return_value=True), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            df, _src = kline_source.fetch_kline_df("fund", "510300.SH", "1d", 10)
        self.assertAlmostEqual(df["volume"].iloc[-1], 8414655.0 * 100)
        fake.fund_etf_hist_em.assert_called_once()

    def test_stock_daily_volume_unchanged(self):
        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare"), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            df, _src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertAlmostEqual(df["volume"].iloc[-1], 8414655.0)

    def test_akshare_failure_returns_none(self):
        fake = _fake_akshare()
        fake.stock_zh_a_hist.side_effect = RuntimeError("限流")
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare"), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            df, _src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertIsNone(df)


class TestMairuiMinuteFetch(KlineSourceTestBase):
    """_fetch_mr_minute_kline: BJ/1m 直接拒绝; fsjy 字段映射 + tail。"""

    def test_bj_and_1m_rejected_without_http(self):
        self.assertIsNone(market._fetch_mr_minute_kline("833533.BJ", "5m", 100))
        self.assertIsNone(market._fetch_mr_minute_kline("600519.SH", "1m", 100))

    def test_fsjy_rows_mapped_and_tailed(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeUrlopenResp(_fsjy_rows())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            df = market._fetch_mr_minute_kline("600519.SH", "5m", 3)
        self.assertIn("/hszbl/fsjy/600519.SH/5m/", captured["url"])
        self.assertEqual(len(df), 3)  # tail(count)
        self.assertEqual(df.index.name, "trade_time")
        for col in ("open", "high", "low", "close", "volume", "amount"):
            self.assertIn(col, df.columns)
        for extra in ("zf", "hs", "zd", "zde", "ud"):
            self.assertNotIn(extra, df.columns)
        self.assertAlmostEqual(df["amount"].iloc[-1], 164359250.0)

    def test_error_response_returns_none(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeUrlopenResp({"error": "数据不存在"})):
            df = market._fetch_mr_minute_kline("600519.SH", "5m", 100)
        self.assertIsNone(df)


class TestFetchKlineEx(KlineSourceTestBase):
    """market.fetch_kline_ex: 三元组返回 + 磁盘缓存记录/复用 source。"""

    def setUp(self):
        super().setUp()
        self.disk = _FakeDisk()
        disk_p = mock.patch.object(market, "_disk_cache", self.disk)
        disk_p.start()
        self.addCleanup(disk_p.stop)

    def test_returns_source_and_records_cache(self):
        with mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()):
            df, name, src = market.fetch_kline_ex("600519.SH", "1w", 100)
        self.assertEqual(src, "mairui")
        self.assertEqual(name, "测试名")
        cached = self.disk.store[("600519.SH", "1w", 100)]
        self.assertEqual(cached["source"], "mairui")
        self.assertEqual(cached["name"], "测试名")

    def test_cache_hit_returns_cached_source(self):
        self.disk.store[("600519.SH", "1w", 100)] = {
            "name": "测试名", "source": "akshare",
            "data": json.loads(_norm_df(10).reset_index()
                               .rename(columns={"index": "trade_date"})
                               .to_json(orient="records", date_format="iso")),
        }
        with mock.patch.object(market, "_fetch_mr_kline") as mr:
            df, name, src = market.fetch_kline_ex("600519.SH", "1w", 100)
        self.assertEqual(src, "akshare")
        self.assertEqual(len(df), 10)
        mr.assert_not_called()  # 缓存命中不发起请求

    def test_fetch_kline_wrapper_drops_source(self):
        with mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()):
            result = market.fetch_kline("600519.SH", "1w", 100)
        self.assertEqual(len(result), 2)
        self.assertIsNotNone(result[0])

    def test_mairui_kline_error_rows_return_none(self):
        # dict = 麦蕊错误响应 (如 {"error": "数据不存在"}) -> None -> 回退
        with mock.patch.object(market, "get_mr", return_value=_FakeMr([{"error": "数据不存在"}])):
            df = market._fetch_mr_kline("600519.SH", "1d", 100)
        self.assertIsNone(df)

    def test_mairui_rows_normalized(self):
        with mock.patch.object(market, "get_mr", return_value=_FakeMr(_mr_rows())):
            df = market._fetch_mr_kline("600519.SH", "1d", 100)
        self.assertEqual(len(df), 10)
        self.assertAlmostEqual(df["volume"].iloc[-1], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
