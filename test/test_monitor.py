# -*- coding: utf-8 -*-
"""持仓监控纯离线单测: 指标 / 六类告警 / 到期平仓 / 节流 / 时段 / 令牌桶 / 钉钉 / 整轮覆盖。

运行:
    python -u visual/test/test_monitor.py
钉钉真连通 (会往群里发一条测试消息):
    set DINGTALK_LIVE=1
    python -u visual/test/test_monitor.py TestDingTalk.test_live_robot_reachable
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import dingtalk  # noqa: E402
import feed as feed_mod  # noqa: E402
import market_hours  # noqa: E402
import monitor  # noqa: E402
import trades  # noqa: E402

FIXTURE_DIR = Path(_VISUAL_DIR) / "test" / "fixtures"


def _load_fixture(name):
    with open(FIXTURE_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def _samples_until(bars, n):
    return [{"ts": b["ts"], "price": b["price"], "volume": b["volume"]} for b in bars[:n]]


class TestMarketHours(unittest.TestCase):
    def test_weekend_not_session(self):
        sat = datetime(2026, 8, 22, 10, 30)
        self.assertFalse(market_hours.in_session(sat))
        self.assertEqual(market_hours.session_elapsed_minutes(sat), 0.0)

    def test_weekday_morning_session(self):
        fri = datetime(2026, 8, 21, 10, 0)
        # 若 XSHG 日历缺包则降级周一~周五, 8-21 是周五
        self.assertTrue(market_hours.in_session(fri))
        elapsed = market_hours.session_elapsed_minutes(fri)
        self.assertGreater(elapsed, 20)
        self.assertLess(elapsed, 60)

    def test_lunch_frozen_at_120(self):
        lunch = datetime(2026, 8, 21, 12, 0)
        self.assertFalse(market_hours.in_session(lunch))
        self.assertEqual(market_hours.session_elapsed_minutes(lunch), 120.0)

    def test_after_close(self):
        after = datetime(2026, 8, 21, 15, 1)
        self.assertFalse(market_hours.in_session(after))


class TestNthTradingDay(unittest.TestCase):
    def test_inclusive_count_skips_weekend(self):
        # 2026-08-21 周五; 3 个交易日 = 21 / 24 / 25
        self.assertEqual(market_hours.nth_trading_day("2026-08-21", 1), "2026-08-21")
        self.assertEqual(market_hours.nth_trading_day("2026-08-21", 3), "2026-08-25")

    def test_non_trading_start_moves_forward(self):
        self.assertEqual(market_hours.nth_trading_day("2026-08-22", 1), "2026-08-24")

    def test_invalid(self):
        self.assertIsNone(market_hours.nth_trading_day("2026-08-21", 0))
        self.assertIsNone(market_hours.nth_trading_day("not-a-date", 3))


class TestHoldExit(unittest.TestCase):
    def _pos(self, **kw):
        p = {
            "hold_days": 3,
            "hold_anchor_date": "2026-08-21",
            "hold_end_date": "2026-08-25",
            "model_name": "A 60分钟超短",
            "symbol": "000001.SZ",
        }
        p.update(kw)
        return p

    def test_am_and_pm_windows(self):
        pos = self._pos()
        am = monitor.evaluate_hold_exit(pos, datetime(2026, 8, 25, 10, 0))
        self.assertEqual([a["alert_type"] for a in am], ["hold_exit_am"])
        self.assertIn("上午10:00", am[0]["detail"])
        pm = monitor.evaluate_hold_exit(pos, datetime(2026, 8, 25, 14, 0))
        self.assertEqual([a["alert_type"] for a in pm], ["hold_exit_pm"])
        self.assertIn("下午14:00", pm[0]["detail"])

    def test_outside_window_or_wrong_day(self):
        pos = self._pos()
        self.assertEqual(monitor.evaluate_hold_exit(pos, datetime(2026, 8, 25, 9, 45)), [])
        self.assertEqual(monitor.evaluate_hold_exit(pos, datetime(2026, 8, 25, 13, 0)), [])
        self.assertEqual(monitor.evaluate_hold_exit(pos, datetime(2026, 8, 24, 10, 0)), [])

    def test_computes_end_date_from_anchor(self):
        pos = self._pos(hold_end_date=None)
        alerts = monitor.evaluate_hold_exit(pos, datetime(2026, 8, 25, 10, 5))
        self.assertEqual([a["alert_type"] for a in alerts], ["hold_exit_am"])


class TestTokenBucket(unittest.TestCase):
    def test_exhaust_and_reject(self):
        b = feed_mod.TokenBucket(rate_per_min=6)
        for _ in range(6):
            self.assertTrue(b.try_acquire())
        self.assertFalse(b.try_acquire())

    def test_retry_after_ms(self):
        class E(Exception):
            retry_after_ms = 1500
        self.assertEqual(feed_mod._retry_after_ms(E("429")), 1500)

        class E2(Exception):
            pass
        self.assertEqual(feed_mod._retry_after_ms(E2("HTTP 429 rate limited")), 60_000)
        self.assertIsNone(feed_mod._retry_after_ms(E2("network down")))

    def test_needs_depth(self):
        self.assertTrue(feed_mod.needs_depth(10.0, stop_loss=9.95))
        self.assertTrue(feed_mod.needs_depth(10.9, limit_up=11.0))
        self.assertFalse(feed_mod.needs_depth(10.0, stop_loss=8.0, limit_up=12.0))


class TestMetricsAndAlerts(unittest.TestCase):
    def test_accel_down_open_crash_603698(self):
        data = _load_fixture("603698_SH_2026-08-19.json")
        bars = data["bars"]
        pos = {
            "symbol": "603698.SH", "name": "航天工程",
            "entry_price": 20.0, "stop_loss": 18.50,
            "take_profit": None, "breakeven": None,
        }
        fired = []
        for n in range(1, 8):
            samples = _samples_until(bars, n)
            bar = bars[n - 1]
            dt = datetime(2026, 8, 19, int(bar["clock"][:2]), int(bar["clock"][3:]))
            m = monitor.compute_metrics(
                samples, now_ts=bar["ts"],
                session_elapsed_min=market_hours.session_elapsed_minutes(dt),
                open_price=20.0,
            )
            alerts = monitor.evaluate_alerts(pos, m, now_dt=dt)
            types = [a["alert_type"] for a in alerts]
            if "accel_down" in types:
                fired.append(bar["clock"])
        self.assertTrue(fired, "开盘急跌应触发 accel_down")
        self.assertLessEqual(fired[0], "09:35")

    def test_accel_up_near_limit_603118(self):
        data = _load_fixture("603118_SH_2026-08-13.json")
        bars = data["bars"]
        pos = {
            "symbol": "603118.SH", "name": "共进股份",
            "entry_price": 10.0, "stop_loss": None,
            "take_profit": 10.90, "breakeven": None,
        }
        limits = {"limit_up": 11.00}
        fired = []
        for n in range(1, 8):
            samples = _samples_until(bars, n)
            bar = bars[n - 1]
            dt = datetime(2026, 8, 13, int(bar["clock"][:2]), int(bar["clock"][3:]))
            m = monitor.compute_metrics(
                samples, now_ts=bar["ts"],
                session_elapsed_min=market_hours.session_elapsed_minutes(dt),
                open_price=10.0,
            )
            alerts = monitor.evaluate_alerts(pos, m, limits=limits, now_dt=dt)
            if any(a["alert_type"] == "accel_up" for a in alerts):
                fired.append(bar["clock"])
        self.assertTrue(fired, "接近涨停应触发 accel_up")
        self.assertLessEqual(fired[0], "09:36")

    def test_sl_breached(self):
        samples = [
            {"ts": 1000, "price": 10.0, "volume": 100},
            {"ts": 1060, "price": 9.0, "volume": 200},
        ]
        pos = {"entry_price": 10.0, "stop_loss": 9.5}
        m = monitor.compute_metrics(samples, now_ts=1060, session_elapsed_min=30)
        alerts = monitor.evaluate_alerts(pos, m, now_dt=datetime(2026, 8, 21, 10, 0))
        self.assertIn("sl_breached", [a["alert_type"] for a in alerts])

    def test_tp_reached(self):
        samples = [
            {"ts": 1000, "price": 10.0, "volume": 100},
            {"ts": 1060, "price": 12.0, "volume": 200},
        ]
        pos = {"entry_price": 10.0, "take_profit": 11.5}
        m = monitor.compute_metrics(samples, now_ts=1060, session_elapsed_min=30)
        alerts = monitor.evaluate_alerts(pos, m, now_dt=datetime(2026, 8, 21, 10, 0))
        self.assertIn("tp_reached", [a["alert_type"] for a in alerts])

    def test_breakeven_cross(self):
        samples = [
            {"ts": 1000, "price": 9.8, "volume": 100},
            {"ts": 1060, "price": 10.2, "volume": 200},
        ]
        pos = {"entry_price": 10.0, "breakeven": 10.0}
        m = monitor.compute_metrics(samples, now_ts=1060, session_elapsed_min=30)
        alerts = monitor.evaluate_alerts(pos, m, now_dt=datetime(2026, 8, 21, 10, 0))
        self.assertIn("breakeven_hit", [a["alert_type"] for a in alerts])

    def test_limit_up_sealed_uses_depth(self):
        samples = [
            {"ts": 1000, "price": 10.9, "volume": 100},
            {"ts": 1060, "price": 11.0, "volume": 500},
        ]
        pos = {"entry_price": 10.0, "take_profit": 11.0}
        m = monitor.compute_metrics(samples, now_ts=1060, session_elapsed_min=30)
        depth = {"ask_volumes": [0, 0, 0, 0, 0], "bid_volumes": [1000, 800, 600, 400, 200]}
        alerts = monitor.evaluate_alerts(
            pos, m, limits={"limit_up": 11.0}, depth=depth,
            now_dt=datetime(2026, 8, 21, 10, 0),
        )
        self.assertIn("limit_up_sealed", [a["alert_type"] for a in alerts])

    def test_calm_afternoon_no_accel(self):
        data = _load_fixture("603698_SH_2026-08-19.json")
        bars = data["bars"]
        # 只用下午三根, 价格几乎横盘
        samples = _samples_until(bars[-3:], 3)
        # 修正 ts 相对
        pos = {
            "entry_price": 20.0, "stop_loss": 18.50,
            "take_profit": None, "breakeven": None,
        }
        dt = datetime(2026, 8, 19, 14, 2)
        m = monitor.compute_metrics(
            samples, now_ts=samples[-1]["ts"],
            session_elapsed_min=market_hours.session_elapsed_minutes(dt),
            open_price=20.0,
        )
        alerts = monitor.evaluate_alerts(pos, m, now_dt=dt)
        self.assertNotIn("accel_down", [a["alert_type"] for a in alerts])


class TestThrottle(unittest.TestCase):
    def test_daily_once(self):
        now = datetime(2026, 8, 19, 10, 0)
        last = {"trade_date": "2026-08-19", "fired_at": "2026-08-19T09:40:00"}
        self.assertFalse(monitor.should_fire(1, "600000.SH", "breakeven_hit", now_dt=now, last=last))
        last["trade_date"] = "2026-08-18"
        self.assertTrue(monitor.should_fire(1, "600000.SH", "sl_breached", now_dt=now, last=last))

    def test_accel_30min(self):
        now = datetime(2026, 8, 19, 10, 0)
        last = {
            "trade_date": "2026-08-19",
            "fired_at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
        }
        self.assertFalse(monitor.should_fire(1, "600000.SH", "accel_down", now_dt=now, last=last))
        last["fired_at"] = (now - timedelta(minutes=31)).isoformat(timespec="seconds")
        self.assertTrue(monitor.should_fire(1, "600000.SH", "accel_down", now_dt=now, last=last))
        self.assertTrue(monitor.should_fire(1, "600000.SH", "accel_up", now_dt=now, last=None))

    def test_hold_exit_daily_once(self):
        now = datetime(2026, 8, 21, 10, 5)
        last = {"trade_date": "2026-08-21", "fired_at": "2026-08-21T10:00:00"}
        self.assertFalse(monitor.should_fire(
            1, "000001.SZ", "hold_exit_am", now_dt=now, last=last))
        last["trade_date"] = "2026-08-20"
        self.assertTrue(monitor.should_fire(
            1, "000001.SZ", "hold_exit_am", now_dt=now, last=last))


class TestReplayFixtures(unittest.TestCase):
    def test_replay_prints_expected(self):
        monitor.clear_buffers()
        hits = monitor.replay(
            ["603698.SH:2026-08-19", "603118.SH:2026-08-13"],
            persist_fixture=False,
            position_overrides={
                "603698.SH": {
                    "name": "航天工程", "entry_price": 20.0, "stop_loss": 18.50,
                    "take_profit": 22.0, "breakeven": 20.0,
                },
                "603118.SH": {
                    "name": "共进股份", "entry_price": 10.0, "stop_loss": 9.0,
                    "take_profit": 11.20, "breakeven": 10.0, "limit_up": 11.0,
                },
            },
        )
        down = [h["alert"]["alert_type"] for h in hits.get("603698.SH:2026-08-19", [])]
        up = [h["alert"]["alert_type"] for h in hits.get("603118.SH:2026-08-13", [])]
        self.assertIn("accel_down", down)
        self.assertIn("accel_up", up)


class TestPriceBuffer(unittest.TestCase):
    def setUp(self):
        monitor.clear_buffers()

    def tearDown(self):
        monitor.clear_buffers()

    def test_skip_stale_or_duplicate_timestamp(self):
        self.assertTrue(monitor.append_sample("x", 100, 10.0, 1))
        self.assertFalse(monitor.append_sample("x", 100, 10.1, 1))
        self.assertFalse(monitor.append_sample("x", 99, 10.2, 1))
        self.assertTrue(monitor.append_sample("x", 101, 10.3, 1))
        buf = monitor.get_buffer("x")
        self.assertEqual([s["ts"] for s in buf], [100, 101])
        self.assertEqual(buf[-1]["price"], 10.3)

    def test_reject_invalid_price(self):
        self.assertFalse(monitor.append_sample("x", 100, None, 1))
        self.assertFalse(monitor.append_sample("x", 100, 0, 1))
        self.assertFalse(monitor.append_sample("x", None, 10.0, 1))


class _FakeUrlResp:
    def __init__(self, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDingTalk(unittest.TestCase):
    def _sign(self, ts, secret):
        raw = hmac.new(
            secret.encode("utf-8"),
            "{}\n{}".format(ts, secret).encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return urllib.parse.quote_plus(base64.b64encode(raw))

    def test_skip_when_unconfigured(self):
        with mock.patch.object(dingtalk, "_load_env"):
            with mock.patch.dict(os.environ, {
                "DINGDING_WEB_HOOK_TOKEN": "",
                "DINGDING_BOT_SIGN": "",
            }, clear=False):
                with mock.patch("dingtalk.urllib.request.urlopen") as urlopen:
                    self.assertFalse(dingtalk.send_markdown("t", "body"))
                    urlopen.assert_not_called()

    def test_success_signs_and_posts_markdown(self):
        token, secret = "tok_abc", "SEC_xyz"
        frozen = 1_700_000_000.0
        ts = str(round(frozen * 1000))
        expected_sign = self._sign(ts, secret)
        with mock.patch.dict(os.environ, {
            "DINGDING_WEB_HOOK_TOKEN": token,
            "DINGDING_BOT_SIGN": secret,
        }, clear=False):
            with mock.patch("dingtalk.time.time", return_value=frozen):
                with mock.patch(
                    "dingtalk.urllib.request.urlopen",
                    return_value=_FakeUrlResp('{"errcode":0,"errmsg":"ok"}'),
                ) as urlopen:
                    ok = dingtalk.send_markdown("持仓监控", "## 正文\n- 一条")
        self.assertTrue(ok)
        req = urlopen.call_args[0][0]
        self.assertIn("access_token=" + token, req.full_url)
        self.assertIn("timestamp=" + ts, req.full_url)
        self.assertIn("sign=" + expected_sign, req.full_url)
        self.assertEqual(urlopen.call_args.kwargs.get("timeout"), 10)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["msgtype"], "markdown")
        self.assertEqual(body["markdown"]["title"], "持仓监控")
        self.assertIn("正文", body["markdown"]["text"])

    def test_errcode_nonzero_returns_false(self):
        with mock.patch.dict(os.environ, {
            "DINGDING_WEB_HOOK_TOKEN": "tok",
            "DINGDING_BOT_SIGN": "sec",
        }, clear=False):
            with mock.patch(
                "dingtalk.urllib.request.urlopen",
                return_value=_FakeUrlResp('{"errcode":310000,"errmsg":"sign not match"}'),
            ):
                self.assertFalse(dingtalk.send_markdown("t", "x"))

    def test_network_error_returns_false(self):
        with mock.patch.dict(os.environ, {
            "DINGDING_WEB_HOOK_TOKEN": "tok",
            "DINGDING_BOT_SIGN": "sec",
        }, clear=False):
            with mock.patch(
                "dingtalk.urllib.request.urlopen",
                side_effect=OSError("timed out"),
            ):
                self.assertFalse(dingtalk.send_markdown("t", "x"))

    @unittest.skipUnless(
        os.environ.get("DINGTALK_LIVE") == "1",
        "set DINGTALK_LIVE=1 to ping the robot",
    )
    def test_live_robot_reachable(self):
        dingtalk._load_env()
        if not os.environ.get("DINGDING_WEB_HOOK_TOKEN", "").strip() or not os.environ.get("DINGDING_BOT_SIGN", "").strip():
            self.skipTest("visual/.env 未配置钉钉 token/sign")
        ok = dingtalk.send_markdown(
            "持仓监控连通性测试",
            "## visual 单测\n钉钉机器人连通性检查，可忽略。",
        )
        self.assertTrue(ok, "钉钉机器人返回失败，检查 token/sign 或网络")


class FakeFeed:
    """监控循环用的假行情: 不碰 AlphaFeed。"""
    backend = "rest"

    def __init__(self, quotes=None, instruments=None, depths=None, seeds=None,
                 quotes_exc=None, depth_exc=None):
        self.quotes_map = quotes or {}
        self.instruments_map = instruments or {}
        self.depths_map = depths or {}
        self.seeds_map = seeds or {}
        self.quotes_exc = quotes_exc
        self.depth_exc = depth_exc
        self.quotes_calls = []
        self.depth_calls = []
        self.seed_calls = []
        self.instruments_calls = []

    def quotes(self, symbols):
        self.quotes_calls.append(list(symbols))
        if self.quotes_exc:
            raise self.quotes_exc
        return {s: dict(self.quotes_map[s]) for s in symbols if s in self.quotes_map}

    def instruments(self, symbols):
        self.instruments_calls.append(list(symbols))
        return {s: dict(self.instruments_map[s]) for s in symbols if s in self.instruments_map}

    def depth(self, symbols):
        self.depth_calls.append(list(symbols))
        if self.depth_exc:
            raise self.depth_exc
        return {s: dict(self.depths_map[s]) for s in symbols if s in self.depths_map}

    def seed_intraday(self, symbols):
        self.seed_calls.append(list(symbols))
        return {s: list(self.seeds_map[s]) for s in symbols if s in self.seeds_map}


class TestBuildMessage(unittest.TestCase):
    def test_group_by_username_sorted(self):
        md = monitor._build_message(
            [
                {
                    "username": "bob",
                    "position": {"symbol": "000001.SZ", "name": "平安"},
                    "alert": {"alert_type": "sl_breached", "detail": "击穿止损"},
                },
                {
                    "username": "alice",
                    "position": {"symbol": "600000.SH", "name": "浦发"},
                    "alert": {"alert_type": "tp_reached", "detail": "到达止盈"},
                },
            ],
            datetime(2026, 8, 21, 10, 0),
        )
        self.assertIn("## 持仓监控 2026-08-21 10:00", md)
        self.assertLess(md.index("alice"), md.index("bob"))
        self.assertIn("sl_breached", md)
        self.assertIn("tp_reached", md)


class PollPipelineTestCase(unittest.TestCase):
    """临时 DB + FakeFeed + mock 钉钉, 覆盖一整轮: 持仓筛选 → 行情 → 判定 → 节流 → 推送。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_db = trades._db_path
        self._orig_iter = trades.PBKDF2_ITERATIONS
        trades.PBKDF2_ITERATIONS = 1000
        trades.init_db(os.path.join(self._tmp.name, "test_monitor.db"))
        monitor.clear_buffers()
        self.now = datetime(2026, 8, 21, 10, 0)

    def tearDown(self):
        monitor.clear_buffers()
        trades._db_path = self._orig_db
        trades.PBKDF2_ITERATIONS = self._orig_iter
        self._tmp.cleanup()

    def _open(self, user_id, symbol, name, entry=10.0, **risk):
        return trades.create_trade(user_id, {
            "symbol": symbol, "name": name, "status": "open",
            "entry_price": entry, "quantity": 100,
            "entry_date": "2026-08-20", "entry_reason": "突破买入",
            **risk,
        })

    def _quote(self, last, ts, volume=200, open_=10.0):
        return {"last_price": last, "volume": volume, "timestamp": ts, "open": open_}


class TestPollPipeline(PollPipelineTestCase):
    def test_empty_watchlist_skips_feed_and_dingtalk(self):
        feed = FakeFeed()
        with mock.patch.object(dingtalk, "send_markdown") as send:
            fired = monitor._poll_once(feed, now_dt=self.now)
        self.assertEqual(fired, [])
        self.assertEqual(feed.quotes_calls, [])
        send.assert_not_called()

    def test_only_authorized_open_with_risk_prices(self):
        admin = trades.create_user("admin", "secret123", is_admin=True)
        bob = trades.create_user("bob", "secret123")
        closed_uid = trades.create_user("carol", "secret123", is_admin=True)
        self._open(admin, "600000.SH", "浦发", take_profit=12.0, breakeven=10.0, stop_loss=8.0)
        self._open(bob, "000001.SZ", "平安", take_profit=13.0, breakeven=11.0, stop_loss=9.0)
        self._open(admin, "300750.SZ", "宁德")  # 无风控价
        trades.create_trade(closed_uid, {
            "symbol": "601398.SH", "name": "工行", "status": "closed",
            "entry_price": 5.0, "exit_price": 5.5, "quantity": 100,
            "entry_date": "2026-08-01", "exit_date": "2026-08-10",
            "entry_reason": "突破买入", "exit_reason": "止盈(达到目标价)",
            "take_profit": 6.0, "breakeven": 5.0, "stop_loss": 4.5,
        })
        pos = trades.list_monitored_positions()
        self.assertEqual({p["symbol"] for p in pos}, {"600000.SH"})
        trades.set_user_monitor(bob, True)
        pos = trades.list_monitored_positions()
        self.assertEqual({p["symbol"] for p in pos}, {"600000.SH", "000001.SZ"})

    def test_sl_breach_persists_and_notifies(self):
        uid = trades.create_user("admin", "secret123", is_admin=True)
        t = self._open(uid, "600000.SH", "浦发", take_profit=12.0, breakeven=10.5, stop_loss=9.5)
        feed = FakeFeed(quotes={"600000.SH": self._quote(9.0, 1_000_060)})
        with mock.patch.object(dingtalk, "send_markdown", return_value=True) as send:
            fired = monitor._poll_once(feed, now_dt=self.now)
        types = [x["alert"]["alert_type"] for x in fired]
        self.assertIn("sl_breached", types)
        send.assert_called_once()
        title, text = send.call_args[0]
        self.assertEqual(title, "持仓监控")
        self.assertIn("admin", text)
        self.assertIn("600000.SH", text)
        self.assertIn("sl_breached", text)
        last = trades.last_monitor_alert(uid, "600000.SH", "sl_breached")
        self.assertIsNotNone(last)
        self.assertEqual(last["trade_date"], "2026-08-21")
        self.assertEqual(last["price"], 9.0)
        rows = trades.list_monitor_alerts(uid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], t["id"])

    def test_second_poll_same_day_throttled(self):
        trades.create_user("admin", "secret123", is_admin=True)
        uid = 1
        self._open(uid, "600000.SH", "浦发", take_profit=12.0, breakeven=10.5, stop_loss=9.5)
        feed = FakeFeed(quotes={"600000.SH": self._quote(9.0, 1_000_060)})
        with mock.patch.object(dingtalk, "send_markdown", return_value=True) as send:
            first = monitor._poll_once(feed, now_dt=self.now)
            feed.quotes_map["600000.SH"] = self._quote(8.8, 1_000_120)
            second = monitor._poll_once(feed, now_dt=self.now + timedelta(minutes=5))
        self.assertTrue(first)
        self.assertEqual(second, [])
        self.assertEqual(send.call_count, 1)

    def test_two_users_merged_one_push(self):
        a = trades.create_user("alice", "secret123", is_admin=True)
        b = trades.create_user("bob", "secret123")
        trades.set_user_monitor(b, True)
        self._open(a, "600000.SH", "浦发", take_profit=12.0, breakeven=10.5, stop_loss=9.5)
        self._open(b, "000001.SZ", "平安", take_profit=12.0, breakeven=10.5, stop_loss=9.5)
        feed = FakeFeed(quotes={
            "600000.SH": self._quote(9.0, 1_000_060),
            "000001.SZ": self._quote(12.0, 1_000_060),
        })
        with mock.patch.object(dingtalk, "send_markdown", return_value=True) as send:
            fired = monitor._poll_once(feed, now_dt=self.now)
        kinds = {(x["username"], x["alert"]["alert_type"]) for x in fired}
        self.assertIn(("alice", "sl_breached"), kinds)
        self.assertIn(("bob", "tp_reached"), kinds)
        send.assert_called_once()
        text = send.call_args[0][1]
        self.assertIn("alice", text)
        self.assertIn("bob", text)

    def test_new_trade_picked_up_next_poll(self):
        uid = trades.create_user("admin", "secret123", is_admin=True)
        feed = FakeFeed()
        with mock.patch.object(dingtalk, "send_markdown") as send:
            self.assertEqual(monitor._poll_once(feed, now_dt=self.now), [])
            send.assert_not_called()
            self._open(uid, "600000.SH", "浦发", take_profit=12.0, breakeven=10.5, stop_loss=9.5)
            feed.quotes_map["600000.SH"] = self._quote(9.0, 1_000_060)
            fired = monitor._poll_once(feed, now_dt=self.now)
        self.assertIn("sl_breached", [x["alert"]["alert_type"] for x in fired])
        send.assert_called_once()

    def test_quotes_rate_limited_no_crash_no_push(self):
        trades.create_user("admin", "secret123", is_admin=True)
        self._open(1, "600000.SH", "浦发", take_profit=12.0, breakeven=10.5, stop_loss=9.5)
        feed = FakeFeed(quotes_exc=feed_mod.RateLimited("429", retry_after_ms=1500))
        with mock.patch.object(dingtalk, "send_markdown") as send:
            fired = monitor._poll_once(feed, now_dt=self.now)
        self.assertEqual(fired, [])
        send.assert_not_called()
        self.assertIn("429", monitor.get_status().get("last_error") or "")

    def test_seed_then_quote_and_near_limit_fetches_depth(self):
        trades.create_user("admin", "secret123", is_admin=True)
        self._open(1, "603118.SH", "共进", entry=10.0, take_profit=10.90, breakeven=10.20, stop_loss=9.50)
        seed = [
            {"ts": 1_000_000, "price": 10.00, "volume": 100},
            {"ts": 1_000_060, "price": 10.50, "volume": 200},
        ]
        feed = FakeFeed(
            quotes={"603118.SH": self._quote(11.00, 1_000_120, open_=10.0)},
            instruments={"603118.SH": {"limit_up": 11.00, "limit_down": 9.00}},
            depths={"603118.SH": {
                "ask_volumes": [0, 0, 0, 0, 0],
                "bid_volumes": [1000, 800, 600, 400, 200],
            }},
            seeds={"603118.SH": seed},
        )
        with mock.patch.object(dingtalk, "send_markdown", return_value=True) as send:
            fired = monitor._poll_once(feed, now_dt=self.now)
        self.assertEqual(feed.seed_calls, [["603118.SH"]])
        self.assertTrue(feed.depth_calls, "逼近涨停应拉五档")
        types = {x["alert"]["alert_type"] for x in fired}
        self.assertIn("tp_reached", types)
        self.assertIn("limit_up_sealed", types)
        send.assert_called_once()

    def test_dingtalk_false_still_persists_alert(self):
        trades.create_user("admin", "secret123", is_admin=True)
        self._open(1, "600000.SH", "浦发", take_profit=12.0, breakeven=10.5, stop_loss=9.5)
        feed = FakeFeed(quotes={"600000.SH": self._quote(9.0, 1_000_060)})
        with mock.patch.object(dingtalk, "send_markdown", return_value=False) as send:
            fired = monitor._poll_once(feed, now_dt=self.now)
        self.assertTrue(fired)
        send.assert_called_once()
        self.assertIsNotNone(trades.last_monitor_alert(1, "600000.SH", "sl_breached"))

    def test_hold_expire_am_notifies_without_quotes(self):
        admin = trades.create_user("admin", "secret123", is_admin=True)
        # A 模型 3 日: 8-19 / 8-20 / 8-21 → 到期日即 self.now
        self._open(admin, "000001.SZ", "平安", model_id=1, entry_date="2026-08-19")
        with mock.patch.object(dingtalk, "send_markdown", return_value=True) as send:
            fired = monitor._check_hold_expire(now_dt=self.now)
        self.assertEqual([x["alert"]["alert_type"] for x in fired], ["hold_exit_am"])
        send.assert_called_once()
        title, text = send.call_args[0]
        self.assertEqual(title, "持仓到期提醒")
        self.assertIn("000001.SZ", text)
        with mock.patch.object(dingtalk, "send_markdown") as send2:
            again = monitor._check_hold_expire(now_dt=self.now + timedelta(minutes=10))
        self.assertEqual(again, [])
        send2.assert_not_called()
        with mock.patch.object(dingtalk, "send_markdown", return_value=True) as send3:
            pm = monitor._check_hold_expire(now_dt=datetime(2026, 8, 21, 14, 0))
        self.assertEqual([x["alert"]["alert_type"] for x in pm], ["hold_exit_pm"])
        send3.assert_called_once()

    def test_hold_expire_batch_uses_latest_buy(self):
        admin = trades.create_user("admin", "secret123", is_admin=True)
        trades.create_trade(admin, {
            "type": "batch", "symbol": "000001.SZ", "name": "平安银行",
            "model_id": 1,
            "legs": [
                {"side": "buy", "price": 10.0, "quantity": 500, "date": "2026-08-03"},
                {"side": "buy", "price": 11.0, "quantity": 500, "date": "2026-08-19"},
            ],
        })
        with mock.patch.object(dingtalk, "send_markdown", return_value=True):
            fired = monitor._check_hold_expire(now_dt=self.now)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["alert"]["alert_type"], "hold_exit_am")


class TestRestFeedMock(unittest.TestCase):
    def test_poll_interval(self):
        f = feed_mod.RestFeed(lambda: None)
        self.assertEqual(f.poll_interval(10), 20.0)
        self.assertEqual(f.poll_interval(101), 30.0)

    def test_quotes_fallback_when_af_down(self):
        af = mock.Mock()
        af.quotes.get.side_effect = RuntimeError("af down")
        got = {}

        def fallback(symbols, fresh=True):
            got["symbols"] = list(symbols)
            got["fresh"] = fresh
            return {"600000.SH": {"last_price": 10.5, "volume": 123, "name": "浦发"}}

        f = feed_mod.RestFeed(lambda: af, fallback_quotes=fallback)
        out = f.quotes(["600000.SH"])
        self.assertEqual(got["symbols"], ["600000.SH"])
        self.assertTrue(got["fresh"])
        self.assertEqual(out["600000.SH"]["last_price"], 10.5)
        self.assertEqual(out["600000.SH"]["volume"], 123)

    def test_quotes_429_sets_backoff(self):
        af = mock.Mock()

        class E(Exception):
            retry_after_ms = 2500

        af.quotes.get.side_effect = E("HTTP 429")
        f = feed_mod.RestFeed(lambda: af)
        with self.assertRaises(feed_mod.RateLimited) as cm:
            f.quotes(["600000.SH"])
        self.assertEqual(cm.exception.retry_after_ms, 2500)
        self.assertTrue(f.in_backoff())
        self.assertEqual(f.quotes(["600000.SH"]), {})

    def test_instruments_cached_same_day(self):
        af = mock.Mock()
        af.instruments.batch.return_value = [{
            "symbol": "600000.SH",
            "ext": {"limit_up": 11.0, "limit_down": 9.0, "name": "浦发"},
        }]
        f = feed_mod.RestFeed(lambda: af)
        a = f.instruments(["600000.SH"])
        b = f.instruments(["600000.SH"])
        self.assertEqual(a["600000.SH"]["limit_up"], 11.0)
        self.assertEqual(b["600000.SH"]["limit_down"], 9.0)
        af.instruments.batch.assert_called_once_with(["600000.SH"])

    def test_quotes_parses_dataframe(self):
        import pandas as pd
        af = mock.Mock()
        af.quotes.get.return_value = pd.DataFrame([{
            "symbol": "600000.SH",
            "last_price": 10.5,
            "prev_close": 10.0,
            "open": 10.1,
            "high": 10.6,
            "low": 10.0,
            "volume": 1000,
            "amount": 10500.0,
            "timestamp": 1_700_000_000_000,  # ms
            "ext.name": "浦发",
            "ext.change_pct": 0.05,
        }])
        f = feed_mod.RestFeed(lambda: af)
        out = f.quotes(["600000.SH"])
        q = out["600000.SH"]
        self.assertEqual(q["last_price"], 10.5)
        self.assertAlmostEqual(q["timestamp"], 1_700_000_000.0)
        self.assertEqual(q["name"], "浦发")


if __name__ == "__main__":
    unittest.main(verbosity=2)
