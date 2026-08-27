"""持仓价格监控: 自建价格序列 + 七类告警 + 到期平仓提醒 + 钉钉推送。

双入口:
  start_background(get_af, fallback_quotes)  — visual/server.py 起 daemon 线程
  python monitor.py [--replay SYM:YYYY-MM-DD ...]  — 单跑 / 离线回放校准
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dingtalk  # noqa: E402
import feed as feed_mod  # noqa: E402
import market_hours  # noqa: E402
import trades  # noqa: E402

import logging  # noqa: E402

log = logging.getLogger("monitor")

# ── 判定阈值 (集中, 便于校准) ──
W_FAST = 60.0                 # 秒
W_SLOW = 180.0                # 秒
Z_DOWN = -2.5
Z_UP = 2.5
VOL_RATIO_MIN = 2.0
ASK_PRESSURE_MIN = 1.5        # 有 depth 时急跌确认
NEAR_SL_BUFFER = 0.35         # (P-SL)/(entry-SL)
NEAR_SL_PCT = 0.01            # (P-SL)/P
NEAR_TP_PCT = 0.01
NEAR_LIMIT_UP_PCT = 0.015
ETA_MINUTES = 10.0
OPEN_DROP_PCT = 0.02          # 开盘档相对开盘价
OPEN_WINDOW_END = (9, 35)     # 09:30-09:35
MONOTONE_N = 3
SIGMA_MIN_SAMPLES = 10
PRIOR_DAILY_ATR_PCT = 0.03    # 开盘 σ 先验: 3% 日波动 / √240
EPS_PRICE = 0.0051
BUFFER_SECONDS = 30 * 60
COOLDOWN_ACCEL_SEC = 30 * 60
DAILY_ONCE = frozenset({
    "breakeven_hit", "be_broken", "sl_breached", "tp_reached", "limit_up_sealed",
    "hold_exit_am", "hold_exit_pm",
})
ACCEL_TYPES = frozenset({"accel_down", "accel_up"})
HOLD_EXIT_TYPES = frozenset({"hold_exit_am", "hold_exit_pm"})
HOLD_EXIT_AM = (10, 0)         # 到期日上午提醒
HOLD_EXIT_AM_END = (11, 30)
HOLD_EXIT_PM = (14, 0)         # 到期日下午提醒
HOLD_EXIT_PM_END = (15, 0)

_CST = timezone(timedelta(hours=8))

# ── 运行时状态 ──
_buffers = {}                 # symbol -> deque[{ts, price, volume}]
_lock = threading.Lock()
_status = {
    "running": False,
    "backend": "rest",
    "last_poll": None,
    "n_symbols": 0,
    "last_error": None,
}
_feed = None
_thread = None


def _load_env():
    if os.environ.get("AF_API_KEY"):
        return
    for env_dir in (SCRIPT_DIR, SCRIPT_DIR.parent):
        env_file = env_dir / ".env"
        if not env_file.exists():
            continue
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            pass
        break


def get_status():
    with _lock:
        return dict(_status)


def _set_status(**kwargs):
    with _lock:
        if "last_error" in kwargs and kwargs["last_error"] is not None:
            try:
                from logger import redact_message
                kwargs["last_error"] = redact_message(kwargs["last_error"])
            except Exception:
                pass
        _status.update(kwargs)


# ── 价格序列 ──
def append_sample(symbol, ts, price, volume):
    """timestamp 没前进则跳过, 避免重复采样点。"""
    if price is None or price <= 0 or ts is None:
        return False
    buf = _buffers.setdefault(symbol, deque())
    if buf and ts <= buf[-1]["ts"]:
        return False
    buf.append({"ts": float(ts), "price": float(price), "volume": int(volume or 0)})
    cutoff = float(ts) - BUFFER_SECONDS
    while buf and buf[0]["ts"] < cutoff:
        buf.popleft()
    return True


def seed_buffer(symbol, samples):
    _buffers[symbol] = deque()
    for s in samples or []:
        append_sample(symbol, s.get("ts"), s.get("price"), s.get("volume"))


def get_buffer(symbol):
    return list(_buffers.get(symbol, ()))


def clear_buffers():
    _buffers.clear()


def _price_at(samples, target_ts):
    """不晚于 target_ts 的最近一根; 没有则 None。"""
    hit = None
    for s in samples:
        if s["ts"] <= target_ts:
            hit = s
        else:
            break
    return hit


def compute_metrics(samples, now_ts=None, session_elapsed_min=None, open_price=None):
    """按秒窗口算速度/加速度/z/量比。samples 已按 ts 升序。"""
    if not samples:
        return None
    now_ts = float(now_ts if now_ts is not None else samples[-1]["ts"])
    last = samples[-1]
    p = last["price"]
    if p <= 0:
        return None

    def _ret(window):
        ref = _price_at(samples, now_ts - window)
        if ref is None or ref["price"] <= 0:
            return None
        # 窗口实际跨度太短则不作数
        dt = now_ts - ref["ts"]
        if dt < window * 0.5:
            return None
        return (p - ref["price"]) / ref["price"]

    v60 = _ret(W_FAST)
    v180 = _ret(W_SLOW)
    v60_pm = (v60 * 60.0 / W_FAST) if v60 is not None else None
    v180_pm = (v180 * 60.0 / W_SLOW) if v180 is not None else None
    accel = None
    if v60_pm is not None and v180_pm is not None:
        accel = v60_pm - v180_pm

    # 1 分钟收益序列 → σ
    rets = []
    for i in range(1, len(samples)):
        dt = samples[i]["ts"] - samples[i - 1]["ts"]
        if dt <= 0 or samples[i - 1]["price"] <= 0:
            continue
        r = (samples[i]["price"] - samples[i - 1]["price"]) / samples[i - 1]["price"]
        # 归一到 60s
        r60 = r * (60.0 / dt) if dt > 0 else r
        rets.append(r60)
    if len(rets) >= SIGMA_MIN_SAMPLES:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        sigma = math.sqrt(var) if var > 0 else PRIOR_DAILY_ATR_PCT / math.sqrt(240)
    else:
        sigma = PRIOR_DAILY_ATR_PCT / math.sqrt(240)
    z = (v60 / sigma) if (v60 is not None and sigma > 0) else None

    vol_ratio = None
    if len(samples) >= 2 and session_elapsed_min and session_elapsed_min > 1:
        dt_min = max((now_ts - samples[0]["ts"]) / 60.0, 1.0)
        # 近 60s 增量 vs 全日平均分钟量
        ref = _price_at(samples, now_ts - 60.0) or samples[0]
        dvol = max(0, last["volume"] - ref["volume"])
        dt = max((now_ts - ref["ts"]) / 60.0, 1.0 / 60.0)
        cur_vpm = dvol / dt
        total_vol = last["volume"]
        avg_vpm = total_vol / max(session_elapsed_min, 1.0)
        if avg_vpm > 0:
            vol_ratio = cur_vpm / avg_vpm

    monotone_down = False
    monotone_up = False
    if len(samples) >= MONOTONE_N:
        tail = samples[-MONOTONE_N:]
        monotone_down = all(tail[i]["price"] < tail[i - 1]["price"] for i in range(1, len(tail)))
        monotone_up = all(tail[i]["price"] > tail[i - 1]["price"] for i in range(1, len(tail)))

    vs_open = None
    if open_price and open_price > 0:
        vs_open = (p - open_price) / open_price

    # 每分钟跌/涨幅 (用 60s 窗口, 供 eta)
    vpm_abs = abs(v60_pm) if v60_pm is not None else None

    return {
        "price": p,
        "ts": now_ts,
        "v60": v60,
        "v180": v180,
        "v60_pm": v60_pm,
        "v180_pm": v180_pm,
        "a": accel,
        "z": z,
        "sigma": sigma,
        "vol_ratio": vol_ratio,
        "monotone_down": monotone_down,
        "monotone_up": monotone_up,
        "vs_open": vs_open,
        "vpm_abs": vpm_abs,
        "prev_price": samples[-2]["price"] if len(samples) >= 2 else None,
    }


def _in_open_window(now_dt):
    t = now_dt.hour * 60 + now_dt.minute
    return (9 * 60 + 30) <= t <= (OPEN_WINDOW_END[0] * 60 + OPEN_WINDOW_END[1])


def _ask_pressure(depth):
    if not depth:
        return None
    asks = depth.get("ask_volumes") or []
    bids = depth.get("bid_volumes") or []
    sa = sum(asks[:5]) if asks else 0
    sb = sum(bids[:5]) if bids else 0
    if sb <= 0:
        return None if sa <= 0 else 99.0
    return sa / sb


def _sealed_limit_up(depth):
    if not depth:
        return False
    asks = depth.get("ask_volumes") or []
    return len(asks) > 0 and asks[0] == 0


def _fmt_pct(x):
    if x is None:
        return "—"
    return f"{x * 100:+.2f}%"


def evaluate_alerts(position, metrics, limits=None, depth=None, now_dt=None):
    """纯函数: 返回 [{alert_type, price, detail}, ...]。"""
    if not metrics:
        return []
    now_dt = now_dt or market_hours.now()
    p = metrics["price"]
    sl = position.get("stop_loss")
    tp = position.get("take_profit")
    be = position.get("breakeven")
    entry = position.get("entry_price")
    limit_up = (limits or {}).get("limit_up")
    out = []

    def add(atype, extra):
        bits = [
            f"现价{p:.2f}",
            extra,
            f"1m速度{_fmt_pct(metrics.get('v60'))}",
            f"量比{metrics['vol_ratio']:.1f}" if metrics.get("vol_ratio") is not None else "量比—",
        ]
        if depth and _ask_pressure(depth) is not None:
            bits.append(f"卖/买压{_ask_pressure(depth):.1f}")
        out.append({"alert_type": atype, "price": p, "detail": " | ".join(bits)})

    # 1) 止损击穿
    if sl is not None and sl > 0 and p <= sl:
        add("sl_breached", f"击穿止损{sl:.2f}")

    # 2) 止盈达成
    if tp is not None and tp > 0 and p >= tp:
        add("tp_reached", f"到达止盈{tp:.2f}")

    # 3) 保本上穿
    if be is not None and be > 0:
        prev = metrics.get("prev_price")
        if prev is not None and prev < be <= p:
            add("breakeven_hit", f"上穿保本{be:.2f}")

    # 3b) 跌破/跌回保本 (价格从上方到达保本价; 全天在保本下方的跳空低开不触发)
    if be is not None and be > 0:
        prev = metrics.get("prev_price")
        if prev is not None and p <= be < prev:
            add("be_broken", f"跌破保本{be:.2f}")

    # 4) 涨停封板 (官方口径: 卖1量=0)
    if _sealed_limit_up(depth):
        extra = f"涨停封板 卖1=0"
        if limit_up:
            extra += f" 板价{limit_up:.2f}"
        add("limit_up_sealed", extra)

    # 5) 加速接近止损
    if sl is not None and sl > 0 and p > sl:
        near = False
        if entry and entry > sl:
            buf = (p - sl) / (entry - sl)
            if buf <= NEAR_SL_BUFFER:
                near = True
        if (p - sl) / p <= NEAR_SL_PCT:
            near = True
        vpm = metrics.get("vpm_abs") or 0
        eta = ((p - sl) / p / vpm / 60.0 * 60.0) if vpm else None
        # eta: (P-SL) / |每分钟绝对跌幅|
        # 每分钟绝对跌幅 = p * |v60_pm|
        drop_per_min = p * abs(metrics["v60_pm"]) if metrics.get("v60_pm") else None
        eta_min = (p - sl) / drop_per_min if drop_per_min and drop_per_min > 0 else None
        accelerating = (
            metrics.get("z") is not None and metrics["z"] <= Z_DOWN
            and metrics.get("a") is not None and metrics["a"] < 0
        )
        vol_ok = metrics.get("vol_ratio") is None or metrics["vol_ratio"] >= VOL_RATIO_MIN
        pressure = _ask_pressure(depth)
        pressure_ok = pressure is None or pressure >= ASK_PRESSURE_MIN
        open_special = (
            _in_open_window(now_dt)
            and metrics.get("vs_open") is not None
            and metrics["vs_open"] <= -OPEN_DROP_PCT
            and metrics.get("monotone_down")
        )
        eta_ok = eta_min is not None and eta_min <= ETA_MINUTES
        if near and (open_special or (accelerating and vol_ok and pressure_ok and eta_ok)):
            why = "开盘急跌" if open_special else "加速下跌"
            eta_s = f" 预估{eta_min:.0f}分钟触达" if eta_min is not None else ""
            add("accel_down", f"{why} 止损{sl:.2f} 距{p - sl:.2f}{eta_s}")

    # 6) 加速接近止盈 / 快涨停
    near_up = False
    if limit_up and limit_up > 0 and (limit_up - p) / limit_up <= NEAR_LIMIT_UP_PCT:
        near_up = True
    if tp is not None and tp > 0 and p < tp and (tp - p) / tp <= NEAR_TP_PCT:
        near_up = True
    rise_per_min = p * abs(metrics["v60_pm"]) if metrics.get("v60_pm") else None
    eta_tp = None
    if tp and tp > p and rise_per_min and rise_per_min > 0:
        eta_tp = (tp - p) / rise_per_min
        if eta_tp <= ETA_MINUTES:
            near_up = True
    accelerating_up = (
        metrics.get("z") is not None and metrics["z"] >= Z_UP
        and metrics.get("a") is not None and metrics["a"] > 0
    )
    vol_ok_up = metrics.get("vol_ratio") is None or metrics["vol_ratio"] >= VOL_RATIO_MIN
    open_up = (
        _in_open_window(now_dt)
        and metrics.get("vs_open") is not None
        and metrics["vs_open"] >= OPEN_DROP_PCT
        and metrics.get("monotone_up")
    )
    if near_up and (open_up or (accelerating_up and vol_ok_up)):
        why = "开盘急涨" if open_up else "加速上涨"
        parts = [why]
        if tp:
            parts.append(f"止盈{tp:.2f}")
        if limit_up:
            parts.append(f"涨停{limit_up:.2f} 距{limit_up - p:.2f}")
        if eta_tp is not None:
            parts.append(f"预估{eta_tp:.0f}分钟触达止盈")
        add("accel_up", " ".join(parts))

    return out


def _clock_mins(dt):
    return dt.hour * 60 + dt.minute


def evaluate_hold_exit(pos, now_dt=None):
    """到期日 10:00-11:30 发 hold_exit_am, 14:00-15:00 发 hold_exit_pm; 非到期日空列表。

    pos 需 hold_days 与 hold_anchor_date (或已算好的 hold_end_date)。
    """
    now_dt = now_dt or market_hours.now()
    hold_days = pos.get("hold_days")
    if not hold_days:
        return []
    end = pos.get("hold_end_date")
    if not end:
        anchor = pos.get("hold_anchor_date") or pos.get("entry_date")
        if not anchor:
            return []
        end = market_hours.nth_trading_day(anchor, int(hold_days))
    today = now_dt.strftime("%Y-%m-%d")
    if not end or today != end:
        return []
    t = _clock_mins(now_dt)
    am_lo = HOLD_EXIT_AM[0] * 60 + HOLD_EXIT_AM[1]
    am_hi = HOLD_EXIT_AM_END[0] * 60 + HOLD_EXIT_AM_END[1]
    pm_lo = HOLD_EXIT_PM[0] * 60 + HOLD_EXIT_PM[1]
    pm_hi = HOLD_EXIT_PM_END[0] * 60 + HOLD_EXIT_PM_END[1]
    if am_lo <= t <= am_hi:
        slot, when = "hold_exit_am", "上午10:00"
    elif pm_lo <= t <= pm_hi:
        slot, when = "hold_exit_pm", "下午14:00"
    else:
        return []
    model = (pos.get("model_name") or "").strip()
    anchor = pos.get("hold_anchor_date") or pos.get("entry_date") or ""
    model_bit = f"{model} " if model else ""
    detail = (
        f"{when}提醒平仓 {model_bit}推荐持仓{int(hold_days)}交易日已到期"
        f"（起算 {anchor}）"
    )
    return [{"alert_type": slot, "price": None, "detail": detail}]


_UNSET = object()


def should_fire(user_id, symbol, alert_type, now_dt=None, last=_UNSET, trade_id=None):
    """节流: 每日一次 / 30 分钟冷却。last 可注入便于单测; 显式 None 表示无历史。

    hold_exit_* 按 (user, symbol, type, trade_id) 去重, 避免同股两笔持仓互相挡住。
    """
    now_dt = now_dt or market_hours.now()
    today = now_dt.strftime("%Y-%m-%d")
    if last is _UNSET:
        last = trades.last_monitor_alert(
            user_id, symbol, alert_type,
            trade_id=trade_id if alert_type in HOLD_EXIT_TYPES else None,
        )
    if last is None:
        return True
    if alert_type in DAILY_ONCE:
        return last.get("trade_date") != today
    if alert_type in ACCEL_TYPES:
        try:
            fired = datetime.fromisoformat(last["fired_at"])
        except (TypeError, ValueError):
            return True
        return (now_dt - fired).total_seconds() >= COOLDOWN_ACCEL_SEC
    return True


def _build_message(fired, now_dt):
    """fired: list of {user, position, alert}。按用户名分组。"""
    now_str = now_dt.strftime("%Y-%m-%d %H:%M")
    groups = defaultdict(list)
    for item in fired:
        u = item["username"]
        pos = item["position"]
        a = item["alert"]
        groups[u].append(
            f"- **{u} · {pos['symbol']} {pos.get('name') or ''}**: "
            f"{a['alert_type']} | {a['detail']}"
        )
    lines = [f"## 持仓监控 {now_str}", ""]
    for u in sorted(groups):
        lines.extend(groups[u])
    return "\n".join(lines)


def _build_hold_message(fired, now_dt):
    """到期平仓提醒 markdown。"""
    now_str = now_dt.strftime("%Y-%m-%d %H:%M")
    slot = "上午 10:00" if now_dt.hour < 12 else "下午 14:00"
    groups = defaultdict(list)
    for item in fired:
        u = item["username"]
        pos = item["position"]
        a = item["alert"]
        groups[u].append(
            f"- **{u} · {pos['symbol']} {pos.get('name') or ''}**: {a['detail']}"
        )
    lines = [
        f"## 持仓到期提醒 {now_str}",
        "",
        f"推荐持仓周期最后交易日，请平仓（{slot}）：",
        "",
    ]
    for u in sorted(groups):
        lines.extend(groups[u])
    return "\n".join(lines)


def _check_hold_expire(now_dt=None, persist=True, notify=True):
    """到期日 10:00 / 14:00 窗口内推送平仓提醒。不拉行情。返回 fired 列表。"""
    now_dt = now_dt or market_hours.now()
    positions = trades.list_hold_expire_positions()
    if not positions:
        return []
    today = now_dt.strftime("%Y-%m-%d")
    fired = []
    for pos in positions:
        for a in evaluate_hold_exit(pos, now_dt=now_dt):
            if not should_fire(
                pos["user_id"], pos["symbol"], a["alert_type"],
                now_dt=now_dt, trade_id=pos["id"],
            ):
                continue
            if persist:
                trades.insert_monitor_alert(
                    pos["user_id"], pos["id"], pos["symbol"], a["alert_type"],
                    today, price=a.get("price"), detail=a["detail"],
                )
            fired.append({
                "username": pos.get("username") or str(pos["user_id"]),
                "position": pos,
                "alert": a,
            })
    if fired and notify:
        dingtalk.send_markdown("持仓到期提醒", _build_hold_message(fired, now_dt))
    return fired


def _poll_once(feed_obj, now_dt=None, persist=True, notify=True):
    """一轮: 取持仓 → 补种 → 快照 → 判定 → 节流 → 推送。返回 fired 列表。"""
    now_dt = now_dt or market_hours.now()
    positions = trades.list_monitored_positions()
    symbols = list(dict.fromkeys(p["symbol"] for p in positions))
    _set_status(n_symbols=len(symbols), last_poll=now_dt.isoformat(timespec="seconds"))
    if not symbols:
        return []

    # 涨跌停价每日一次
    limits_map = {}
    try:
        limits_map = feed_obj.instruments(symbols) or {}
    except Exception as e:
        log.warning(f"instruments 失败: {e}")

    # 补种空序列
    missing = [s for s in symbols if not _buffers.get(s)]
    if missing:
        try:
            seeded = feed_obj.seed_intraday(missing)
            for s, samples in (seeded or {}).items():
                seed_buffer(s, samples)
        except Exception as e:
            log.warning(f"补种失败: {e}")

    try:
        quotes = feed_obj.quotes(symbols) or {}
    except feed_mod.RateLimited as e:
        _set_status(last_error=str(e))
        return []
    except Exception as e:
        _set_status(last_error=str(e))
        log.warning(f"quotes 失败: {e}")
        return []

    for sym, q in quotes.items():
        ts = q.get("timestamp") or now_dt.timestamp()
        append_sample(sym, ts, q.get("last_price"), q.get("volume"))

    elapsed = market_hours.session_elapsed_minutes(now_dt)

    # 按需 depth
    depth_syms = []
    for pos in positions:
        q = quotes.get(pos["symbol"])
        if not q or q.get("last_price") is None:
            continue
        lim = limits_map.get(pos["symbol"]) or {}
        if feed_mod.needs_depth(
            q["last_price"],
            stop_loss=pos.get("stop_loss"),
            limit_up=lim.get("limit_up"),
            limit_down=lim.get("limit_down"),
        ):
            depth_syms.append(pos["symbol"])
    depths = {}
    if depth_syms:
        try:
            depths = feed_obj.depth(list(dict.fromkeys(depth_syms))) or {}
        except feed_mod.RateLimited:
            depths = {}
        except Exception as e:
            log.warning(f"depth 失败: {e}")

    fired = []
    today = now_dt.strftime("%Y-%m-%d")
    for pos in positions:
        samples = get_buffer(pos["symbol"])
        q = quotes.get(pos["symbol"]) or {}
        metrics = compute_metrics(
            samples,
            now_ts=(q.get("timestamp") or (samples[-1]["ts"] if samples else None)),
            session_elapsed_min=elapsed,
            open_price=q.get("open"),
        )
        alerts = evaluate_alerts(
            pos, metrics, limits=limits_map.get(pos["symbol"]),
            depth=depths.get(pos["symbol"]), now_dt=now_dt,
        )
        for a in alerts:
            if not should_fire(pos["user_id"], pos["symbol"], a["alert_type"], now_dt=now_dt):
                continue
            if persist:
                trades.insert_monitor_alert(
                    pos["user_id"], pos["id"], pos["symbol"], a["alert_type"],
                    today, price=a["price"], detail=a["detail"],
                )
            fired.append({
                "username": pos.get("username") or str(pos["user_id"]),
                "position": pos,
                "alert": a,
            })

    if fired and notify:
        md = _build_message(fired, now_dt)
        dingtalk.send_markdown("持仓监控", md)
    return fired


def _loop(get_af, fallback_quotes):
    global _feed
    _load_env()
    _feed = feed_mod.RestFeed(get_af, fallback_quotes=fallback_quotes)
    _set_status(running=True, backend=_feed.backend, last_error=None)
    log.info("持仓监控线程已启动")
    while True:
        try:
            now = market_hours.now()
            if not market_hours.in_session(now):
                wait = min(market_hours.seconds_until_session(now), 300)
                time.sleep(max(5.0, wait))
                continue
            _check_hold_expire(now_dt=now)
            positions = trades.list_monitored_positions()
            n = len({p["symbol"] for p in positions})
            interval = _feed.poll_interval(n)
            if _feed.in_backoff():
                time.sleep(5)
                continue
            _poll_once(_feed, now_dt=now)
            time.sleep(interval)
        except Exception as e:
            _set_status(last_error=str(e))
            log.warning(f"循环异常: {e}")
            time.sleep(30)


def start_background(get_af, fallback_quotes=None):
    """daemon 线程。重复调用只起一次。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    t = threading.Thread(
        target=_loop, args=(get_af, fallback_quotes),
        name="position-monitor", daemon=True,
    )
    t.start()
    _thread = t
    return t


# ── 回放校准 ──
def _end_time_ms(day: str) -> int:
    """该日 CST 15:00 对应的 UTC 毫秒时间戳 (与 build_daily_qfq_cache 同口径)。"""
    y, m, d = (int(x) for x in day.split("-"))
    cst_close = datetime(y, m, d, 15, 0, 0)
    return int((cst_close - datetime(1970, 1, 1)).total_seconds() * 1000) - 8 * 3600 * 1000


def _load_fixture(symbol, day):
    path = SCRIPT_DIR / "test" / "fixtures" / f"{symbol.replace('.', '_')}_{day}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_fixture(symbol, day, payload):
    d = SCRIPT_DIR / "test" / "fixtures"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{symbol.replace('.', '_')}_{day}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"已写入 {path}")


def fetch_replay_bars(af, symbol, day):
    """拉历史 1m (adjust=none), 只保留 day 当日。"""
    dfs = af.klines.batch(
        [symbol], period="1m", count=240, adjust="none",
        to_dataframe=True, end_time=_end_time_ms(day),
    )
    df = (dfs or {}).get(symbol)
    if df is None or getattr(df, "empty", True):
        return []
    samples = feed_mod._df_to_samples(df)
    day_samples = []
    for s in samples:
        dt = datetime.fromtimestamp(s["ts"], tz=_CST)
        if dt.strftime("%Y-%m-%d") != day:
            continue
        hm = dt.hour * 60 + dt.minute
        if hm < 9 * 60 + 30 or hm > 15 * 60:
            continue
        s = dict(s)
        s["clock"] = dt.strftime("%H:%M")
        day_samples.append(s)
    return day_samples


def replay(spec_list, persist_fixture=True, position_overrides=None):
    """spec_list: ['603698.SH:2026-08-19', ...]。逐 bar 喂判定, 打印触发时刻。"""
    _load_env()
    af = None
    key = os.environ.get("AF_API_KEY", "")
    if key:
        try:
            from alphafeed import AlphaFeed
            af = AlphaFeed(api_key=key)
        except Exception as e:
            log.warning(f"AlphaFeed 不可用: {e}")

    overrides = position_overrides or {}
    all_hits = {}
    for spec in spec_list:
        if ":" not in spec:
            log.warning(f"跳过无效参数 {spec}, 需要 SYMBOL:YYYY-MM-DD")
            continue
        symbol, day = spec.split(":", 1)
        symbol, day = symbol.strip().upper(), day.strip()
        fixture = _load_fixture(symbol, day)
        bars = (fixture or {}).get("bars") if fixture else None
        if not bars and af is not None:
            log.info(f"拉取 {symbol} {day} 1m ...")
            bars = fetch_replay_bars(af, symbol, day)
            if bars and persist_fixture:
                _save_fixture(symbol, day, {"symbol": symbol, "day": day, "bars": bars})
        if not bars:
            log.warning(f"{symbol} {day} 无数据 (无 fixture 且未拉到 K 线)")
            continue

        ov = overrides.get(symbol) or {}
        # 默认风控价: 用当日高低点附近, 保证能测到规则; 调用方可覆盖
        highs = max(b["price"] for b in bars)
        lows = min(b["price"] for b in bars)
        open_px = bars[0]["price"]
        pos = {
            "symbol": symbol,
            "name": ov.get("name") or symbol,
            "entry_price": ov.get("entry_price", open_px),
            "stop_loss": ov.get("stop_loss", lows * 0.995),
            "take_profit": ov.get("take_profit", highs * 0.995),
            "breakeven": ov.get("breakeven", open_px),
            "quantity": 100,
            "user_id": 0,
        }
        limit_up = ov.get("limit_up")
        if limit_up is None and len(bars) > 0:
            # 回放没有 instruments, 用当日最高价近似涨停 (仅用于 near-limit 测试)
            limit_up = highs
        limits = {"limit_up": limit_up, "limit_down": ov.get("limit_down")}

        clear_buffers()
        hits = []
        last_fired = {}  # in-memory throttle
        cum = []
        for i, bar in enumerate(bars):
            cum.append({"ts": bar["ts"], "price": bar["price"], "volume": bar["volume"]})
            seed_buffer(symbol, cum)
            dt = datetime.fromtimestamp(bar["ts"], tz=_CST).replace(tzinfo=None)
            elapsed = market_hours.session_elapsed_minutes(dt)
            metrics = compute_metrics(
                cum, now_ts=bar["ts"], session_elapsed_min=elapsed, open_price=open_px,
            )
            alerts = evaluate_alerts(pos, metrics, limits=limits, depth=None, now_dt=dt)
            for a in alerts:
                key_a = a["alert_type"]
                prev = last_fired.get(key_a)
                last_row = None
                if prev is not None:
                    last_row = {
                        "trade_date": prev.strftime("%Y-%m-%d"),
                        "fired_at": prev.isoformat(timespec="seconds"),
                    }
                if not should_fire(0, symbol, key_a, now_dt=dt, last=last_row):
                    continue
                last_fired[key_a] = dt
                clock = bar.get("clock") or dt.strftime("%H:%M")
                hits.append({"clock": clock, "alert": a})
                print(f"  {clock}  {a['alert_type']:16s}  {a['detail']}", flush=True)
        print(f"[Replay] {symbol} {day}: {len(hits)} 次触发 / {len(bars)} 根", flush=True)
        all_hits[f"{symbol}:{day}"] = hits
    return all_hits


def main():
    parser = argparse.ArgumentParser(description="持仓价格监控")
    parser.add_argument(
        "--replay", nargs="+", metavar="SYM:YYYY-MM-DD",
        help="离线回放 1m K 线, 打印告警触发时刻",
    )
    parser.add_argument("--once", action="store_true", help="只跑一轮 (需在交易时段)")
    args = parser.parse_args()
    _load_env()
    from logger import configure as _log_configure
    _log_configure()
    trades.init_db()

    if args.replay:
        replay(args.replay)
        return

    def _get_af():
        from alphafeed import AlphaFeed
        key = os.environ.get("AF_API_KEY", "")
        if not key:
            raise RuntimeError("未设置 AF_API_KEY")
        return AlphaFeed(api_key=key)

    if args.once:
        f = feed_mod.RestFeed(_get_af)
        hold_fired = _check_hold_expire()
        fired = _poll_once(f)
        log.info(
            f"本轮触发 {len(fired)} 条价格告警 / {len(hold_fired)} 条到期提醒"
        )
        return

    start_background(_get_af)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("已停止")


if __name__ == "__main__":
    main()
