"""异步节流错误通知 + 日志观察者。

设计要点(见 doc/VISUAL_错误处理补强计划.md):
- notify_error / notify_exception / notify_alert **零阻塞**:只入队, 由守护线程消费; 队列满则丢弃计数。
- 同 source 在窗口内聚合计数(只发一条摘要); 跨 source 有全局预算, 防通知风暴。
- 只发**脱敏摘要**(异常类型 + 文件:行), 完整堆栈只进本地日志, 不出境。
- install_log_handler() 监听 root logger 上带 exc_info 的 ERROR 记录, 真实出错位置取
  exc_info 最深栈帧(Flask 的 log_exception 其 record.pathname 指向 flask/app.py, 不可用)。
- 本模块自身绝不抛异常, 也不因自身日志触发递归通知。

两类入队:
  * **异常摘要** (notify_error/notify_exception): 文案由本模块拼 (源 + 类型 + 位置)。
  * **自定义文案** (notify_alert): 运维事件 (如数据源熔断) 的标题/正文由调用方给。
    两者共用同一条队列、同一个工作线程、同一套按 source 去重与全局预算。

环境变量:
  ERROR_NOTIFY_DISABLED=1      全部静默(测试/CI 用)
  ERROR_NOTIFY_WINDOW_SEC      同 source 聚合窗口(默认 300)
  ERROR_NOTIFY_BUDGET_PER_MIN  跨 source 每分钟上限(默认 5)
  ERROR_NOTIFY_QUEUE_MAX       队列容量(默认 50)
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import traceback
from collections import deque

log = logging.getLogger("error_notify")

_PREFIXES = ("error_notify", "dingtalk", "ntfy", "myappnotify")

WINDOW_SEC = max(1.0, float(os.environ.get("ERROR_NOTIFY_WINDOW_SEC", "300")))
BUDGET_PER_MIN = max(1, int(os.environ.get("ERROR_NOTIFY_BUDGET_PER_MIN", "5")))
QUEUE_MAX = max(1, int(os.environ.get("ERROR_NOTIFY_QUEUE_MAX", "50")))

_q: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX)
_lock = threading.Lock()
_recent: dict = {}        # source -> [window_start, count, notified]
_sent_times: deque = deque()
_dropped = 0              # 队列满丢弃
_suppressed = 0           # 窗口/预算抑制
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_sending = threading.local()

_THREAD_RE = re.compile(r"Thread-\d+")


def _disabled() -> bool:
    return os.environ.get("ERROR_NOTIFY_DISABLED", "").strip() in ("1", "true", "yes")


def normalize_source(source) -> str:
    """剥离 CPython 线程名的自增序号, 使同类线程共享节流键。"""
    return _THREAD_RE.sub("Thread-N", str(source or "?")).strip() or "?"


def describe_exc(exc) -> tuple:
    """(异常类型名, 文件:行) — 位置取最深栈帧。"""
    if exc is None:
        return ("Error", "?")
    tb = getattr(exc, "__traceback__", None)
    loc = "?"
    if tb is not None:
        frames = traceback.extract_tb(tb)
        if frames:
            f = frames[-1]
            loc = f"{os.path.basename(f.filename)}:{f.lineno}"
    return (type(exc).__name__, loc)


def notify_error(source, exc_type=None, location=None):
    """零阻塞入队。任何情况下不抛。"""
    if _disabled():
        return False
    global _dropped
    item = {
        "kind": "error",
        "source": normalize_source(source),
        "exc_type": str(exc_type or "Error"),
        "location": str(location or "?"),
        "ts": time.time(),
    }
    _start_worker()
    try:
        _q.put_nowait(item)
        return True
    except queue.Full:
        with _lock:
            _dropped += 1
        return False


def notify_alert(source, title, text):
    """运维事件通知: 标题/正文由调用方给 (如"数据源熔断: alphafeed")。

    与 notify_error 共用队列/工作线程/按 source 去重/全局预算/双通道, 区别只在文案:
    异常摘要由本模块拼, 事件文案由调用方拼 (数据源熔断这类事件不是异常, 摘要格式说
    不清"哪个源、为什么、冷却多久")。零阻塞, 任何情况下不抛。
    """
    if _disabled():
        return False
    global _dropped
    try:
        from logger import redact_message
        text = redact_message(text)
    except Exception:
        pass
    item = {
        "kind": "alert",
        "source": normalize_source(source),
        "title": str(title or "服务告警"),
        "text": str(text or ""),
        "ts": time.time(),
    }
    _start_worker()
    try:
        _q.put_nowait(item)
        return True
    except queue.Full:
        with _lock:
            _dropped += 1
        return False


def notify_exception(source, exc):
    """便捷封装: 从异常对象提取类型与位置。"""
    exc_type, location = describe_exc(exc)
    return notify_error(source, exc_type, location)


def stats() -> dict:
    with _lock:
        return {
            "queued": _q.qsize(),
            "dropped": _dropped,
            "suppressed": _suppressed,
            "sources": sorted(_recent.keys()),
        }


# ── 发送 ──
def _summary_text(source, exc_type, location, count) -> str:
    mins = max(1, int(WINDOW_SEC // 60)) if WINDOW_SEC >= 60 else 0
    window = f"近{mins}分钟" if mins else "近期"
    text = f"[{source}] {window} {count} 次: {exc_type} @ {location}"
    try:
        from logger import redact_message
        text = redact_message(text)
    except Exception:
        pass
    return text


def _send_both(title, text) -> None:
    """两通道各自独立 try/except; 期间关闭日志观察者防递归。"""
    _sending.active = True
    try:
        try:
            import dingtalk
            dingtalk.send_markdown(title, text)
        except Exception:
            log.warning("钉钉通知失败", exc_info=False)
        try:
            import ntfy
            ntfy.send_markdown(title, text)
        except Exception:
            log.warning("ntfy 通知失败", exc_info=False)
    finally:
        _sending.active = False


def _budget_ok(now) -> bool:
    while _sent_times and now - _sent_times[0] > 60.0:
        _sent_times.popleft()
    return len(_sent_times) < BUDGET_PER_MIN


def _process_batch(items) -> int:
    """按 source 聚合后发送; 返回实际发送条数。"""
    global _suppressed
    if not items:
        return 0
    groups: dict = {}
    for it in items:
        groups.setdefault(it["source"], []).append(it)

    sent = 0
    now = time.time()
    for source, group in groups.items():
        with _lock:
            state = _recent.get(source)
            if state and now - state[0] < WINDOW_SEC and state[2]:
                state[1] += len(group)
                _suppressed += 1
                continue
            base = state[1] if state and now - state[0] < WINDOW_SEC else 0
            window_start = state[0] if state and now - state[0] < WINDOW_SEC else now
            count = base + len(group)
            if not _budget_ok(now):
                _suppressed += 1
                _recent[source] = [window_start, count, False]
                continue
            _sent_times.append(now)
            _recent[source] = [window_start, count, True]

        last = group[-1]
        if last.get("kind") == "alert":
            # 事件通知: 文案调用方给, 不做摘要聚合 (同一 source 窗口内只发第一条)
            _send_both(last.get("title") or "服务告警", last.get("text") or "")
        else:
            _send_both("服务告警", _summary_text(source, last["exc_type"],
                                                 last["location"], count))
        sent += 1
    return sent


def drain_once(timeout: float = 0.0) -> int:
    """消费一批(测试可直接调用, 避免依赖线程时序)。"""
    items = []
    try:
        if timeout > 0:
            items.append(_q.get(timeout=timeout))
        else:
            items.append(_q.get_nowait())
    except queue.Empty:
        return 0
    while True:
        try:
            items.append(_q.get_nowait())
        except queue.Empty:
            break
    return _process_batch(items)


def _run():
    while True:
        try:
            drain_once(timeout=0.2)
        except Exception:
            # 通知线程自身绝不因单次失败退出
            time.sleep(0.5)


def _start_worker():
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name="error-notify", daemon=True)
        _worker.start()


# ── 日志观察者 ──
class _ObserverHandler(logging.Handler):
    """监听 ERROR 且带 exc_info 的记录 → 异步通知。"""

    def emit(self, record):
        try:
            if getattr(_sending, "active", False):
                return
            if record.levelno < logging.ERROR or not record.exc_info:
                return
            name = record.name or ""
            if name.startswith(_PREFIXES):
                return
            exc_info = record.exc_info
            if not isinstance(exc_info, tuple) or len(exc_info) < 3:
                return
            exc_type = exc_info[0]
            type_name = getattr(exc_type, "__name__", None) or str(exc_type)
            loc = "?"
            tb = exc_info[2]
            if tb is not None:
                frames = traceback.extract_tb(tb)
                if frames:
                    f = frames[-1]
                    loc = f"{os.path.basename(f.filename)}:{f.lineno}"
            notify_error(f"log:{type_name}", type_name, loc)
        except Exception:
            pass


def install_log_handler():
    """幂等地给 root logger 挂观察者。"""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _ObserverHandler):
            return h
    handler = _ObserverHandler()
    handler.setLevel(logging.ERROR)
    root.addHandler(handler)
    return handler


def uninstall_log_handler():
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, _ObserverHandler):
            root.removeHandler(h)
