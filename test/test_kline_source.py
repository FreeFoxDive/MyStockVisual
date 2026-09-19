"""kline_source 数据源包装层测试: 默认路由 / env 切换 / 自动回退 / 新鲜度守卫 / 列映射。

契约 (README「K线数据源配置」):
- 默认链 = 券商/付费源优先 (分钟→alphafeed, 股票/指数→mairui, 基金→alphafeed)
- 图表默认前复权 (forward); get_daily_bar 等传 none
- KLINE_SOURCE_{MINUTE,STOCK,INDEX,FUND} 逗号分隔链, 主源失败自动回退
- 分钟末根 bar 距今 > MINUTE_STALE_DAYS 天视为该源失败
- akshare 兜底: 中文列名映射; 基金日K volume 与 AF/快照同为「手」
- market.fetch_kline_ex 返回 (df, name, source), 磁盘缓存记录 source
  (key 含 qfq/raw 与数据源链 chain_tag, 改 KLINE_SOURCE_* 即失效)

运行:
    venv/Scripts/python.exe -u visual/test/test_kline_source.py
"""

import contextlib
import json
import os
import sys
import time
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
        self.last_div = None

    def stock_history(self, symbol, period, div, lt=None):
        self.last_div = div
        return self.rows

    def index_history(self, symbol, period, lt=None):
        return self.rows


class _FakeDisk:
    def __init__(self):
        self.store = {}

    def get(self, symbol, period, count, ttl, adjust="forward", chain_tag=""):
        return self.store.get(
            (symbol, period, count, kline_source.adjust_tag(adjust), chain_tag))

    def set(self, symbol, period, count, data, adjust="forward", chain_tag=""):
        self.store[(symbol, period, count, kline_source.adjust_tag(adjust), chain_tag)] = data


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
        # 源级熔断是进程级状态: 用例里大量"连造失败"的场景会污染后续用例的源链
        # (坏源进冷却就被跳过), 故每个用例前重置。
        kline_source.reset_health()
        # 隔离外部环境: 全套跑时 app 会加载真实 .env, 泄漏 KLINE_SOURCE_*,
        # 使依赖「默认链」的用例串扰。此处临时清空, 用例内可再用 _no_kline_env 覆盖。
        env_guard = mock.patch.dict(os.environ)
        env_guard.start()
        self.addCleanup(env_guard.stop)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        # 适配器内部走 market 属性访问, 统一屏蔽分类判定与名称查询
        for p in (
            mock.patch.object(market, "_lookup_name", return_value="测试名"),
            mock.patch.object(market, "_is_etf", return_value=False),
            mock.patch.object(market, "_is_index_symbol", return_value=False),
            # 熔断会推告警: 单个文件单跑时这套是唯一兜底 (discover 跑由 test_source_alert
            # 在模块级默认置 SOURCE_ALERT_DISABLED 兜底), 不打桩就会真发钉钉/ntfy。
            # 需要验证"确实通知了"的用例在自己的 with 里再 patch 一层即可。
            mock.patch.object(kline_source.source_alert, "notify", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)


class TestDefaultRouting(KlineSourceTestBase):
    """默认链: 分钟→alphafeed, 股票/指数→mairui, 基金→alphafeed。"""

    def test_stock_defaults_to_mairui(self):
        with _no_kline_env(), mock.patch.object(
                market, "_fetch_mr_kline", return_value=_norm_df()) as mr:
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 100)
        self.assertEqual(src, "mairui")
        self.assertEqual(len(df), 30)
        mr.assert_called_once_with("600519.SH", "1d", 100, adjust="forward")

    def test_minute_defaults_to_alphafeed(self):
        with _no_kline_env(), mock.patch.object(
                market, "_fetch_minute_kline", return_value=_minute_df()) as af:
            _df, src = kline_source.fetch_kline_df("minute", "600519.SH", "1m", 100)
        self.assertEqual(src, "alphafeed")
        af.assert_called_once_with("600519.SH", "1m", 100, adjust="forward")

    def test_fund_defaults_to_alphafeed(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_is_etf", return_value=True), \
             mock.patch.object(market, "_fetch_af_kline", return_value=_norm_df()) as af, \
             mock.patch.object(market, "_fetch_fund_kline") as mr_fund:
            _df, src = kline_source.fetch_kline_df("fund", "510300.SH", "1d", 100)
        self.assertEqual(src, "alphafeed")
        af.assert_called_once_with("510300.SH", "1d", 100, adjust="forward")
        mr_fund.assert_not_called()

    def test_stock_weekly_defaults_to_alphafeed(self):
        # AlphaFeed 原生支持周K: 股票周K不应下沉到抖动的 akshare
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_af_kline", return_value=_norm_df()) as af, \
             mock.patch.object(kline_source.AkshareSource, "fetch") as ak:
            df, src = kline_source.fetch_kline_df("stock", "688617.SH", "1w", 200)
        self.assertEqual(src, "alphafeed")
        self.assertEqual(len(df), 30)
        af.assert_called_once_with("688617.SH", "1w", 200, adjust="forward")
        ak.assert_not_called()

    def test_index_defaults_to_mairui(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_is_index_symbol", return_value=True), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()) as mr:
            _df, src = kline_source.fetch_kline_df("index", "000300.SH", "1w", 100)
        self.assertEqual(src, "mairui")
        mr.assert_called_once_with("000300.SH", "1w", 100, adjust="forward")

    def test_all_sources_fail_returns_none(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=None), \
             mock.patch.object(market, "_fetch_af_kline", return_value=None), \
             mock.patch.object(kline_source.AkshareSource, "fetch", return_value=None):
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 100)
        self.assertIsNone(df)
        self.assertIsNone(src)


class TestAdjustRouting(KlineSourceTestBase):
    def test_mairui_stock_uses_fr_for_forward(self):
        fake = _FakeMr(_mr_rows())
        with _no_kline_env(), mock.patch.object(market, "get_mr", return_value=fake):
            df = market._fetch_mr_kline("600519.SH", "1d", 100, adjust="forward")
        self.assertIsNotNone(df)
        self.assertEqual(fake.last_div, "fr")

    def test_mairui_stock_uses_n_for_none(self):
        fake = _FakeMr(_mr_rows())
        with _no_kline_env(), mock.patch.object(market, "get_mr", return_value=fake):
            df = market._fetch_mr_kline("600519.SH", "1d", 100, adjust="none")
        self.assertIsNotNone(df)
        self.assertEqual(fake.last_div, "n")

    def test_mairui_fund_skipped_on_forward(self):
        with _no_kline_env(KLINE_SOURCE_FUND="mairui,alphafeed"), \
             mock.patch.object(market, "_is_etf", return_value=True), \
             mock.patch.object(market, "_fetch_fund_kline") as mr_fund, \
             mock.patch.object(market, "_fetch_af_kline", return_value=_norm_df()) as af:
            _df, src = kline_source.fetch_kline_df(
                "fund", "588200.SH", "1d", 100, adjust="forward")
        self.assertEqual(src, "alphafeed")
        mr_fund.assert_not_called()
        af.assert_called_once()

    def test_mairui_fund_allowed_on_none(self):
        with _no_kline_env(KLINE_SOURCE_FUND="mairui,alphafeed"), \
             mock.patch.object(market, "_is_etf", return_value=True), \
             mock.patch.object(market, "_fetch_fund_kline", return_value=_norm_df()) as mr_fund:
            _df, src = kline_source.fetch_kline_df(
                "fund", "510300.SH", "1d", 100, adjust="none")
        self.assertEqual(src, "mairui")
        mr_fund.assert_called_once()

    def test_akshare_passes_qfq(self):
        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare"), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10, adjust="forward")
        self.assertEqual(fake.stock_zh_a_hist.call_args.kwargs["adjust"], "qfq")

    def test_akshare_passes_empty_for_none(self):
        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare"), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10, adjust="none")
        self.assertEqual(fake.stock_zh_a_hist.call_args.kwargs["adjust"], "")


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
        self.assertEqual(fake.stock_zh_a_hist_min_em.call_args.kwargs["adjust"], "qfq")
        self.assertEqual(len(df), 5)

    def test_describe_chains_reflect_env(self):
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare,mairui"):
            chains = kline_source.describe_chains()
        self.assertEqual(chains["stock"], "akshare,mairui")
        self.assertEqual(chains["minute"], "alphafeed,akshare")
        self.assertEqual(chains["fund"], "alphafeed,akshare")


class TestChainTag(unittest.TestCase):
    """chain_tag: 磁盘缓存 key 的数据源链标识, 改链即变。"""

    def test_chain_tag_reflects_env_and_is_stable(self):
        with _no_kline_env():
            default_tag = kline_source.chain_tag("stock")
            self.assertEqual(default_tag, kline_source.chain_tag("stock"))
        with _no_kline_env(KLINE_SOURCE_STOCK="alphafeed,akshare"):
            new_tag = kline_source.chain_tag("stock")
        self.assertNotEqual(default_tag, new_tag)
        self.assertEqual(len(new_tag), 8)

    def test_disk_cache_key_includes_chain_tag(self):
        disk = market.DiskCache()
        a = disk._key("601058.SH", "1d", 1006, "forward", "t1")
        b = disk._key("601058.SH", "1d", 1006, "forward", "t2")
        raw = disk._key("601058.SH", "1d", 1006, "none", "t1")
        self.assertNotEqual(a, b)
        self.assertIn("t1", a.name)
        self.assertIn("raw", raw.name)
        self.assertNotEqual(a, raw)

    def test_disk_cache_isolates_across_chain_tag(self):
        disk = market.DiskCache()
        disk.set("ZZTEST.SH", "1d", 7, {"source": "a"}, adjust="forward",
                 chain_tag="t1")
        try:
            self.assertIsNotNone(
                disk.get("ZZTEST.SH", "1d", 7, 60, adjust="forward",
                         chain_tag="t1"))
            self.assertIsNone(
                disk.get("ZZTEST.SH", "1d", 7, 60, adjust="forward",
                         chain_tag="t2"))
        finally:
            disk._key("ZZTEST.SH", "1d", 7, "forward", "t1").unlink(
                missing_ok=True)


class TestFailover(KlineSourceTestBase):
    def test_mairui_failure_falls_to_alphafeed(self):
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=None), \
             mock.patch.object(market, "_fetch_af_kline",
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
             mock.patch.object(market, "_fetch_af_kline", return_value=None), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            _df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertEqual(src, "akshare")

    def test_exception_in_source_counts_as_failure(self):
        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_mr_kline",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(market, "_fetch_af_kline", return_value=None), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            _df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertEqual(src, "akshare")

    def test_unsupported_source_skipped_without_request(self):
        # alphafeed 不支持指数 -> 即使排在前位也不该发起请求
        with _no_kline_env(KLINE_SOURCE_INDEX="alphafeed,mairui"), \
             mock.patch.object(market, "_is_index_symbol", return_value=True), \
             mock.patch.object(market, "_fetch_af_kline") as af, \
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


class TestSourceTimeoutAndBreaker(KlineSourceTestBase):
    """硬超时与源级熔断: 坏源不得把每次切换都拖住, 也不得让请求无谓 404。"""

    def test_hanging_source_times_out_and_falls_through(self):
        """akshare 无内建超时: 挂起时应按超时判失败并下沉, 而非一直等。"""
        def _hang(*_a, **_k):
            time.sleep(0.6)
            return _cn_daily_df()

        fake = _fake_akshare(stock_daily=_cn_daily_df())
        with _no_kline_env(KLINE_SOURCE_STOCK="akshare,mairui"), \
             mock.patch.object(kline_source, "SOURCE_TIMEOUT_SEC", 0.1), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()) as mr, \
             mock.patch.object(kline_source.AkshareSource, "fetch", side_effect=_hang):
            t0 = time.monotonic()
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
            elapsed = time.monotonic() - t0
        self.assertEqual(src, "mairui")
        self.assertIsNotNone(df)
        self.assertLess(elapsed, 0.5, "超时未生效: 请求被挂起源拖住")
        mr.assert_called_once()

    def test_breaker_skips_source_after_consecutive_failures(self):
        """连续失败达阈值后进入冷却: 不再对该源发起请求。"""
        with _no_kline_env(KLINE_SOURCE_STOCK="alphafeed,mairui"), \
             mock.patch.object(kline_source, "SOURCE_FAIL_THRESHOLD", 2), \
             mock.patch.object(market, "_fetch_af_kline", return_value=None) as af, \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()):
            for _ in range(2):
                _df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
                self.assertEqual(src, "mairui")
            self.assertEqual(af.call_count, 2)
            self.assertTrue(kline_source._in_cooldown("alphafeed"))
            af.reset_mock()
            # 第三次: alphafeed 在冷却中被跳过, 一次请求都不该发
            _df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
            self.assertEqual(src, "mairui")
            af.assert_not_called()

    def test_success_resets_failure_counter(self):
        """成功一次即清零连续计数, 偶发抖动不该累积成熔断。"""
        with _no_kline_env(KLINE_SOURCE_STOCK="alphafeed,mairui"), \
             mock.patch.object(kline_source, "SOURCE_FAIL_THRESHOLD", 2), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=None), \
             mock.patch.object(market, "_fetch_af_kline",
                               side_effect=[None, _norm_df(), None]):
            kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)  # af 失败
            kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)  # af 成功 → 清零
            kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)  # af 失败(计数=1)
        self.assertFalse(kline_source._in_cooldown("alphafeed"),
                         "成功后应清零, 不该熔断")

    def test_source_skip_is_not_counted_as_failure(self):
        """能力缺口 (SourceSkip) 不计源故障: 别的市场没权限/别的标的没数据, 不该把这个
        源在**有数据**的市场上一并冷却 —— 分时链只有这一个源, 冷却它等于全市场 404。"""
        def _skip(*_a, **_k):
            raise kline_source.SourceSkip("本市场无此功能")

        with _no_kline_env(KLINE_SOURCE_STOCK="alphafeed,mairui"), \
             mock.patch.object(kline_source, "SOURCE_FAIL_THRESHOLD", 2), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()), \
             mock.patch.object(kline_source.AlphaFeedSource, "fetch", side_effect=_skip) as af:
            for _ in range(4):        # 远超阈值
                _df, src = kline_source.fetch_kline_df("stock", "AAPL.US", "1d", 10)
                self.assertEqual(src, "mairui", "SourceSkip 后应正常下沉到下一源")
            self.assertEqual(af.call_count, 4, "跳过不是故障, 不该进冷却而拒绝再试")
            self.assertFalse(kline_source._in_cooldown("alphafeed"),
                             "能力缺口把源打进冷却 → 有数据的市场会跟着 404")

    def test_cooldown_notifies_once_per_source_per_day(self):
        """进冷却要推一条告警 (带最后失败的标的/周期), 且同一源当天只推一条。

        回归点: 源熔断原来只在日志里, 没人盯日志就等于没发生; 而"冷却→probe 失败→再冷却"
        能反复触发, 所以必须有当日闸门, 否则一次故障会刷一整天通知。
        """
        seen = []
        with _no_kline_env(KLINE_SOURCE_STOCK="alphafeed,mairui"), \
             mock.patch.object(kline_source, "SOURCE_FAIL_THRESHOLD", 2), \
             mock.patch.object(market, "_fetch_af_kline", return_value=None), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()), \
             mock.patch.object(kline_source.source_alert, "notify",
                               side_effect=lambda src, reason, **kw: (
                                   seen.append((src, reason, kw)), True)[1]):
            kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)   # 失败 1
            self.assertEqual(seen, [], "没到阈值不该告警")
            for _ in range(4):                                            # 触发 + 反复再触发
                kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertEqual(len(seen), 1, "同一个源当天只该推一条")
        src, reason, kw = seen[0]
        self.assertEqual(src, "alphafeed")
        self.assertIn("连续失败", reason)
        self.assertEqual(kw.get("detail"), "600519.SH 1d (stock)",
                         "要带最后失败的那次请求的标的与周期")

    def test_all_sources_in_cooldown_does_not_bypass(self):
        """全部冷却也不得无条件突破熔断。"""
        with _no_kline_env(KLINE_SOURCE_STOCK="alphafeed,mairui"), \
             mock.patch.object(market, "_fetch_mr_kline", return_value=None), \
             mock.patch.object(market, "_fetch_af_kline",
                               return_value=_norm_df()) as af:
            kline_source._note_fail("alphafeed")
            kline_source._note_fail("alphafeed")
            kline_source._note_fail("alphafeed")
            kline_source._note_fail("mairui")
            kline_source._note_fail("mairui")
            kline_source._note_fail("mairui")
            self.assertTrue(kline_source._in_cooldown("alphafeed"))
            self.assertTrue(kline_source._in_cooldown("mairui"))
            df, src = kline_source.fetch_kline_df("stock", "600519.SH", "1d", 10)
        self.assertIsNone(src)
        self.assertIsNone(df)
        af.assert_not_called()


class TestMinuteStalenessGuard(KlineSourceTestBase):
    def test_stale_minute_data_rejected(self):
        stale_end = (market.market_hours.now() - pd.Timedelta(days=40)).strftime("%Y-%m-%d %H:%M")
        # 必须 mock 兜底源: 否则过旧的 alphafeed 被拒后链路会真实访问 akshare,
        # CI 有网时取到新鲜数据 → 断言失败 (用例本意是验证新鲜度守卫)。
        with _no_kline_env(), \
             mock.patch.object(market, "_fetch_minute_kline",
                               return_value=_minute_df(end=stale_end)), \
             mock.patch.object(kline_source.AkshareSource, "fetch", return_value=None):
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
    def test_fund_daily_volume_stays_shou(self):
        # 基金默认 AF/东财同为「手」, 不再 ×100
        fake = _fake_akshare(fund_daily=_cn_daily_df())
        with _no_kline_env(KLINE_SOURCE_FUND="akshare"), \
             mock.patch.object(market, "_is_etf", return_value=True), \
             mock.patch.dict(sys.modules, {"akshare": fake}):
            df, _src = kline_source.fetch_kline_df("fund", "510300.SH", "1d", 10)
        self.assertAlmostEqual(df["volume"].iloc[-1], 8414655.0)
        fake.fund_etf_hist_em.assert_called_once()
        self.assertEqual(fake.fund_etf_hist_em.call_args.kwargs["adjust"], "qfq")

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
        # chain_tag 固定为 "t1", 便于断言磁盘缓存 key
        ct_p = mock.patch.object(kline_source, "chain_tag", return_value="t1")
        ct_p.start()
        self.addCleanup(ct_p.stop)

    def test_returns_source_and_records_cache(self):
        with mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()):
            df, name, src = market.fetch_kline_ex("600519.SH", "1w", 100)
        self.assertEqual(src, "mairui")
        self.assertEqual(name, "测试名")
        cached = self.disk.store[("600519.SH", "1w", 100, "qfq", "t1")]
        self.assertEqual(cached["source"], "mairui")
        self.assertEqual(cached["name"], "测试名")

    def test_cache_hit_returns_cached_source(self):
        self.disk.store[("600519.SH", "1w", 100, "qfq", "t1")] = {
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

    def test_raw_and_qfq_cache_separated(self):
        with mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()) as mr:
            market.fetch_kline_ex("600519.SH", "1w", 100, adjust="forward")
            market.fetch_kline_ex("600519.SH", "1w", 100, adjust="none")
        self.assertIn(("600519.SH", "1w", 100, "qfq", "t1"), self.disk.store)
        self.assertIn(("600519.SH", "1w", 100, "raw", "t1"), self.disk.store)
        self.assertEqual(mr.call_count, 2)

    def test_chain_tag_change_invalidates_cache(self):
        """切数据源链 (chain_tag 变) 后旧缓存不再命中, 重新发起 fetch。"""
        with mock.patch.object(market, "_fetch_mr_kline", return_value=_norm_df()) as mr:
            market.fetch_kline_ex("600519.SH", "1w", 100)  # 写入 t1
            with mock.patch.object(kline_source, "chain_tag", return_value="t2"):
                df, name, src = market.fetch_kline_ex("600519.SH", "1w", 100)
        self.assertIsNotNone(df)
        self.assertEqual(mr.call_count, 2)  # t2 未命中 t1 缓存
        self.assertIn(("600519.SH", "1w", 100, "qfq", "t1"), self.disk.store)
        self.assertIn(("600519.SH", "1w", 100, "qfq", "t2"), self.disk.store)

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
