"""数据源退避通知: **每个源每天最多一条**。

由 `market` 在麦蕊命中官方 429 限流时调用。闸门按源名记，跨日恢复。

写法定式照 `af_intraday.py`: {key: date} + 锁 + 可 patch 的 `_today()` + `reset()`。
真正的发送委托给 `error_notify.notify_alert` (异步队列 + 工作线程 + 全局预算 + 双通道),
本模块只决定"该不该发、发什么"。

环境变量:
  SOURCE_ALERT_DISABLED=1   关掉数据源告警 (默认开; 未配置的通道由 dingtalk/ntfy 各自跳过)

**测试侧注意**: 触发 429 退避的用例必须打桩本条路径 (`source_alert.notify`), 否则会真的发出
钉钉/ntfy 消息 —— `visual/test/test_source_alert.py` 在模块级把 SOURCE_ALERT_DISABLED
默认置 1 兜底 (unittest discover 会先 import 完所有测试模块再跑), 单个文件单跑时由各
测试基类自己打桩。要真验通道连通性, 用 README 里 DINGTALK_LIVE=1 / NTFY_LIVE=1 那套。
"""
from __future__ import annotations

import logging
import os
import threading

import market_hours
from logger import redact_message

log = logging.getLogger("source_alert")

_lock = threading.Lock()
_notified = {}     # {源名: date} 当日已通知过的源


def _today():
    """当前北京日期 (闸门按自然日滚动; 测试可 patch 本函数)。"""
    return market_hours.now().date()


def _disabled() -> bool:
    return os.environ.get("SOURCE_ALERT_DISABLED", "").strip() in ("1", "true", "yes")


def _gate_key(source) -> str:
    """当日闸门的键为源名。"""
    return str(source or "?").strip() or "?"


def reset():
    """清空闸门 (测试隔离用)。"""
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


def _text(source, reason, detail, cooldown_sec) -> str:
    """正文必须自带源名。钉钉/ntfy 的 title 只出现在会话列表预览，群消息正文只有 text。"""
    who = _gate_key(source)
    parts = [f"数据源 {who}：{reason}"]
    if cooldown_sec:
        parts.append(f"→ {cooldown_sec:.0f}s 内跳过该源")
    line = " ".join(parts)
    if detail:
        # 通知正文可能带 URL/响应片段; 密钥脱敏后再发出去。日志侧只记源和原因。
        line += f"（最后一次请求: {redact_message(detail)}）"
    return line + " · 同一源当日仅通知一次"


def notify(source, reason, detail="", cooldown_sec=None) -> bool:
    """该源今天的退避通知: 发返回 True, 被当日闸门挡下或已关闭返回 False。

    先占配额再发 (两个线程同时触发也只发一条), 但**发不出去就退回配额** ——
    "每天一条"限的是真发出去的那条, 不是把当天的机会浪费在一次入队失败上。
    任何异常都不外抛 —— 告警绝不能影响取数主流程。
    """
    try:
        if _disabled():
            return False
        if not _claim(source):
            log.info("数据源 %s 今日已通知过 %s, 不再重复推送", source, reason)
            return False
        text = _text(source, reason, detail, cooldown_sec)
        import error_notify
        ok = error_notify.notify_alert(f"source:{source}", f"数据源告警: {source}", text)
        if ok:
            # 不记 text/detail: 麦蕊 URL 末段是证书 key, 整段正文仍被 CodeQL 视为敏感。
            log.info("已推送数据源告警: %s %s", source, reason)
        else:
            _release(source)
        return bool(ok)
    except Exception as e:      # 告警路径不许把取数拖下水
        _release(source)
        log.warning("数据源告警推送失败 (%s): %s", source, e)
        return False
