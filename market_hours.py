"""A 股交易日历与时段 (visual 自包含)。

优先 pandas_market_calendars 的 XSHG 日历; 包缺失时降级为周一~周五 + 固定时段,
并打一条警告, 不阻止服务启动。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache

# A 股连续竞价: 09:30-11:30, 13:00-15:00; 午休按已过 120 分钟冻结
_AM_START = (9, 30)
_AM_END = (11, 30)
_PM_START = (13, 0)
_PM_END = (15, 0)
_SESSION_MINUTES = 240

_warned_fallback = False


@lru_cache(maxsize=16)
def _xshg_days(start_year: int, end_year: int):
    import pandas_market_calendars as mcal

    calendar = mcal.get_calendar("XSHG")
    schedule = calendar.schedule(
        start_date=f"{start_year}-01-01",
        end_date=f"{end_year}-12-31",
    )
    return frozenset(schedule.index.strftime("%Y-%m-%d"))


def _weekday_fallback(day: str) -> bool:
    global _warned_fallback
    if not _warned_fallback:
        print(
            "[MarketHours] 未安装 pandas_market_calendars, 交易日降级为周一~周五",
            flush=True,
        )
        _warned_fallback = True
    try:
        dt = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return False
    return dt.weekday() < 5


def is_trading_day(value=None) -> bool:
    """value 为 datetime 或 YYYY-MM-DD; 默认今天。"""
    now = value if isinstance(value, datetime) else None
    if now is None and value is None:
        now = datetime.now()
    if now is not None:
        day = now.strftime("%Y-%m-%d")
        year = now.year
    else:
        day = str(value)[:10]
        year = int(day[:4])
    try:
        return day in _xshg_days(year - 1, year + 1)
    except Exception:
        return _weekday_fallback(day)


def _mins(h, m):
    return h * 60 + m


def in_session(now: datetime | None = None) -> bool:
    """交易日且落在 09:30-11:30 或 13:00-15:00 (含开盘, 不含 15:00 整点之后)。"""
    now = now or datetime.now()
    if not is_trading_day(now):
        return False
    t = _mins(now.hour, now.minute)
    # 闭区间含 11:30 / 15:00 (1m K 线时间戳常打在整点)
    am = _mins(*_AM_START) <= t <= _mins(*_AM_END)
    pm = _mins(*_PM_START) <= t <= _mins(*_PM_END)
    return am or pm


def session_elapsed_minutes(now: datetime | None = None) -> float:
    """当日已过交易分钟数 (0~240)。午休冻结在 120。非交易日/未开盘返回 0。"""
    now = now or datetime.now()
    if not is_trading_day(now):
        return 0.0
    t = now.hour * 60 + now.minute + now.second / 60.0
    am_s, am_e = _mins(*_AM_START), _mins(*_AM_END)
    pm_s, pm_e = _mins(*_PM_START), _mins(*_PM_END)
    if t < am_s:
        return 0.0
    if t <= am_e:
        return t - am_s
    if t < pm_s:
        return 120.0
    if t <= pm_e:
        return 120.0 + (t - pm_s)
    return float(_SESSION_MINUTES)


def seconds_until_session(now: datetime | None = None) -> float:
    """距离下一个连续竞价窗口的秒数。已在窗口内返回 0。"""
    now = now or datetime.now()
    if in_session(now):
        return 0.0
    t = _mins(now.hour, now.minute)
    candidates = []
    if is_trading_day(now):
        if t < _mins(*_AM_START):
            target = now.replace(hour=_AM_START[0], minute=_AM_START[1], second=0, microsecond=0)
            candidates.append(target)
        elif t < _mins(*_PM_START):
            target = now.replace(hour=_PM_START[0], minute=_PM_START[1], second=0, microsecond=0)
            candidates.append(target)
    # 下一个交易日 09:30
    day = now.date() + timedelta(days=1)
    for _ in range(10):
        dt = datetime(day.year, day.month, day.day, _AM_START[0], _AM_START[1])
        if is_trading_day(dt):
            candidates.append(dt)
            break
        day += timedelta(days=1)
    if not candidates:
        return 60.0
    nxt = min(candidates)
    return max(1.0, (nxt - now).total_seconds())
