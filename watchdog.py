"""后台线程看门狗: 死线程自愈 + 卡死/持续失败检测。

业界"进程内看门狗"一环:
- monitor 循环内的 last_poll 即心跳(死锁会自然停跳);
- 本模块周期检查: 线程死了就幂等重启(带指数退避与熔断), 心跳停/持续失败则告警;
- 容器 HEALTHCHECK 只提供可见性(不会触发重启), 进程级自愈靠 WATCHDOG_EXIT_ON_STALL。

Python 无法安全终止线程: 卡死(活着但不推进)只能告警, 除非显式开启进程级退出。

环境变量:
  WATCHDOG_INTERVAL_SEC     检查间隔(默认 60)
  WATCHDOG_GRACE_SEC        启动/开盘后的宽限期, 期内不判卡死(默认 300)
  MONITOR_STALL_SEC         交易时段内心跳最大年龄(默认 600)
  MONITOR_FAIL_SEC          last_error 持续未变的最大时长(默认 600)
  WATCHDOG_MAX_RESTARTS     连续重启次数上限, 超过熔断(默认 3)
  WATCHDOG_EXIT_ON_STALL=1  判定卡死后 os._exit(1) 触发容器重启(默认关)
"""
from __future__ import annotations

import logging
import os
import threading
import time

import error_notify

log = logging.getLogger("watchdog")

INTERVAL_SEC = max(5.0, float(os.environ.get("WATCHDOG_INTERVAL_SEC", "60")))
GRACE_SEC = max(0.0, float(os.environ.get("WATCHDOG_GRACE_SEC", "300")))
STALL_SEC = max(30.0, float(os.environ.get("MONITOR_STALL_SEC", "600")))
FAIL_SEC = max(30.0, float(os.environ.get("MONITOR_FAIL_SEC", "600")))
MAX_RESTARTS = max(1, int(os.environ.get("WATCHDOG_MAX_RESTARTS", "3")))
RESTART_BACKOFF_BASE = max(5.0, float(os.environ.get("WATCHDOG_RESTART_BACKOFF_SEC", "60")))


def _exit_on_stall() -> bool:
    return os.environ.get("WATCHDOG_EXIT_ON_STALL", "").strip() in ("1", "true", "yes")


_state = {
    "last_check": None,
    "last_check_ts": None,
    "last_actions": [],
    "restart_streak": 0,
    "breaker": False,
}
_lock = threading.Lock()
_thread = None
_session_seen_ts = None      # 本次开盘时段首次被观察到的时间(宽限期基准)
_next_restart_at = 0.0
_fail_key = None             # 当前持续失败的 last_error 文本
_fail_since = None
_fail_notified = False


def get_state():
    with _lock:
        return dict(_state)


def reset_state():
    """测试/重启用: 清空看门狗内部状态。"""
    global _session_seen_ts, _next_restart_at, _fail_key, _fail_since, _fail_notified
    with _lock:
        _state.update(last_check=None, last_check_ts=None, last_actions=[],
                      restart_streak=0, breaker=False)
    _session_seen_ts = None
    _next_restart_at = 0.0
    _fail_key = None
    _fail_since = None
    _fail_notified = False


def _age(since_ts, now):
    if since_ts is None:
        return None
    return max(0.0, now - float(since_ts))


def _restart_monitor(get_af, fallback_quotes, now):
    """幂等重启 monitor 线程; 返回 (ok, reason)。带指数退避与熔断。"""
    global _next_restart_at
    if now < _next_restart_at:
        return (False, "backoff")
    with _lock:
        _state["restart_streak"] += 1
        streak = _state["restart_streak"]
        if streak > MAX_RESTARTS:
            _state["breaker"] = True
    if streak > MAX_RESTARTS:
        if streak == MAX_RESTARTS + 1:
            error_notify.notify_error("watchdog-breaker", "RestartBreaker",
                                      f"连续重启超过 {MAX_RESTARTS} 次, 停止自愈")
        return (False, "breaker")
    _next_restart_at = now + min(RESTART_BACKOFF_BASE * (2 ** (streak - 1)), 1800.0)
    try:
        import monitor
        monitor.start_background(get_af, fallback_quotes)
        return (True, None)
    except Exception as e:
        error_notify.notify_exception("watchdog-restart", e)
        return (False, str(e))


def check_once(now=None, in_session=None, get_af=None, fallback_quotes=None):
    """执行一轮检查, 返回动作列表。纯函数式(除显式自愈/通知副作用), 便于测试。"""
    global _session_seen_ts, _fail_key, _fail_since, _fail_notified
    import market_hours
    import monitor

    now = time.time() if now is None else float(now)
    actions = []

    st = monitor.get_status()
    alive = monitor.is_thread_alive()

    if alive:
        # 线程存活: 复位退避与熔断, 允许下次崩溃时立即重启
        with _lock:
            _state["restart_streak"] = 0
            _state["breaker"] = False
        _next_restart_reset()

    if not alive:
        actions.append("thread-dead")
        error_notify.notify_error("monitor-thread", "ThreadExit", "monitor")
        ok, why = _restart_monitor(get_af, fallback_quotes, now)
        actions.append(f"restart:{'ok' if ok else why}")

    if in_session is None:
        try:
            in_session = bool(market_hours.in_session(market_hours.now()))
        except Exception:
            in_session = False

    if in_session:
        if _session_seen_ts is None:
            _session_seen_ts = now
        in_grace = (now - _session_seen_ts) < GRACE_SEC
    else:
        _session_seen_ts = None
        in_grace = True

    # 卡死: 交易时段 + 非 backoff + 非宽限 + 心跳停
    if alive and in_session and not in_grace and not st.get("in_backoff"):
        age = _age(st.get("last_poll_ts"), now)
        if age is not None and age > STALL_SEC:
            actions.append("stall")
            error_notify.notify_error(
                "monitor-stall", "Stall", f"last_poll {int(age)}s 未更新")
            if _exit_on_stall():
                log.error(f"监控卡死 {int(age)}s, WATCHDOG_EXIT_ON_STALL=1 → 退出进程触发重启")
                os._exit(1)

    # 持续失败: last_error 在交易时段内长时间未变
    if in_session:
        err = st.get("last_error")
        if err:
            if err != _fail_key:
                _fail_key, _fail_since, _fail_notified = err, now, False
            elif not _fail_notified and _age(_fail_since, now) > FAIL_SEC:
                _fail_notified = True
                actions.append("persistent-failure")
                error_notify.notify_error(
                    "monitor-fail", "PersistentError",
                    f"last_error 持续 {int(_age(_fail_since, now))}s 未恢复")
        else:
            _fail_key, _fail_since, _fail_notified = None, None, False

    with _lock:
        _state.update(
            last_check=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            last_check_ts=now,
            last_actions=list(actions),
        )
    return actions


def _next_restart_reset():
    global _next_restart_at
    _next_restart_at = 0.0


def run_once_safe(now=None, in_session=None, get_af=None, fallback_quotes=None):
    """自保包装: 单轮任何异常都不外抛, 循环因此永不退出。"""
    try:
        return check_once(now=now, in_session=in_session, get_af=get_af,
                          fallback_quotes=fallback_quotes)
    except Exception as e:
        log.error(f"看门狗单轮异常: {e}", exc_info=e)
        return ["error"]


def _run(get_af, fallback_quotes):
    log.info("看门狗线程已启动")
    while True:
        run_once_safe(get_af=get_af, fallback_quotes=fallback_quotes)
        time.sleep(INTERVAL_SEC)


def start_background(get_af=None, fallback_quotes=None):
    """daemon 线程, 重复调用只起一次。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    t = threading.Thread(target=_run, args=(get_af, fallback_quotes),
                         name="watchdog", daemon=True)
    t.start()
    _thread = t
    return t


def is_running():
    return _thread is not None and _thread.is_alive()
