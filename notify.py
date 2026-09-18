"""有序非阻塞业务通知队列 (钉钉 + ntfy)。

与 error_notify.py 的分工:

- error_notify: 只发**脱敏错误摘要**, 按 source 聚合 + 预算, 同类消息会被合并;
- notify (本模块): 承载**有序业务通知** (持仓提醒 / 选股进度 / 因子构建), 不聚合,
  严格按入队顺序投递。

为什么需要它: myappnotify 的 ``urlopen(timeout=10)`` 是同步的, 两个通道最坏阻塞
20 秒。监控循环、选股 worker、因子构建线程都不得被推送拖住 —— 生产者只入队,
由单个守护线程消费。

保序: 单线程 + FIFO, 同一时刻只有一条消息在发; 一条消息内先钉钉后 ntfy, 各自
独立 try/except (单通道失败不影响另一通道, 也不打乱后续消息顺序)。失败原地重试
一次 (NOTIFY_RETRY_BACKOFF_SEC), 仍失败只计数并经 error_notify 暴露, 不无限重试。

进度消息 (``kind="progress"`` + ``key``): 入队时移除同 key 的旧进度消息只留最新;
出队时入队超过 NOTIFY_PROGRESS_TTL_SEC 的进度消息直接丢弃 —— 进度语义单调,
丢弃旧值不产生乱序, 也不会让用户收到一串已经过时的进度。

事实来源是站内 (DB): 业务事件一律**先写库再入队**; 页面读库, 推送丢失不影响站内
时序与正确性。进程重启会丢队列中未发出的推送 (站内仍完整), 这是刻意保留的简单性。

环境变量:
  NOTIFY_DISABLED=1        全部静默 (测试/CI)
  NOTIFY_QUEUE_MAX         队列容量 (默认 200)
  NOTIFY_PROGRESS_TTL_SEC  进度消息过期秒数 (默认 300; 0 = 不过期)
  NOTIFY_RETRY_BACKOFF_SEC 失败重试退避秒 (默认 2.0; 0 = 不重试)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

log = logging.getLogger("notify")

QUEUE_MAX = max(1, int(os.environ.get("NOTIFY_QUEUE_MAX", "200")))
PROGRESS_TTL_SEC = max(0.0, float(os.environ.get("NOTIFY_PROGRESS_TTL_SEC", "300")))
RETRY_BACKOFF_SEC = max(0.0, float(os.environ.get("NOTIFY_RETRY_BACKOFF_SEC", "2.0")))

_cv = threading.Condition()
_q: deque = deque()
_busy = False
_sent = 0
_failed = 0
_skipped = 0      # 未配置任何通道 → 不算失败 (与 _configured 的注释对齐)
_dropped = 0
_expired = 0
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _disabled() -> bool:
    return os.environ.get("NOTIFY_DISABLED", "").strip() in ("1", "true", "yes")


def notify(title, text, *, key=None, kind="event", ts=None) -> bool:
    """零阻塞入队。任何情况下不抛, 生产者永不因推送卡住。

    key + kind="progress" 时同 key 只保留最新一条 (进度语义单调, 丢弃旧值不产生乱序)。
    队列满时优先丢弃最旧的进度消息 (没有则丢最旧一条), 新消息总是保留。
    返回是否入队成功 (未配置/静默/异常都返回 False, 不影响调用方流程)。
    """
    if _disabled():
        return False
    global _dropped
    try:
        _start_worker()
        item = {
            "title": str(title), "text": str(text),
            "key": key, "kind": str(kind or "event"),
            "ts": time.time() if ts is None else float(ts),
        }
        with _cv:
            if item["kind"] == "progress" and item["key"] is not None:
                for i, it in enumerate(_q):
                    if it["kind"] == "progress" and it["key"] == item["key"]:
                        del _q[i]
                        break
            if len(_q) >= QUEUE_MAX:
                victim = next(
                    (i for i, it in enumerate(_q) if it["kind"] == "progress"), 0)
                del _q[victim]
                _dropped += 1
                log.warning("通知队列已满, 丢弃最旧一条 (累计 %d)", _dropped)
            _q.append(item)
            _cv.notify_all()
        return True
    except Exception as e:      # 通知绝不反向影响业务线程
        log.warning("通知入队失败: %s", e)
        return False


def stats() -> dict:
    with _cv:
        return {"queued": len(_q), "busy": _busy, "sent": _sent,
                "failed": _failed, "skipped": _skipped, "dropped": _dropped,
                "expired": _expired}


# ── 发送 ──
def _configured() -> bool:
    """是否至少配置了一个推送通道 (未配置时的 False 不计入失败)。"""
    try:
        import dingtalk
        dingtalk._load_env()
    except Exception:
        pass
    if os.environ.get("DINGDING_WEB_HOOK_TOKEN") and os.environ.get("DINGDING_BOT_SIGN"):
        return True
    try:
        import ntfy
        ntfy._load_env()
        return bool(ntfy._env_ready())
    except Exception:
        return False


def _send_both(title, text) -> bool:
    """先钉钉后 ntfy, 各自独立 try/except; 任一成功即算成功。"""
    ok = False
    try:
        import dingtalk
        ok = bool(dingtalk.send_markdown(title, text)) or ok
    except Exception:
        log.warning("钉钉推送异常", exc_info=False)
    try:
        import ntfy
        ok = bool(ntfy.send_markdown(title, text)) or ok
    except Exception:
        log.warning("ntfy 推送异常", exc_info=False)
    return ok


def _report_failure(item):
    """失败只经 error_notify 暴露 (带窗口聚合); 未配置通道时不报 (不是故障)。"""
    if not _configured():
        return
    try:
        import error_notify
        error_notify.notify_error("push", "SendFailed", item.get("title") or "-")
    except Exception:
        pass


def _deliver(item) -> bool:
    """发送一条消息 (含一次重试与计数)。返回是否成功。

    未配置任何通道 (钉钉/ntfy 都没配) 时不计入 failed 也不上报 —— 那是"没开推送",
    不是"推送失败"。
    """
    global _sent, _failed, _skipped
    ok = _send_both(item["title"], item["text"])
    if not ok and RETRY_BACKOFF_SEC > 0:
        time.sleep(RETRY_BACKOFF_SEC)
        ok = _send_both(item["title"], item["text"])
    if ok:
        with _cv:
            _sent += 1
        return True
    configured = _configured()
    with _cv:
        if configured:
            _failed += 1
        else:
            _skipped += 1
    if configured:
        log.warning("推送失败 (钉钉/ntfy 均未成功): %s", item["title"])
        _report_failure(item)
    return False


def _pop(timeout: float):
    """取队首消息并**在同一把锁内**置 busy (顺带丢弃过期进度); 超时返回 None。

    取件与置忙必须原子: 否则 flush() 可能在 "已出队、还没置忙" 的空窗里看到
    「队列空且空闲」而提前返回 (消费侧还以为消息没发完)。
    """
    global _expired, _busy
    deadline = time.monotonic() + max(0.0, timeout)
    with _cv:
        while True:
            now = time.time()
            while _q:
                it = _q[0]
                if (it["kind"] == "progress" and PROGRESS_TTL_SEC > 0
                        and now - it["ts"] > PROGRESS_TTL_SEC):
                    _q.popleft()
                    _expired += 1
                    continue
                _q.popleft()
                _busy = True
                return it
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            _cv.wait(remaining)


def _finish_busy():
    global _busy
    with _cv:
        _busy = False
        _cv.notify_all()


def drain_once(timeout: float = 0.0) -> int:
    """同步消费一条 (测试用: 关掉 worker 后可确定性断言)。返回处理条数。"""
    item = _pop(timeout)
    if item is None:
        return 0
    try:
        _deliver(item)
    finally:
        _finish_busy()
    return 1


def flush(timeout: float = 5.0) -> int:
    """等待队列排空且当前消息发送完毕 (测试/关停前用)。返回超时后剩余条数。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _cv:
            if not _q and not _busy:
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return len(_q)
            _cv.wait(min(0.05, remaining))


def _run():
    while True:
        try:
            item = _pop(0.5)
            if item is None:
                continue
            try:
                _deliver(item)
            finally:
                _finish_busy()
        except Exception:
            # 通知线程自身绝不因单次失败退出
            _finish_busy()
            time.sleep(0.5)


def _start_worker():
    """幂等启动消费线程 (懒启动, 测试可 patch 掉)。"""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name="notify", daemon=True)
        _worker.start()
