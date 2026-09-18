# -*- coding: utf-8 -*-
"""GET /api/intraday 路由测试: 鉴权 + 成交量面板提示框所需字段 (amount / VOL MA)。

契约核心: 分时成交量面板的提示框与日K 同口径 —— 成交量 + VOL MA5/10/20 + 成交额。
这些均线本就在 compute_all_indicators 里算好, 必须随 bar 一起吐给前端; 只留在
DataFrame 上不吐, 前端那一行就只能显示空白/「—」。任何把字段又收回去的改动都要
被这条测试拦住。

运行:
    venv/Scripts/python.exe -u visual/test/test_intraday_api.py
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from stock_indicators_cn import sma

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import security  # noqa: E402
import trades  # noqa: E402

SYM = "002472.SZ"
DAY = "2026-09-18"
PREV_DAY = "2026-09-17"


def _reset_rate_limit():
    security._rate_limit_tokens = float(security.RATE_LIMIT_PER_MIN)
    security._rate_limit_last_refill = time.time()


def _csrf(client):
    client.get("/login.html")
    headers = {}
    c = client.get_cookie("csrf_token")
    if c is not None:
        headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
    return headers


def _minute_df(n=40):
    """同一交易日的分钟线 (分时接口按 normalize().max() 截当日, 跨日会切掉前半段)。"""
    idx = pd.date_range(f"{DAY} 09:30", periods=n, freq="min")
    base = [34.0 + i * 0.01 for i in range(n)]
    return pd.DataFrame(
        {
            "open": base,
            "high": [c + 0.05 for c in base],
            "low": [c - 0.05 for c in base],
            "close": base,
            "volume": [1000.0 + i * 10 for i in range(n)],
            "amount": [1_000_000.0 + i * 1_000 for i in range(n)],
        },
        index=idx,
    )


def _avg_minute_df(closes, volumes, shares=False, day=DAY):
    """成交额与成交量**自洽**的分钟 df: amount = close × 股数。

    shares=False → volume 是「手」, 股数 = volume×100 (AlphaFeed/麦蕊 fsjy/东财 口径);
    shares=True  → volume 已经是股。两种口径的均价必须一模一样 —— 这正是要钉住的点:
    单位系数猜错会让均价整体差 100 倍, 而「差 100 倍」在图上就是把价格轴拉飞。
    """
    mult = 1.0 if shares else 100.0
    idx = pd.date_range(f"{day} 09:30", periods=len(closes), freq="min")
    close = [float(c) for c in closes]
    vol = [float(v) for v in volumes]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 0.03 for c in close],
            "low": [c - 0.03 for c in close],
            "close": close,
            "volume": vol,
            "amount": [c * v * mult for c, v in zip(close, vol)],
        },
        index=idx,
    )


_AVG_CLOSES = [34.0, 34.4, 33.8, 34.1, 34.9]
_AVG_VOLS = [800.0, 1500.0, 2200.0, 600.0, 1900.0]


def _vwap(closes, volumes):
    """累计成交额/累计成交量 (单位系数在分子分母里约掉, 与源的口径无关)。"""
    out, num, den = [], 0.0, 0.0
    for c, v in zip(closes, volumes):
        num += c * v
        den += v
        out.append(num / den)
    return out


def _none_if_nan(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


class FakeIntradaySource:
    """冒充 kline_source: 记录调用参数, 回一份固定分钟 df。"""

    def __init__(self, df):
        self.df = df
        self.calls = []

    def fetch_kline_df(self, category, symbol, period, count, adjust="forward"):
        self.calls.append(
            {"category": category, "symbol": symbol, "period": period,
             "count": count, "adjust": adjust}
        )
        return self.df, "fake"


class IntradayRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_intraday.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("intraday_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        _reset_rate_limit()
        from api import kline as kline_mod
        self.kline_mod = kline_mod
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        _csrf(self.client)

    def _mock_source(self, df=None):
        df = _minute_df() if df is None else df
        self.source = FakeIntradaySource(df)
        return mock.patch.object(self.kline_mod, "kline_source", self.source)

    def test_requires_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get(f"/api/intraday?symbol={SYM}").status_code, 401)

    def test_missing_symbol(self):
        with self._mock_source():
            self.assertEqual(self.client.get("/api/intraday").status_code, 400)

    def test_fetch_uses_minute_source_forward_adjusted(self):
        """分时的数据来源口径: minute 分类 + 前复权 (与图表默认口径一致)。"""
        with self._mock_source():
            self.client.get(f"/api/intraday?symbol={SYM}&period=1m&count=240")
        self.assertEqual(len(self.source.calls), 1)
        call = self.source.calls[0]
        self.assertEqual(call["category"], "minute")
        self.assertEqual(call["adjust"], "forward")
        self.assertEqual(call["symbol"], SYM)
        self.assertEqual(call["period"], "1m")

    def test_bars_carry_volume_panel_fields(self):
        """成交量面板提示框要的三样东西必须都在 bar 上: 成交量 / VOL MA5-10-20 / 成交额。"""
        with self._mock_source():
            r = self.client.get(f"/api/intraday?symbol={SYM}&period=1m")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        bars = body["bars"]
        self.assertEqual(body["symbol"], SYM)
        self.assertEqual(body["period"], "1m")
        # 响应级日期: 图表左上角标题写「分时 · 2026-09-18」用 (bar 只有 HH:MM,
        # 客户端拿本地时钟推"今天"会在周末/节假日说错日子)
        self.assertEqual(body["date"], DAY, "分时响应缺当日日期")
        self.assertEqual(len(bars), 40, "当日 bar 一根不少")

        for i, bar in enumerate(bars):
            for key in ("amount", "avg_price", "vol_ma5", "vol_ma10", "vol_ma20"):
                self.assertIn(key, bar, f"第 {i} 根缺 {key}")
            self.assertEqual(bar["amount"], 1_000_000.0 + i * 1_000)

        # VOL MA 必须与后端对同一份 df 算出的 sma 一致 (暖机段 None, 不是 0)
        vol = _minute_df()["volume"]
        for n, key in ((5, "vol_ma5"), (10, "vol_ma10"), (20, "vol_ma20")):
            expect = sma(vol, n).tolist()
            self.assertEqual(
                [bar[key] for bar in bars], [_none_if_nan(v) for v in expect],
                f"{key} 口径漂移 (暖机段应为 null, 不是 0)",
            )
        self.assertIsNone(bars[0]["vol_ma5"], "第 1 根没有 5 根样本 → null")
        self.assertIsNone(bars[3]["vol_ma5"])
        self.assertAlmostEqual(bars[4]["vol_ma5"], (1000.0 + 1010 + 1020 + 1030 + 1040) / 5)
        self.assertIsNone(bars[18]["vol_ma20"])
        self.assertIsNotNone(bars[19]["vol_ma20"])

    def _avg_values(self, df):
        with self._mock_source(df):
            r = self.client.get(f"/api/intraday?symbol={SYM}&period=1m")
        self.assertEqual(r.status_code, 200)
        return [bar["avg_price"] for bar in r.get_json()["bars"]]

    def test_avg_price_is_cumulative_vwap_from_open(self):
        """分时均价 = 当日累计成交额/累计成交量 (东财分时那条黄线), 从开盘点起累计。"""
        got = self._avg_values(_avg_minute_df(_AVG_CLOSES, _AVG_VOLS))
        self.assertEqual(len(got), len(_AVG_CLOSES), "当日 bar 一根不少")
        for i, want in enumerate(_vwap(_AVG_CLOSES, _AVG_VOLS)):
            self.assertAlmostEqual(got[i], want, places=6, msg=f"第 {i} 根均价口径漂移")
        # 第 1 根就是自己的收盘价 (累计只剩它自己)
        self.assertAlmostEqual(got[0], _AVG_CLOSES[0], places=6)
        # 均价必然落在当日已出现过的价格区间内: 单位系数猜错 (差 100 倍) 时这条必红
        self.assertGreaterEqual(min(got), min(_AVG_CLOSES))
        self.assertLessEqual(max(got), max(_AVG_CLOSES))

    def test_avg_price_same_for_lots_and_shares(self):
        """volume 是「手」还是「股」都必须得到同一条均价线 (源口径差异不该改图)。"""
        lots = self._avg_values(_avg_minute_df(_AVG_CLOSES, _AVG_VOLS, shares=False))
        shares = self._avg_values(_avg_minute_df(_AVG_CLOSES, _AVG_VOLS, shares=True))
        for i, (a, b) in enumerate(zip(lots, shares)):
            self.assertAlmostEqual(a, b, places=6, msg=f"第 {i} 根均价随单位口径漂移")

    def test_avg_price_null_when_amount_missing(self):
        """源不带成交额 (market._normalize 会填 0.0) → 均价全 null, 前端据此不画线。"""
        df = _avg_minute_df(_AVG_CLOSES, _AVG_VOLS)
        df["amount"] = 0.0
        self.assertEqual(self._avg_values(df), [None] * len(_AVG_CLOSES))

    def test_avg_price_unbiased_when_one_bar_lacks_amount(self):
        """个别 bar 缺成交额时, 它的成交量不能进分母 (否则之后每根均价都偏低)。"""
        df = _avg_minute_df(_AVG_CLOSES, _AVG_VOLS)
        df.iloc[0, df.columns.get_loc("amount")] = 0.0   # 开盘那根没成交额
        got = self._avg_values(df)
        self.assertIsNone(got[0], "该根无有效成交额 → null (前端断线起点)")
        for i in range(1, len(_AVG_CLOSES)):
            want = _vwap(_AVG_CLOSES[1:], _AVG_VOLS[1:])[i - 1]
            self.assertAlmostEqual(got[i], want, places=6,
                                   msg=f"第 {i} 根被缺额那根的成交量带偏了")

    def test_avg_price_ignores_previous_day(self):
        """跨日数据先截当日再累计: 前一天的量不能进分母 (否则开盘那几根均价偏低)。"""
        prev = _avg_minute_df([10.0, 10.0], [99999.0, 99999.0], day=PREV_DAY)
        today = _avg_minute_df(_AVG_CLOSES, _AVG_VOLS)
        both = pd.concat([prev, today])
        with self._mock_source(both):
            r = self.client.get(f"/api/intraday?symbol={SYM}&period=1m")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["date"], DAY, "日期要取截出来的那一天, 不是数据里的第一天")
        got = [bar["avg_price"] for bar in body["bars"]]
        self.assertEqual(len(got), len(_AVG_CLOSES), "只回当日 bar")
        for i, want in enumerate(_vwap(_AVG_CLOSES, _AVG_VOLS)):
            self.assertAlmostEqual(got[i], want, places=6, msg="前一日成交量被算进了均价")


if __name__ == "__main__":
    unittest.main(verbosity=2)
