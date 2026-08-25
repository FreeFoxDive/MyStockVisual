"""行情接入层: REST 轮询 (Starter 套餐) + 预留 WebSocket 实现位。

监控循环只用按代码查询 quotes.get(symbols=...), 禁用 universes= 池查询,
以免挤占选股的 20/min 额度。

quotes 令牌桶硬上限 6 次/分钟 (额度 60/min 的 10%)。
instruments.batch 每日一次; depth.batch 由调用方按需触发。
429 按响应体 retry_after_ms 退避。
"""
from __future__ import annotations

import threading
import time
import logging
from datetime import datetime


log = logging.getLogger("feed")


QUOTES_BATCH = 50
QUOTES_RATE_PER_MIN = 6          # 硬上限, 给选股留余量
DEPTH_NEAR_LIMIT_PCT = 0.015     # 距涨跌停 1.5% 才拉盘口
DEPTH_NEAR_STOP_PCT = 0.01       # 距止损 1% 才拉盘口


class RateLimited(Exception):
    """上游 429。retry_after_ms 供调用方睡眠。"""

    def __init__(self, message, retry_after_ms=None):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class TokenBucket:
    def __init__(self, rate_per_min=QUOTES_RATE_PER_MIN):
        self.capacity = float(rate_per_min)
        self.tokens = float(rate_per_min)
        self.rate = rate_per_min / 60.0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def try_acquire(self, n=1) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


def _retry_after_ms(exc):
    """从 SDK / HTTP 异常里抠 retry_after_ms。"""
    for attr in ("retry_after_ms", "retry_after"):
        val = getattr(exc, attr, None)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = getattr(resp, "json", None)
        if callable(body):
            try:
                data = body()
                if isinstance(data, dict) and data.get("retry_after_ms") is not None:
                    return int(data["retry_after_ms"])
            except Exception:
                pass
    msg = str(exc)
    if "429" in msg or "rate" in msg.lower() or "限流" in msg:
        return 60_000
    return None


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class RestFeed:
    """AlphaFeed REST 轮询后端。判定逻辑对数据来源无感, 只消费本接口。"""

    backend = "rest"

    def __init__(self, get_af, fallback_quotes=None):
        self._get_af = get_af
        self._fallback_quotes = fallback_quotes
        self._quotes_bucket = TokenBucket(QUOTES_RATE_PER_MIN)
        self._depth_bucket = TokenBucket(QUOTES_RATE_PER_MIN)
        self._limit_cache = {}          # {day: {symbol: {limit_up, limit_down, name}}}
        self._limit_lock = threading.Lock()
        self.backoff_until = 0.0        # monotonic deadline

    def poll_interval(self, n_symbols: int) -> float:
        return 30.0 if n_symbols > 100 else 20.0

    def in_backoff(self) -> bool:
        return time.monotonic() < self.backoff_until

    def _set_backoff(self, retry_after_ms):
        sec = max(1.0, (retry_after_ms or 60_000) / 1000.0)
        self.backoff_until = time.monotonic() + sec
        log.warning(f"429 退避 {sec:.0f}s")

    def quotes(self, symbols):
        """按代码拉快照, 返回 {symbol: quote_dict}。

        quote_dict: last_price, prev_close, open, high, low, volume, amount,
                    timestamp (epoch seconds, 交易所时间), name, change_pct (小数)。
        令牌不足时跳过本轮 (返回 {}); 失败回退 fallback_quotes。
        """
        symbols = list(dict.fromkeys(s for s in symbols if s))
        if not symbols:
            return {}
        if self.in_backoff():
            return {}
        n_req = (len(symbols) + QUOTES_BATCH - 1) // QUOTES_BATCH
        if not self._quotes_bucket.try_acquire(n_req):
            log.warning("quotes 令牌不足, 跳过本轮")
            return {}
        try:
            af = self._get_af()
            out = {}
            for batch in _chunks(symbols, QUOTES_BATCH):
                df = af.quotes.get(symbols=batch, to_dataframe=True)
                if df is None or getattr(df, "empty", True):
                    continue
                for _, row in df.iterrows():
                    q = _row_to_quote(row)
                    if q and q.get("symbol"):
                        out[q["symbol"]] = q
            return out
        except Exception as e:
            wait = _retry_after_ms(e)
            if wait is not None:
                self._set_backoff(wait)
                raise RateLimited(str(e), retry_after_ms=wait) from e
            log.warning(f"quotes 失败, 尝试回退: {e}")
            return self._fallback(symbols)

    def _fallback(self, symbols):
        if not self._fallback_quotes:
            return {}
        try:
            raw = self._fallback_quotes(symbols, fresh=True)
        except TypeError:
            try:
                raw = self._fallback_quotes(symbols)
            except Exception as e:
                log.warning(f"回退快照失败: {e}")
                return {}
        except Exception as e:
            log.warning(f"回退快照失败: {e}")
            return {}
        now_ts = time.time()
        out = {}
        for sym, q in (raw or {}).items():
            if not q:
                continue
            out[sym] = {
                "symbol": sym,
                "last_price": q.get("last_price"),
                "prev_close": q.get("prev_close"),
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "volume": q.get("volume") or 0,
                "amount": q.get("amount"),
                "timestamp": now_ts,
                "name": q.get("name"),
                "change_pct": None,  # 麦蕊是百分数, 不用, 避免差 100 倍
            }
        return out

    def instruments(self, symbols):
        """当日涨跌停价, 进程内按交易日缓存。返回 {symbol: {limit_up, limit_down, name}}。"""
        symbols = list(dict.fromkeys(s for s in symbols if s))
        import market_hours
        day = market_hours.now().strftime("%Y-%m-%d")
        with self._limit_lock:
            cache = self._limit_cache.get(day, {})
            missing = [s for s in symbols if s not in cache]
            if not missing:
                return {s: cache[s] for s in symbols if s in cache}
        try:
            af = self._get_af()
            insts = af.instruments.batch(missing) or []
        except Exception as e:
            log.warning(f"instruments.batch 失败: {e}")
            insts = []
        fresh = {}
        for item in insts:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol")
            ext = item.get("ext") or {}
            if not sym:
                continue
            fresh[sym] = {
                "limit_up": _to_float(ext.get("limit_up")),
                "limit_down": _to_float(ext.get("limit_down")),
                "name": ext.get("name") or item.get("name"),
            }
        with self._limit_lock:
            if day not in self._limit_cache:
                self._limit_cache.clear()
                self._limit_cache[day] = {}
            self._limit_cache[day].update(fresh)
            cache = self._limit_cache[day]
            return {s: cache[s] for s in symbols if s in cache}

    def depth(self, symbols):
        """五档盘口。返回 {symbol: depth_dict}。令牌不足返回 {}。"""
        symbols = list(dict.fromkeys(s for s in symbols if s))
        if not symbols:
            return {}
        n_req = (len(symbols) + QUOTES_BATCH - 1) // QUOTES_BATCH
        if not self._depth_bucket.try_acquire(n_req):
            log.warning("depth 令牌不足, 跳过")
            return {}
        try:
            af = self._get_af()
            out = {}
            for batch in _chunks(symbols, QUOTES_BATCH):
                result = af.depth.batch(batch) or {}
                if isinstance(result, dict):
                    out.update(result)
            return out
        except Exception as e:
            wait = _retry_after_ms(e)
            if wait is not None:
                self._set_backoff(wait)
                raise RateLimited(str(e), retry_after_ms=wait) from e
            log.warning(f"depth.batch 失败: {e}")
            return {}

    def seed_intraday(self, symbols):
        """用当日 1m 分时补种序列。返回 {symbol: [{ts, price, volume}, ...]}。

        volume 转为当日累计 (与快照 volume 口径一致)。
        """
        symbols = list(dict.fromkeys(s for s in symbols if s))
        if not symbols:
            return {}
        try:
            af = self._get_af()
            dfs = af.klines.intraday_batch(symbols, to_dataframe=True) or {}
        except Exception as e:
            log.warning(f"intraday_batch 失败, 回退 klines.batch 1m: {e}")
            try:
                af = self._get_af()
                dfs = af.klines.batch(
                    symbols, period="1m", count=240, adjust="none", to_dataframe=True
                ) or {}
            except Exception as e2:
                log.warning(f"klines.batch 1m 失败: {e2}")
                return {}
        out = {}
        for sym, df in dfs.items():
            samples = _df_to_samples(df)
            if samples:
                out[sym] = samples
        return out


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def _row_to_quote(row):
    """把 quotes.get DataFrame 的一行转成标准 dict。timestamp 统一为 epoch 秒。"""
    try:
        data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    except Exception:
        return None
    # ext.* 列可能被 pandas 展平
    name = data.get("ext.name") or (data.get("ext") or {}).get("name") if isinstance(data.get("ext"), dict) else data.get("ext.name")
    change_pct = data.get("ext.change_pct")
    if change_pct is None and isinstance(data.get("ext"), dict):
        change_pct = data["ext"].get("change_pct")
    ts = data.get("timestamp")
    ts_sec = None
    if ts is not None:
        try:
            ts = float(ts)
            ts_sec = ts / 1000.0 if ts > 1e12 else ts
        except (TypeError, ValueError):
            ts_sec = None
    return {
        "symbol": data.get("symbol"),
        "last_price": _to_float(data.get("last_price")),
        "prev_close": _to_float(data.get("prev_close")),
        "open": _to_float(data.get("open")),
        "high": _to_float(data.get("high")),
        "low": _to_float(data.get("low")),
        "volume": _to_int(data.get("volume")),
        "amount": _to_float(data.get("amount")),
        "timestamp": ts_sec,
        "name": name,
        "change_pct": _to_float(change_pct),  # 官方是小数
    }


def _df_to_samples(df):
    if df is None or getattr(df, "empty", True):
        return []
    time_col = "trade_time" if "trade_time" in df.columns else (
        "trade_date" if "trade_date" in df.columns else None
    )
    samples = []
    cum_vol = 0
    for _, row in df.iterrows():
        price = _to_float(row.get("close") if "close" in df.columns else row.get("last_price"))
        if price is None or price <= 0:
            continue
        vol = _to_int(row.get("volume"))
        # 分时 volume 通常是当根成交量; 快照是累计。统一累加。
        cum_vol += vol
        ts = None
        if time_col:
            raw = row.get(time_col)
            ts = _parse_ts(raw)
        if ts is None:
            continue
        samples.append({"ts": ts, "price": price, "volume": cum_vol})
    return samples


def _parse_ts(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v / 1000.0 if v > 1e12 else v
    try:
        import pandas as pd
        from datetime import timezone, timedelta
        t = pd.to_datetime(raw, errors="coerce")
        if t is None or (hasattr(t, "value") and t.value != t.value):  # NaT
            return None
        # 交易所时间字符串 (如 trade_time) 是北京时间; naive 的 Timestamp.timestamp()
        # 按 UTC 解释会差 8 小时, 这里显式按 +8 定本地时区再取 epoch。
        if t.tz is None:
            t = t.tz_localize(timezone(timedelta(hours=8)))
        return float(t.timestamp())
    except Exception:
        return None


def needs_depth(price, stop_loss=None, limit_up=None, limit_down=None):
    """是否值得拉盘口: 距涨跌停 ≤1.5% 或距止损 ≤1%。"""
    if price is None or price <= 0:
        return False
    if stop_loss is not None and stop_loss > 0:
        if abs(price - stop_loss) / price <= DEPTH_NEAR_STOP_PCT:
            return True
        if price <= stop_loss:
            return True
    if limit_up is not None and limit_up > 0:
        if (limit_up - price) / limit_up <= DEPTH_NEAR_LIMIT_PCT:
            return True
    if limit_down is not None and limit_down > 0:
        if (price - limit_down) / limit_down <= DEPTH_NEAR_LIMIT_PCT:
            return True
    return False
