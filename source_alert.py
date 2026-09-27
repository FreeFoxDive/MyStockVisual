"""数据源熔断/退避通知: **每个源每天最多一条**。

要通知的两件事 (两者都会让整条取数链降级, 但平时只在日志里, 没人盯):
  * `kline_source` 的源级熔断 —— 连续失败达阈值 → 冷却 SOURCE_COOLDOWN_SEC, 窗口内跳过该源;
  * `market` 的麦蕊 429 退避 —— 命中官方限流 → 进程级退避, 窗口内不再发 HTTP。

闸门 key 就是**源名**(alphafeed / mairui / akshare...), 也就是同一源的两类事件共用一条
当日配额 (同一天最多收到一条关于麦蕊的告警, 正文里区分是 429 还是连续失败)。这是刻意的:
"每源每天一条"是防打扰口径, 不是"每种故障各一条"; 要拆成两个键, 改 `_gate_key()` 一行。

写法定式照 `af_intraday.py`: {key: date} + 锁 + 可 patch 的 `_today()` + `reset()`。
真正的发送委托给 `error_notify.notify_alert` (异步队列 + 工作线程 + 全局预算 + 双通道),
本模块只决定"该不该发、发什么"。

环境变量:
  SOURCE_ALERT_DISABLED=1   关掉数据源告警 (默认开; 未配置的通道由 dingtalk/ntfy 各自跳过)

**测试侧注意**: 触发熔断的用例必须打桩本条路径 (`source_alert.notify`), 否则会真的发出
钉钉/ntfy 消息 —— `visual/test/test_source_alert.py` 在模块级把 SOURCE_ALERT_DISABLED
默认置 1 兜底 (unittest discover 会先 import 完所有测试模块再跑), 单个文件单跑时由各
测试基类自己打桩。要真验通道连通性, 用 README 里 DINGTALK_LIVE=1 / NTFY_LIVE=1 那套。
"""
from __future__ import annotations

import logging
import os
import threading

import market_hours

log = logging.getLogger("source_alert")

_lock = threading.Lock()
_notified = {}     # {源名: date} 当日已通知过的源


def _today():
    """当前北京日期 (闸门按自然日滚动; 测试可 patch 本函数)。"""
    return market_hours.now().date()


def _disabled() -> bool:
    return os.environ.get("SOURCE_ALERT_DISABLED", "").strip() in ("1", "true", "yes")


def _gate_key(source) -> str:
    """当日闸门的键。现在是源名 (同源的两类故障合用一条配额), 见模块 docstring。"""
    return str(source or "?").strip() or "?"


def reset():
    """清空闸门 (测试隔离用; kline_source.reset_health 也会调它)。"""
    with _lock:
        _notified.clear()


def notified_today():
    """当日已通知过的源 → 日期 (供测试/日志查看)。"""
    return dict(_notified)


def _claim(source) -> bool:
    """占用该源今天的配额: True = 还没发过 (该发), False = 今天已经发过。"""
    key = _gate_key(source)
    today = _today()
    with _lock:
        if _notified.get(key) == today:
            return False
        _notified[key] = today
        return True


def _release(source) -> None:
    """退回配额 (入队失败时): 今天其实一条都没发出去, 不该白白占掉唯一一次机会。"""
    key = _gate_key(source)
    with _lock:
        if _notified.get(key) == _today():
            _notified.pop(key, None)


def _text(reason, detail, cooldown_sec) -> str:
    parts = [reason]
    if cooldown_sec:
        parts.append(f"→ {cooldown_sec:.0f}s 内跳过该源")
    line = " ".join(parts)
    if detail:
        line += f"（最后失败: {detail}）"
    return line + " · 同一源当日仅通知一次"


def notify(source, reason, detail="", cooldown_sec=None) -> bool:
    """该源今天的熔断/退避通知: 发返回 True, 被当日闸门挡下或已关闭返回 False。

    先占配额再发 (两个线程同时熔断也只发一条), 但**发不出去就退回配额** ——
    "每天一条"限的是真发出去的那条, 不是把当天的机会浪费在一次入队失败上。
    任何异常都不外抛 —— 告警绝不能影响取数主流程。
    """
    try:
        if _disabled():
            return False
        if not _claim(source):
            log.info("数据源 %s 今日已通知过 %s, 不再重复推送", source, reason)
            return False
        text = _text(reason, detail, cooldown_sec)
        import error_notify
        ok = error_notify.notify_alert(f"source:{source}", f"数据源告警: {source}", text)
        if ok:
            log.info("已推送数据源告警: %s %s", source, text)
        else:
            _release(source)
        return bool(ok)
    except Exception as e:      # 告警路径不许把取数拖下水
        _release(source)
        log.warning("数据源告警推送失败 (%s): %s", source, e)
        return False
