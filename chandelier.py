# -*- coding: utf-8 -*-
"""吊灯持仓的价位。口径与 turtle_screen 实盘默认一致，价用未复权。

盘中止损：保本未开用初始止损（主板 1.5N，创业板/科创板 ×1.2），已开用保本价。
保本价在买入时定死：max(买入价+0.6N, 买入价×1.004)。
保本开关：截至昨天的最高价到过买入价+2.2N。买入当日的最高价不计入。
收盘线：从初始止损起，每个有量交易日开盘前按
max(昨线, 截至昨天最高价−2.8N, 前 20 个有量交易日最低价) 只升不降，
再与当天的盘中止损取高。20 日最低回落时线不跟着往下。
停牌（成交量≤0）不抬最高价、不进 20 日窗口、不计入持有天数。
持有满 15 个有量交易日（不含买入当日）则今天收盘卖。
"""
from __future__ import annotations

STOP_MULT = 1.5
GEM_STOP_FACTOR = 1.2
TRAIL_MULT = 2.8
LOW_WINDOW = 20
BE_TRIGGER_N = 2.2
BE_OFFSET_N = 0.60
BE_FRICTION = 0.004          # 买入价 × (1 + 0.003 + 0.001)
HOLD_MAX = 15

REASON_TRAIL = "吊灯回撤"
REASON_LOW = "20日新低"
REASON_STOP = "仍是止损"
REASON_BE = "保本"


def gem_board(code) -> bool:
    return str(code).split(".")[0].zfill(6).startswith(("30", "68"))


def stop_mult(code) -> float:
    return round(STOP_MULT * (GEM_STOP_FACTOR if gem_board(code) else 1.0), 4)


def _px(v) -> float:
    return round(float(v) + 1e-10, 3)


def initial_stop(entry, n, code) -> float:
    return _px(float(entry) - stop_mult(code) * float(n))


def breakeven_price(entry, n) -> float:
    entry = float(entry)
    n = float(n)
    return _px(max(entry + BE_OFFSET_N * n, entry * (1.0 + BE_FRICTION)))


def _traded(bars):
    out = []
    for b in bars or []:
        try:
            vol = float(b.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol <= 0:
            continue
        day = str(b.get("date") or "")[:10]
        if len(day) != 10:
            continue
        try:
            high = float(b["high"])
            low = float(b["low"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"date": day, "high": high, "low": low})
    out.sort(key=lambda x: x["date"])
    return out


def compute_levels(entry, n, code, bars, today, entry_date) -> dict:
    """早盘用：bars 为未复权日 K，today / entry_date 为 YYYY-MM-DD。

    只使用 date < today 的有量 K。今天还没走完，不拿今天的高低价抬线。
    收盘线逐日只升不降，和 turtle_screen.simulate_trade 的清仓线同一口径。
    """
    entry = float(entry)
    n = float(n)
    today = str(today)[:10]
    entry_date = str(entry_date)[:10]
    hard = initial_stop(entry, n, code)
    be_px = breakeven_price(entry, n)
    prior = [b for b in _traded(bars) if b["date"] < today]
    after = [b for b in prior if b["date"] > entry_date]
    hh = entry
    # 买入之后每个交易日开盘时的「前 20 日最低」，留下其中最高的那次。
    raised_low = None
    for i, b in enumerate(prior):
        if b["date"] <= entry_date:
            continue
        window = prior[max(0, i - LOW_WINDOW):i]
        if window:
            low20 = min(x["low"] for x in window)
            if raised_low is None or low20 > raised_low:
                raised_low = low20
        if b["high"] > hh:
            hh = b["high"]
    if prior:
        low20 = min(x["low"] for x in prior[-LOW_WINDOW:])
        if raised_low is None or low20 > raised_low:
            raised_low = low20
    armed = hh + 1e-9 >= entry + BE_TRIGGER_N * n
    intra = be_px if armed else hard
    close = intra
    reason = REASON_BE if armed else REASON_STOP
    trail = hh - TRAIL_MULT * n
    if trail > close + 1e-9:
        close, reason = trail, REASON_TRAIL
    if raised_low is not None and raised_low > close + 1e-9:
        close, reason = raised_low, REASON_LOW
    counts_today = today > entry_date
    held = len(after) + (1 if counts_today else 0)
    return {
        "stop": _px(intra),
        "initial_stop": hard,
        "breakeven": be_px,
        "armed": bool(armed),
        "close": _px(close),
        "reason": reason,
        "held": held,
        "expire": bool(counts_today and held >= HOLD_MAX),
    }


def carry_forward(levels, previous) -> dict:
    """上一份快照只升不降。K 线没拉全时，不让收盘线或保本开关退回去。

    previous 用持仓行的 ch_asof / ch_close / ch_reason / ch_armed。
    没有上一份，或买入价、N 变更后快照已清空（ch_asof 为空），原样返回。
    """
    out = dict(levels)
    if not previous or not previous.get("ch_asof") or previous.get("ch_close") is None:
        return out
    if previous.get("ch_armed") and not out["armed"]:
        out["armed"] = True
        out["stop"] = out["breakeven"]
    prev_close = float(previous["ch_close"])
    if prev_close > float(out["close"]) + 1e-9:
        out["close"] = _px(prev_close)
        if previous.get("ch_reason"):
            out["reason"] = previous["ch_reason"]
    if float(out["stop"]) > float(out["close"]) + 1e-9:
        out["close"] = out["stop"]
        out["reason"] = REASON_BE if out["armed"] else REASON_STOP
    return out


def morning_lines(rows, when_text, heading="吊灯早盘") -> str:
    """一条价位通知。rows 为已算好的持仓，字段见 compute_levels 加上 symbol/name。"""
    lines = [f"## {heading} {when_text}", ""]
    for r in rows:
        arm = "开" if r["armed"] else "关"
        extra = " 收盘卖" if r["expire"] else ""
        lines.append(
            f"- **{r['symbol']} {r.get('name') or ''}**: "
            f"盘中止损{r['stop']:.2f} 保本{arm} "
            f"收盘线{r['close']:.2f}({r['reason']}) "
            f"持有{r['held']}日{extra}"
        )
    return "\n".join(lines)
