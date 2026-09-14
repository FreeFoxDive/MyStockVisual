"""轻量性能观测: 请求耗时分解 + 上游健康计数。

用于定位「切换 symbol 忽快忽慢」的耗时构成 (磁盘命中 / 实际数据源 / 强制行情
往返 / 指标计算 / 序列化)。设 KLINE_PERF_LOG=1 时 /api/kline 打一条结构化日志;
默认关闭时只多几次 dict 写入, 可忽略。

计数 (bump) 无条件累计, 便于把「回退 / 429 / 单源失败」的频率随日志一并输出,
确认慢请求是否集中在少数坏源上。
"""
from __future__ import annotations

import os
import threading
import time

# 仅控制 /api/kline 的日志输出; 计数与 Span 在关闭时仍可用 (开销可忽略)。
ENABLED = os.environ.get("KLINE_PERF_LOG", "").strip().lower() not in (
    "", "0", "false", "no", "off",
)

_lock = threading.Lock()
_counters: dict = {}


def bump(name: str, n: int = 1) -> None:
    """上游异常计数 (429 退避 / 数据源回退 / 单源失败)。"""
    with _lock:
        _counters[name] = _counters.get(name, 0) + n


def counters() -> dict:
    with _lock:
        return dict(_counters)


def reset_counters() -> None:
    with _lock:
        _counters.clear()


def add_ms(timing, key: str, ms: float) -> None:
    """累加耗时 (ms)。timing 为 None 时静默跳过 (未开启观测的调用路径)。"""
    if timing is None:
        return
    timing[key] = round(timing.get(key, 0.0) + ms, 1)


class Span:
    """with Span(timing, "fetch_ms"): ... → 退出时累加该键耗时 (ms)。

    timing 为 None 时仍可作上下文管理器使用 (不记录), 便于把观测代码留在
    热路径上而不必到处写 if。
    """

    __slots__ = ("_sink", "_key", "_t0")

    def __init__(self, sink, key: str):
        self._sink = sink
        self._key = key

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        add_ms(self._sink, self._key, (time.perf_counter() - self._t0) * 1000.0)
        return False


def fmt(timing, **extra) -> str:
    """timing + 额外字段拼成一行 key=value, 便于 grep/聚合。"""
    fields = dict(timing or {})
    fields.update(extra)
    return " ".join(f"{k}={v}" for k, v in fields.items())
