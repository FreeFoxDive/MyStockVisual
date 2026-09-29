# -*- coding: utf-8 -*-
"""memguard: 用临时目录伪造 cgroup 文件, 覆盖阈值、节流、恢复和 oom_kill。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import memguard  # noqa: E402


def _write(root: Path, name: str, text: str):
    (root / name).write_text(text, encoding="utf-8")


class MemguardTest(unittest.TestCase):
    def setUp(self):
        memguard.reset_state()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root, "memory.max", "1000")
        _write(self.root, "memory.stat", "inactive_file 0\n")
        _write(self.root, "memory.events", "oom_kill 0\n")
        _write(self.root, "memory.pressure", "some avg10=0.10 avg60=0.00 avg300=0.00 total=1\n")
        self.notes = []

    def tearDown(self):
        memguard.reset_state()
        self._tmp.cleanup()

    def _snap(self, current, oom="0"):
        _write(self.root, "memory.current", str(current))
        _write(self.root, "memory.events", f"oom_kill {oom}\n")
        return memguard.sample(self.root)

    def _check(self, current, now, oom="0", relieve=None):
        snap = self._snap(current, oom)
        return memguard.check_once(
            snap=snap, now=now,
            notify_fn=lambda title, text: self.notes.append(title),
            relieve_fn=relieve or (lambda: ["cleared"]),
        )

    def test_warn_notifies_once_inside_cooldown(self):
        self._check(800, now=0)          # 80%
        self._check(820, now=10)
        self.assertEqual(self.notes, ["内存预警"])
        self.assertFalse(memguard.under_pressure())

    def test_crit_relieves_once_and_sets_pressure(self):
        calls = {"n": 0}

        def relieve():
            calls["n"] += 1
            return ["清空K线缓存"]

        self._check(950, now=0, relieve=relieve)   # 95%
        self._check(960, now=10, relieve=relieve)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(memguard.under_pressure())
        self.assertEqual(self.notes, ["内存紧急"])

    def test_recover_clears_pressure(self):
        self._check(950, now=0)
        self.assertTrue(memguard.under_pressure())
        self._check(100, now=10)          # 10% < 65
        self.assertFalse(memguard.under_pressure())
        self.assertIn("内存恢复", self.notes)

    def test_oom_kill_increase_notifies_immediately(self):
        self._check(100, now=0, oom="0")
        self.notes.clear()
        self._check(100, now=1, oom="1")
        self.assertEqual(self.notes, ["容器内 OOM"])

    def test_crit_notify_throttled_but_pressure_kept(self):
        self._check(950, now=0)
        self._check(950, now=60)
        self.assertEqual(self.notes, ["内存紧急"], "30 分钟内紧急通知只发一次")
        self._check(950, now=memguard.COOLDOWN_SEC + 1)
        self.assertEqual(self.notes, ["内存紧急", "内存紧急"])

    def test_warn_after_crit_keeps_pressure_until_recover_line(self):
        self._check(950, now=0)
        self._check(800, now=10)          # 80%: 仍高于恢复线 65%
        self.assertTrue(memguard.under_pressure(), "回落到预警区间不能解除压力")

    def test_second_crit_episode_relieves_again(self):
        calls = {"n": 0}

        def relieve():
            calls["n"] += 1
            return []

        self._check(950, now=0, relieve=relieve)
        self._check(100, now=10, relieve=relieve)
        self._check(950, now=20, relieve=relieve)
        self.assertEqual(calls["n"], 2, "恢复之后再次进入紧急要重新自救")

    def test_no_notify_when_usage_unknown(self):
        memguard.check_once(snap={"pct": None, "oom_kill": None}, now=0,
                            notify_fn=lambda t, x: self.notes.append(t))
        self.assertEqual(self.notes, [])

    def test_cgroup_v1_layout(self):
        for name in ("memory.current", "memory.max", "memory.events", "memory.pressure"):
            (self.root / name).unlink(missing_ok=True)
        _write(self.root, "memory.usage_in_bytes", "800")
        _write(self.root, "memory.limit_in_bytes", "1000")
        _write(self.root, "memory.stat", "total_inactive_file 100\n")
        _write(self.root, "memory.oom_control", "oom_kill_disable 0\nunder_oom 0\noom_kill 3\n")
        snap = memguard.sample(self.root)
        self.assertEqual(snap["usage_bytes"], 700)
        self.assertEqual(snap["pct"], 70.0)
        self.assertEqual(snap["oom_kill"], 3)

    def test_unlimited_cgroup_falls_back_to_configured_limit(self):
        (self.root / "memory.max").unlink()
        _write(self.root, "memory.current", str(200 * 1024 * 1024))
        snap = memguard.sample(self.root)
        self.assertEqual(snap["limit_bytes"], int(memguard.LIMIT_MB * 1024 * 1024))
        self.assertEqual(snap["pct"], 50.0)
        # v1 无上限的哨兵值同样视为没设上限
        (self.root / "memory.current").unlink()
        _write(self.root, "memory.usage_in_bytes", str(200 * 1024 * 1024))
        _write(self.root, "memory.limit_in_bytes", "9223372036854771712")
        self.assertEqual(memguard.sample(self.root)["pct"], 50.0)

    def test_relieve_clears_caches_and_stops_build(self):
        import chips
        import factors
        import market
        import premium
        from api import kline as kline_api
        market.kline_cache.set("k", {"x": 1})
        kline_api._tail_cache.set("t", {"x": 1})
        chips._cache[("A", "1d")] = (0.0, {"x": 1})
        premium._cache["p"] = (0.0, {"x": 1})
        with mock.patch.object(factors, "stop_build_children", return_value=1) as stop:
            actions = memguard.relieve()
        stop.assert_called_once()
        self.assertEqual(len(market.kline_cache), 0)
        self.assertEqual(len(kline_api._tail_cache), 0)
        self.assertEqual(chips._cache, {})
        self.assertEqual(premium._cache, {})
        self.assertTrue(memguard.under_pressure())
        self.assertIn("终止构建子进程1个", actions)

    def test_sample_subtracts_inactive_file(self):
        _write(self.root, "memory.current", "1000")
        _write(self.root, "memory.stat", "inactive_file 400\n")
        snap = memguard.sample(self.root)
        self.assertEqual(snap["usage_bytes"], 600)
        self.assertEqual(snap["limit_bytes"], 1000)
        self.assertEqual(snap["pct"], 60.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
