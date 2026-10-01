"""每日因子库: 全市场日K级因子 (技术/量价/筹码) + 基本面/资金快照。

为什么需要它: 选股原来逐只拉日K (300 只 ≈50 分钟) 且每次重算。因子库把
「全市场拉K + 因子计算」搬到**交易日 18:00** 一次完成 (100 只/批, 90% 额度
= 54/min, 实测 7843 只 79 批 ≈87 秒), 扫描只读快照 → 秒级; 同一天同条件
不再重复扫 (结果去重见 api/screener.py 的 cache_key)。

存储 (``.cache/``):

- ``factors.db``                     SQLite: ``bars`` / ``meta`` / ``builds`` 三张表
- ``factors/snapshot_<day>.pkl.gz``  当日全市场因子表 (扫描直接读它)

指标口径与图表一致: 技术指标一律用 ``indicators.compute_all_indicators``
(同一套 stock-indicators-cn 实现), 筹码用 ``chips.compute_chips``。

只在交易日跑: 调度线程每 5 分钟巡检, 交易日到点且当日未成功才构建; 失败每 30
分钟重试, 最多 3 次。开始/完成/异常都会通知管理员 (站内 + 钉钉/ntfy), 进度
(phase/percent) 落 ``builds`` 表供页面查询。
"""
from __future__ import annotations

import gzip
import logging
import os
import pickle
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from stock_indicators_cn.validation import admit_ohlc

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / ".cache"
DB_PATH = CACHE_DIR / "factors.db"
SNAP_DIR = CACHE_DIR / "factors"

BARS_COUNT = max(60, int(os.environ.get("FACTORS_BARS", "150")))
MIN_BARS = 35                 # 少于这个根数不产出因子 (指标窗口不够)
BATCH_SIZE = 100              # AF 批量单次上限 (见 af_limits)
RETRY_MIN_SEC = 30 * 60
MAX_ATTEMPTS = 3
SCHED_INTERVAL_SEC = 300
PHASE_WEIGHTS = {"bars": (0, 70), "factors": (70, 92), "meta": (92, 100)}

log = logging.getLogger("factors")

_build_children = []
_build_children_lock = threading.Lock()
_live_running = False


class BuildAlreadyRunning(RuntimeError):
    """同一 Web 进程已有因子构建子进程。"""


class HeavyLock:
    """跨进程互斥 (构建子进程 / 盘中快照 / 搜索索引)。Windows 上退化为空操作。"""

    def __init__(self):
        self._local = threading.local()

    def acquire(self, blocking=True):
        if os.name == "nt":
            return True
        import fcntl
        path = CACHE_DIR / "heavy.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+", encoding="utf-8")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError:
            fh.close()
            return False
        self._local.fh = fh
        return True

    def release(self):
        if os.name == "nt":
            return
        fh = getattr(self._local, "fh", None)
        if fh is None:
            return
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            self._local.fh = None


heavy_lock = HeavyLock()


def _track_child(proc, day):
    with _build_children_lock:
        alive = [(p, d) for p, d in _build_children if p.poll() is None]
        alive.append((proc, day))
        _build_children[:] = alive


def stop_build_children():
    """内存紧急时只停构建子进程, Web 进程继续服务。返回终止个数。

    SIGTERM 下子进程来不及走 build() 的 finally, 构建记录会停在 running;
    这里替它落成 error, 页面不会一直显示「构建中」, 调度按失败退避重试。
    """
    with _build_children_lock:
        procs = [(p, d) for p, d in _build_children if p.poll() is None]
    stopped = 0
    for proc, day in procs:
        try:
            proc.terminate()
            stopped += 1
        except Exception:
            continue
        try:
            _update_build(day, state="error", phase=None,
                          error="内存紧急, 构建子进程已终止", done_at=_iso())
        except Exception as e:
            log.warning("标记构建终止失败 %s: %s", day, e)
    return stopped


def build_child_running():
    with _build_children_lock:
        return any(p.poll() is None for p, _d in _build_children)


def live_running():
    return _live_running


def _under_pressure():
    try:
        import memguard
        return memguard.under_pressure()
    except Exception:
        return False


def _log_rss(label):
    try:
        import memguard
        memguard.log_rss(log, label)
    except Exception:
        pass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol     TEXT PRIMARY KEY,
    name       TEXT,
    n          INTEGER NOT NULL,
    first_date TEXT,
    last_date  TEXT,
    dates      BLOB NOT NULL,
    open       BLOB NOT NULL,
    high       BLOB NOT NULL,
    low        BLOB NOT NULL,
    close      BLOB NOT NULL,
    volume     BLOB NOT NULL,
    amount     BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    symbol       TEXT PRIMARY KEY,
    float_shares REAL,
    total_shares REAL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS builds (
    day        TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    phase      TEXT,
    percent    INTEGER NOT NULL DEFAULT 0,
    n_symbols  INTEGER NOT NULL DEFAULT 0,
    n_rows     INTEGER NOT NULL DEFAULT 0,
    n_missing  INTEGER NOT NULL DEFAULT 0,
    sources    TEXT,
    error      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    done_at    TEXT
);
"""

_build_lock = threading.Lock()
_build_state = {"day": None, "running": False}
_ready = False
_snap_lock = threading.Lock()
_snap_cache = {"key": None, "df": None, "day": None}
_thread = None
_thread_lock = threading.Lock()


# ── 时间/路径 ──
def _now_dt():
    import market_hours
    return market_hours.now()


def _iso(dt=None):
    return (dt or _now_dt()).isoformat(timespec="seconds")


def last_closed_trading_day(now=None):
    """最近**已收盘**的交易日 (YYYY-MM-DD): 交易日 15:00 后算今天, 否则往前找。"""
    import market_hours
    now = now or market_hours.now()
    day = now.date()
    if market_hours.is_trading_day(now) and (now.hour, now.minute) >= (15, 0):
        return day.isoformat()
    probe = now - timedelta(days=1)
    for _ in range(30):
        if market_hours.is_trading_day(probe):
            return probe.date().isoformat()
        probe -= timedelta(days=1)
    return day.isoformat()


def due_day(now=None):
    """当前该构建的交易日; **未到构建时刻返回 None** (不构建)。

    因子库只在 ``FACTORS_BUILD_AT``(默认 18:00) 后开跑: 未到点一律不构建, 也不做
    "上一交易日漏跑补建" —— 晚间进程不在时, 就顺延到下一个交易日 18:00。
    15:00~18:00 尤其不能把"今天"当目标: 龙虎榜要收盘后才发布、质押 15:30 才刷新,
    提前构建会让当天快照永久缺这些列 (18:00 又因 already_done 不再构建)。
    """
    import market_hours
    now = now or _now_dt()
    if (now.hour, now.minute) >= build_at() and market_hours.is_trading_day(now):
        return now.date().isoformat()
    return None


def before_build_at(now=None):
    """现在是否还没到当日构建时刻 (未到点不该自动构建, 手动重建也需 force)。"""
    now = now or _now_dt()
    return (now.hour, now.minute) < build_at()


def build_at():
    """FACTORS_BUILD_AT=HH:MM → (hour, minute); 非法回落 18:00。"""
    raw = os.environ.get("FACTORS_BUILD_AT", "").strip()
    if raw:
        try:
            h, m = raw.split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return (h, m)
        except (TypeError, ValueError):
            pass
        log.warning("FACTORS_BUILD_AT 无效 (%s), 回落 18:00", raw)
    return (18, 0)


def _conn():
    global _ready
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    if not _ready:
        conn.executescript(_SCHEMA)
        conn.commit()
        _ready = True
    return conn


def init_store():
    """建表 (幂等)。"""
    conn = _conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)


# ── K 线存取 ──
_NUM_COLS = ("open", "high", "low", "close", "volume", "amount")


def save_bars(conn, symbol, name, df):
    """写一只标的的日K (覆盖)。df 需含 ohlcv (+amount 可选), 索引为日期。"""
    if df is None or len(df) == 0:
        return False
    dates = pd.to_datetime(df.index).values.astype("datetime64[D]").astype(np.int32)
    blobs = [dates.tobytes()]
    for col in _NUM_COLS:
        series = df[col] if col in df.columns else pd.Series(
            np.zeros(len(df)), index=df.index)
        values = series.to_numpy(dtype=float, na_value=np.nan)
        # Preserve bad prices so live admission cannot mistake Inf's finite
        # nan_to_num replacement for a valid historical high/low.
        if col not in ("open", "high", "low", "close"):
            values = np.nan_to_num(values)
        blobs.append(values.astype(np.float64).tobytes())
    conn.execute(
        "INSERT INTO bars(symbol,name,n,first_date,last_date,dates,open,high,low,close,"
        "volume,amount,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, n=excluded.n, "
        "first_date=excluded.first_date, last_date=excluded.last_date, dates=excluded.dates, "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
        "volume=excluded.volume, amount=excluded.amount, updated_at=excluded.updated_at",
        (symbol, name or symbol, len(df), str(dates[0]), str(dates[-1]), *blobs, _iso()))
    return True


def load_bars(symbol):
    """读一只标的的日K → DataFrame (索引 DatetimeIndex) 或 None。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM bars WHERE symbol=?", (symbol,)).fetchone()
    finally:
        conn.close()
    return _row_to_df(row)


def _row_to_df(row):
    if row is None:
        return None
    n = int(row["n"])
    dates = np.frombuffer(row["dates"], dtype=np.int32).astype("datetime64[D]")
    data = {"date": dates}
    for col in _NUM_COLS:
        data[col] = np.frombuffer(row[col], dtype=np.float64)
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").iloc[:n]


def load_bars_many(symbols):
    """一次连接读多只日K → {symbol: df}。

    盘中口径要给全市场补 bar, 逐只 load_bars 会开约 7800 次连接 (每次都跑 PRAGMA)。
    """
    if not symbols:
        return {}
    conn = _conn()
    try:
        marks = ",".join("?" * len(symbols))
        rows = conn.execute(
            f"SELECT * FROM bars WHERE symbol IN ({marks})", list(symbols)).fetchall()
    finally:
        conn.close()
    out = {}
    for row in rows:
        df = _row_to_df(row)
        if df is not None:
            out[row["symbol"]] = df
    return out


def iter_bars(symbols=None):
    """遍历全部 (或指定) 标的的日K; 逐行读避免一次性载入全市场。"""
    conn = _conn()
    try:
        if symbols is None:
            cur = conn.execute("SELECT * FROM bars ORDER BY symbol")
        else:
            marks = ",".join("?" * len(symbols))
            cur = conn.execute(f"SELECT * FROM bars WHERE symbol IN ({marks})", list(symbols))
        for row in cur.fetchall():
            df = _row_to_df(row)
            if df is not None:
                yield row["symbol"], row["name"], df
    finally:
        conn.close()


# ── 股本 (周更) ──
def load_shares():
    """{symbol: (float_shares, total_shares)}; 空表示还没建过。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT symbol, float_shares, total_shares FROM meta").fetchall()
        return {r["symbol"]: (r["float_shares"], r["total_shares"]) for r in rows}
    finally:
        conn.close()


def save_shares(pairs):
    """pairs: {symbol: (float_shares, total_shares)}。"""
    if not pairs:
        return 0
    now = _iso()
    conn = _conn()
    try:
        conn.executemany(
            "INSERT INTO meta(symbol,float_shares,total_shares,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET float_shares=excluded.float_shares, "
            "total_shares=excluded.total_shares, updated_at=excluded.updated_at",
            [(s, f, t, now) for s, (f, t) in pairs.items()])
        conn.commit()
        return len(pairs)
    finally:
        conn.close()


def shares_age_days():
    conn = _conn()
    try:
        row = conn.execute("SELECT MAX(updated_at) AS ts FROM meta").fetchone()
    finally:
        conn.close()
    if not row or not row["ts"]:
        return None
    try:
        return (datetime.fromisoformat(row["ts"]) - datetime.fromisoformat(_iso())).total_seconds() / -86400.0
    except ValueError:
        return None


# ── builds (构建记录 = 页面的进度/最近 5 交易日) ──
def get_build(day):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM builds WHERE day=?", (day,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_builds(limit=10):
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM builds ORDER BY day DESC LIMIT ?",
                            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _update_build(day, **fields):
    allowed = {"state", "phase", "percent", "n_symbols", "n_rows", "n_missing",
               "sources", "error", "attempts", "started_at", "done_at"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return
    args.append(day)
    conn = _conn()
    try:
        conn.execute(f"UPDATE builds SET {', '.join(sets)} WHERE day=?", args)
        conn.commit()
    finally:
        conn.close()


def _ensure_build_row(day):
    conn = _conn()
    try:
        conn.execute("INSERT OR IGNORE INTO builds(day,state,attempts) VALUES(?,?,0)",
                     (day, "idle"))
        conn.commit()
        return dict(conn.execute("SELECT * FROM builds WHERE day=?", (day,)).fetchone())
    finally:
        conn.close()


def _bump_attempts(day):
    conn = _conn()
    try:
        conn.execute("UPDATE builds SET attempts=attempts+1 WHERE day=?", (day,))
        conn.commit()
        row = conn.execute("SELECT attempts FROM builds WHERE day=?", (day,)).fetchone()
        return int(row["attempts"]) if row else 0
    finally:
        conn.close()


# ── 因子计算 (纯函数, 便于单测) ──
def _f(v, default=None):
    """NaN/None/非数 → default。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if x != x else x


def _streak(closes):
    """末尾连涨/连跌天数。"""
    up = down = 0
    for i in range(len(closes) - 1, 0, -1):
        diff = closes[i] - closes[i - 1]
        if diff > 0 and down == 0:
            up += 1
        elif diff < 0 and up == 0:
            down += 1
        else:
            break
    return up, down


def _limit_flags(symbol, name, change_pct, close):
    """涨停/跌停判定 (按板块幅度 + ST)。ETF/无涨跌幅限制的品种返回 (False, False)。"""
    if change_pct is None:
        return False, False
    code = str(symbol).split(".")[0]
    if not code.isdigit() or len(code) != 6:
        return False, False
    if code.startswith(("15", "16", "50", "51", "52", "56", "58")):   # ETF/LOF
        return False, False
    st = "ST" in str(name or "").upper()
    # 北交所 (83/87/88/43/82, 以及 2025 起的 92 新代码) ±30%, ST 同样 30%。
    if code.startswith(("83", "87", "88", "43", "82", "92")):
        pct = 29.8
    elif code.startswith(("300", "301", "688", "689")):
        pct = 19.8          # 创业板/科创板注册制 ±20%, ST 也是 20% 不是 5%
    else:
        pct = 4.8 if st else 9.8
    return bool(change_pct >= pct), bool(change_pct <= -pct)


def compute_factors(df, symbol="", name="", float_shares=None):
    """单只标的 → 因子 dict (列名与 screener_metrics 的 field 一致)。失败返回 None。

    df: 日K DataFrame (索引日期, 含 open/high/low/close/volume/amount)。
    技术指标复用 indicators.compute_all_indicators (与图表同一套口径)。
    """
    if df is None or len(df) < MIN_BARS:
        return None
    if not admit_ohlc(df, symbol=symbol, context="Visual 因子", report=log.warning):
        return None
    from indicators import compute_all_indicators, sma, volume_shares_mult

    out, _ind = compute_all_indicators(df, "1d", serialize=False)
    c = out["close"]
    close = _f(c.iloc[-1])
    prev_close = _f(c.iloc[-2])
    if close is None or prev_close in (None, 0):
        return None
    hi, lo = out["high"], out["low"]

    ma = {n: sma(c, n) for n in (5, 10, 20, 60)}
    ma_last = {n: _f(s.iloc[-1]) for n, s in ma.items()}
    ma_prev = {n: _f(s.iloc[-2]) for n, s in ma.items()}
    above = {n: bool(ma_last[n] is not None and close > ma_last[n]) for n in ma_last}
    vals = [v for v in ma_last.values() if v is not None]
    converge = (max(vals) - min(vals)) / close * 100 if len(vals) == 4 else None

    def _cross(fast, slow):
        f0, f1, s0, s1 = ma_prev[fast], ma_last[fast], ma_prev[slow], ma_last[slow]
        if None in (f0, f1, s0, s1):
            return False
        return bool(f0 <= s0 and f1 > s1)

    change_pct = (close / prev_close - 1) * 100
    up_streak, down_streak = _streak(c.to_numpy(dtype=float))
    vol = out["volume"].to_numpy(dtype=float)
    vol_ma5 = _f(out["vol_ma5"].iloc[-1])
    volume = _f(out["volume"].iloc[-1])
    high = _f(hi.iloc[-1])
    low = _f(lo.iloc[-1])

    def _chg(n):
        if len(c) < n + 1:
            return None
        base = _f(c.iloc[-n - 1])
        return None if not base else (close / base - 1) * 100

    def _new_high(n):
        if len(c) < n:
            return False
        return bool(high is not None and high >= np.nanmax(hi.to_numpy(dtype=float)[-n:]))

    def _new_low(n):
        if len(c) < n:
            return False
        return bool(low is not None and low <= np.nanmin(lo.to_numpy(dtype=float)[-n:]))

    mult = volume_shares_mult(c, out["volume"],
                              out["amount"] if "amount" in out.columns else None, tail=10)
    turnover = None
    if float_shares and float_shares > 0 and volume is not None:
        turnover = volume * mult / float_shares * 100.0

    dif, dea = _f(out["macd_dif"].iloc[-1]), _f(out["macd_dea"].iloc[-1])
    dif_p, dea_p = _f(out["macd_dif"].iloc[-2]), _f(out["macd_dea"].iloc[-2])
    hist, hist_p = _f(out["macd_hist"].iloc[-1]), _f(out["macd_hist"].iloc[-2])
    k, d = _f(out["kdj_k"].iloc[-1]), _f(out["kdj_d"].iloc[-1])
    k_p, d_p = _f(out["kdj_k"].iloc[-2]), _f(out["kdj_d"].iloc[-2])
    boll_up, boll_mid, boll_low = (_f(out["boll_up"].iloc[-1]), _f(out["boll_mid"].iloc[-1]),
                                   _f(out["boll_low"].iloc[-1]))
    limit_up, limit_down = _limit_flags(symbol, name, change_pct, close)

    return {
        "symbol": symbol, "name": name or symbol, "close": close, "prev_close": prev_close,
        "change_pct": change_pct,
        "amount": _f(out["amount"].iloc[-1], 0.0) if "amount" in out.columns else None,
        "volume": volume, "turnover": turnover,
        "amplitude": ((high - low) / prev_close * 100) if (high is not None and low is not None) else None,
        "atr_pct": (lambda a: None if not a or not close else a / close * 100)(_f(out["atr14"].iloc[-1])),
        # 均线
        "ma5": ma_last[5], "ma10": ma_last[10], "ma20": ma_last[20], "ma60": ma_last[60],
        "above_ma5": above[5], "above_ma10": above[10], "above_ma20": above[20],
        "above_ma60": above[60],
        "ma5_cross_ma20": _cross(5, 20), "ma10_cross_ma60": _cross(10, 60),
        "ma_bull_align": bool(all(above[n] for n in (5, 10, 20, 60)) and len(vals) == 4
                              and vals == sorted(vals, reverse=True)),
        "ma_converge_pct": converge,
        # MACD
        "macd_dif": dif, "macd_dea": dea, "macd_hist": hist,
        "macd_cross_up": bool(dif_p is not None and dea_p is not None and dif is not None
                              and dea is not None and dif_p <= dea_p and dif > dea),
        "macd_cross_down": bool(dif_p is not None and dea_p is not None and dif is not None
                                and dea is not None and dif_p >= dea_p and dif < dea),
        "macd_above_zero": bool(dif is not None and dif > 0),
        "macd_hist_expand": bool(hist is not None and hist_p is not None
                                 and hist > 0 and hist > hist_p),
        # KDJ / RSI
        "kdj_k": k, "kdj_d": d, "kdj_j": _f(out["kdj_j"].iloc[-1]),
        "kdj_golden": bool(k_p is not None and d_p is not None and k is not None
                           and d is not None and k_p <= d_p and k > d),
        "rsi6": _f(out["rsi6"].iloc[-1]), "rsi12": _f(out["rsi12"].iloc[-1]),
        "rsi24": _f(out["rsi24"].iloc[-1]),
        # BOLL / 其他
        "boll_up": boll_up, "boll_mid": boll_mid, "boll_low": boll_low,
        "boll_break_up": bool(boll_up is not None and close > boll_up),
        "boll_above_mid": bool(boll_mid is not None and close > boll_mid),
        "boll_break_low": bool(boll_low is not None and close < boll_low),
        "wr14": _f(out["wr14"].iloc[-1]), "cci14": _f(out["cci14"].iloc[-1]),
        "bias6": _f(out["bias6"].iloc[-1]), "bias12": _f(out["bias12"].iloc[-1]),
        "bias24": _f(out["bias24"].iloc[-1]), "dmi_adx": _f(out["dmi_adx"].iloc[-1]),
        # 量价
        "vol_ma5": vol_ma5,
        "vol_ratio5": (volume / vol_ma5) if (volume is not None and vol_ma5) else None,
        "vol_shrink5": bool(volume is not None and vol_ma5 and volume < 0.7 * vol_ma5),
        "chg_3d": _chg(3), "chg_5d": _chg(5), "chg_10d": _chg(10), "chg_20d": _chg(20),
        "up_streak": up_streak, "down_streak": down_streak,
        "new_high_20": _new_high(20), "new_high_60": _new_high(60),
        "new_low_20": _new_low(20), "new_low_60": _new_low(60),
        "limit_up": limit_up, "limit_down": limit_down,
        "bars": len(df),
    }


def compute_chip_factors(df, float_shares, mult):
    """筹码获利盘/集中度/平均成本 (同一份日K, 不再取数)。无数据返回 {}。"""
    if df is None or len(df) < MIN_BARS or not float_shares or float_shares <= 0:
        return {}
    try:
        from chips import compute_chips
        # 直接用 numpy 数组拼 bars: iterrows 在全市场 7800 只上是主要开销之一
        vol = np.nan_to_num(df["volume"].to_numpy(dtype=float))
        amount = (np.nan_to_num(df["amount"].to_numpy(dtype=float))
                  if "amount" in df.columns else np.zeros(len(df)))
        opens = np.nan_to_num(df["open"].to_numpy(dtype=float))
        closes = np.nan_to_num(df["close"].to_numpy(dtype=float))
        highs = np.nan_to_num(df["high"].to_numpy(dtype=float))
        lows = np.nan_to_num(df["low"].to_numpy(dtype=float))
        dates = [str(i)[:10] for i in df.index]
        scale = mult / float_shares * 100.0
        bars = [{
            "date": dates[i], "open": opens[i], "close": closes[i],
            "high": highs[i], "low": lows[i], "volume": vol[i], "amount": amount[i],
            "hsl": vol[i] * scale,
        } for i in range(len(df))]
        res = compute_chips(bars)
    except Exception as e:
        log.warning("筹码计算失败: %s", e)
        return {}
    if not res:
        return {}
    close = _f(df["close"].iloc[-1])
    avg = _f(res.get("avgCost"))
    return {
        "chip_profit": (_f(res.get("profitRatio")) or 0.0) * 100.0,
        "chip_concentration": (_f(res.get("pct90Con")) or 0.0) * 100.0,
        "chip_avg_cost": avg,
        "above_chip_cost": bool(close is not None and avg and close > avg),
    }


# ── 取数 ──
def universe():
    """全市场标的池 (A股+北交所+场内基金, 本地列表零额度)。"""
    import market
    rows = market._load_stock_list() or []
    return [(r["symbol"], r.get("name") or r["symbol"]) for r in rows if r.get("symbol")]


_kline_rate_warned = False


def kline_rate():
    """拉日K的令牌速率 (次/分钟): AlphaFeed 日K批量额度的 90% = 54/min。

    单只日K在 visual 里也走批量接口, 与全市场批量共用这一份额度 (见 af_limits)。
    覆盖用 ``FACTORS_KLINE_PER_MIN``。``SCREENER_KLINE_PER_MIN`` 是旧逐只扫描的
    限速 (默认 6), 留下这个值会把全市场构建从约 87 秒拖到十几分钟, 这里不再读取。
    """
    global _kline_rate_warned
    import af_limits
    default = af_limits.bucket_rate("kline_daily_batch")
    raw = os.environ.get("FACTORS_KLINE_PER_MIN", "").strip()
    legacy = os.environ.get("SCREENER_KLINE_PER_MIN", "").strip()
    if not raw and legacy and not _kline_rate_warned:
        _kline_rate_warned = True
        log.warning("SCREENER_KLINE_PER_MIN=%s 不再控制因子库拉K, 请改用 "
                    "FACTORS_KLINE_PER_MIN (当前仍按 %d/min)", legacy, default)
    if not raw:
        return max(1, default)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        log.warning("FACTORS_KLINE_PER_MIN 无效 (%s), 回落 %d", raw, default)
        return max(1, default)


def live_quote_rate():
    """盘中补 bar 的快照速率。与 feed.QUOTES_RATE_PER_MIN 分用同一份 120/min 额度,
    不能再按 90% (108/min) 单独开桶, 否则和监控/图表叠在一起会打满限额。

    默认 20/min (全市场约 79 批, 4 分钟量级), 且不超过额度的 90%。
    """
    import af_limits
    cap = af_limits.bucket_rate("quotes_symbol")
    raw = os.environ.get("FACTORS_LIVE_QUOTES_PER_MIN", "20").strip()
    try:
        rate = int(raw)
    except (TypeError, ValueError):
        rate = 20
    return max(1, min(rate, cap))


# 日K批量额度是全进程共享的 (图表的单只日K也走同一接口): 构建按 kline_rate()
# 取令牌, 图表取不到令牌时直接走回退源, 避免两边合计打出 429 后整批标的丢失。
_daily_kline_bucket = None
_daily_kline_bucket_rate = None
_daily_kline_bucket_path = None
_daily_kline_bucket_lock = threading.Lock()
FETCH_BARS_RETRIES = 2          # 失败批次的额外重试次数
FETCH_BARS_RETRY_SLEEP = 2.0   # 每次重试前的等待 (秒)


def daily_kline_bucket():
    """Web 与构建子进程共用的日K批量令牌桶。"""
    global _daily_kline_bucket, _daily_kline_bucket_rate, _daily_kline_bucket_path
    rate = kline_rate()
    path = CACHE_DIR / "af_daily_budget.sqlite3"
    with _daily_kline_bucket_lock:
        if (_daily_kline_bucket is None or _daily_kline_bucket_rate != rate
                or _daily_kline_bucket_path != path):
            from shared_budget import SharedTokenBucket
            _daily_kline_bucket = SharedTokenBucket(path, rate_per_min=rate)
            _daily_kline_bucket_rate = rate
            _daily_kline_bucket_path = path
        return _daily_kline_bucket


def _fetch_bars_chunk(af, chunk, count):
    """拉一批日K; 失败返回 None (调用方决定重试)。"""
    return af.klines.batch(list(chunk), period="1d", count=count,
                           adjust="forward", to_dataframe=True)


def _acquire_token(bucket):
    """按令牌补充速率睡眠, 避免 50ms 忙等占 CPU。"""
    while not bucket.try_acquire():
        rate = getattr(bucket, "rate", None)
        wait = 1.0 / rate if isinstance(rate, (int, float)) and rate > 0 else 1.0
        time.sleep(min(wait, 5.0))


def fetch_bars(symbols, count=BARS_COUNT, progress_cb=None):
    """批量拉日K (前复权), 按批 yield ``(batch, stats)``。

    调用方处理完一批就丢掉 DataFrame, 不要把约 8000 只同时留在内存里。
    stats 含 batches / done / failed_batches, 随每一批更新。
    令牌桶按 kline_rate() 限速, 与图表共用 (见 daily_kline_bucket)。
    失败批次退避重试 FETCH_BARS_RETRIES 次; 仍失败的批次数记在 failed_batches。
    progress_cb(done_batches, total_batches) 用于落库进度。
    """
    import market
    af = market.get_af()
    bucket = daily_kline_bucket()
    chunks = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    failed = 0
    for i, chunk in enumerate(chunks, 1):
        dfs = None
        for attempt in range(FETCH_BARS_RETRIES + 1):
            _acquire_token(bucket)
            try:
                dfs = _fetch_bars_chunk(af, chunk, count)
                break
            except Exception as e:
                log.warning("批量拉K失败 (%d/%d, 第 %d 次): %s",
                            i, len(chunks), attempt + 1, e)
                dfs = None
                if attempt < FETCH_BARS_RETRIES:
                    time.sleep(FETCH_BARS_RETRY_SLEEP)
        batch_failed = dfs is None
        if batch_failed:
            failed += 1
        batch = {}
        for sym in chunk:
            df = (dfs or {}).get(sym)
            if df is None or len(df) == 0:
                continue
            try:
                batch[sym] = market._normalize(df, prefer_time=False, preserve_ohlc=True)
            except Exception:
                continue
        if progress_cb:
            try:
                progress_cb(i, len(chunks))
            except Exception:
                pass
        yield batch, {"batches": len(chunks), "done": i, "failed_batches": failed}


def _fetch_shares(symbols):
    """AF instruments.batch → 流通/总股本 (股本变动少, 7 天刷新一次)。"""
    import market
    af = market.get_af()
    out = {}
    chunks = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    for chunk in chunks:
        try:
            insts = af.instruments.batch(list(chunk)) or []
        except Exception as e:
            log.warning("instruments.batch 失败: %s", e)
            continue
        for it in insts:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "").strip()
            ext = it.get("ext") or {}
            fs, ts = ext.get("float_shares"), ext.get("total_shares")
            if sym and (fs or ts):
                out[sym] = (fs, ts)
    return out


# ── 外部快照 (任一源失败只记状态, 不中断构建) ──
def _code_of(symbol):
    return str(symbol).split(".")[0]


def _src_spot():
    """东财全市场快照: 市值/PE/PB/换手率/成交额 (push2 不通时返回空)。"""
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    out = {}
    for _i, r in df.iterrows():
        code = str(r.get("代码") or "").zfill(6)
        if not code:
            continue
        out[code] = {
            "pe": _f(r.get("市盈率-动态")), "pb": _f(r.get("市净率")),
            "float_value": _f(r.get("流通市值")), "total_value": _f(r.get("总市值")),
        }
    return out


# 业绩报表: 报告期刚切换时只有少量公司已披露。行数不到这个阈值就看更早的完整期,
# 避免 1–4 月误用“只有几百家的年报”而丢掉已完整披露的三季报。
YJBB_MIN_ROWS = 3000


def _yjbb_candidates(day):
    """报告期结束日早于 day 的候选, 新→旧 (未结束的季度不取)。"""
    year = int(str(day)[:4])
    stamp = str(day).replace("-", "")
    periods = []
    for y, md in ((year, "0930"), (year, "0630"), (year, "0331"),
                  (year - 1, "1231"), (year - 1, "0930"), (year - 1, "0630")):
        period = f"{y}{md}"
        if period < stamp:
            periods.append(period)
    return periods


def _parse_yjbb(df, period):
    out = {"_period": period, "rows": {}}
    if df is None or len(df) == 0:
        return out
    rows = out["rows"]
    for _i, r in df.iterrows():
        code = str(r.get("股票代码") or "").zfill(6)
        if code and code not in rows:      # 同一代码多行时取首行 (最新)
            rows[code] = {
                "roe": _f(r.get("净资产收益率")),
                "industry": str(r.get("所处行业") or "").strip() or None,
                "eps": _f(r.get("每股收益")), "bps": _f(r.get("每股净资产")),
            }
    return out


def _src_yjbb(day):
    """东财业绩报表: ROE + 所处行业 (+ 每股收益/每股净资产)。

    从新到旧找第一个覆盖不少于 ``YJBB_MIN_ROWS`` 的报告期; 都不够时用行数最多的那期。
    """
    import akshare as ak
    last_err = None
    best = None
    for period in _yjbb_candidates(day):
        try:
            df = ak.stock_yjbb_em(date=period)
        except Exception as e:
            last_err = e
            continue
        parsed = _parse_yjbb(df, period)
        n = len(parsed["rows"])
        if n == 0:
            continue
        if best is None or n > best[0]:
            best = (n, parsed)
        if n >= YJBB_MIN_ROWS:
            return parsed
    if best:
        return best[1]
    if last_err:
        raise last_err
    return {"_period": None, "rows": {}}


def _src_flow():
    """麦蕊全市场主力净流入快照 (一次调用全市场)。"""
    import market
    rows = market.mr_zljlr() or []
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("dm") or "").strip().zfill(6)
        if not code:
            continue
        out[code] = {"main_net_inflow": _f(r.get("zljlr")),
                     "main_net_ratio": _f(r.get("zljlrl"))}
    return out


def _src_lhb(day):
    """东财龙虎榜 (当日上榜名单)。"""
    import akshare as ak
    ymd = str(day).replace("-", "")
    df = ak.stock_lhb_detail_em(start_date=ymd, end_date=ymd)
    if df is None or len(df) == 0:
        return {}
    return {str(r.get("代码") or "").zfill(6): True for _i, r in df.iterrows()}


def _src_pledge():
    """股权质押比例 (复用 market 的每日缓存, 不额外取数)。"""
    import market
    data = market._load_pledge() or {}
    return {str(k).zfill(6): _f((v or {}).get("ratio")) for k, v in data.items()}


def _src_etf_premium():
    """ETF 折溢价率 (东财 ETF 实时行情, 一次调用全市场)。"""
    import akshare as ak
    df = ak.fund_etf_spot_em()
    out = {}
    for _i, r in df.iterrows():
        code = str(r.get("代码") or "").zfill(6)
        if code:
            out[code] = _f(r.get("基金折价率"))
    return out


def external_meta(day, symbols):
    """汇总外部快照 → (symbol→列, 数据源状态)。逐源 try/except, 失败不影响其他源。"""
    sources = {}
    codes = {_code_of(s): s for s in symbols}
    spot, yjbb, flow, lhb, pledge, etf = {}, {}, {}, {}, {}, {}

    for name, fn, holder in (("spot", _src_spot, spot), ("yjbb", _src_yjbb, yjbb),
                             ("flow", _src_flow, flow), ("lhb", _src_lhb, lhb),
                             ("pledge", _src_pledge, pledge),
                             ("etf_premium", _src_etf_premium, etf)):
        t0 = time.time()
        try:
            data = fn(day) if name in ("yjbb", "lhb") else fn()
            if name == "yjbb":
                yjbb_period = data.get("_period")
                data = data.get("rows") or {}
                sources[name] = f"ok:{len(data)}@{yjbb_period}"
            else:
                sources[name] = f"ok:{len(data)}"
            holder.update(data)
        except Exception as e:
            sources[name] = f"fail:{type(e).__name__}"
            log.warning("外部数据源 %s 失败: %s", name, e)
        sources[name] += f" {time.time() - t0:.1f}s"

    out = {}
    for code, sym in codes.items():
        row = {}
        sp = spot.get(code) or {}
        yj = yjbb.get(code) or {}
        fl = flow.get(code) or {}
        row.update({k: v for k, v in sp.items() if v is not None})
        if yj:
            row["roe"] = yj.get("roe")
            row["industry"] = yj.get("industry")
            row["eps"] = yj.get("eps")      # 供无 spot 时反推 PE/PB
            row["bps"] = yj.get("bps")
        if fl:
            row["main_net_inflow"] = fl.get("main_net_inflow")
            row["main_net_ratio"] = fl.get("main_net_ratio")
        if lhb.get(code):
            row["lhb_today"] = True
        pr = pledge.get(code)
        if pr is not None:
            row["pledge_ratio"] = pr
        pm = etf.get(code)
        if pm is not None:
            row["etf_premium"] = pm
        if row:
            out[sym] = row
    return out, sources


# ── 构建 ──
def _notify_state(kind, day, extra=""):
    """开始/完成/异常: 站内通知管理员 + 钉钉/ntfy (先落库再入队)。"""
    titles = {"start": "因子库更新开始", "done": "因子库更新完成", "error": "因子库更新失败"}
    alert_types = {"start": "factors_build_start", "done": "factors_build_done",
                   "error": "factors_build_error"}
    title = titles.get(kind, "因子库更新")
    text = f"## {title} {day}\n\n{extra}"
    try:
        import trades
        for u in trades.list_users() or []:
            if u.get("is_admin"):
                trades.insert_monitor_alert(
                    u["id"], None, "", alert_types.get(kind, "factors_build_error"), day, price=None,
                    detail=f"{title} {day}: {extra}".strip()[:400])
    except Exception as e:
        log.warning("因子库通知落库失败: %s", e)
    try:
        import notify
        notify.notify(title, text, key=f"factors:{day}" if kind == "start" else None,
                      kind="progress" if kind == "start" else "event")
    except Exception as e:
        log.warning("因子库通知推送失败: %s", e)


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _canonical_day(day):
    """把显式日期收成 YYYY-MM-DD; 非法返回 None。

    经正则 + date.fromisoformat 再拼回规范串, 不沿用原始请求文本, 避免不可信
    内容进入 subprocess argv (CodeQL py/command-line-injection)。
    """
    m = _DAY_RE.fullmatch(str(day or "").strip())
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(0)).isoformat()
    except ValueError:
        return None


def _check_day(day):
    """显式日期: YYYY-MM-DD, 且是不晚于最近已收盘交易日的交易日。非法返回原因。"""
    text = _canonical_day(day)
    if not text:
        return "日期格式应为 YYYY-MM-DD"
    import market_hours
    if not market_hours.is_trading_day(text):
        return f"{text} 不是交易日"
    latest = last_closed_trading_day()
    if text > latest:
        return f"{text} 晚于最近已收盘交易日 {latest}"
    return None


def _is_provisional_row(row):
    return bool(row) and "_provisional" in str(row.get("sources") or "")


def _provisional_needs_rebuild(row, now=None):
    """18:00 前强制产出的当日快照, 到点后还要再覆盖一次 (龙虎榜/质押那时才齐)。"""
    return _is_provisional_row(row) and not before_build_at(now)


def _should_mark_provisional(day, now=None):
    now = now or _now_dt()
    return str(day) == now.date().isoformat() and before_build_at(now)


def build(day=None, force=False, notify=True):
    """构建因子库 → 结果 dict。**只允许 18:00 后开跑** (force 或显式 day 例外)。

    已在跑/当日已成功 (且非 force) 时直接返回现状。显式 day 必须是已收盘交易日;
    K 线会截到该日, 最后一根不是该日的标的 (停牌) 不进因子表。
    交易日 18:00 前的强制构建记为 provisional, 到点后调度会再覆盖一次。
    """
    global _build_state
    if day is not None:
        err = _check_day(day)
        if err:
            return {"ok": False, "reason": "bad_day", "day": str(day), "message": err}
        day = _canonical_day(day)
    if not force and day is None and before_build_at():
        return {"ok": False, "reason": "before_build_at", "day": None,
                "message": "因子库只在 %02d:%02d 后构建" % build_at()}
    day = day or due_day() or last_closed_trading_day()
    with _build_lock:
        if _build_state["running"]:
            return {"ok": False, "reason": "running", "day": _build_state["day"]}
        row = _ensure_build_row(day)
        if row["state"] == "done" and not force and not _provisional_needs_rebuild(row):
            return {"ok": True, "reason": "already", "day": day, "n_rows": row["n_rows"]}
        if not force and int(row["attempts"] or 0) >= MAX_ATTEMPTS:
            return {"ok": False, "reason": "attempts_exhausted", "day": day}
        # running 标志与 attempts 自增都放进 try 内: 这两步若抛错(如写锁超时), 之前
        # 写在 try 外会让 finally 不执行 → running 永久为真, 之后所有构建都被拒。
        _build_state.update({"running": True, "day": day})

    attempts = 0
    held = False
    try:
        with _build_lock:
            attempts = _bump_attempts(day)
            _update_build(day, state="running", phase="bars", percent=0, error=None,
                          started_at=_iso(), done_at=None)
        if notify:
            _notify_state("start", day, f"全市场日K + 因子计算 (第 {attempts} 次尝试)")
        held = heavy_lock.acquire(blocking=True)
        try:
            result = _build_inner(day)
        finally:
            if held:
                heavy_lock.release()
                held = False
        sources = dict(result["sources"] or {})
        if _should_mark_provisional(day):
            sources["_provisional"] = True
        with _build_lock:
            _update_build(day, state="done", phase="done", percent=100,
                          done_at=_iso(), error=None,
                          n_symbols=result["n_symbols"], n_rows=result["n_rows"],
                          n_missing=result["n_missing"],
                          sources=_json_sources(sources))
        if notify:
            _notify_state("done", day,
                          f"{result['n_rows']} 只入因子表 (拉取 {result['n_symbols']} 只, "
                          f"缺失 {result['n_missing']}), 用时 {result['elapsed']:.0f}s")
        return {"ok": True, "day": day, **result}
    except Exception as e:
        detail = _error_detail(e)
        log.warning("因子库构建失败: %s", detail, exc_info=False)
        with _build_lock:
            try:
                _update_build(day, state="error", phase=None, error=detail[:300],
                              done_at=_iso())
            except Exception:
                pass
        if notify:
            _notify_state("error", day, f"第 {attempts or 1} 次失败: {detail[:160]}")
        return {"ok": False, "day": day, "error": detail[:300]}
    finally:
        with _build_lock:
            _build_state.update({"running": False, "day": None})


def _json_sources(sources):
    import json
    return json.dumps(sources or {}, ensure_ascii=False)


def _error_detail(e):
    """对外/入推送的错误文案: 密钥脱敏 + 限流归类, 完整堆栈只进日志。

    与项目既有约定一致 (logger.sanitize_error): 上游异常原文可能带内部 URL/响应体,
    不能原样入库、下发到页面或推到共享群。
    """
    try:
        from logger import sanitize_error
        return sanitize_error(e)
    except Exception:
        return f"构建失败 ({type(e).__name__})"


def build_blocked_reason(day=None, force=False):
    """现在能不能开跑 (给"手动重建"如实回报): None = 可以, 否则给拒绝原因。"""
    if day is not None:
        err = _check_day(day)
        if err:
            return err
        day = _canonical_day(day)
    if not force and day is None and before_build_at():
        return "因子库只在 %02d:%02d 后构建（可用「强制重建」立即执行）" % build_at()
    day = day or due_day() or last_closed_trading_day()
    if _build_state["running"]:
        return f"已有构建在进行中（{_build_state['day'] or '日期未知'}）"
    if build_child_running():
        return "已有构建子进程在进行中"
    row = get_build(day)
    if row:
        if row["state"] == "running" and row.get("started_at"):
            try:
                age = (datetime.fromisoformat(_iso())
                       - datetime.fromisoformat(row["started_at"])).total_seconds()
                if age < RETRY_MIN_SEC:
                    return f"{day} 已有构建在进行中"
            except ValueError:
                pass
        if row["state"] == "done" and not force and not _provisional_needs_rebuild(row):
            return f"{day} 已构建完成（可用「强制重建」覆盖）"
        if not force and int(row["attempts"] or 0) >= MAX_ATTEMPTS:
            return f"{day} 今日已失败 {row['attempts']} 次（可用「强制重建」覆盖）"
    return None


def _bars_as_of(df, day):
    """截到 day (含) 为止。最后一根不是该日 (停牌/缺口) 返回 None, 避免把旧因子标成当天。"""
    if df is None or len(df) == 0:
        return None
    cut = df[pd.to_datetime(df.index) <= pd.Timestamp(day)]
    if len(cut) == 0 or str(cut.index[-1])[:10] != str(day):
        return None
    return cut


def _build_inner(day):
    t0 = time.time()
    _log_rss("因子构建开始")
    uni = universe()
    symbols = [s for s, _n in uni]
    names = dict(uni)
    _update_build(day, n_symbols=len(symbols), phase="bars", percent=1)

    def on_bars(done, total):
        lo, hi = PHASE_WEIGHTS["bars"]
        pct = lo + int((hi - lo) * done / max(1, total))
        _update_build(day, percent=pct, phase="bars")

    # 股本先于拉K: 流式处理时不能等全部 DataFrame 到齐再去要股本
    shares = load_shares()
    age = shares_age_days()
    if not shares or age is None or age > 7:
        try:
            fetched = _fetch_shares(symbols)
            if fetched:
                save_shares(fetched)
                shares = load_shares()
        except Exception as e:
            log.warning("股本刷新失败: %s", e)

    # 按批写 bars + 计算因子, 一批算完就丢掉 DataFrame
    tags = {"bars": 0, "factors": 0, "meta": 0}
    rows = []
    missing = 0
    got = 0
    bar_stats = {"batches": 0, "failed_batches": 0}
    n_symbols = max(1, len(symbols))
    conn = _conn()
    try:
        for batch, bar_stats in fetch_bars(symbols, progress_cb=on_bars):
            for sym, df in batch.items():
                got += 1
                view = _bars_as_of(df, day)
                try:
                    # bars 表存最新拉取的序列 (盘中补 bar 要用); 因子按 day 截断后的 view 算
                    save_bars(conn, sym, names.get(sym), df)
                except Exception as e:
                    log.warning("写K线失败 %s: %s", sym, e)
                    continue
                tags["bars"] += 1
                if view is None:
                    missing += 1          # 停牌或该日没有 bar, 不把上一交易日的因子标成当天
                    continue
                fs = (shares.get(sym) or (None, None))[0]
                feats = compute_factors(view, sym, names.get(sym), float_shares=fs)
                if not feats:
                    missing += 1
                    continue
                if fs:
                    try:
                        from indicators import volume_shares_mult
                        mult = volume_shares_mult(view["close"], view["volume"],
                                                  view["amount"] if "amount" in view.columns else None,
                                                  tail=10)
                        feats.update(compute_chip_factors(view, fs, mult))
                    except Exception as e:
                        log.warning("筹码因子失败 %s: %s", sym, e)
                feats["trade_date"] = day
                rows.append(feats)
                tags["factors"] += 1
                # 大批次中先分段提交，避免长时间占用写锁。
                if got % 25 == 0:
                    conn.commit()
                if got % 200 == 0:
                    # _update_build 另开连接，写进度前必须释放当前事务。
                    lo, hi = PHASE_WEIGHTS["factors"]
                    conn.commit()
                    _update_build(day, percent=lo + int((hi - lo) * got / n_symbols),
                                  phase="factors")
            # 即使本批缺数据或末尾标的被跳过，也要在下一次取数进度回调前释放写锁。
            conn.commit()
            del batch
        conn.commit()
    finally:
        conn.close()
    missing += len(symbols) - got

    # 外部快照 (基本面/资金/筹码) — 失败只记 sources
    _update_build(day, phase="meta", percent=PHASE_WEIGHTS["meta"][0])
    ext, sources = external_meta(day, [r["symbol"] for r in rows])
    # 拉K失败的批次: 写进 sources, 页面能看到丢了多少只 (否则只是 n_missing 变大)
    if bar_stats["failed_batches"]:
        sources["bars"] = (f"fail:{bar_stats['failed_batches']}/"
                           f"{bar_stats['batches']}批")

    # ETF/个股的市值兜底: 无 spot 时用 股本 × 收盘 估
    for rec in rows:
        e = ext.get(rec["symbol"]) or {}
        fs, ts = shares.get(rec["symbol"]) or (None, None)
        close = rec.get("close")
        if e.get("float_value") is None and fs and close:
            e["float_value"] = fs * close
        if e.get("total_value") is None and ts and close:
            e["total_value"] = ts * close
        if e.get("pe") is None and e.get("eps") and close:
            e["pe"] = close / e["eps"] if e["eps"] else None
        if e.get("pb") is None and e.get("bps") and close:
            e["pb"] = close / e["bps"] if e["bps"] else None
        rec.update({k: v for k, v in e.items() if v is not None})

    df = pd.DataFrame(rows)
    _write_snapshot(day, df)
    _update_build(day, percent=100, phase="meta", n_rows=len(df), sources=_json_sources(sources))
    _log_rss("因子构建结束")
    return {"n_symbols": len(symbols), "n_rows": len(df), "n_missing": missing,
            "sources": sources, "elapsed": time.time() - t0}


def _snapshot_path(day):
    return SNAP_DIR / f"snapshot_{day}.pkl.gz"


def _write_snapshot(day, df):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _snapshot_path(day).with_suffix(".tmp")
    with gzip.open(tmp, "wb") as fh:
        pickle.dump({"day": day, "df": df, "saved_at": time.time()}, fh, protocol=4)
    os.replace(tmp, _snapshot_path(day))
    with _snap_lock:
        _snap_cache.update({"key": None, "df": None, "day": None})


def available_days():
    """已有快照的交易日 (新→旧)。"""
    if not SNAP_DIR.exists():
        return []
    days = []
    for p in SNAP_DIR.glob("snapshot_*.pkl.gz"):
        days.append(p.name[len("snapshot_"):-len(".pkl.gz")])
    return sorted(days, reverse=True)


def snapshot(day=None):
    """(因子表 DataFrame, day); 无快照返回 (None, None)。内存缓存按 mtime 失效。"""
    days = available_days()
    if not days:
        return None, None
    if day and day not in days:
        day = None
    day = day or days[0]
    path = _snapshot_path(day)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None, None
    with _snap_lock:
        if _snap_cache["key"] == key and _snap_cache["df"] is not None:
            return _snap_cache["df"], _snap_cache["day"]
    try:
        with gzip.open(path, "rb") as fh:
            payload = pickle.load(fh)
        df = payload.get("df")
    except Exception as e:
        log.warning("读取因子快照失败 %s: %s", day, e)
        return None, None
    with _snap_lock:
        _snap_cache.update({"key": key, "df": df, "day": payload.get("day") or day})
    return df, _snap_cache["day"]


def current_version():
    """扫描用的数据版本 = 最新快照的交易日 (无快照返回 None)。"""
    days = available_days()
    return days[0] if days else None


# ── 盘中口径 (可选): 批量快照补当日 bar 后重算 ──
_live_lock = threading.Lock()
_live_cache = {"day": None, "ts": 0.0, "df": None, "version": None}


def live_snapshot(max_age_sec=None, progress_cb=None):
    """盘中口径因子表: 用批量快照补当日 bar 后重算; 不可用返回 (None, None)。

    只在**连续竞价与午休** (``trading``/``break``) 生效: 集合竞价 (09:15-09:30) 与
    盘前拿到的是上一交易日残留快照, 拼上去会凭空多一根今日 bar (量/振幅/KDJ 全失真);
    收盘后因子库本身已是当日收盘口径, 也不该再拼。

    默认 10 分钟缓存 (FACTORS_LIVE_TTL_SEC), 多用户/多次扫描共享; 返回的版本号带
    **时段桶** (``<交易日>+live@HHMM``), 让下游的结果去重能按桶刷新而不是整天冻结。
    口径提示: 用未复权实时价拼前复权序列, 除权日会有一日偏差 (18:00 重建纠正)。
    """
    import market_hours
    ttl = max_age_sec or float(os.environ.get("FACTORS_LIVE_TTL_SEC", "600"))
    now = _now_dt()
    if market_hours.session_phase(now) not in ("trading", "break"):
        return None, None
    base, day = snapshot()
    if base is None or len(base) == 0:
        return None, None
    with _live_lock:
        if (_live_cache["df"] is not None and _live_cache["day"] == day
                and time.time() - _live_cache["ts"] < ttl):
            return _live_cache["df"], _live_cache["version"]

    if _under_pressure():
        log.info("盘中因子表跳过: 内存压力, 回退收盘口径")
        return None, None
    if not heavy_lock.acquire(blocking=False):
        log.info("盘中因子表跳过: 重任务占用中, 回退收盘口径")
        return None, None
    global _live_running
    _live_running = True
    try:
        return _live_snapshot_compute(base, day, ttl, now, progress_cb)
    finally:
        _live_running = False
        heavy_lock.release()


def _live_apply(rec, usable, bars_by_sym, shares, today, day):
    sym = rec.get("symbol")
    q = usable.get(sym)
    if q is None:
        return rec
    last = _f(q.get("last_price"))
    # Validate raw snapshot fields before _patch_last_bar's display fallbacks.
    quote_bar = pd.DataFrame([{f: q.get(f) for f in ("open", "high", "low")}
                              | {"close": last}], index=[today])
    if not admit_ohlc(quote_bar, symbol=sym, context="Visual 盘中快照", report=log.warning):
        return None
    bars = bars_by_sym.get(sym)
    if bars is None or len(bars) == 0:
        rec = dict(rec)
        rec["close"] = last
        prev = _f(rec.get("prev_close"))
        if prev:
            rec["change_pct"] = (last / prev - 1) * 100
        return rec
    patched = _patch_last_bar(bars, today, q, last)
    if not admit_ohlc(patched, symbol=sym, context="Visual 盘中历史", report=log.warning):
        return None
    fs = (shares.get(sym) or (None, None))[0]
    feats = compute_factors(patched, sym, rec.get("name"), float_shares=fs)
    if feats:
        for k in ("chip_profit", "chip_concentration", "chip_avg_cost", "above_chip_cost"):
            if rec.get(k) is not None:
                feats[k] = rec.get(k)      # 筹码不重算 (成本高), 沿用收盘口径
        feats["trade_date"] = day
        return feats
    # A failed quality check cannot fall back to yesterday's selectable factors.
    return None if patched.attrs.get("ohlc_invalid") else rec


def _live_snapshot_compute(base, day, ttl, now, progress_cb):
    today = now.date().isoformat()
    _log_rss("盘中因子开始")
    quotes = _fetch_quotes(list(base["symbol"]), progress_cb=progress_cb)
    if not quotes:
        return None, None
    # 当日有成交的快照交给 _live_apply 校验 OHLC; 非法 last_price 也必须
    # 被标记并跳过, 不能在此过滤后沿用旧因子。非当日/无成交仍沿用收盘口径。
    usable = {}
    for sym, q in quotes.items():
        if _quote_is_today(q, today):
            usable[sym] = q
    shares = load_shares()          # 一次性读, 别在逐只循环里查库
    rows = []
    t0 = time.time()
    cols = list(base.columns)
    chunk = []
    chunk_syms = []
    chunk_cap = max(1, int(os.environ.get("FACTORS_LIVE_CHUNK", "300")))

    def _flush():
        bars_by_sym = load_bars_many(chunk_syms) if chunk_syms else {}
        for rec in chunk:
            result = _live_apply(rec, usable, bars_by_sym, shares, today, day)
            if result is not None:
                rows.append(result)
        chunk.clear()
        chunk_syms.clear()

    for values in base.itertuples(index=False, name=None):
        rec = dict(zip(cols, values))
        chunk.append(rec)
        if rec.get("symbol") in usable:
            chunk_syms.append(rec["symbol"])
        if len(chunk_syms) >= chunk_cap or len(chunk) >= chunk_cap:
            _flush()
    if chunk:
        _flush()
    df = pd.DataFrame(rows) if rows else base.iloc[:0].copy()
    # 版本带时段桶: 结果去重 (screener cache_key) 会随桶滚动, 不会整天复用同一份盘中结果
    bucket = int(time.time() // max(60.0, ttl))
    version = f"{day}+live@{bucket}"
    with _live_lock:
        _live_cache.update({"day": day, "ts": time.time(), "df": df, "version": version})
    log.info("盘中因子表已刷新: %d 只, %.1fs, 版本 %s", len(df), time.time() - t0, version)
    _log_rss("盘中因子结束")
    return df, version


def _quote_is_today(quote, today):
    """快照能当今日 bar: 成交量 > 0; 带时间戳时日期必须是 today。

    与 market._daily_bar_from_quote 同一口径。停牌/上一交易日残留 (volume=0 或
    时间戳不是今天) 不能拼上去, 否则量比、振幅、KDJ 会失真。
    """
    import datetime as dt_mod
    ts = (quote or {}).get("timestamp")
    if ts is not None:
        try:
            ts_date = dt_mod.datetime.fromtimestamp(
                float(ts), dt_mod.timezone(dt_mod.timedelta(hours=8))
            ).date().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            ts_date = None
        if ts_date is not None and ts_date != str(today)[:10]:
            return False
    vol = _f((quote or {}).get("volume"))
    return vol is not None and vol > 0


def _patch_last_bar(bars, today, quote, last):
    """把当日实时价并进最后一根日K (同日替换, 新日追加)。不可用则原样返回。"""
    if not _quote_is_today(quote, today):
        return bars
    df = bars.copy()
    high = _f(quote.get("high")) or last
    low = _f(quote.get("low")) or last
    openp = _f(quote.get("open")) or last
    volume = _f(quote.get("volume"))
    amount = _f(quote.get("amount"))
    last_day = str(df.index[-1])[:10]
    if last_day == today:
        df.iloc[-1, df.columns.get_loc("close")] = last
        df.iloc[-1, df.columns.get_loc("high")] = max(high, last)
        df.iloc[-1, df.columns.get_loc("low")] = min(low, last)
        if volume is not None:
            df.iloc[-1, df.columns.get_loc("volume")] = volume
        if amount is not None and "amount" in df.columns:
            df.iloc[-1, df.columns.get_loc("amount")] = amount
    else:
        row = {c: 0.0 for c in ("open", "high", "low", "close", "volume", "amount")}
        row.update({"open": openp, "high": max(high, last), "low": min(low, last),
                    "close": last, "volume": volume or 0.0,
                    "amount": amount or 0.0})
        df = pd.concat([df, pd.DataFrame([row], index=pd.to_datetime([today]))])
    return df


def _fetch_quotes(symbols, progress_cb=None):
    """批量快照 (live_quote_rate 限速, 不占满实时快照额度) → {symbol: quote}。

    progress_cb(done, total) 给选股任务写进度: 全市场约 79 批、要几分钟,
    不回报的话页面一直显示「扫描中 0/?」。
    """
    import feed as feed_mod
    import market
    af = market.get_af()
    bucket = feed_mod.TokenBucket(rate_per_min=live_quote_rate())
    out = {}
    chunks = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    for i, chunk in enumerate(chunks, 1):
        _acquire_token(bucket)
        try:
            df = af.quotes.get(symbols=list(chunk), to_dataframe=True)
        except Exception as e:
            log.warning("盘中快照拉取失败: %s", e)
            continue
        if df is None or len(df) == 0:
            continue
        for sym, row in df.iterrows():
            try:
                d = row.to_dict()
            except Exception:
                continue
            out[str(d.get("symbol") or sym)] = d
        if progress_cb:
            try:
                progress_cb(i, len(chunks))
            except Exception:
                pass
    return out


def status(last_days=5):
    """给页面: 当前构建状态 + 最近 N 个交易日完成情况。"""
    days = available_days()
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM builds ORDER BY day DESC LIMIT ?",
                            (max(last_days * 3, 12),)).fetchall()
    finally:
        conn.close()
    by_day = {r["day"]: dict(r) for r in rows}

    # 最近 N 个交易日 (按日历), 缺记录的日子标未构建
    import market_hours
    now = _now_dt()
    cal, probe = [], now
    for _ in range(30):
        if len(cal) >= last_days:
            break
        if market_hours.is_trading_day(probe):
            cal.append(probe.date().isoformat())
        probe -= timedelta(days=1)
    history = []
    for d in cal:
        rec = by_day.get(d) or {}
        history.append({
            "day": d, "state": rec.get("state") or ("missing" if d not in days else "done"),
            "n_rows": rec.get("n_rows"), "n_symbols": rec.get("n_symbols"),
            "n_missing": rec.get("n_missing"), "percent": rec.get("percent", 0),
            "error": rec.get("error"), "attempts": rec.get("attempts", 0),
            "done_at": rec.get("done_at"), "sources": rec.get("sources"),
        })

    today = due_day(now) or last_closed_trading_day(now)
    cur = by_day.get(today) or {}
    running = bool(_build_state["running"])
    snap_day = days[0] if days else None
    return {
        "state": "running" if running else (cur.get("state") or "idle"),
        "day": today, "phase": cur.get("phase"), "percent": int(cur.get("percent") or 0),
        "attempts": int(cur.get("attempts") or 0),
        "error": cur.get("error"),
        "n_rows": cur.get("n_rows"),
        "snapshot_day": snap_day,
        "snapshot_rows": int(len(_snap_cache["df"])) if _snap_cache["df"] is not None else None,
        "available_days": days[:last_days],
        "last_days": history,
        "build_at": "%02d:%02d" % build_at(),
        "bars_count": BARS_COUNT,
    }


# ── 调度: 只在交易日 18:00 后构建一次 ──
def should_build(now=None):
    """(是否该构建, 原因)。**只在交易日 18:00 后**构建, 无补建。

    到点后目标日 = 今天; 已成功/已有快照/重试用尽/距上次失败不足 ``RETRY_MIN_SEC``
    都不再跑 (退避对 error 与 running 同样生效)。
    """
    import market_hours
    now = now or _now_dt()
    if not market_hours.is_trading_day(now):
        return False, "non_trading_day"
    day = due_day(now)
    if not day:
        return False, "before_build_at"
    row = get_build(day)
    # 18:00 前强制构建的当日快照会缺龙虎榜/质押, 到点后必须再覆盖, 不能当 already_done
    if row and row["state"] == "done" and not _provisional_needs_rebuild(row, now):
        return False, "already_done"
    if (current_version() or "") >= day and not _provisional_needs_rebuild(row, now):
        # 目标日已有快照 (构建记录被清理/由旧版本产出) → 视为已完成
        return False, "already_done"
    if row and int(row["attempts"] or 0) >= MAX_ATTEMPTS:
        return False, "attempts_exhausted"
    if row and row["state"] in ("running", "error"):
        last_try = row.get("done_at") or row.get("started_at")
        if last_try:
            try:
                waited = (datetime.fromisoformat(_iso())
                          - datetime.fromisoformat(last_try)).total_seconds()
                if waited < RETRY_MIN_SEC:
                    return False, "recent_failure"
            except ValueError:
                pass
    return True, "due"


def _spawn_build(day, force=False):
    """在子进程里跑构建, 返回 Popen。

    全市场因子 + 筹码计算实测约 2 分钟纯 CPU, 放在 Web 进程里会占住 GIL,
    图表/推送/监控在这段时间全部变慢。数据库与快照照旧落盘, 状态接口不变。
    Linux 上 nice +10, 把 CPU 让给 Web 进程和其他服务。
    """
    import subprocess
    import sys
    # day 可能来自 /api/factors/rebuild 的 JSON; 只把正则收成的规范串放进 argv。
    day = _canonical_day(day)
    if not day:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    err = _check_day(day)
    if err:
        raise ValueError(err)
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--build", "--day", day]
    if force:
        cmd.append("--force")
    log.info("因子库构建子进程: %s", " ".join(cmd))

    def _demote():
        try:
            os.nice(10)
        except OSError:
            pass

    kwargs = {"cwd": str(SCRIPT_DIR.parent)}
    if os.name != "nt":
        kwargs["preexec_fn"] = _demote
    # API 两个并发请求可能都通过预检查；检查与 Popen 必须在同一把锁内。
    with _build_children_lock:
        alive = [(p, d) for p, d in _build_children if p.poll() is None]
        _build_children[:] = alive
        if alive:
            raise BuildAlreadyRunning("已有构建子进程在进行中")
        proc = subprocess.Popen(cmd, **kwargs)
        _build_children.append((proc, day))
        return proc


def spawn_manual(day=None, force=False):
    """手动重建: 与定时任务一样走子进程, 不在 Web 进程里算。"""
    day = day or due_day() or last_closed_trading_day()
    return _spawn_build(day, force=force)


def _loop():
    init_store()
    log.info("因子库调度线程已启动 (交易日 %02d:%02d, 间隔 %ds, 失败退避 %d 分钟, 最多 %d 次)",
             build_at()[0], build_at()[1], SCHED_INTERVAL_SEC, RETRY_MIN_SEC // 60, MAX_ATTEMPTS)
    child = None
    while True:
        try:
            if child is not None and child.poll() is None:
                pass                      # 上一轮构建还在跑, 不重复拉起
            else:
                child = None
                now = _now_dt()
                ok, why = should_build(now)
                if ok:
                    day = due_day(now)    # 到点后即今天 (未到点 should_build 已拦下)
                    if _under_pressure():
                        log.warning("因子库跳过本轮: 内存压力")
                    else:
                        log.info("因子库开始构建 (%s, day=%s)", why, day)
                        child = _spawn_build(day)
        except Exception as e:
            log.warning("因子库调度异常: %s", e)
        time.sleep(SCHED_INTERVAL_SEC)


def start_scheduler():
    """启动调度线程 (幂等); app.start_background_jobs 调用。"""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        init_store()
        _thread = threading.Thread(target=_loop, name="factors-scheduler", daemon=True)
        _thread.start()
        return _thread


def main():
    """手动入口: python -u visual/factors.py --build [--force] [--day YYYY-MM-DD]"""
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="每日因子库")
    ap.add_argument("--build", action="store_true", help="立即构建")
    ap.add_argument("--force", action="store_true", help="忽略当日已完成/重试上限")
    ap.add_argument("--day", help="指定交易日 (默认最近已收盘交易日)")
    ap.add_argument("--status", action="store_true", help="只打印当前状态")
    args = ap.parse_args()
    init_store()
    if args.status or not args.build:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return
    print(json.dumps(build(day=args.day, force=args.force), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
