"""Flask 路由: K线/筹码/分时/末根增量 (/api/kline|kline/tail|chips|intraday)。"""
from __future__ import annotations

import logging
import os
import time

from flask import request

import kline_source
import market_hours
import perf
import premium
from api import api_bp
from api.common import _error, _json
from chips import get_chips
from indicators import compute_all_indicators, intraday_avg_price
from logger import sanitize_error as _sanitize_error
from market import (
    MINUTE_COUNTS,
    MINUTE_PERIODS,
    TTLCache,
    _fetch_instrument_meta,
    _is_etf,
    _is_index_symbol,
    _safe_float,
    _safe_int,
    fetch_kline_ex,
    fetch_quote,
    kline_cache,
    kline_cache_long,
    kline_cache_minute,
    normalize_symbol,
)

log = logging.getLogger("api")

# 日K: 3年可见 (3×252) + RSI 收敛 warmup 250
DAILY_COUNT = 3 * 252 + 250  # 1006

# 图表当日 bar 的快照新鲜度: 默认复用 quote_cache (TTL 1.25s), 不再每次强制一次
# 实时行情往返 —— 实测该往返在 0~700ms 抖动, 是磁盘 TTL 抬高后热路径上仅剩的
# 耗时来源。当日 bar 仍**完全由后端快照产出** (契约不变: 前端不派生 OHLCV),
# 只是允许最多旧 1.25s; 前端另有 SSE 报价流 (1.25s) 与 /api/kline/tail (10s,
# 仍走强制新鲜快照) 持续纠正末根 bar。
# 成交校验 (market.get_daily_bar) 不经过这里, 始终强制新鲜快照。
# KLINE_TODAY_BAR_FRESH=1 → 恢复"每次强制拉新快照"的旧行为 (更实时, 但慢)。
TODAY_BAR_QUOTE_FRESH = os.environ.get("KLINE_TODAY_BAR_FRESH", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# 末根增量接口的短 TTL: 把多客户端/10s 轮询合并成受控的上游调用量
KLINE_TAIL_TTL = max(5.0, float(os.environ.get("KLINE_TAIL_TTL", "10")))
_tail_cache = TTLCache(KLINE_TAIL_TTL)


def _attach_quote(resp, symbol):
    """给 kline 响应挂实时快照 (30s 缓存); 失败静默, quote 缺席即可。

    fetch_quote 返回 quote_cache 共享条目, 复制后再挂 is_trading_day, 避免
    污染缓存; 前端据此得知当日 bar 是否可用。
    """
    try:
        q = fetch_quote(symbol)
        if q:
            now = market_hours.now()
            quote = dict(q)
            quote["is_trading_day"] = market_hours.is_trading_day(now)
            resp["quote"] = quote
    except Exception:
        pass


# 单根 bar 的字段与转换器 (顺序即 JSON 字段顺序)。逐行与向量化序列化共用同一张
# 表, 避免两处字段清单漂移 (漏字段会静默让前端某条指标曲线消失)。
_BAR_FIELDS = (
    ("open", _safe_float), ("high", _safe_float), ("low", _safe_float),
    ("close", _safe_float), ("volume", _safe_int), ("amount", _safe_float),
    ("ma5", _safe_float), ("ma10", _safe_float), ("ma20", _safe_float),
    ("macd_dif", _safe_float), ("macd_dea", _safe_float), ("macd_hist", _safe_float),
    ("rsi6", _safe_float), ("rsi12", _safe_float), ("rsi24", _safe_float),
    ("kdj_k", _safe_float), ("kdj_d", _safe_float), ("kdj_j", _safe_float),
    ("atr14", _safe_float), ("ema13", _safe_float), ("impulse", _safe_int),
    ("obv", _safe_float), ("maobv", _safe_float),
    ("vol_ma5", _safe_float), ("vol_ma10", _safe_float), ("vol_ma20", _safe_float),
    ("boll_mid", _safe_float), ("boll_up", _safe_float), ("boll_low", _safe_float),
    ("wr14", _safe_float), ("cci14", _safe_float),
    ("bias6", _safe_float), ("bias12", _safe_float), ("bias24", _safe_float),
    ("dmi_pdi", _safe_float), ("dmi_mdi", _safe_float), ("dmi_adx", _safe_float),
)


def _bar_date(idx, period):
    """单根 bar 的日期文本 (分钟周期精确到分钟, 其余到日)。"""
    if hasattr(idx, "strftime"):
        return idx.strftime(
            "%Y-%m-%d %H:%M" if period in MINUTE_PERIODS else "%Y-%m-%d"
        )
    return str(idx)


def _serialize_bar(idx, row, period):
    """单根 bar → JSON entry (含全部指标)。保留供逐行/单根调用方与测试使用。"""
    out = {"date": _bar_date(idx, period)}
    for name, conv in _BAR_FIELDS:
        out[name] = conv(row.get(name))
    return out


def serialize_bars(df, period):
    """整段 K 线 → JSON entries (含全部指标)。

    替代 `[_serialize_bar(i, r, period) for i, r in df.iterrows()]`: iterrows
    每行构造一个 Series, 实测 1006 根约 86ms, 是指标计算 (约 29ms) 的 3 倍,
    也是 /api/kline 最大的单项 CPU。这里按列 tolist() 一次成型 (numpy 标量 →
    Python 原生类型, 与逐行取值口径一致), 再按行拼 dict。
    """
    n = len(df)
    if n == 0:
        return []
    idx = df.index
    if hasattr(idx, "strftime"):
        dates = idx.strftime(
            "%Y-%m-%d %H:%M" if period in MINUTE_PERIODS else "%Y-%m-%d"
        )
    else:
        dates = [str(i) for i in idx]
    cols = {name: (df[name].tolist() if name in df.columns else None)
            for name, _ in _BAR_FIELDS}
    fields = [(name, conv, cols[name]) for name, conv in _BAR_FIELDS]
    out = []
    for i in range(n):
        bar = {"date": dates[i]}
        for name, conv, vals in fields:
            bar[name] = conv(vals[i]) if vals is not None else None
        out.append(bar)
    return out


def _session_meta(now=None):
    """接口 meta 的统一时段字段 (唯一口径来自 market_hours)。

    与 /api/ping、/api/stream/status 同源 (market_status), 前端据 quote_live /
    next_live_in_sec 判断要不要刷新、何时唤醒 —— 集合竞价期 quote_live 为真,
    in_session 仍为假, 两者不能互相替代。
    """
    st = market_hours.market_status(now)
    return {
        "server_time": st["time"],
        "is_trading_day": st["is_trading_day"],
        "session_phase": st["session_phase"],
        "is_auction": st["is_auction"],
        "quote_live": st["quote_live"],
        "next_live_at": st["next_live_at"],
        "next_live_in_sec": st["next_live_in_sec"],
    }


@api_bp.route("/api/kline", methods=["GET"])
def kline():
    """K线 + 全部指标 (前端主图数据源)。

    观测: KLINE_PERF_LOG=1 时打一条 kline_perf 日志, 分解磁盘命中 / 实际数据源 /
    强制行情往返 / 指标计算 / 序列化耗时, 用于定位切换标的忽快忽慢的瓶颈。
    关闭时直接走 _kline_body(None), 不在热路径上加任何计时。
    """
    if not perf.ENABLED:
        return _kline_body(None)
    timing = {}
    t0 = time.perf_counter()
    try:
        return _kline_body(timing)
    finally:
        perf.add_ms(timing, "total_ms", (time.perf_counter() - t0) * 1000.0)
        hot = {k: v for k, v in perf.counters().items() if v}
        log.info("kline_perf %s counters=%s", perf.fmt(
            timing,
            symbol=request.args.get("symbol"),
            period=request.args.get("period") or "1d",
        ), hot or "-")


def _kline_body(timing):
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")

    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    # 复权: forward(前,默认) / hfq(后) / none(不复权); 分钟周期固定前复权 (源口径)
    adjust = kline_source.normalize_adjust(request.args.get("adjust"))
    if period in MINUTE_PERIODS:
        adjust = kline_source.ADJUST_FORWARD
    # 日K: 3年可见 + RSI 收敛 warmup (DAILY_COUNT 模块常量, tail 接口须同 count)
    if period == "1d":
        default_count = DAILY_COUNT
    else:
        default_count = MINUTE_COUNTS.get(period, 200)
    try:
        count = min(int(request.args.get("count") or str(default_count)), 1500)
    except ValueError:
        count = default_count

    revision = time.time_ns() // 1000
    cache_key = f"{symbol}:{period}:{count}:{kline_source.adjust_tag(adjust)}"
    skip_1d_cache = period == "1d" and market_hours.is_trading_day(
        market_hours.now().date().isoformat()
    )
    if period in MINUTE_PERIODS:
        cache = kline_cache_minute
    elif period in ("1w", "1M"):
        cache = kline_cache_long
    else:
        cache = kline_cache
    cached = None if skip_1d_cache else cache.get(cache_key)
    if cached:
        if timing is not None:
            timing["disk"] = "mem"
        resp = cached.copy()
        resp["meta"] = dict(resp.get("meta") or {})
        resp["meta"].update(cached=True, **_session_meta())
        with perf.Span(timing, "attach_quote_ms"):
            _attach_quote(resp, symbol)
        return _json(resp)

    try:
        df, name, source = fetch_kline_ex(symbol, period, count, adjust=adjust,
                                         timing=timing,
                                         quote_fresh=TODAY_BAR_QUOTE_FRESH)
    except Exception as e:
        log.warning("获取K线失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("获取K线失败，请稍后重试", 500)

    # 请求的复权无数据 (如 AF 不支持后复权) → 回退前复权重取一次, 保住图表
    adjust_fallback = False
    if df is None and adjust != kline_source.ADJUST_FORWARD and period not in MINUTE_PERIODS:
        log.warning("%s %s 无 %s 复权数据, 回退前复权", symbol, period, adjust)
        adjust = kline_source.ADJUST_FORWARD
        adjust_fallback = True
        cache_key = f"{symbol}:{period}:{count}:{kline_source.adjust_tag(adjust)}"
        try:
            df, name, source = fetch_kline_ex(symbol, period, count, adjust=adjust,
                                             timing=timing,
                                             quote_fresh=TODAY_BAR_QUOTE_FRESH)
        except Exception as e:
            log.warning("复权回退获取失败 %s: %s", symbol, _sanitize_error(e))
        if df is None:
            return _error(f"无法获取 {symbol} 的K线数据", 404)

    if df is None:
        return _error(f"无法获取 {symbol} 的K线数据", 404)

    try:
        with perf.Span(timing, "indicators_ms"):
            df, indicators = compute_all_indicators(df, period)
    except Exception as e:
        log.warning("指标计算失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("指标计算失败", 500)

    # 主图已是前复权, impulse 直接用 compute_all_indicators 结果 (不再另拉 qfq)

    is_etf = _is_etf(symbol)
    deferred = []
    # 溢价依赖 akshare 净值 (无缓存无超时), 原内联在此处会拖慢每次 ETF 切换。
    # 改为只预热后台计算, 立即返回; 客户端用 /api/kline/deferred 取回 (见 premium.py)。
    if is_etf and period not in MINUTE_PERIODS:
        premium.request(symbol, period, count, df=df)
        deferred.append("premium")

    with perf.Span(timing, "serialize_ms"):
        klines = serialize_bars(df, period)

    inst_meta = _fetch_instrument_meta(symbol) or {}
    now = market_hours.now()
    resp = {
        "symbol": symbol,
        "name": name,
        "period": period,
        "count": len(klines),
        "_revision": revision,
        "is_etf": is_etf,
        "is_index": _is_index_symbol(symbol),
        "float_shares": inst_meta.get("float_shares"),
        "total_shares": inst_meta.get("total_shares"),
        "instrument_type": inst_meta.get("type"),
        "obv_params": indicators.get("obv", {}).get("params"),
        "macd_params": indicators["macd"]["params"],
        "boll_params": indicators.get("boll", {}).get("params"),
        "klines": klines,
        "meta": {
            "cached": False,
            **_session_meta(now),
            "last_trade_date": klines[-1]["date"] if klines else None,
            "source": source,
            "adjust": adjust,
            "adjust_fallback": adjust_fallback,
            # 慢派生字段清单: 客户端据此拉 /api/kline/deferred 补齐 (现仅 ETF 溢价)
            "deferred": deferred,
        },
    }

    # 缓存里不存 quote: TTLCache 存引用, 事后挂 quote 会污染缓存条目,
    # 让后续命中拿到最长 TTL 前的旧快照。改为存副本 (不含 quote), 每次返回前现挂。
    _attach_quote(resp, symbol)
    if not skip_1d_cache:
        cache.set(cache_key, {k: v for k, v in resp.items() if k != "quote"})

    return _json(resp)


@api_bp.route("/api/kline/tail", methods=["GET"])
def kline_tail():
    """日/周/月K 末 N 根 (含全部指标), 供图表增量刷新。

    与 /api/kline 同口径 (同一 fetch_kline_ex + compute_all_indicators), count
    必须与图表一致 —— 否则 OBV 等全序列指标会漂移。非交易日/盘前时服务端本就不
    拼当日 bar, 故返回的末根仍是最近交易日, 前端按 date 合并不会凭空多一根。
    """
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    if period in MINUTE_PERIODS or period not in ("1d", "1w", "1M"):
        return _error("tail 仅支持 1d/1w/1M", 400)
    adjust = kline_source.normalize_adjust(request.args.get("adjust"))
    default_count = DAILY_COUNT if period == "1d" else MINUTE_COUNTS.get(period, 300)
    try:
        count = min(int(request.args.get("count") or str(default_count)), 1500)
    except ValueError:
        count = default_count
    try:
        n = max(1, min(int(request.args.get("n") or "1"), 10))
    except ValueError:
        n = 1

    try:
        return _json(build_kline_tail(symbol, period, count, adjust, n))
    except LookupError:
        # LookupError 的文本可能包含上游响应或内部路径，不应直接返回给客户端。
        return _error("无法获取K线数据", 404)
    except Exception:
        return _error("指标更新失败", 500)


def build_kline_tail(symbol, period, count, adjust, n=2):
    cache_key = f"{symbol}:{period}:{count}:{kline_source.adjust_tag(adjust)}:{n}"
    cached = _tail_cache.get(cache_key)
    if cached is not None:
        return cached

    revision = time.time_ns() // 1000
    try:
        df, name, source = fetch_kline_ex(symbol, period, count, adjust=adjust)
    except Exception as e:
        log.warning("获取K线失败(tail) %s %s: %s", symbol, period, _sanitize_error(e))
        raise RuntimeError("获取K线失败") from e
    if df is None:
        raise LookupError(f"无法获取 {symbol} 的K线数据")
    try:
        df, _ind = compute_all_indicators(df, period)
    except Exception as e:
        log.warning("指标计算失败(tail) %s %s: %s", symbol, period, _sanitize_error(e))
        raise RuntimeError("指标计算失败") from e

    bars = serialize_bars(df.tail(n), period)
    now = market_hours.now()
    resp = {
        "symbol": symbol,
        "_revision": revision,
        "name": name or symbol,
        "period": period,
        "count": len(bars),
        "bars": bars,
        "meta": {
            **_session_meta(now),
            "last_trade_date": bars[-1]["date"] if bars else None,
            "source": source,
            "adjust": adjust,
        },
    }
    _tail_cache.set(cache_key, resp)
    return resp


@api_bp.route("/api/kline/deferred", methods=["GET"])
def kline_deferred():
    """慢派生字段的补齐接口 (现仅 ETF 溢价 premium)。

    溢价走 akshare 净值 (无缓存无超时), 若内联在 /api/kline 会拖慢每次 ETF 切换,
    故 /api/kline 只预热并在 meta.deferred 里声明; 客户端随后拉这里补齐。

    未就绪返回 200 + ready=false (不是错误): 前端退避重试, 不弹错误提示。
    """
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    if period in MINUTE_PERIODS or period not in ("1d", "1w", "1M"):
        return _error("deferred 仅支持 1d/1w/1M", 400)
    fields = [f.strip() for f in (request.args.get("fields") or "premium").split(",")]
    if "premium" not in fields:
        return _error("不支持的 fields", 400)
    try:
        count = max(1, min(int(request.args.get("count") or str(DAILY_COUNT)), 1500))
    except ValueError:
        count = DAILY_COUNT
    return _json({"symbol": symbol, "period": period,
                  "premium": premium.get(symbol, period, count)})


@api_bp.route("/api/chips", methods=["GET"])
def chips():
    """筹码分布 (股票/ETF); 指数或无数据返回 chips=null。

    period 1d/1w/1M 只决定日线回看窗口 (210/600/1500 根), 算法与粒度不变。
    指数无份额与换手率, 筹码无意义, 故仍拦截。
    """
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "1d"
    if period not in ("1d", "1w", "1M"):
        period = "1d"
    if _is_index_symbol(symbol):
        return _json({"symbol": symbol, "chips": None})
    try:
        data = get_chips(symbol, period)
    except Exception as e:
        log.warning("获取筹码分布异常 %s: %s", symbol, _sanitize_error(e))
        return _error("获取筹码分布失败", 500)
    return _json({"symbol": symbol, "chips": data})


@api_bp.route("/api/intraday", methods=["GET"])
def intraday():
    symbol_raw = request.args.get("symbol")
    if not symbol_raw:
        return _error("缺少 symbol 参数")
    symbol = normalize_symbol(symbol_raw)
    period = request.args.get("period") or "5m"
    try:
        count = min(int(request.args.get("count") or "120"), 250)
    except ValueError:
        count = 120
    try:
        # 分时走 "intraday" 类别: 优先 AlphaFeed 日内走势接口 (只回当日, 权限不可用
        # 当日退回分钟K批量), 失败继续沿链回退; df 已标准化前复权。
        # 注意不能挂到 "minute" 类别 —— 那是跨天分钟K历史视图 (1m 要 1200 根),
        # 换成只回当日的接口会把 1m/5m/15m/30m/60m 视图打坏。
        df, _src = kline_source.fetch_kline_df(
            "intraday", symbol, period, count, adjust="forward"
        )
        if df is None or len(df) == 0:
            return _error(f"无法获取 {symbol} 的分钟线", 404)
        last_day = df.index.normalize().max()
        mask = df.index.normalize() == last_day
        df = df[mask]
        if len(df) == 0:
            return _error("无分时数据", 404)
        df, indicators = compute_all_indicators(df, period="1d", with_atr_val=True)
        # 分时均线 (均价): 当日累计成交额/累计成交量, 口径同东财分时那条黄线。
        # 必须用「截当日之后」的 df 累计 —— 从前一日接着累加不是分时均价。
        avgs = intraday_avg_price(
            df["close"], df["volume"],
            df["amount"] if "amount" in df.columns else None,
        )
        bars = []
        for i, (idx, row) in enumerate(df.iterrows()):
            ts = idx.strftime("%H:%M") if hasattr(idx, "strftime") else str(idx)[-8:-3]
            bars.append({
                "time": ts,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "volume": _safe_int(row.get("volume")),
                "amount": _safe_float(row.get("amount")),
                "avg_price": avgs[i],
                "macd_dif": indicators["macd"]["dif"][i],
                "macd_dea": indicators["macd"]["dea"][i],
                "macd_hist": indicators["macd"]["hist"][i],
                "kdj_k": indicators["kdj"]["k"][i] if "kdj" in indicators else None,
                "kdj_d": indicators["kdj"]["d"][i] if "kdj" in indicators else None,
                "kdj_j": indicators["kdj"]["j"][i] if "kdj" in indicators else None,
                "rsi6": indicators["rsi"]["rsi6"][i] if "rsi" in indicators else None,
                "rsi12": indicators["rsi"]["rsi12"][i] if "rsi" in indicators else None,
                "rsi24": indicators["rsi"]["rsi24"][i] if "rsi" in indicators else None,
                "atr14": indicators["atr"]["values"][i] if "atr" in indicators else None,
                # 成交量面板的提示框与日K 同口径 (成交量 + VOL MA + 成交额), 指标已在
                # compute_all_indicators 里算好, 这里只是补进 bar。
                "vol_ma5": _safe_float(row.get("vol_ma5")),
                "vol_ma10": _safe_float(row.get("vol_ma10")),
                "vol_ma20": _safe_float(row.get("vol_ma20")),
            })
        return _json({
            "symbol": symbol,
            "period": period,
            # 当日日期: bar 只有 HH:MM, 而图表左上角标题要「分时 · 2026-09-18」这种口径;
            # 不能让客户端用本地时钟推"今天" —— 周末/节假日打开时会说错日子。
            "date": last_day.strftime("%Y-%m-%d"),
            "bars": bars,
        })
    except Exception as e:
        log.warning("获取分钟线失败 %s %s: %s", symbol, period, _sanitize_error(e))
        return _error("获取分钟线失败，请稍后重试", 500)
