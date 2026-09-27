"""行情接入层: REST 轮询 (Starter 套餐) + 预留 WebSocket 实现位。

监控循环只用按代码查询 quotes.get(symbols=...), 禁用 universes= 池查询,
以免挤占选股的 20/min 额度。

quotes 令牌桶硬上限 6 次/分钟 (额度 60/min 的 10%)。
depth 走 depth_get_batch（底层 depth.get 逐只，本地 30 次/分钟）；
与 AF 原生 depth.batch 分离，Starter 套餐可用。
429 按响应体 retry_after_ms 退避。
"""
from __future__ import annotations

import threading
import time
import logging
from datetime import datetime

import af_intraday


log = logging.getLogger("feed")


QUOTES_BATCH = 50
QUOTES_RATE_PER_MIN = 6          # 硬上限, 给选股留余量
DEPTH_GET_RATE_PER_MIN = 30      # depth.get 模拟 batch：1 只/次，本地令牌桶
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


def _by_market(symbols):
    """按市场 (cn/hk/us) 分组, 保持首次出现的顺序。

    权限与当日熔断都是按市场记的, 而批量接口一次只认一个市场的权限 —— 分时相关的
    批量请求 (日内走势批量、分钟K批量兜底) 都要先分组, 否则一只没权限的港/美股会把
    整批 (含 A股) 一起 403 掉。
    """
    groups = {}
    for s in symbols:
        groups.setdefault(af_intraday.market_of(s), []).append(s)
    return groups


def depth_get_batch(af, symbols, bucket: TokenBucket, *, log_skip: bool = True):
    """用 depth.get 逐只模拟 batch（与 AF 原生 depth.batch 分离）。

    每只 symbol 消耗 bucket 1 令牌；令牌不足时跳过该只，已取得的仍返回。
    返回 {symbol: depth_dict}。
    """
    symbols = list(dict.fromkeys(s for s in symbols if s))
    out = {}
    for sym in symbols:
        if not bucket.try_acquire(1):
            if log_skip:
                log.warning("depth.get 令牌不足, 跳过 %s", sym)
            continue
        try:
            d = af.depth.get(sym)
            if d:
                out[sym] = d
        except Exception as e:
            if _retry_after_ms(e) is not None:
                raise
            log.warning("depth.get 失败 %s: %s", sym, e)
    return out


def depth_batch_native(af, symbols):
    """AF 原生 depth.batch（高套餐）。Starter 会 PermissionError，勿在监控主路径调用。"""
    symbols = list(dict.fromkeys(s for s in symbols if s))
    if not symbols:
        return {}
    out = {}
    for batch in _chunks(symbols, QUOTES_BATCH):
        result = af.depth.batch(batch) or {}
        if isinstance(result, dict):
            out.update(result)
    return out


class RestFeed:
    """AlphaFeed REST 轮询后端。判定逻辑对数据来源无感, 只消费本接口。"""

    backend = "rest"

    def __init__(self, get_af, fallback_quotes=None):
        self._get_af = get_af
        self._fallback_quotes = fallback_quotes
        self._quotes_bucket = TokenBucket(QUOTES_RATE_PER_MIN)
        self._depth_get_bucket = TokenBucket(DEPTH_GET_RATE_PER_MIN)
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
                    timestamp (epoch seconds, 交易所时间), name,
                    change_pct (百分数, 官方小数已在此 ×100)。
        令牌不足时跳过本轮 (返回 {}); 失败回退 fallback_quotes;
        AF 批量成功但个别标的无数据 (典型: 指数) 时, 缺失部分走 fallback
        (market.fetch_quotes 内含麦蕊 指数/ETF/股票 逐类回退), 否则这些标的
        上的预警永远拿不到行情、静默失效。
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
            missing = [s for s in symbols if s not in out]
            if missing:
                out.update(self._fallback(missing))
            self._publish_snapshot(out)
            return out
        except Exception as e:
            wait = _retry_after_ms(e)
            if wait is not None:
                self._set_backoff(wait)
                raise RateLimited(str(e), retry_after_ms=wait) from e
            log.warning(f"quotes 失败, 尝试回退: {e}")
            return self._fallback(symbols)

    @staticmethod
    def _publish_snapshot(out):
        """把 AF 快照写入 market 的盘后长 TTL 缓存 (盘中最后一次轮询即收盘价)。

        仅写快照, 不碰 1.25s 实时缓存; 任何异常都不能拖垮监控轮询。
        """
        if not out:
            return
        try:
            import market
            for sym, q in out.items():
                market.publish_quote_snapshot(sym, q)
        except Exception as e:
            log.warning(f"快照入缓存失败: {e}")

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
                # 回退源 market.fetch_quotes 输出 change_pct 已是百分数, 直接透传
                "change_pct": _to_float(q.get("change_pct")),
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
        """五档盘口 batch 语义；底层 depth_get_batch（depth.get 逐只，30/min）。"""
        symbols = list(dict.fromkeys(s for s in symbols if s))
        if not symbols:
            return {}
        try:
            return depth_get_batch(
                self._get_af(), symbols, self._depth_get_bucket, log_skip=True,
            )
        except Exception as e:
            wait = _retry_after_ms(e)
            if wait is not None:
                self._set_backoff(wait)
                raise RateLimited(str(e), retry_after_ms=wait) from e
            log.warning("depth_get_batch 失败: %s", e)
            return {}

    def seed_intraday(self, symbols):
        """用当日 1m 分时补种序列。返回 {symbol: [{ts, price, volume}, ...]}。

        volume 转为当日累计 (与快照 volume 口径一致)。

        优先日内走势接口, 并**按市场分组**请求: 权限是按市场授权的, 而 intraday_batch
        是一次 HTTP 带多只 (≤100 只时单分片的异常会原样抛出), 混一只没权限的港/美股就
        会让整批 403 —— 所以不能像早先那样"一批被拒就把所有市场都标记成当日不可用"
        (那会把 A股 的日内走势偏好一起关掉)。原接口兜底同样按市场分组: 混合批一旦被
        市场权限拒, 整批都拿不到数据, 分组才能保住有权限的那部分。
        """
        symbols = list(dict.fromkeys(s for s in symbols if s))
        if not symbols:
            return {}
        try:
            af = self._get_af()
        except Exception as e:
            log.warning(f"补种取不到 AF 客户端: {e}")
            return {}
        out = {}
        missing = []
        for group in _by_market(symbols).values():
            if not af_intraday.available(group[0]):
                missing.extend(group)
                continue
            try:
                dfs = af.klines.intraday_batch(group, to_dataframe=True) or {}
            except Exception as e:
                if af_intraday.is_permission_error(e):
                    af_intraday.note_denied(e, group[0])   # 只关这一组所属的市场
                else:
                    log.warning(f"intraday_batch 失败 ({group[0]} 等 {len(group)} 只), "
                                f"回退 klines.batch 1m: {e}")
                missing.extend(group)
                continue
            for sym, df in dfs.items():
                samples = _df_to_samples(df)
                if samples:
                    out[sym] = samples
            missing.extend(s for s in group if s not in out)
        for group in _by_market(missing).values():
            try:
                more = af.klines.batch(
                    group, period="1m", count=240, adjust="none", to_dataframe=True
                ) or {}
            except Exception as e:
                log.warning(f"klines.batch 1m 兜底失败 ({group[0]} 等 {len(group)} 只): {e}")
                continue
            for sym, df in more.items():
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
    # ext.* 列可能被 pandas 展平; 未展平时从 ext dict 取
    raw_ext = data.get("ext")
    ext = raw_ext if isinstance(raw_ext, dict) else {}

    def _ext(key):
        v = data.get(f"ext.{key}")
        return ext.get(key) if v is None else v

    name = _ext("name")
    # ext.change_pct 官方是小数 (0.01 表示 1%); 在唯一出口统一转百分数,
    # 与麦蕊 pc 口径一致, 消费方 (monitor 涨跌幅预警 / 前端) 不再区分数据源。
    change_pct = _to_float(_ext("change_pct"))
    if change_pct is not None:
        change_pct *= 100.0
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
        "change_pct": change_pct,  # 百分数 (官方小数已 ×100)
        "turnover_rate": _to_float(_ext("turnover_rate")),  # 官方是小数
        "vol_ratio": _to_float(_ext("vol_ratio") if _ext("vol_ratio") is not None
                                else _ext("volume_ratio")),
        "amplitude": _to_float(_ext("amplitude")),          # 官方是小数
        "change_amount": _to_float(_ext("change_amount")),
        "type": _ext("type"),
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
