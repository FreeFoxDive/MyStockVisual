# -*- coding: utf-8 -*-
"""notify 队列: 保序 / 零阻塞 / 进度去重 / 过期丢弃 / 失败降级。

用例用「挡板」让 worker 卡在发送中, 之后的 notify() 确定性地停留在队列里,
从而可以断言入队侧行为; 断言投递侧时再放行 + flush。

运行:
    venv/Scripts/python.exe -u visual/test/test_notify.py
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import notify  # noqa: E402


class _Recorder:
    """记录各通道收到的 (通道, 标题, 正文)。"""

    BLOCK_TITLE = "__block__"

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def dingtalk(self, title, text):
        with self.lock:
            self.calls.append(("dingtalk", title, text))
        return True

    def ntfy(self, title, text):
        with self.lock:
            self.calls.append(("ntfy", title, text))
        return True

    def titles(self, channel="dingtalk"):
        return [c[1] for c in self.calls if c[0] == channel]


class NotifyQueueTest(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self._release = None
        self._gate_patch = None
        for p in (
            mock.patch("dingtalk.send_markdown", self.rec.dingtalk),
            mock.patch("ntfy.send_markdown", self.rec.ntfy),
            mock.patch.object(notify, "_sent", 0),
            mock.patch.object(notify, "_failed", 0),
            mock.patch.object(notify, "_skipped", 0),
            mock.patch.object(notify, "_dropped", 0),
            mock.patch.object(notify, "_expired", 0),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._unblock)
        notify.flush(5.0)  # 清掉上一用例可能残留的消息

    # ── 工具 ──
    def _block(self):
        """让 worker 卡在发送「占位」消息上; 返回放行用 Event。"""
        started, release = threading.Event(), threading.Event()
        real = notify._send_both

        def gated(title, text):
            if title == _Recorder.BLOCK_TITLE:
                started.set()
                release.wait(5.0)
                return True
            return real(title, text)

        self._gate_patch = mock.patch.object(notify, "_send_both", gated)
        self._gate_patch.start()
        self._release = release
        self.assertTrue(notify.notify(_Recorder.BLOCK_TITLE, "占位"))
        self.assertTrue(started.wait(5.0), "worker 未开始发送")
        return release

    def _unblock(self):
        if self._release is not None:
            self._release.set()
        if self._gate_patch is not None:
            notify.flush(5.0)
            self._gate_patch.stop()
            self._gate_patch = None
        notify.flush(5.0)

    # ── 用例 ──
    def test_sequence_preserved(self):
        for i in range(5):
            self.assertTrue(notify.notify(f"消息{i}", f"正文{i}"))
        self.assertEqual(notify.flush(5.0), 0)
        self.assertEqual(self.rec.titles(), [f"消息{i}" for i in range(5)])
        # 一条消息两个通道, 顺序固定: 先钉钉后 ntfy
        self.assertEqual([c[0] for c in self.rec.calls[:2]], ["dingtalk", "ntfy"])

    def test_producer_returns_fast_while_worker_sends(self):
        self._block()
        t0 = time.perf_counter()
        for i in range(20):
            notify.notify(f"排队{i}", "x")
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.5, "生产者不得等发送")
        with notify._cv:
            self.assertGreaterEqual(len(notify._q), 20)

    def test_progress_same_key_keeps_latest(self):
        self._block()
        notify.notify("选股 25%", "k=1", key="screener:1", kind="progress")
        notify.notify("选股 75%", "k=1", key="screener:1", kind="progress")
        notify.notify("另一个任务", "k=2", key="screener:2", kind="progress")
        self._release.set()
        self.assertEqual(notify.flush(5.0), 0)
        titles = self.rec.titles()
        self.assertNotIn("选股 25%", titles, "同 key 旧进度应被替换")
        self.assertIn("选股 75%", titles)
        self.assertIn("另一个任务", titles)

    def test_stale_progress_dropped_on_pop(self):
        self._block()
        notify.notify("过期进度", "x", key="screener:9", kind="progress",
                      ts=time.time() - notify.PROGRESS_TTL_SEC - 60)
        self._release.set()
        self.assertEqual(notify.flush(5.0), 0)
        self.assertNotIn("过期进度", self.rec.titles())
        self.assertEqual(notify._expired, 1)

    def test_event_message_delivered_even_if_old(self):
        self._block()
        notify.notify("迟到的完成", "x", ts=time.time() - notify.PROGRESS_TTL_SEC - 60)
        self._release.set()
        self.assertEqual(notify.flush(5.0), 0)
        self.assertIn("迟到的完成", self.rec.titles())

    def test_queue_full_drops_oldest_progress(self):
        with mock.patch.object(notify, "QUEUE_MAX", 3):
            self._block()
            notify.notify("事件A", "x")                                  # 队首事件
            notify.notify("进度1", "x", key="p1", kind="progress")
            notify.notify("进度2", "x", key="p2", kind="progress")
            notify.notify("进度3", "x", key="p3", kind="progress")       # 触发丢弃
        self._release.set()
        self.assertEqual(notify.flush(5.0), 0)
        self.assertEqual(notify._dropped, 1)
        titles = self.rec.titles()
        self.assertIn("事件A", titles, "事件消息不应被丢")
        self.assertNotIn("进度1", titles, "应丢最旧的进度消息")

    def test_one_channel_failure_does_not_block_other(self):
        with mock.patch("dingtalk.send_markdown", side_effect=RuntimeError("down")):
            self.assertTrue(notify.notify("只有 ntfy", "x"))
            self.assertEqual(notify.flush(5.0), 0)
        self.assertIn("只有 ntfy", self.rec.titles("ntfy"))
        self.assertEqual(notify._sent, 1)
        self.assertEqual(notify._failed, 0)

    def test_failed_send_counted_and_not_retried_forever(self):
        with mock.patch.object(notify, "_send_both", return_value=False), \
             mock.patch.object(notify, "_configured", return_value=True), \
             mock.patch.object(notify, "_report_failure") as rep:
            notify.notify("发不出去", "x")
            self.assertEqual(notify.flush(5.0), 0)
        self.assertEqual(notify._failed, 1)
        self.assertEqual(notify._sent, 0)
        rep.assert_called_once()

    def test_unconfigured_channels_are_not_failures(self):
        """回归: 没配任何通道时不该计 failed (那是"没开推送", 不是"推送失败")。"""
        with mock.patch.object(notify, "_send_both", return_value=False), \
             mock.patch.object(notify, "_configured", return_value=False), \
             mock.patch.object(notify, "_report_failure") as rep:
            notify.notify("静默通道", "x")
            self.assertEqual(notify.flush(5.0), 0)
        self.assertEqual(notify._failed, 0)
        self.assertEqual(notify._skipped, 1)
        rep.assert_not_called()
        self.assertEqual(notify.stats()["skipped"], 1)

    def test_disabled_silences_everything(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DISABLED": "1"}):
            self.assertFalse(notify.notify("静默", "x"))
        self.assertEqual(notify.flush(1.0), 0)
        self.assertEqual(self.rec.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
