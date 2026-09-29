"""进程内存看门狗: 读 cgroup 用量, 接近上限时通知并自救。

容器内看不到宿主机上其他服务, 进程被杀之后也发不出消息 —— 那一层由
scripts/host_memwatch.sh 兜底。这里只负责本进程 / 本 cgroup:

- 用量 >= MEMGUARD_WARN_PCT (默认 75): 钉钉 + ntfy 预警, 同一级别 30 分钟一条
- 用量 >= MEMGUARD_CRIT_PCT (默认 90): 紧急通知, 并清缓存 / malloc_trim /
  停掉因子构建子进程 / 置 under_pressure (live 快照、手动重建、新选股拒绝)
- 回落到 MEMGUARD_RECOVER_PCT (默认 65) 以下: 恢复通知, 清除压力标志
- memory.events 的 oom_kill 计数增加: 立即通知 (cgroup 杀掉了容器内的子进程)

用量口径与 docker stats 一致: memory.current - inactive_file (页缓存不算)。
读不到 cgroup 时用 /proc/self/status 的 VmRSS 对比 MEMGUARD_LIMIT_MB。
"""
from __future__ import annotations

import gc
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("memguard")

INTERVAL_SEC = max(5.0, float(os.environ.get("MEMGUARD_INTERVAL_SEC", "15")))
WARN_PCT = float(os.environ.get("MEMGUARD_WARN_PCT", "75"))
CRIT_PCT = float(os.environ.get("MEMGUARD_CRIT_PCT", "90"))
RECOVER_PCT = float(os.environ.get("MEMGUARD_RECOVER_PCT", "65"))
COOLDOWN_SEC = max(60.0, float(os.environ.get("MEMGUARD_COOLDOWN_SEC", "1800")))
LIMIT_MB = float(os.environ.get("MEMGUARD_LIMIT_MB", "400"))

_lock = threading.Lock()
_thread = None
_state = {
    "level": "ok",          # ok / warn / crit
    "pressure": False,
    "relieved": False,
    "last_notify": {},      # kind -> monotonic
    "oom_kill_seen": None,
    "last": None,
    "last_actions": [],
}


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_int(path):
    text = _read_text(path)
    if text is None:
        return None
    text = text.strip()
    if not text or text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def read_proc_status():
    """VmRSS / VmHWM, 单位 kB。不在 Linux 上时为 None。"""
    rss = hwm = None
    text = _read_text("/proc/self/status")
    if not text:
        return {"rss_kb": None, "hwm_kb": None}
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                rss = int(parts[1])
        elif line.startswith("VmHWM:"):
            parts = line.split()
            if len(parts) >= 2:
                hwm = int(parts[1])
    return {"rss_kb": rss, "hwm_kb": hwm}


def _stat_value(text, key):
    if not text:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


# cgroup v1 无上限时 limit_in_bytes 是接近 2^63 的哨兵值, 当作没设上限
_UNLIMITED = 1 << 60


def _pressure_avg10(text):
    """memory.pressure 里 some avg10。"""
    if not text:
        return None
    for line in text.splitlines():
        if not line.startswith("some "):
            continue
        for part in line.split():
            if part.startswith("avg10="):
                try:
                    return float(part.split("=", 1)[1])
                except ValueError:
                    return None
    return None


def read_cgroup(root=None):
    """cgroup v2 优先, 其次 v1。root 供测试指向临时目录。"""
    bases = [Path(root)] if root else [Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/memory")]
    current = limit = inactive = oom_kill = None
    pressure = None
    for base in bases:
        current = _read_int(base / "memory.current")
        if current is not None:
            limit = _read_int(base / "memory.max")
            inactive = _stat_value(_read_text(base / "memory.stat"), "inactive_file")
            oom_kill = _stat_value(_read_text(base / "memory.events"), "oom_kill")
            pressure = _pressure_avg10(_read_text(base / "memory.pressure"))
            break
        current = _read_int(base / "memory.usage_in_bytes")
        if current is not None:
            limit = _read_int(base / "memory.limit_in_bytes")
            stat = _read_text(base / "memory.stat")
            inactive = _stat_value(stat, "total_inactive_file")
            if inactive is None:
                inactive = _stat_value(stat, "inactive_file")
            oom_kill = _stat_value(_read_text(base / "memory.oom_control"), "oom_kill")
            break
    if limit is not None and limit >= _UNLIMITED:
        limit = None
    return {
        "current": current,
        "limit": limit,
        "inactive_file": inactive,
        "oom_kill": oom_kill,
        "pressure_avg10": pressure,
    }


def sample(root=None):
    """当前内存快照。读不到的字段为 None, 不抛。"""
    proc = read_proc_status()
    cg = read_cgroup(root)
    usage = limit = None
    if cg["current"] is not None:
        inactive = cg["inactive_file"] or 0
        usage = max(0, cg["current"] - inactive)
        limit = cg["limit"]
    if limit is None:
        limit = int(LIMIT_MB * 1024 * 1024)
        if usage is None and proc["rss_kb"] is not None:
            usage = proc["rss_kb"] * 1024
    pct = None
    if usage is not None and limit:
        pct = round(usage / limit * 100.0, 1)
    return {
        "rss_kb": proc["rss_kb"],
        "hwm_kb": proc["hwm_kb"],
        "usage_bytes": usage,
        "limit_bytes": limit,
        "pct": pct,
        "oom_kill": cg["oom_kill"],
        "pressure_avg10": cg["pressure_avg10"],
    }


def log_rss(logger, label):
    """重任务前后打一行, 方便用 docker logs 对上峰值。"""
    try:
        s = sample()
        logger.info(
            "内存 %s rss_kb=%s hwm_kb=%s usage=%s limit=%s pct=%s",
            label, s["rss_kb"], s["hwm_kb"], s["usage_bytes"], s["limit_bytes"], s["pct"],
        )
    except Exception:
        pass


def under_pressure():
    with _lock:
        return bool(_state["pressure"])


def status():
    with _lock:
        last = dict(_state["last"]) if _state["last"] else None
        return {
            "level": _state["level"],
            "pressure": _state["pressure"],
            "last_actions": list(_state["last_actions"]),
            "sample": last,
            "running": _thread is not None and _thread.is_alive(),
        }


def reset_state():
    """测试用。"""
    with _lock:
        _state.update(level="ok", pressure=False, relieved=False,
                      last_notify={}, oom_kill_seen=None, last=None, last_actions=[])


def _cooled(kind, now):
    with _lock:
        last = _state["last_notify"].get(kind)
        if last is not None and now - last < COOLDOWN_SEC:
            return False
        _state["last_notify"][kind] = now
        return True


def _cache_sizes():
    sizes = {}
    try:
        import market
        sizes["kline"] = len(market.kline_cache)
        sizes["kline_minute"] = len(market.kline_cache_minute)
        sizes["kline_long"] = len(market.kline_cache_long)
    except Exception:
        pass
    try:
        from api import kline as kline_api
        sizes["tail"] = len(kline_api._tail_cache)
    except Exception:
        pass
    try:
        import chips
        sizes["chips"] = len(chips._cache)
    except Exception:
        pass
    try:
        import premium
        sizes["premium"] = len(premium._cache)
    except Exception:
        pass
    return sizes


def _fmt_mb(n):
    if n is None:
        return "?"
    return f"{n / (1024 * 1024):.0f}MB"


def relieve():
    """临界自救。返回做了哪些动作 (写进通知)。"""
    actions = []
    try:
        import market
        market.kline_cache.clear()
        market.kline_cache_minute.clear()
        market.kline_cache_long.clear()
        actions.append("清空K线缓存")
    except Exception as e:
        actions.append(f"清K线缓存失败:{type(e).__name__}")
    try:
        from api import kline as kline_api
        kline_api._tail_cache.clear()
        actions.append("清空tail缓存")
    except Exception:
        pass
    try:
        import chips
        chips.clear_cache()
        actions.append("清空筹码缓存")
    except Exception:
        pass
    try:
        import premium
        premium.clear()
        actions.append("清空溢价缓存")
    except Exception:
        pass
    try:
        import market
        with market._etf_nav_lock:
            market._etf_nav_cache.clear()
        actions.append("清空ETF净值缓存")
    except Exception:
        pass
    try:
        n = gc.collect()
        actions.append(f"gc={n}")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        actions.append("malloc_trim")
    except Exception:
        actions.append("malloc_trim跳过")
    try:
        import factors
        stopped = factors.stop_build_children()
        if stopped:
            actions.append(f"终止构建子进程{stopped}个")
        else:
            actions.append("无构建子进程")
    except Exception as e:
        actions.append(f"停构建失败:{type(e).__name__}")
    with _lock:
        _state["pressure"] = True
        _state["level"] = "crit"
    return actions


def _notify(title, text):
    try:
        import notify
        notify.notify(title, text)
    except Exception as e:
        log.warning("内存通知入队失败: %s", e)


def _describe(snap, extra=""):
    sizes = _cache_sizes()
    child = "?"
    live = "?"
    try:
        import factors
        child = "在跑" if factors.build_child_running() else "无"
        live = "在跑" if factors.live_running() else "无"
    except Exception:
        pass
    size_txt = " ".join(f"{k}={v}" for k, v in sizes.items()) or "-"
    lines = [
        f"用量 {_fmt_mb(snap.get('usage_bytes'))} / {_fmt_mb(snap.get('limit_bytes'))}"
        f" ({snap.get('pct')}%)",
        f"RSS {snap.get('rss_kb')}kB 峰值 {snap.get('hwm_kb')}kB",
        f"PSI some avg10={snap.get('pressure_avg10')}",
        f"缓存 {size_txt}",
        f"因子构建 {child} 盘中快照 {live}",
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def check_once(snap=None, now=None, notify_fn=None, relieve_fn=None, root=None):
    """一轮检查。notify_fn / relieve_fn 供测试替换。返回动作列表。"""
    now = time.monotonic() if now is None else float(now)
    notify_fn = notify_fn or _notify
    relieve_fn = relieve_fn or relieve
    if snap is None:
        try:
            snap = sample(root)
        except Exception as e:
            log.warning("读取内存失败: %s", e)
            return []
    actions = []
    pct = snap.get("pct")
    oom = snap.get("oom_kill")

    with _lock:
        seen = _state["oom_kill_seen"]
        _state["last"] = dict(snap)
    if oom is not None and seen is not None and oom > seen:
        actions.append("oom_kill")
        notify_fn("容器内 OOM", _describe(snap, f"oom_kill {seen} → {oom} (构建子进程可能已被 cgroup 杀掉)"))
    if oom is not None:
        with _lock:
            _state["oom_kill_seen"] = oom

    if pct is None:
        with _lock:
            _state["last_actions"] = actions
        return actions

    with _lock:
        level = _state["level"]
        pressure = _state["pressure"]
        relieved = _state["relieved"]

    if pct >= CRIT_PCT:
        actions.append("crit")
        did = []
        if not relieved:
            try:
                did = list(relieve_fn() or [])
            except Exception as e:
                did = [f"自救失败:{type(e).__name__}"]
            with _lock:
                _state["relieved"] = True
                _state["pressure"] = True
                _state["level"] = "crit"
            actions.extend(did)
        else:
            with _lock:
                _state["pressure"] = True
                _state["level"] = "crit"
        if _cooled("crit", now):
            extra = "自救: " + ("、".join(did) if did else "本轮已做过, 未重复")
            notify_fn("内存紧急", _describe(snap, extra))
            actions.append("notify:crit")
    elif pct >= WARN_PCT:
        actions.append("warn")
        with _lock:
            if not _state["pressure"]:
                _state["level"] = "warn"
        if _cooled("warn", now):
            notify_fn("内存预警", _describe(snap))
            actions.append("notify:warn")
    elif pct < RECOVER_PCT and (pressure or level != "ok"):
        actions.append("recover")
        with _lock:
            _state["pressure"] = False
            _state["level"] = "ok"
            _state["relieved"] = False
        if _cooled("recover", now):
            notify_fn("内存恢复", _describe(snap, "已低于恢复线, 重任务重新放行"))
            actions.append("notify:recover")

    with _lock:
        _state["last_actions"] = list(actions)
    return actions


def _run():
    log.info("内存看门狗已启动 (间隔 %.0fs, 预警 %.0f%%, 紧急 %.0f%%)",
             INTERVAL_SEC, WARN_PCT, CRIT_PCT)
    while True:
        try:
            check_once()
        except Exception as e:
            log.warning("内存看门狗单轮异常: %s", e)
        time.sleep(INTERVAL_SEC)


def start_background():
    """daemon 线程, 重复调用只起一次。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_run, name="memguard", daemon=True)
    _thread.start()
    return _thread
