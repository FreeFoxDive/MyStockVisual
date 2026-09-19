# -*- coding: utf-8 -*-
"""分时取数: 日内走势接口优先 + 权限失败当日熔断的单测。

契约核心 (分时图/监控补种的数据来源):
  * 有权限时走 `/v1/klines/intraday`, 不碰分钟K批量;
  * 权限被拒 (401/402/403 或 code 提示) → 本次回退分钟K批量, 且**当日**不再尝试;
  * 其它失败 (异常/空数据/额度桶空) 只本次回退, 下一次仍试 —— 偶发失败与
    "开盘前没数据" 不该把一整天降级成原接口;
  * 日内走势额度按 af_limits 折算 (60 × 0.9 = 54/min), 与「分钟K批量 30/min」独立;
  * 1m/5m/15m/30m/60m 分钟K视图 (跨天历史) 绝不能被换成只回当日的接口。

运行:
    venv/Scripts/python.exe -u visual/test/test_intraday_source.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import af_intraday  # noqa: E402
import af_limits  # noqa: E402
import feed as feed_mod  # noqa: E402
import kline_source  # noqa: E402
import market as market_mod  # noqa: E402

DAY = "2026-09-18"
SYM = "002472.SZ"


def _sdk_df(n=6, day=DAY):
    """仿 AlphaFeed `to_dataframe=True` 的分钟 df (trade_time 列 + OHLCV/amount)。

    行数默认 6 —— `market._normalize` 的门槛是 ≥5 根, 少于此按"无数据"处理
    (开盘头几分钟的 1~4 根就是这条路, 见 test_few_bars_falls_back)。
    """
    times = pd.date_range(f"{day} 09:31", periods=n, freq="min")
    close = [10.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame({
        "symbol": SYM,
        "trade_date": [t.strftime("%Y-%m-%d") for t in times],
        "trade_time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
        "open": close,
        "high": [c + 0.05 for c in close],
        "low": [c - 0.05 for c in close],
        "close": close,
        "volume": [1000.0] * n,
        "amount": [c * 1000.0 for c in close],
    })


class _FakeKlines:
    """记录调用序列的假 klines 资源。"""

    def __init__(self, intraday=None, intraday_exc=None, batch=None, batch_exc=None):
        self.intraday_result = intraday
        self.intraday_exc = intraday_exc
        self.batch_result = batch
        self.batch_exc = batch_exc
        self.calls = []

    def intraday(self, symbol, *, period=None, count=None, to_dataframe=False):
        self.calls.append(("intraday", symbol, period, count))
        if self.intraday_exc is not None:
            raise self.intraday_exc
        return self.intraday_result

    def batch(self, symbols, *, period=None, count=None, adjust=None, to_dataframe=False):
        self.calls.append(("batch", tuple(symbols), period, count, adjust))
        if self.batch_exc is not None:
            raise self.batch_exc
        sym = list(symbols)[0]
        return {sym: self.batch_result} if self.batch_result is not None else {}

    @property
    def names(self):
        return [c[0] for c in self.calls]


class _Denied(Exception):
    """仿 alphafeed.PermissionError: 带 status_code / code。"""

    def __init__(self, msg="plan does not include intraday", status_code=403,
                 code="PLAN_REQUIRED"):
        super().__init__(msg)
        self.status_code = status_code
        self.code = code


class _Budget:
    """可控的令牌桶替身 (真 PacedBudget 有最小间隔, 会把"第二次调用"拦成桶空)。"""

    def __init__(self, grant=True):
        self.grant = grant
        self.tries = 0

    def try_acquire(self, n=1):
        self.tries += 1
        return self.grant


class _Base(unittest.TestCase):
    def setUp(self):
        af_intraday.reset()
        self.i_df = _sdk_df()
        self.b_df = _sdk_df(n=240)
        self.kl = _FakeKlines(intraday=self.i_df, batch=self.b_df)
        self.af = mock.Mock(klines=self.kl)
        self.budget = _Budget()
        self._patchers = [
            mock.patch.object(market_mod, "get_af", return_value=self.af),
            mock.patch.object(market_mod, "_intraday_budget", self.budget),
            # 港/美股令牌桶是进程级共享的 (容量 8/min): 用例之间会互相把桶抽干, 让港/美股
            # 用例变成"看跑的顺序决定过不过"。这里换成恒放行, 与本文件要测的熔断无关。
            mock.patch.object(market_mod, "_hkus_kline_bucket", _Budget()),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, symbol=SYM, period="1m", count=240):
        return market_mod._fetch_intraday_kline(symbol, period, count)


class TestIntrodayPreference(_Base):
    def test_uses_intraday_and_skips_batch(self):
        """有权限: 走日内走势接口, 一根都不碰分钟K批量。"""
        df = self.fetch()
        self.assertIsNotNone(df)
        self.assertEqual(self.kl.names, ["intraday"])
        self.assertEqual(len(df), 6)
        self.assertEqual(self.kl.calls[0][2:], ("1m", 240))

    def test_few_bars_falls_back_for_normalize_threshold(self):
        """开盘头几分钟只有 1~4 根: 退回原接口 (它带前一日尾巴, 能过 _normalize 门槛)。"""
        self.kl.intraday_result = _sdk_df(n=3)
        df = self.fetch()
        self.assertEqual(self.kl.names, ["intraday", "batch"])
        self.assertIsNotNone(df)

    def test_empty_intraday_falls_back_without_latch(self):
        """空数据 (开盘前/停牌) 只本次回退: 不熔断, 下一次仍然试日内走势。"""
        self.kl.intraday_result = _sdk_df(n=0)
        df = self.fetch()
        self.assertEqual(self.kl.names, ["intraday", "batch"])
        self.assertIsNotNone(df)

        self.kl.calls.clear()
        self.fetch()
        self.assertEqual(self.kl.names, ["intraday", "batch"], "空数据不该封掉日内走势")

    def test_generic_error_falls_back_without_latch(self):
        """超时/网络类异常只本次回退 —— 把一整天都降级代价太大。"""
        self.kl.intraday_exc = TimeoutError("read timeout")
        df = self.fetch()
        self.assertEqual(self.kl.names, ["intraday", "batch"])
        self.assertIsNotNone(df)

        self.kl.calls.clear()
        self.fetch()
        self.assertEqual(self.kl.names[0], "intraday", "普通异常不该当天封接口")

    def test_batch_failure_returns_none(self):
        """两条路都失败 → None (路由层 404), 不抛异常。"""
        self.kl.intraday_exc = TimeoutError("nope")
        self.kl.batch_exc = RuntimeError("upstream down")
        self.assertIsNone(self.fetch())


class TestPermissionLatch(_Base):
    def test_denied_falls_back_and_stops_for_the_day(self):
        """403: 本次回退原接口, 当日不再重试 (第二次调用连 intraday 都不发起)。"""
        self.kl.intraday_exc = _Denied()
        df = self.fetch()
        self.assertEqual(self.kl.names, ["intraday", "batch"])
        self.assertIsNotNone(df)
        self.assertFalse(af_intraday.available(SYM))
        self.assertEqual(af_intraday.denied_markets(), {"cn": af_intraday._today()})

        self.kl.calls.clear()
        self.kl.intraday_exc = None          # 就算上游恢复了, 当日也不该再问
        self.fetch()
        self.assertEqual(self.kl.names, ["batch"], "权限被拒后当日不该再试日内走势")

    def test_latch_is_per_market(self):
        """熔断按市场记: 一个市场被拒不得影响另外两个。

        实测本套餐权限按市场授权 (A股有日内分时, HK/US 403), 所以自选表里带一只美股
        是常态 —— 全局熔断会让 A股 当天全部退回分钟K批量, 正是要避免的。
        三个市场两两各验一遍 (谁被拒 → 谁仍可用)。
        """
        cases = [("AAPL.US", ["600519.SH", "00700.HK"]),
                 ("00700.HK", ["600519.SH", "AAPL.US"]),
                 ("600519.SH", ["00700.HK", "AAPL.US"])]
        for denied, still_ok in cases:
            with self.subTest(denied=denied):
                af_intraday.reset()
                self.kl.calls.clear()
                self.kl.intraday_exc = _Denied(
                    msg=f"No permission for 日内分时查询 (markets: {denied[-2:]})")
                self.fetch(symbol=denied)
                self.assertFalse(af_intraday.available(denied))
                for sym in still_ok:
                    self.assertTrue(af_intraday.available(sym),
                                    f"{denied} 被拒不该影响 {sym}")
                self.kl.calls.clear()
                self.kl.intraday_exc = None
                df = self.fetch(symbol=still_ok[0])
                self.assertEqual(self.kl.names, ["intraday"], f"{still_ok[0]} 仍走日内走势")
                self.assertIsNotNone(df)

    def test_latch_per_market_recovers_next_day(self):
        """跨日恢复也按市场各算一遍: 昨日被拒的市场, 次日应当重新尝试。"""
        self.kl.intraday_exc = _Denied()
        for sym in ("AAPL.US", "00700.HK", "600519.SH"):
            self.fetch(symbol=sym)
            self.assertFalse(af_intraday.available(sym))
        tomorrow = af_intraday._today() + pd.Timedelta(days=1).to_pytimedelta()
        with mock.patch.object(af_intraday, "_today", return_value=tomorrow):
            for sym in ("AAPL.US", "00700.HK", "600519.SH"):
                self.assertTrue(af_intraday.available(sym), f"{sym} 跨日必须恢复尝试")

    def test_market_of_matches_market_module(self):
        """本模块不能反向 import market, 归类口径必须与 market._symbol_market 一致。"""
        for sym, want in (("600519.SH", "cn"), ("002472.SZ", "cn"), ("430047.BJ", "cn"),
                          ("000001.SH", "cn"), ("00700.HK", "hk"), ("00001.HK", "hk"),
                          ("AAPL.US", "us"), ("AAPL", "us"), ("", "cn")):
            self.assertEqual(af_intraday.market_of(sym), want, sym)
            self.assertEqual(af_intraday.market_of(sym), market_mod._symbol_market(sym), sym)

    def test_auth_error_also_latches(self):
        """401/402 与 403 同类: 都是套餐/权限层面的不可用。"""
        self.kl.intraday_exc = _Denied(status_code=401, code="UNAUTHORIZED")
        self.fetch()
        self.assertFalse(af_intraday.available(SYM))

    def test_latch_expires_next_day(self):
        """熔断只针对「当日」: 跨日自动恢复 (否则等于永久降级)。"""
        self.kl.intraday_exc = _Denied()
        self.fetch()
        self.assertFalse(af_intraday.available(SYM))

        tomorrow = af_intraday._today() + pd.Timedelta(days=1).to_pytimedelta()
        with mock.patch.object(af_intraday, "_today", return_value=tomorrow):
            self.assertTrue(af_intraday.available(SYM), "跨日必须恢复尝试")
            self.kl.calls.clear()
            self.kl.intraday_exc = None
            self.fetch()
            self.assertEqual(self.kl.names, ["intraday"])

    def test_note_denied_is_idempotent(self):
        """同一市场同一交易日只记一次 (日志不刷屏)。"""
        self.assertTrue(af_intraday.note_denied(_Denied(), SYM))
        self.assertFalse(af_intraday.note_denied(_Denied(), SYM))
        self.assertTrue(af_intraday.note_denied(_Denied(), "AAPL.US"), "另一个市场另记一次")

    def test_rate_limit_error_does_not_latch(self):
        """429 是限流不是权限: 本次回退即可, 不能当日封接口。"""
        self.kl.intraday_exc = _Denied(msg="Rate limit exceeded (60/min)",
                                       status_code=429, code="RATE_LIMITED")
        self.fetch()
        self.assertTrue(af_intraday.available(SYM))


class TestIntradayBudget(_Base):
    def test_budget_exhausted_skips_intraday(self):
        """额度桶空 → 本次直接用原接口, 且不熔断。"""
        self.budget.grant = False
        df = self.fetch()
        self.assertEqual(self.kl.names, ["batch"])
        self.assertIsNotNone(df)
        self.assertTrue(af_intraday.available(SYM))

    def test_budget_rate_is_ninety_percent_of_limit(self):
        """新增取数路径按 af_limits 折算: 日内走势 60/min × 0.9 = 54/min。"""
        self.assertEqual(af_limits.bucket_rate("intraday_symbol"), 54)
        self.assertEqual(af_limits.limit("intraday_symbol"), 60)

    def test_real_budget_paces_calls(self):
        """真 PacedBudget 有最小间隔 (60/54 ≈ 1.11s): 连续两次调用第二次落到原接口。"""
        from live_budget import PacedBudget
        with mock.patch.object(market_mod, "_intraday_budget", PacedBudget(54)):
            self.fetch()
            self.kl.calls.clear()
            self.fetch()
            self.assertEqual(self.kl.names, ["batch"], "无间隔连击不得连发上游请求")


class TestMinuteViewsUntouched(_Base):
    def test_minute_kline_path_never_calls_intraday(self):
        """分钟K视图 (跨天历史) 走的还是 klines.batch —— 换接口会把它们切成只有当日。"""
        df = market_mod._fetch_minute_kline(SYM, "1m", 1200)
        self.assertEqual(self.kl.names, ["batch"])
        self.assertEqual(self.kl.calls[0][2], "1m")
        self.assertIsNotNone(df)

    def test_intraday_category_source_only_serves_intraday(self):
        src = kline_source.SOURCES["alphafeed_intraday"]
        self.assertTrue(src.supports("intraday", "1m"))
        self.assertFalse(src.supports("minute", "1m"), "分时源不得顶替分钟K类别")
        self.assertFalse(src.supports("intraday", "1d"))

    def test_default_intraday_chain_is_af_only(self):
        """默认分时链只有日内走势源: 不兜 akshare (实测其分钟数据不稳, 且是静默换源)。

        "minute" 那条链仍带 akshare —— 两者是不同类别、不同取舍, 别被顺手合并。
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KLINE_SOURCE_INTRADAY", None)
            os.environ.pop("KLINE_SOURCE_MINUTE", None)
            self.assertEqual(kline_source._chain("intraday"), ["alphafeed_intraday"])
            self.assertNotIn("akshare", kline_source.DEFAULT_CHAINS["intraday"])
            self.assertEqual(kline_source._chain("minute"), ["alphafeed", "akshare"])

    def test_intraday_chain_configurable_to_akshare(self):
        """可配置: 显式配 akshare 兜底时链里就有它 (默认不含 ≠ 不能用)。"""
        with mock.patch.dict(os.environ,
                             {"KLINE_SOURCE_INTRADAY": "alphafeed_intraday,akshare"}):
            self.assertEqual(kline_source._chain("intraday"),
                             ["alphafeed_intraday", "akshare"])

    def test_akshare_covers_intraday_category(self):
        """akshare 能服务 intraday 类别 (分钟接口返回多日, 路由按当日截断, 口径不变)。

        注意这是**能力**不是默认行为: 默认链里没有它, 要显式配。
        """
        self.assertTrue(kline_source.SOURCES["akshare"].supports("intraday", "1m"))

    def test_bad_intraday_env_name_falls_back_to_default(self):
        """env 源名写错要退回默认链, 不能变成空链 —— 单源默认下空链等于分时全 404。"""
        with mock.patch.dict(os.environ, {"KLINE_SOURCE_INTRADAY": "nosuchsrc"}):
            self.assertEqual(kline_source._chain("intraday"), ["alphafeed_intraday"])


class TestPermissionErrorDetection(unittest.TestCase):
    def test_status_codes(self):
        for code in (401, 402, 403):
            self.assertTrue(af_intraday.is_permission_error(_Denied(status_code=code)))
        # status 明确时以 status 为准: 限流/缺资源/上游错误都不能升级成"当日不再尝试"
        for code in (400, 404, 429, 500):
            self.assertFalse(af_intraday.is_permission_error(
                _Denied(status_code=code, code="RATE_LIMITED")))

    def test_code_and_text_hints_when_status_missing(self):
        self.assertTrue(af_intraday.is_permission_error(
            _Denied(status_code=None, code="FORBIDDEN")))
        self.assertTrue(af_intraday.is_permission_error(
            _Denied(status_code=None, code=None)))
        self.assertFalse(af_intraday.is_permission_error(
            Exception("connection reset by peer")))

    def test_real_sdk_exceptions(self):
        """用 SDK 真异常对象钉住映射 (403 → PermissionError, 429 → RateLimitError)。"""
        try:
            from alphafeed import AuthenticationError, NotFoundError
            from alphafeed import PermissionError as AfPermissionError
            from alphafeed import RateLimitError
        except Exception as e:  # pragma: no cover - SDK 未安装时跳过
            self.skipTest(f"alphafeed SDK 不可用: {e}")
        self.assertTrue(af_intraday.is_permission_error(
            AfPermissionError("plan", code="C", status_code=403)))
        self.assertTrue(af_intraday.is_permission_error(
            AuthenticationError("auth", code="C", status_code=401)))
        self.assertFalse(af_intraday.is_permission_error(
            RateLimitError("slow down", code="C", status_code=429)))
        self.assertFalse(af_intraday.is_permission_error(
            NotFoundError("missing", code="C", status_code=404)))


class TestCapabilityGapVsFailure(_Base):
    """「能力缺口」必须抛 SourceSkip, 「上游故障」才返回 None 记故障。

    分时默认链只有 alphafeed_intraday 一个源, 返回 None 会被路由记成源故障、连记 3 次
    整源冷却 60s, 而冷却与市场无关 —— 港/美股 403(常态) 或某个当日无数据的代码, 就能把
    A股 的分时一起打成 404。判据: 上游**答了**(200, 哪怕 0 根) 或明确没权限 = 能力缺口。
    """

    def test_latched_market_without_data_raises_skip(self):
        """已熔断的市场 + 原接口也取不到 (港/美股两条路都 403) → SourceSkip。"""
        af_intraday.note_denied(_Denied(msg="no permission (markets: US)"), "AAPL.US")
        self.af.klines.batch = mock.Mock(return_value={})   # 原接口答复但没数据
        with self.assertRaises(kline_source.SourceSkip) as cm:
            self.fetch(symbol="AAPL.US")
        self.assertIn("无日内走势权限", str(cm.exception))
        self.assertTrue(af_intraday.available("600519.SH"), "A股 不该被连坐")

    def test_no_data_for_symbol_raises_skip(self):
        """未熔断, 但上游两个接口都"答复了、只是没数据" → 也是能力缺口, 不是故障。"""
        self.kl.intraday_result = _sdk_df(n=0)         # 200 但 0 根
        self.af.klines.batch = mock.Mock(return_value={})
        with self.assertRaises(kline_source.SourceSkip) as cm:
            self.fetch()
        self.assertIn("无数据", str(cm.exception))

    def test_upstream_error_is_a_real_failure(self):
        """上游真出错/超时 → 返回 None (路由据此记故障, 该源该冷却)。"""
        self.kl.intraday_exc = TimeoutError("read timeout")
        self.kl.batch_exc = TimeoutError("read timeout")
        self.assertIsNone(self.fetch(), "上游故障要返回 None, 不能当成能力缺口")

    def test_market_permission_denial_still_uses_the_old_interface(self):
        """A股 被熔断时原接口还能给数据 → 正常返回, 不抛 (「权限有问题就退原来的接口」)。"""
        af_intraday.note_denied(_Denied(), SYM)
        self.kl.batch_result = self.b_df              # 原接口有数据
        self.kl.intraday_exc = AssertionError("不该再试日内走势")
        df = self.fetch()
        self.assertIsNotNone(df)
        self.assertEqual(self.kl.names, ["batch"])


class TestSeedIntradaySharesLatch(_Base):
    """监控补种与分时图共用同一份按市场熔断 (否则监控每轮都撞一次 403)。"""

    def _feed(self):
        return feed_mod.RestFeed(lambda: self.af)

    def test_seed_falls_back_when_denied(self):
        self.af.klines.intraday_batch = mock.Mock(side_effect=_Denied())
        self.af.klines.batch = mock.Mock(return_value={SYM: self.b_df})

        out = self._feed().seed_intraday([SYM])
        self.assertTrue(self.af.klines.intraday_batch.called)
        self.assertTrue(self.af.klines.batch.called, "被拒后要退回分钟K批量")
        self.assertFalse(af_intraday.available(SYM))
        self.assertTrue(out, "回退路径必须真的补出样本")

    def test_seed_mixed_markets_does_not_blame_cn(self):
        """混一只没权限的港/美股时, **不能**把 A股 也标记成当日不可用。

        回归点: `intraday_batch` 是一次 HTTP 带多只 (≤100 只时单分片异常原样抛出),
        混了没权限的市场就整批 403 —— 早先的实现"一批被拒 → 给 pending 里每只都
        note_denied", 于是自选表里加一只美股就把 A股 的日内走势偏好一起关掉, 与
        README 承诺相反。现在按市场分组请求, 只有真正被拒的那组被标记。
        """
        def _per_market(syms, **_kw):
            if any(str(s).endswith(".US") for s in syms):
                raise _Denied(msg="no permission (markets: US)")
            return {SYM: self.b_df}

        self.af.klines.intraday_batch = mock.Mock(side_effect=_per_market)
        self.af.klines.batch = mock.Mock(return_value={"AAPL.US": self.b_df})

        out = self._feed().seed_intraday([SYM, "AAPL.US"])
        self.assertTrue(af_intraday.available(SYM), "A股 不该被美股连坐")
        self.assertFalse(af_intraday.available("AAPL.US"), "被拒的市场要记当日熔断")
        self.assertEqual(sorted(out), sorted([SYM, "AAPL.US"]), "两边都要补出样本")
        # 分组请求: A股 那组一次, 美股那组一次
        groups = [c.args[0] for c in self.af.klines.intraday_batch.call_args_list]
        self.assertEqual(groups, [[SYM], ["AAPL.US"]])
        # 原接口兜底只发还没拿到的那组 (整批混发会被市场权限一起拒掉)
        self.assertEqual(self.af.klines.batch.call_args.args[0], ["AAPL.US"])

    def test_seed_skips_intraday_after_denial(self):
        af_intraday.note_denied(_Denied(), SYM)
        self.af.klines.intraday_batch = mock.Mock(side_effect=AssertionError("不该再调"))
        self.af.klines.batch = mock.Mock(return_value={SYM: self.b_df})

        out = self._feed().seed_intraday([SYM])
        self.assertTrue(self.af.klines.batch.called)
        self.assertTrue(out)

    def test_seed_only_asks_intraday_for_allowed_markets(self):
        """一只 A股 + 一只美股: A股 走日内走势, 美股 (当日已熔断) 直接走原接口。"""
        af_intraday.note_denied(_Denied(), "AAPL.US")
        self.af.klines.intraday_batch = mock.Mock(return_value={SYM: self.b_df})
        self.af.klines.batch = mock.Mock(return_value={"AAPL.US": self.b_df})

        out = self._feed().seed_intraday([SYM, "AAPL.US"])
        asked = self.af.klines.intraday_batch.call_args[0][0]
        self.assertEqual(asked, [SYM], "有权限的市场才问日内走势")
        self.assertEqual(self.af.klines.batch.call_args[0][0], ["AAPL.US"],
                         "被熔断的市场直接走批量")
        self.assertEqual(sorted(out), sorted([SYM, "AAPL.US"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
