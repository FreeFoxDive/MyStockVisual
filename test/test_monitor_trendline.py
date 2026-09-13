# -*- coding: utf-8 -*-
"""趋势线跌破监控测试: 纯评估函数 / 监控线加载与缓存 / 整轮接入 / feed 指数回退。

覆盖:
- monitor.evaluate_trendline_break: hline / trend / ray 线性外推 / 阈值边界 /
  未来锚点 off 回退 / 非法参数
- monitor._load_line_monitors: monitor 字段过滤 + updated_at 缓存
- monitor._evaluate_trendline_monitors / _poll_once: 触发 / 每日一次 / 落库 / 推送
- feed.RestFeed.quotes: AF 缺失标的 (典型: 指数) 回退 fallback_quotes

运行:
    venv/Scripts/python.exe -u visual/test/test_monitor_trendline.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import trades  # noqa: E402


def _dates(n):
    return [f"d{i:03d}" for i in range(n)]


class EvaluateTrendlineBreakTest(unittest.TestCase):
    """monitor.evaluate_trendline_break 纯函数。"""

    def setUp(self):
        import monitor
        self.mon = monitor
        self.dates = _dates(120)  # idx 0..119

    def test_hline_triggers_below_threshold(self):
        pts = [{"t": "d005", "p": 100.0, "off": 0}]
        r = self.mon.evaluate_trendline_break(pts, "hline", 2, self.dates, 97.9)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["value"], 100.0)
        self.assertAlmostEqual(r["threshold"], 98.0)

    def test_hline_no_trigger_above_threshold(self):
        pts = [{"t": "d005", "p": 100.0, "off": 0}]
        self.assertIsNone(
            self.mon.evaluate_trendline_break(pts, "hline", 2, self.dates, 98.1))

    def test_trend_extrapolates_to_last_bar(self):
        # 锚点 (idx10, 10) → (idx110, 20); 最后一根 idx119 处线值 20.9
        pts = [{"t": "d010", "p": 10.0, "off": 0}, {"t": "d110", "p": 20.0, "off": 0}]
        r = self.mon.evaluate_trendline_break(pts, "trend", 2, self.dates, 20.4)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["value"], 20.9, places=6)
        self.assertAlmostEqual(r["threshold"], 20.9 * 0.98, places=6)
        self.assertIsNone(
            self.mon.evaluate_trendline_break(pts, "trend", 2, self.dates, 20.5))

    def test_ray_uses_same_extrapolation(self):
        pts = [{"t": "d010", "p": 10.0, "off": 0}, {"t": "d110", "p": 20.0, "off": 0}]
        r = self.mon.evaluate_trendline_break(pts, "ray", 2, self.dates, 20.4)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["value"], 20.9, places=6)

    def test_unknown_date_falls_back_to_off(self):
        # t 未命中日期表 → off 兜底: idx = 119 - 110 = 9
        pts = [{"t": "1999-01-01", "p": 10.0, "off": -110}, {"t": "d110", "p": 20.0, "off": 0}]
        r = self.mon.evaluate_trendline_break(pts, "trend", 2, self.dates, 20.4)
        self.assertIsNotNone(r)
        # value = 10 + 10*(119-9)/(110-9) = 20.891
        self.assertAlmostEqual(r["value"], 10 + 10 * 110 / 101, places=6)

    def test_rejects_invalid_inputs(self):
        pts = [{"t": "d010", "p": 10.0, "off": 0}, {"t": "d110", "p": 20.0, "off": 0}]
        self.assertIsNone(self.mon.evaluate_trendline_break(pts, "poly", 2, self.dates, 1.0))
        self.assertIsNone(self.mon.evaluate_trendline_break(pts, "trend", 0.05, self.dates, 1.0))
        self.assertIsNone(self.mon.evaluate_trendline_break(pts, "trend", 25, self.dates, 1.0))
        self.assertIsNone(self.mon.evaluate_trendline_break(pts, "trend", 2, self.dates[:1], 1.0))
        self.assertIsNone(self.mon.evaluate_trendline_break(pts, "trend", 2, self.dates, None))
        self.assertIsNone(self.mon.evaluate_trendline_break(
            [{"t": "d010", "p": 10.0, "off": 0}], "trend", 2, self.dates, 1.0))
        self.assertIsNone(self.mon.evaluate_trendline_break(
            [{"t": "d010", "p": "x", "off": 0}, {"t": "d110", "p": 20.0, "off": 0}],
            "trend", 2, self.dates, 1.0))


def _drawing(did="dtest1", name="支撑线", dtype="trend", pct=2, enabled=True,
             points=None, period="1d", adjust=None):
    mon = {"enabled": enabled, "pct": pct}
    if adjust is not None:
        mon["adjust"] = adjust
    return {
        "id": did, "type": dtype,
        "points": points or [{"t": "d010", "p": 10.0, "off": 0},
                             {"t": "d110", "p": 20.0, "off": 0}],
        "style": {"color": "#e6a23c", "width": 1, "dash": False},
        "extendRight": False, "name": name,
        "monitor": mon, "createdAt": 0,
    }


class _FakeDF:
    def __init__(self, dates):
        self.index = dates

    def __len__(self):
        return len(self.index)


class LineMonitorEvalTest(unittest.TestCase):
    """_load_line_monitors + _evaluate_trendline_monitors: 加载/触发/每日一次/落库/推送。"""

    SYM = "600519.SH"

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_line_monitor.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("line_mon_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        import monitor
        cls.mon = monitor

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        self.mon._line_mon_cache.clear()
        conn = trades.get_conn()
        try:
            conn.execute("DELETE FROM chart_drawings")
            conn.execute("DELETE FROM trendline_monitor_state")
            conn.commit()
        finally:
            conn.close()

    def _save(self, drawings, symbol=None, period="1d"):
        trades.save_chart_drawings(self.uid, symbol or self.SYM, period, drawings)

    def _patch_notify(self):
        return (mock.patch("monitor.dingtalk.send_markdown"),
                mock.patch("monitor.ntfy.send_markdown"))

    def test_load_filters_disabled_and_types(self):
        self._save([_drawing("d1"), _drawing("d2", enabled=False),
                    _drawing("d3", dtype="poly"), _drawing("d4", pct=99)])
        mons = self.mon._load_line_monitors()
        self.assertEqual([m["drawing_id"] for m in mons], ["d1"])
        m = mons[0]
        self.assertEqual(m["user_id"], self.uid)
        self.assertEqual(m["symbol"], self.SYM)
        self.assertEqual(m["line_type"], "trend")
        self.assertEqual(m["pct"], 2)

    def test_load_skips_non_monitorable_period(self):
        self._save([_drawing("d1")], period="15m")
        self.assertEqual(self.mon._load_line_monitors(), [])

    def test_load_cache_hits_until_updated(self):
        self._save([_drawing("d1")])
        self.assertEqual(len(self.mon._load_line_monitors()), 1)
        mons = self.mon._load_line_monitors()
        self.assertEqual(len(mons), 1)
        # 同 updated_at 下缓存命中 (即使底层数据被篡改也不重读)
        self.assertEqual(len(self.mon._load_line_monitors()), 1)

    def test_load_defaults_adjust_forward(self):
        self._save([_drawing("d1"), _drawing("d2", adjust="none")])
        mons = {m["drawing_id"]: m for m in self.mon._load_line_monitors()}
        self.assertEqual(mons["d1"]["adjust"], "forward")
        self.assertEqual(mons["d2"]["adjust"], "none")

    def test_load_bar_dates_passes_adjust(self):
        import market
        fake = _FakeDF(["2026-09-01", "2026-09-02", "2026-09-03"])
        with mock.patch.object(market, "fetch_kline", return_value=(fake, "平安银行")) as fk:
            out = self.mon._load_bar_dates("000001.SZ", "1d", "none")
        fk.assert_called_once_with("000001.SZ", "1d", 1006, adjust="none")
        self.assertEqual(out, (["2026-09-01", "2026-09-02", "2026-09-03"], 3))

    def test_evaluate_uses_monitor_adjust(self):
        self._save([_drawing("dadj", adjust="none")])
        mons = self.mon._load_line_monitors()
        self.assertEqual(mons[0]["adjust"], "none")
        now = datetime(2026, 9, 13, 10, 0, 0)
        with mock.patch.object(self.mon, "_load_bar_dates",
                               return_value=(_dates(120), 120)) as lbd, \
             mock.patch("monitor.dingtalk.send_markdown"), \
             mock.patch("monitor.ntfy.send_markdown"):
            self.mon._evaluate_trendline_monitors(
                mons, {self.SYM: {"last_price": 20.4}}, now)
        lbd.assert_called_once_with(self.SYM, "1d", "none")

    def test_evaluate_fires_once_per_day(self):
        self._save([_drawing("d1", name="主升浪支撑")])
        mons = self.mon._load_line_monitors()
        quotes = {self.SYM: {"last_price": 20.4}}
        now = datetime(2026, 9, 13, 10, 0, 0)
        with mock.patch.object(self.mon, "_load_bar_dates",
                               return_value=(_dates(120), 120)):
            dt, nt = self._patch_notify()
            with dt as d, nt as n:
                fired = self.mon._evaluate_trendline_monitors(mons, quotes, now)
            self.assertEqual(len(fired), 1)
            self.assertAlmostEqual(fired[0]["price"], 20.4)
            d.assert_called_once()
            n.assert_called_once()
            self.assertIn("趋势线跌破预警", d.call_args.args[0])
            # 当日第二轮不重触
            with dt as d2, nt as n2:
                self.assertEqual(
                    self.mon._evaluate_trendline_monitors(mons, quotes, now), [])
            d2.assert_not_called()
            n2.assert_not_called()
        # 落库: monitor_alerts + 触发状态
        alerts = trades.list_monitor_alerts(self.uid)
        self.assertEqual(alerts[0]["alert_type"], "trendline_break")
        self.assertEqual(alerts[0]["symbol"], self.SYM)
        self.assertIn("主升浪支撑", alerts[0]["detail"])
        self.assertIsNotNone(
            trades.get_trendline_fired(self.uid, "d1"))

    def test_evaluate_skips_missing_quote(self):
        self._save([_drawing("d1")])
        mons = self.mon._load_line_monitors()
        now = datetime(2026, 9, 13, 10, 0, 0)
        with mock.patch.object(self.mon, "_load_bar_dates",
                               return_value=(_dates(120), 120)):
            dt, nt = self._patch_notify()
            with dt as d, nt as n:
                self.assertEqual(
                    self.mon._evaluate_trendline_monitors(mons, {}, now), [])
                self.assertEqual(self.mon._evaluate_trendline_monitors(
                    mons, {self.SYM: {"last_price": None}}, now), [])
        d.assert_not_called()
        n.assert_not_called()

    def test_next_day_fires_again(self):
        self._save([_drawing("d1")])
        mons = self.mon._load_line_monitors()
        quotes = {self.SYM: {"last_price": 20.4}}
        day1 = datetime(2026, 9, 13, 10, 0, 0)
        day2 = datetime(2026, 9, 14, 10, 0, 0)
        with mock.patch.object(self.mon, "_load_bar_dates",
                               return_value=(_dates(120), 120)):
            dt, nt = self._patch_notify()
            with dt, nt:
                self.assertEqual(
                    len(self.mon._evaluate_trendline_monitors(mons, quotes, day1)), 1)
                self.assertEqual(
                    len(self.mon._evaluate_trendline_monitors(mons, quotes, day2)), 1)

    def test_no_persist_no_notify(self):
        self._save([_drawing("d1")])
        mons = self.mon._load_line_monitors()
        quotes = {self.SYM: {"last_price": 20.4}}
        now = datetime(2026, 9, 13, 10, 0, 0)
        before = len(trades.list_monitor_alerts(self.uid))
        with mock.patch.object(self.mon, "_load_bar_dates",
                               return_value=(_dates(120), 120)):
            dt, nt = self._patch_notify()
            with dt as d, nt as n:
                fired = self.mon._evaluate_trendline_monitors(
                    mons, quotes, now, persist=False, notify=False)
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(trades.list_monitor_alerts(self.uid)), before)
        self.assertIsNone(trades.get_trendline_fired(self.uid, "d1"))
        d.assert_not_called()
        n.assert_not_called()


class PollOnceIntegrationTest(unittest.TestCase):
    """_poll_once 整轮: 趋势线监控标的并入行情拉取并触发。"""

    SYM = "000001.SH"  # 指数, 验证指数也能被监控

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_line_poll.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("poll_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        import monitor
        cls.mon = monitor

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        self.mon._line_mon_cache.clear()
        self.mon.clear_buffers()
        conn = trades.get_conn()
        try:
            conn.execute("DELETE FROM chart_drawings")
            conn.execute("DELETE FROM trendline_monitor_state")
            conn.execute("DELETE FROM monitor_alerts")
            conn.commit()
        finally:
            conn.close()

    def test_poll_once_evaluates_line_monitors(self):
        trades.save_chart_drawings(
            self.uid, self.SYM, "1d",
            [_drawing("dp1", name="上证支撑", dtype="hline", pct=3,
                      points=[{"t": "d005", "p": 3000.0, "off": 0}])])
        feed_obj = mock.MagicMock()
        feed_obj.instruments.return_value = {}
        feed_obj.seed_intraday.return_value = {}
        feed_obj.quotes.return_value = {self.SYM: {"last_price": 2900.0, "volume": 1}}
        now = datetime(2026, 9, 13, 10, 0, 0)
        with mock.patch.object(self.mon, "_load_bar_dates",
                               return_value=(_dates(120), 120)):
            with mock.patch("monitor.dingtalk.send_markdown") as d, \
                 mock.patch("monitor.ntfy.send_markdown") as n:
                self.mon._poll_once(feed_obj, now_dt=now)
        # _poll_once 返回值只含持仓告警 (与价格预警同约定), 趋势线触发看落库与推送
        alerts = trades.list_monitor_alerts(self.uid)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["alert_type"], "trendline_break")
        self.assertEqual(alerts[0]["symbol"], self.SYM)
        self.assertEqual(alerts[0]["price"], 2900.0)
        self.assertIsNotNone(trades.get_trendline_fired(self.uid, "dp1"))
        # 监控标的并入了行情拉取 universe (指数也可被监控)
        fetched = feed_obj.quotes.call_args.args[0]
        self.assertIn(self.SYM, fetched)
        d.assert_called_once()
        n.assert_called_once()


class FeedFallbackTest(unittest.TestCase):
    """RestFeed.quotes: AF 缺失标的 (指数) 回退 fallback_quotes。"""

    def test_missing_symbol_falls_back(self):
        import pandas as pd
        from feed import RestFeed

        class _Quotes:
            @staticmethod
            def get(symbols, to_dataframe=True):
                return pd.DataFrame([{
                    "symbol": "600519.SH", "last_price": 100.0, "prev_close": 99.0,
                    "open": 99.5, "high": 101.0, "low": 99.0, "volume": 1000,
                    "amount": 1e5, "timestamp": 1700000000000, "name": "贵州茅台",
                }])

        class _AF:
            quotes = _Quotes()

        def fallback(symbols, fresh=False):
            assert "000001.SH" in symbols, f"回退应只收缺失标的, 实收 {symbols}"
            return {"000001.SH": {"last_price": 3000.0, "prev_close": 2990.0,
                                  "open": 2995.0, "high": 3010.0, "low": 2985.0,
                                  "volume": 2, "amount": 3.0, "name": "上证指数"}}

        f = RestFeed(lambda: _AF(), fallback_quotes=fallback)
        out = f.quotes(["600519.SH", "000001.SH"])
        self.assertAlmostEqual(out["600519.SH"]["last_price"], 100.0)
        self.assertAlmostEqual(out["000001.SH"]["last_price"], 3000.0)
        self.assertIsNone(out["000001.SH"]["change_pct"])  # 麦蕊百分数不透传
        self.assertEqual(out["000001.SH"]["name"], "上证指数")

    def test_no_fallback_when_all_present(self):
        import pandas as pd
        from feed import RestFeed

        class _Quotes:
            @staticmethod
            def get(symbols, to_dataframe=True):
                return pd.DataFrame([{
                    "symbol": "600519.SH", "last_price": 100.0, "volume": 1,
                }])

        class _AF:
            quotes = _Quotes()

        called = []

        def fallback(symbols, fresh=False):
            called.append(symbols)
            return {}

        f = RestFeed(lambda: _AF(), fallback_quotes=fallback)
        out = f.quotes(["600519.SH"])
        self.assertIn("600519.SH", out)
        self.assertEqual(called, [], "AF 全量命中时不应触发回退")


if __name__ == "__main__":
    unittest.main(verbosity=2)
