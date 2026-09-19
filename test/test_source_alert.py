# -*- coding: utf-8 -*-
"""数据源熔断/退避告警的当日闸门: **每源每天最多一条**。

契约:
  * 同一源同一天第二次熔断不再推送 (但事件本身照旧进日志);
  * 不同源各算各的 (alphafeed 发过不影响 mairui);
  * 跨日自动恢复 (按自然日滚动, 不永久静默);
  * 入队失败要**退回配额** —— 当天其实一条都没发出去, 不该白白占掉唯一机会;
  * 文案包含"原因 + 冷却时长 + 最后失败的标的/周期", 走 error_notify 的异步通道
    (发送器是 10s 超时的 HTTP, 绝不能落在请求线程上);
  * 关掉开关 (SOURCE_ALERT_DISABLED) 或告警路径自身出错, 都不能影响取数主流程。

运行:
    venv/Scripts/python.exe -u visual/test/test_source_alert.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import error_notify  # noqa: E402
import source_alert  # noqa: E402

# 整套用例不许发出真实的钉钉/ntfy 消息。`unittest discover` 会先 import 完所有测试模块
# 再执行, 所以在模块级置默认值即可覆盖整轮 (各用例要验证"会发"时自己在 patch 里摘掉它)。
# 单个文件单跑时, 由触发熔断的测试基类 (KlineSourceTestBase / BackoffBase) 各自打桩兜底。
os.environ.setdefault("SOURCE_ALERT_DISABLED", "1")


class SourceAlertGateTest(unittest.TestCase):
    def setUp(self):
        source_alert.reset()
        self.sent = []
        self._p = mock.patch.object(
            error_notify, "notify_alert",
            side_effect=lambda src, title, text: (self.sent.append((src, title, text)), True)[1])
        self._p.start()
        self.addCleanup(self._p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SOURCE_ALERT_DISABLED", None)

    def test_second_call_same_source_same_day_is_suppressed(self):
        self.assertTrue(source_alert.notify("alphafeed", "连续失败 3 次",
                                            detail="600519.SH 1m (stock)", cooldown_sec=60))
        self.assertFalse(source_alert.notify("alphafeed", "连续失败 3 次",
                                             detail="600519.SH 1m (stock)", cooldown_sec=60))
        self.assertEqual(len(self.sent), 1, "同一源同一天只该发一条")

    def test_other_sources_are_independent(self):
        self.assertTrue(source_alert.notify("alphafeed", "连续失败 3 次"))
        self.assertTrue(source_alert.notify("mairui", "429 限流退避"))
        self.assertTrue(source_alert.notify("akshare", "连续失败 3 次"))
        self.assertEqual([s[0] for s in self.sent],
                         ["source:alphafeed", "source:mairui", "source:akshare"])

    def test_same_source_recovers_next_day(self):
        self.assertTrue(source_alert.notify("mairui", "429 限流退避"))
        self.assertFalse(source_alert.notify("mairui", "429 限流退避"))
        tomorrow = source_alert._today() + __import__("datetime").timedelta(days=1)
        with mock.patch.object(source_alert, "_today", return_value=tomorrow):
            self.assertTrue(source_alert.notify("mairui", "429 限流退避"),
                            "跨日必须恢复推送 (否则一次熔断就永久静默)")

    def test_same_source_shares_one_quota_across_reasons(self):
        """闸门 key 是**源名**: 麦蕊的 429 退避与连续失败熔断合用当天一条配额。

        这是刻意的 (每源每天一条是防打扰口径); 若哪天想拆成"每种故障各一条",
        改 `_gate_key()` 一行即可 —— 这条用例就是那个决定的锚点。
        """
        self.assertTrue(source_alert.notify("mairui", "429 限流退避"))
        self.assertFalse(source_alert.notify("mairui", "连续失败 3 次"))
        self.assertEqual(source_alert._gate_key("mairui"), "mairui")

    def test_message_carries_reason_detail_and_cooldown(self):
        source_alert.notify("alphafeed", "连续失败 3 次",
                            detail="600519.SH 1m (stock)", cooldown_sec=60)
        _src, title, text = self.sent[0]
        self.assertEqual(title, "数据源告警: alphafeed")
        self.assertIn("连续失败 3 次", text)
        self.assertIn("60s", text)
        self.assertIn("600519.SH 1m (stock)", text, "要能一眼看出是哪个标的/周期在坏")
        self.assertIn("当日仅通知一次", text, "文案要说清后续不会再刷")

    def test_disabled_switch(self):
        with mock.patch.dict(os.environ, {"SOURCE_ALERT_DISABLED": "1"}):
            self.assertFalse(source_alert.notify("alphafeed", "连续失败 3 次"))
        self.assertEqual(self.sent, [])
        self.assertTrue(source_alert.notify("alphafeed", "连续失败 3 次"),
                        "关掉开关不该占配额, 打开后仍能发")

    def test_enqueue_failure_releases_the_quota(self):
        """入队失败 (队列满/被静默) 要退回配额: 否则当天的唯一一次机会白丢。"""
        with mock.patch.object(error_notify, "notify_alert", return_value=False) as na:
            self.assertFalse(source_alert.notify("alphafeed", "连续失败 3 次"))
            self.assertEqual(na.call_count, 1)
        self.assertEqual(source_alert.notified_today(), {}, "没发出去就不该占配额")
        self.assertTrue(source_alert.notify("alphafeed", "连续失败 3 次"))

    def test_notify_error_never_escapes(self):
        """告警路径自身抛异常也不能影响取数 (调用方在熔断路径上, 不能被打断)。"""
        with mock.patch.object(error_notify, "notify_alert",
                               side_effect=RuntimeError("boom")):
            self.assertFalse(source_alert.notify("alphafeed", "连续失败 3 次"))
        self.assertEqual(source_alert.notified_today(), {})
        self.assertTrue(source_alert.notify("alphafeed", "连续失败 3 次"))

    def test_reset_clears_the_gate(self):
        self.assertTrue(source_alert.notify("alphafeed", "连续失败 3 次"))
        source_alert.reset()
        self.assertTrue(source_alert.notify("alphafeed", "连续失败 3 次"))


class SourceAlertWiringTest(unittest.TestCase):
    """接入点: 源熔断与 429 退避都要走到 source_alert.notify (不接就等于没告警)。"""

    def test_kline_source_note_fail_calls_notify(self):
        import kline_source
        kline_source.reset_health()
        with mock.patch.object(kline_source, "SOURCE_FAIL_THRESHOLD", 2), \
             mock.patch.object(kline_source.source_alert, "notify", return_value=True) as n:
            kline_source._note_fail("alphafeed", detail="600519.SH 1m (stock)")
            self.assertEqual(n.call_count, 0, "没到阈值不该告警")
            kline_source._note_fail("alphafeed", detail="600519.SH 1m (stock)")
        self.assertEqual(n.call_count, 1, "进冷却时该告警一次")
        src, reason, kw = n.call_args.args[0], n.call_args.args[1], n.call_args.kwargs
        self.assertEqual(src, "alphafeed")
        self.assertIn("连续失败", reason)
        self.assertEqual(kw.get("detail"), "600519.SH 1m (stock)", "要带上最后失败的上下文")
        self.assertEqual(kw.get("cooldown_sec"), kline_source.SOURCE_COOLDOWN_SEC)
        kline_source.reset_health()

    def test_reset_health_also_clears_the_alert_gate(self):
        import source_alert as sa
        sa.notify("alphafeed", "连续失败 3 次")   # 占掉今天的配额
        import kline_source
        kline_source.reset_health()
        self.assertEqual(sa.notified_today(), {}, "reset_health 要连告警闸门一起清 (测试隔离)")

    def test_mr_429_calls_notify_with_safe_detail(self):
        import market
        with mock.patch.object(market.source_alert, "notify", return_value=True) as n:
            e = Exception("HTTP 429")
            e.status_code = 429
            with mock.patch.object(market, "MAIRUI_BACKOFF_SEC", 60.0):
                market._mr_note_429(e, detail="stock_history 600519.SH")
        src, reason, kw = n.call_args.args[0], n.call_args.args[1], n.call_args.kwargs
        self.assertEqual(src, "mairui")
        self.assertIn("429", reason)
        self.assertIn("stock_history 600519.SH", kw.get("detail"))
        self.assertEqual(kw.get("cooldown_sec"), 60.0)

    def test_mr_detail_helpers_never_include_the_licence_key(self):
        import market
        self.assertEqual(market._mr_call_detail("stock_history", ("600519.SH", 9)),
                         "stock_history() 600519.SH")
        # 非"代码形态"的参数 (URL/dict/带空格) 一律不带进通知
        self.assertEqual(market._mr_call_detail("x", ("https://api.mairuiapi.com/a/b/key",)),
                         "x()")
        self.assertEqual(market._mr_call_detail("x", ({"symbol": "600519.SH"},)), "x()")
        self.assertEqual(market._mr_call_detail("x", ()), "x()")
        detail = market._mr_url_detail("https://api.mairuiapi.com/hszbl/fsjy/600519.SH/SECRETKEY")
        self.assertEqual(detail, "https://api.mairuiapi.com/hszbl/fsjy/600519.SH")
        self.assertNotIn("SECRETKEY", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
