"""东方财富筹码分布 (CYQ) 移植。

数据源: 东财日K接口 (klt=101, fqt=1 前复权, 含 f61 换手率)。
算法: 东财官网 CYQCalculator (factor=150 价格分桶 + 按日换手率衰减 + 三角分布),
      一字板按矩形面积=三角形2倍特例处理。

用前复权价使筹码价格轴与图表价格轴对齐; 换手率与复权无关。
仅用于股票; ETF/指数由调用方拦截。

数据源由环境变量 CHIPS_SOURCE 控制:
    af   (默认) AlphaFeed 前复权日K + 当前流通股本回算换手率 (近似, 无需东财);
    em   东财日K (含真实换手率, 精确), 走 http→https→备用主机 回退重试;
    auto 先东财, 失败再 AlphaFeed。
东财在部分网络/代理下不可达 (push2his 的 https 被阻断、直连被墙), 故默认用 af。
可用 CHIPS_EM_URLS 覆盖东财 URL 列表 (逗号分隔)。
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time

import requests

from logger import redact_message

log = logging.getLogger("chips")

FACTOR = 150          # 东财价格分桶数
WINDOW = 210          # 东财滚动窗口 (根)
CACHE_TTL = 300.0     # 成功结果内存缓存 (秒)
CACHE_TTL_FAIL = 30.0 # 失败缓存 (秒, 尽快重试)
FETCH_RETRIES = 3     # 每个 URL 尝试次数
FETCH_TIMEOUT = 5.0   # 单次请求超时 (秒)
FETCH_DEADLINE = 15.0 # 整个取数流程总超时 (秒), 避免 /api/chips 长时间挂起

_FIELDS1 = "f1,f2,f3,f4,f5,f6"
_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

# 明文 http 优先: 部分网络/代理阻断该主机 https。
_DEFAULT_URLS = (
    "http://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "http://push2.eastmoney.com/api/qt/stock/kline/get",
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}

_cache = {}           # symbol -> (ts, data|None)
_lock = threading.Lock()


def _em_urls():
    raw = os.environ.get("CHIPS_EM_URLS", "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return list(_DEFAULT_URLS)


def _market_code(symbol):
    """东财 secid 市场码: 沪市 1, 深市/北交所 0。"""
    return 1 if symbol.rsplit(".", 1)[-1].upper() == "SH" else 0


def _fetch_rows(params):
    """按 URL 回退链 + 重试拉取 klines; 返回 (rows, err)。

    东财/代理在短时间内可能返回 503 空响应, 属瞬时抖动, 用递增退避重试。
    整体受 FETCH_DEADLINE 约束, 避免拖垮 /api/chips 请求。
    """
    last_err = "未知错误"
    deadline = time.time() + FETCH_DEADLINE
    for url in _em_urls():
        for attempt in range(FETCH_RETRIES):
            if time.time() >= deadline:
                return None, f"{last_err} (总超时 {FETCH_DEADLINE:.0f}s)"
            try:
                resp = requests.get(url, params=params, timeout=FETCH_TIMEOUT,
                                    headers=_HEADERS)
                if resp.status_code == 200:
                    rows = ((resp.json() or {}).get("data") or {}).get("klines") or []
                    if rows:
                        return rows, None
                    last_err = "HTTP 200 无 klines"
                else:
                    last_err = f"HTTP {resp.status_code}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {redact_message(str(e))}"
            time.sleep(1.0 * (attempt + 1))
        log.info("筹码数据源不可用 %s (%s), 回退下一个", url, last_err)
    return None, last_err


def fetch_chip_bars(symbol, count=WINDOW):
    """拉取东财前复权日K (含换手率), 返回 bar list 或 None。

    bar: {date, open, close, high, low, volume, amount, hsl}
    """
    code = symbol.split(".")[0]
    params = {
        "secid": f"{_market_code(symbol)}.{code}",
        "fields1": _FIELDS1,
        "fields2": _FIELDS2,
        "klt": "101",
        "fqt": "1",              # 前复权, 与图表价格轴对齐
        "end": "20500101",
        "lmt": str(count),
    }
    rows, err = _fetch_rows(params)
    if rows is None:
        log.warning("获取筹码K线失败 %s: %s", symbol, err)
        return None

    bars = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 11:
            continue
        try:
            hsl = float(parts[10]) if parts[10] not in ("", "-") else 0.0
            bars.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "hsl": hsl,
            })
        except (ValueError, IndexError):
            continue
    return bars or None


def _volume_to_shares_mult(df):
    """判断 AF 日K volume 单位: 100=手(×100 为股), 1=股。

    用 amount/(volume*close) 的中位数: ≈100 → 手, ≈1 → 股。
    """
    ratios = []
    for _, row in df.tail(10).iterrows():
        try:
            v = float(row["volume"])
            a = float(row.get("amount") or 0.0)
            c = float(row["close"])
        except (ValueError, TypeError, KeyError):
            continue
        if v > 0 and a > 0 and c > 0:
            ratios.append(a / (v * c))
    if not ratios:
        return 100  # 无成交额可判: 默认按「手」(AF 股票口径)
    ratios.sort()
    med = ratios[len(ratios) // 2]
    return 100 if med > 10 else 1


def fetch_af_chip_bars(symbol, count=WINDOW):
    """用 AlphaFeed 前复权日K + 当前流通股本回算换手率, 返回 bar list 或 None。

    换手率 = 成交量(股) / 流通股本 × 100 (%)。近似点: 用当前流通股本回算,
    忽略历史股本变化; 价格前复权, 与图表价格轴对齐。
    """
    import market  # 惰性导入, 避免与 api/ 包的导入顺序纠缠
    try:
        df = market._fetch_af_kline(symbol, "1d", count, adjust="forward")
    except Exception as e:
        log.warning("AlphaFeed 取筹码K线失败 %s: %s", symbol, redact_message(str(e)))
        return None
    if df is None or len(df) == 0:
        return None
    meta = market._fetch_instrument_meta(symbol)
    float_shares = (meta or {}).get("float_shares")
    if not float_shares or float_shares <= 0:
        log.info("无流通股本, 跳过筹码 %s", symbol)
        return None

    mult = _volume_to_shares_mult(df)
    bars = []
    for idx, row in df.iterrows():
        try:
            volume = float(row["volume"])
            bars.append({
                "date": str(idx)[:10],
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": volume,
                "amount": float(row.get("amount") or 0.0),
                "hsl": (volume * mult / float_shares) * 100.0,
            })
        except (ValueError, TypeError, KeyError):
            continue
    return bars or None


def compute_chips(bars, factor=FACTOR):
    """移植东财 CYQCalculator, 返回直方图与汇总; 无效返回 None。

    bars 需含 open/close/high/low/hsl (按时间升序)。
    同时返回两种成本口径:
        avgCost    加权平均成本 Σ(价×筹码)/Σ筹码 (语义更准, 对齐东财 App);
        medianCost 50% 分位成本 (对齐东财网页版 CYQ 原生输出)。
    pct90/pct70 为成本区间, 与口径无关。
    """
    if not bars:
        return None
    maxprice = max(b["high"] for b in bars)
    minprice = min(b["low"] for b in bars)
    if not (maxprice > minprice):
        return None

    accuracy = max(0.01, (maxprice - minprice) / (factor - 1))
    yrange = [round(minprice + accuracy * i, 2) for i in range(factor)]
    xdata = [0.0] * factor

    for b in bars:
        o, c, h, l = b["open"], b["close"], b["high"], b["low"]
        avg = (o + c + h + l) / 4.0
        turnover = min(1.0, (b.get("hsl") or 0.0) / 100.0)

        if h == l:
            g0 = float(factor - 1)
            g1 = int((avg - minprice) // accuracy)
        else:
            g0 = 2.0 / (h - l)
            g1 = int((avg - minprice) // accuracy)
        g1 = max(0, min(factor - 1, g1))

        # 衰减
        keep = 1.0 - turnover
        for n in range(factor):
            xdata[n] *= keep

        if h == l:
            xdata[g1] += g0 * turnover / 2.0
        else:
            H = int(math.floor((h - minprice) / accuracy))
            L = int(math.ceil((l - minprice) / accuracy))
            H = max(0, min(factor - 1, H))
            L = max(0, min(factor, L))
            for j in range(L, H + 1):
                curprice = minprice + accuracy * j
                if curprice <= avg:
                    if abs(avg - l) < 1e-8:
                        xdata[j] += g0 * turnover
                    else:
                        xdata[j] += (curprice - l) / (avg - l) * g0 * turnover
                else:
                    if abs(h - avg) < 1e-8:
                        xdata[j] += g0 * turnover
                    else:
                        xdata[j] += (h - curprice) / (h - avg) * g0 * turnover

    total = sum(xdata)
    if total <= 0:
        return None

    def cost_by_chip(chip):
        s = 0.0
        for i in range(factor):
            if s + xdata[i] > chip:
                return minprice + i * accuracy
            s += xdata[i]
        return minprice + (factor - 1) * accuracy

    def percent_chips(pct):
        ps = [(1 - pct) / 2.0, (1 + pct) / 2.0]
        lo = cost_by_chip(total * ps[0])
        hi = cost_by_chip(total * ps[1])
        con = 0.0 if (lo + hi) == 0 else (hi - lo) / (lo + hi)
        return [round(lo, 2), round(hi, 2)], con

    current = bars[-1]["close"]
    below = 0.0
    for i in range(factor):
        if current >= minprice + i * accuracy:
            below += xdata[i]

    p90, c90 = percent_chips(0.9)
    p70, c70 = percent_chips(0.7)

    # 平均成本: 加权平均 (均本) 与 50% 分位中位成本 (中位价)
    weighted_cost = sum(
        (minprice + accuracy * i) * xdata[i] for i in range(factor)
    ) / total
    median_cost = cost_by_chip(total * 0.5)

    return {
        "min": round(minprice, 2),
        "max": round(maxprice, 2),
        "factor": factor,
        "buckets": [
            {"price": yrange[i], "weight": xdata[i] / total}
            for i in range(factor)
        ],
        "avgCost": round(weighted_cost, 2),
        "medianCost": round(median_cost, 2),
        "profitRatio": below / total,
        "pct90": p90,
        "pct70": p70,
        "pct90Con": c90,
        "pct70Con": c70,
        "current": current,
        "count": len(bars),
    }


def _chips_source():
    """当前筹码数据源: af(默认) / em / auto。"""
    return os.environ.get("CHIPS_SOURCE", "af").strip().lower() or "af"


def _load_bars(symbol):
    """按 CHIPS_SOURCE 取 bar; 返回 (bars|None, source)。"""
    mode = _chips_source()
    if mode == "em":
        return fetch_chip_bars(symbol), "em"
    if mode == "auto":
        bars = fetch_chip_bars(symbol)          # 东财精确优先
        if bars:
            return bars, "em"
        return fetch_af_chip_bars(symbol), "af"  # 东财失败再 AF
    return fetch_af_chip_bars(symbol), "af"      # 默认 AF 近似


def get_chips(symbol):
    """取筹码分布 (内存 TTL 缓存); 失败/无数据返回 None。

    成功缓存 CACHE_TTL, 失败仅缓存 CACHE_TTL_FAIL (抖动后尽快重试)。
    结果含 "source": af(AlphaFeed 近似) / em(东财精确)。
    同时返回 avgCost(加权平均) 与 medianCost(50% 分位中位价)。
    """
    now = time.time()
    with _lock:
        ent = _cache.get(symbol)
        if ent:
            ttl = CACHE_TTL if ent[1] else CACHE_TTL_FAIL
            if now - ent[0] < ttl:
                return ent[1]
    bars, source = _load_bars(symbol)
    result = compute_chips(bars) if bars else None
    if result:
        result["source"] = source
    with _lock:
        _cache[symbol] = (time.time(), result)
    return result
