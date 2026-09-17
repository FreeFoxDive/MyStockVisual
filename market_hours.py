"""A 股交易日历与时段 (visual 自包含)。

优先 pandas_market_calendars 的 XSHG 日历; 包缺失时降级为周一~周五 + 固定时段,
并打一条警告, 不阻止服务启动。
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import logging

log = logging.getLogger("market_hours")

# A 股连续竞价: 09:30-11:30, 13:00-15:00; 午休按已过 120 分钟冻结
_AM_START = (9, 30)
_AM_END = (11, 30)
_PM_START = (13, 0)
_PM_END = (15, 0)
_SESSION_MINUTES = 240
# 开盘集合竞价: 09:15 起接受申报并给出虚拟匹配价, 09:25 撮合出开盘价, 09:30
# 连续竞价接棒。这段已有快照意义 (要退回只认 09:25 就改这一个常量)。
_AUCTION_START = (9, 15)
# 行情流提前建连时刻: 交易日 09:00 起允许建立 SSE, 只发保活帧不取上游数据,
# 换取 09:15 第一帧零握手延迟。
_STREAM_OPEN = (9, 0)

_CST = timezone(timedelta(hours=8))


def now():
    """当前北京时间 (naive UTC+8 墙钟), 不受部署容器时区影响。

    Docker 基础镜像 (python:3.12-slim) 默认 UTC, 直接用 datetime.now() 判断
    交易时段会整体错位 8 小时, 导致盘中永不轮询。所有时段判定统一走这里。
    """
    return datetime.now(_CST).replace(tzinfo=None)


def _now():
    """模块内部取当前北京时间 (避开与形参 now 的重名)。"""
    return now()

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


@lru_cache(maxsize=16)
def _xshg_sorted(start_year: int, end_year: int):
    """交易日按日期升序 (YYYY-MM-DD 字典序即时间序), 供二分找「下一个交易日」。

    原来的逐日 is_trading_day 扫描带 10 天上限, 长假 (春节/国庆叠加周末) 超限
    就静默放弃; 这里对缓存日历做二分, 没有天数上限。
    """
    return tuple(sorted(_xshg_days(start_year, end_year)))


def calendar_source() -> str:
    """交易日历来源: "xshg" (pandas_market_calendars) 或 "weekday" (降级)。

    降级时节假日会被当交易日 (抓到陈旧快照), 接口透出这个字段让前端能提示,
    不再只在日志里 warning。
    """
    y = _now().year
    try:
        _xshg_days(y - 1, y + 1)
        return "xshg"
    except Exception:
        return "weekday"


def _weekday_fallback(day: str) -> bool:
    global _warned_fallback
    if not _warned_fallback:
        log.warning("未安装 pandas_market_calendars, 交易日降级为周一~周五")
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
        now = _now()
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
    now = now or _now()
    if not is_trading_day(now):
        return False
    t = _mins(now.hour, now.minute)
    # 闭区间含 11:30 / 15:00 (1m K 线时间戳常打在整点)
    am = _mins(*_AM_START) <= t <= _mins(*_AM_END)
    pm = _mins(*_PM_START) <= t <= _mins(*_PM_END)
    return am or pm


def session_phase(now: datetime | None = None) -> str:
    """当前时段: non_trading | pre | auction | trading | break | closed。

    唯一时段口径, 供接口透传与"当日 bar 是否可用/是否终值"判定, 避免各处
    自行拼 is_trading_day + in_session 组合 (午休曾是 in_session=False 的陷阱)。

    auction = 交易日 09:15-09:30 开盘集合竞价: 快照已有效, 但当日 bar 未成型。
    """
    now = now or _now()
    if not is_trading_day(now):
        return "non_trading"
    t = _mins(now.hour, now.minute)
    if t < _mins(*_AUCTION_START):
        return "pre"
    if t < _mins(*_AM_START):
        return "auction"
    if in_session(now):
        return "trading"
    if t < _mins(*_PM_START):
        return "break"
    if t <= _mins(*_PM_END):
        return "trading"
    return "closed"


# 当日 bar 已成型(开盘后)的时段: 盘前 / 集合竞价 / 非交易日的快照是上一交易日
# 残留 (volume 可能 >0), 拼出来会凭空多一根"今日"bar。判定统一写
# `phase in BAR_READY_PHASES` 或 `phase not in BAR_READY_PHASES`, **不要写
# `phase == "pre"`** —— 竞价时段会让那种写法静默失效。
BAR_READY_PHASES = ("trading", "break", "closed")

# 行情有意义、需要向上游取数的时段 (集合竞价 + 连续竞价, 不含午休)。
LIVE_PHASES = ("auction", "trading")


def is_auction(now: datetime | None = None) -> bool:
    """开盘集合竞价 (交易日 09:15-09:30)。"""
    return session_phase(now) == "auction"


def is_live(now: datetime | None = None) -> bool:
    """现在要不要拉行情 —— 快照/五档/SSE 取数/磁盘 TTL 的唯一口径。

    与 in_session() 的区别: in_session() 仍只表示连续竞价 (09:30-11:30,
    13:00-15:00), 量比估算 (session_elapsed_minutes)、watchdog 停顿判定、
    监控价格预警都依赖它, 不要在那些地方换成 is_live()。
    """
    return session_phase(now) in LIVE_PHASES


def can_connect_stream(now: datetime | None = None) -> bool:
    """现在能不能建立行情流 (交易日 09:00 ~ 15:00, 含午休)。

    只管"能不能连"; "要不要取数"由 is_live() 决定 —— 09:00-09:15 建连只花
    保活字节, 不消耗上游额度, 换来 09:15 首帧零握手延迟; 午休期间保持连接,
    13:00 的第一帧同样不必重新握手。

    收盘后不再允许 (否则空闲连接会整夜占线程并持续发保活帧): 客户端收到
    closed 相位就断开, 下一个交易日 09:00 再按 next_stream_at() 连回来。
    """
    now = now or _now()
    if not is_trading_day(now):
        return False
    t = _mins(now.hour, now.minute)
    return _mins(*_STREAM_OPEN) <= t <= _mins(*_PM_END)


def session_elapsed_minutes(now: datetime | None = None) -> float:
    """当日已过交易分钟数 (0~240)。午休冻结在 120。非交易日/未开盘返回 0。"""
    now = now or _now()
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


def _as_date(value):
    """datetime / date / YYYY-MM-DD → date; 无效返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def next_trading_day(day=None, inclusive: bool = False) -> datetime | None:
    """day 当天 (inclusive) 或之后第一个交易日 (00:00 时刻); 找不到返回 None。

    走缓存日历二分, 没有长假天数上限 (旧实现逐日扫 10 天就放弃)。
    """
    base = _as_date(day) or _now().date()
    if not inclusive:
        base += timedelta(days=1)
    try:
        days = _xshg_sorted(base.year - 1, base.year + 1)
    except Exception:
        # 日历包缺失: 退化为周一~周五, 与 is_trading_day 的降级口径一致
        for _ in range(400):
            if _weekday_fallback(base.isoformat()):
                return datetime(base.year, base.month, base.day)
            base += timedelta(days=1)
        return None
    i = bisect_left(days, base.isoformat())
    if i >= len(days):
        return None
    return datetime.strptime(days[i], "%Y-%m-%d")


def _at(day: datetime | None, hm) -> datetime | None:
    if day is None:
        return None
    return day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def next_session_open(now: datetime | None = None) -> datetime | None:
    """下一个连续竞价开盘时刻: 今日 09:30 / 今日 13:00 / 次交易日 09:30。

    已在连续竞价中返回 None (没有"下一个"可言)。
    """
    now = now or _now()
    if in_session(now):
        return None
    if is_trading_day(now):
        t = _mins(now.hour, now.minute)
        if t < _mins(*_AM_START):
            return _at(now, _AM_START)
        if t < _mins(*_PM_START):
            return _at(now, _PM_START)
    return _at(next_trading_day(now), _AM_START)


def next_live_at(now: datetime | None = None) -> datetime | None:
    """下一个"开始拉行情"时刻: 今日 09:15 / 今日 13:00 / 次交易日 09:15。

    与 next_session_open() 的差别只在 09:15-09:30 —— 集合竞价期快照已有意义。
    """
    now = now or _now()
    if is_live(now):
        return None
    if is_trading_day(now):
        t = _mins(now.hour, now.minute)
        if t < _mins(*_AUCTION_START):
            return _at(now, _AUCTION_START)
        if t < _mins(*_PM_START):
            return _at(now, _PM_START)
    return _at(next_trading_day(now), _AUCTION_START)


def next_stream_at(now: datetime | None = None) -> datetime | None:
    """下一个可建立行情流的时刻: 今日 09:00 / 次交易日 09:00。已在窗口内 None。"""
    now = now or _now()
    if can_connect_stream(now):
        return None
    if is_trading_day(now) and _mins(now.hour, now.minute) < _mins(*_STREAM_OPEN):
        return _at(now, _STREAM_OPEN)
    return _at(next_trading_day(now), _STREAM_OPEN)


def _secs_until(target: datetime | None, now: datetime) -> float | None:
    if target is None:
        return None
    return max(0.0, round((target - now).total_seconds(), 1))


def seconds_until_session(now: datetime | None = None) -> float:
    """距离下一个连续竞价窗口的秒数。已在窗口内返回 0。"""
    now = now or _now()
    if in_session(now):
        return 0.0
    secs = _secs_until(next_session_open(now), now)
    return 60.0 if secs is None else max(1.0, secs)


def seconds_until_live(now: datetime | None = None) -> float:
    """距离下一个"开始拉行情"时刻的秒数 (含集合竞价)。已在活跃时段返回 0。"""
    now = now or _now()
    if is_live(now):
        return 0.0
    secs = _secs_until(next_live_at(now), now)
    return 60.0 if secs is None else max(1.0, secs)


def _fmt_dt(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def market_status(now: datetime | None = None) -> dict:
    """接口透传的统一市场状态 (ping / stream/status / kline meta 共用一份口径)。

    in_session 语义与历史一致 (仅连续竞价), 老客户端不受影响; quote_live 才是
    "现在该不该拉行情" (含集合竞价)。next_live_in_sec 下发的是**相对秒数**——
    客户端时钟不准也能精确唤醒, 不要下发绝对时刻让前端去跟本地时钟比。

    server_ms 必须按 _CST 解释后再取 epoch: now 是 naive 北京时间, 直接
    .timestamp() 会在 UTC 容器里被当成 UTC, 整体差 8 小时。
    """
    now = now or _now()
    phase = session_phase(now)
    live = phase in LIVE_PHASES
    stream_ok = can_connect_stream(now)
    nxt_live = None if live else next_live_at(now)
    nxt_open = next_session_open(now)
    nxt_stream = None if stream_ok else next_stream_at(now)
    return {
        "time": str(now),
        "server_ms": int(now.replace(tzinfo=_CST).timestamp() * 1000),
        "is_trading_day": is_trading_day(now),
        "in_session": in_session(now),
        "session_phase": phase,
        "is_auction": phase == "auction",
        "quote_live": live,
        "stream_allowed": stream_ok,
        "next_open_at": _fmt_dt(nxt_open),
        "next_open_in_sec": _secs_until(nxt_open, now),
        "next_live_at": _fmt_dt(nxt_live),
        "next_live_in_sec": _secs_until(nxt_live, now),
        "next_stream_at": _fmt_dt(nxt_stream),
        "next_stream_in_sec": _secs_until(nxt_stream, now),
        "calendar_source": calendar_source(),
    }


def nth_trading_day(start_day: str, n: int):
    """推荐持仓周期的第 n 个交易日 (含起算日)。

    start_day 当天若是交易日则计为第 1 日, 否则从其后第一个交易日起算。
    n < 1 或日期无效返回 None; 一年内找不到也返回 None。
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    try:
        dt = datetime.strptime(str(start_day)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    found = 0
    for _ in range(max(n * 4, n + 60)):
        if is_trading_day(dt):
            found += 1
            if found >= n:
                return dt.strftime("%Y-%m-%d")
        dt += timedelta(days=1)
    return None
