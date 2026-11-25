"""AlphaFeed A-share and ETF snapshots shared by the Visual HTTP and SSE views."""
from __future__ import annotations

from collections import deque
import gzip
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
import zlib
from datetime import datetime, time as wall_time, timedelta, timezone

import af_limits
import market_hours
import perf
from feed import _retry_after_ms, _row_to_quote
from live_budget import PacedBudget
from logger import sanitize_error

log = logging.getLogger("quote_snapshots")
POOLS = ("CN_Stock", "CN_ETF")
POOL_INTERVAL = 60 / af_limits.bucket_rate("quotes_universe")
INTERVAL = max(2 * POOL_INTERVAL, float(os.getenv("QUOTE_SNAPSHOT_INTERVAL_SEC", "3")))
MAX_LIVE_AGE = max(12, 3 * INTERVAL)
CLOSE_CHECKS = (wall_time(15, 1), wall_time(15, 2), wall_time(15, 5))
CLOSE_TIME = wall_time(15)
MORNING_START = wall_time(9, 30)
MORNING_END = wall_time(11, 30)
CLOSE_FIELDS = ("open", "high", "low", "last_price", "prev_close", "volume", "amount")
CST = timezone(timedelta(hours=8))
ARCHIVE = Path(__file__).resolve().parent / ".cache" / "quote_close.json.gz"
_worker = None
_start_lock = threading.Lock()


def current():
    return _worker


def start(api_key):
    global _worker
    if os.getenv("QUOTE_SNAPSHOT_MODE", "universe").lower() != "universe" or not api_key:
        return
    with _start_lock:
        if _worker is not None:
            return
        from alphafeed import AlphaFeed
        client = AlphaFeed(api_key=api_key, timeout=float(os.getenv("QUOTE_SNAPSHOT_TIMEOUT_SEC", "5")), max_retries=0)
        _worker = SnapshotWorker(client)
        threading.Thread(target=_worker.run, name="quote-snapshots", daemon=True).start()


class SnapshotWorker:
    def __init__(self, client, archive=ARCHIVE):
        self.client = client
        self.archive = archive
        self.budget = PacedBudget(af_limits.bucket_rate("quotes_universe"))
        self.pools = {pool: {} for pool in POOLS}
        self.revision = 0
        self.condition = threading.Condition()
        self.backoff = {}
        self.failures = {}
        self.denied = set()
        self.pause_until = 0
        self.close_keys = {}
        self.close_attempt = None
        self.midday_keys = {}
        self.morning_latest = {}
        self.checked_chart_day = None
        self.samples = deque(maxlen=100)
        self.rounds = 0
        self.archive_dirty = False
        self.sse_totals = {}

    def _publish(self, pool, rows):
        with self.condition:
            self.pools = {**self.pools, pool: rows}
            self.revision += 1
            self.condition.notify_all()

    def wait(self, revision, timeout):
        with self.condition:
            self.condition.wait_for(lambda: self.revision != revision, timeout=timeout)

    def lookup(self, symbol, now):
        """Return the newest pool entry and whether a live legacy refresh is needed."""
        pools = self.pools
        quote = next((pools[pool][symbol] for pool in POOLS if symbol in pools[pool]), None)
        if not quote:
            return None, True
        phase = market_hours.session_phase(now)
        day = now.date().isoformat()
        recently_observed = now.replace(tzinfo=CST).timestamp() - quote["snapshot_received_at"] <= MAX_LIVE_AGE
        fresh = quote["quote_trade_date"] == day and recently_observed
        status = quote["quote_status"]
        if phase in market_hours.LIVE_PHASES:
            status = "live" if fresh else "last_quote"
        elif phase == "break":
            exchange_time = datetime.fromtimestamp(quote["timestamp"], CST).time()
            status = ("midday" if quote["quote_trade_date"] == day and MORNING_START <= exchange_time <= MORNING_END
                      else "last_quote")
        elif phase == "closed":
            if quote["quote_trade_date"] != day or status != "close_confirmed":
                status = "close_pending"
        elif status == "close_confirmed":
            target = now.date() - timedelta(days=1) if phase == "pre" else now.date()
            recent = market_hours.last_trading_day(target)
            if recent and quote["quote_trade_date"] != recent.date().isoformat():
                status = "close_pending"
        elif status == "live":
            status = "last_quote"
        return (quote if status == quote["quote_status"] else {**quote, "quote_status": status},
                phase in market_hours.LIVE_PHASES and not fresh)

    def _fetch(self, pool, expected_day, *, require_today=False, after_close=False):
        import market

        if (pool in self.denied or time.monotonic() < max(self.pause_until, self.backoff.get(pool, 0))
                or not self.budget.acquire(timeout=POOL_INTERVAL)):
            return False
        started = time.perf_counter()
        try:
            raw = self.client.quotes.get(universes=pool, to_dataframe=False)
            downloaded = time.perf_counter()
            now = market_hours.now()
            received = now.replace(tzinfo=CST).timestamp()
            status = "live" if market_hours.is_live(now) else "close_pending"
            revision = time.time_ns() // 1000
            rows = self.pools[pool].copy()
            valid = matched = 0
            morning_latest = None
            for record in raw:
                q = _row_to_quote(record)
                if not q or not q.get("symbol") or not market._af_quote_valid(q):
                    continue
                symbol = market.normalize_symbol(q["symbol"])
                timestamp = market._safe_epoch(q["timestamp"])
                if timestamp is None or not math.isfinite(timestamp) or not 0 < timestamp <= received + 60:
                    continue
                exchange_time = datetime.fromtimestamp(timestamp, CST)
                day = exchange_time.date().isoformat()
                if day > expected_day:
                    continue
                valid += 1
                exchange_clock = exchange_time.time()
                if day == expected_day:
                    if after_close and exchange_clock >= CLOSE_TIME:
                        matched += 1
                    if MORNING_START <= exchange_clock <= MORNING_END:
                        morning_latest = max(morning_latest or exchange_time, exchange_time)
                old = rows.get(symbol)
                if old and timestamp < old["timestamp"]:
                    continue
                confirmed = old and timestamp == old["timestamp"] and old["quote_status"] == "close_confirmed"
                if confirmed and not (after_close and day == expected_day):
                    continue
                q["name"] = q.get("name") or record.get("name") or (old or {}).get("name") or symbol
                row = market._af_quote_to_std(q, symbol)
                if confirmed and all(row[key] == old.get(key) for key in CLOSE_FIELDS):
                    rows[symbol] = {**old, "snapshot_received_at": received}
                    continue
                rows[symbol] = {**row, "_revision": revision, "quote_trade_date": day,
                                "snapshot_received_at": received,
                                "quote_status": "close_pending" if after_close else status}
            if not valid:
                raise ValueError("标的池没有有效快照")
            self._publish(pool, rows)
            if (require_today and morning_latest is None) or (after_close and not matched):
                raise ValueError("标的池尚无目标时段快照")
            if morning_latest is not None:
                self.morning_latest[pool] = max(self.morning_latest.get(pool, morning_latest), morning_latest)
            self.failures.pop(pool, None)
            self.backoff.pop(pool, None)
            perf.bump("quote_pool_success")
            if perf.ENABLED:
                log.info("quote_pool pool=%s rows=%d fetch_ms=%.1f total_ms=%.1f", pool, valid,
                         (downloaded - started) * 1000, (time.perf_counter() - started) * 1000)
            return True
        except Exception as exc:
            code = getattr(exc, "status_code", None)
            if code in (401, 403):
                self.denied.add(pool)
            else:
                failures = self.failures[pool] = self.failures.get(pool, 0) + 1
                delay = min(120, INTERVAL * 2 ** min(failures, 6))
                if code == 429:
                    delay = max(delay, (_retry_after_ms(exc) or 0) / 1000)
                    self.pause_until = time.monotonic() + delay
                self.backoff[pool] = time.monotonic() + delay
            perf.bump(f"quote_pool_error_{code or 'other'}")
            log.warning("快照池 %s 获取失败: %s", pool, sanitize_error(exc))
            return False

    def _restore(self):
        try:
            with gzip.open(self.archive, "rt", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("无效的收盘快照归档")
            version = data.get("version")
            if version in (1, 2):
                pools = data["pools"]
                if not isinstance(pools, dict):
                    raise ValueError("无效的快照池")
                restored = {}
                for pool in POOLS:
                    rows = pools.get(pool, {})
                    if not isinstance(rows, dict):
                        raise ValueError("无效的快照池记录")
                    if version == 1:
                        for symbol, quote in rows.items():
                            if (quote["quote_status"] == "close_confirmed"
                                    and datetime.fromtimestamp(quote["timestamp"], CST).time() < CLOSE_TIME):
                                rows[symbol] = {**quote, "quote_status": "close_pending"}
                    restored[pool] = rows
                # V1 advanced its shared marker even when neither fetch succeeded.
                self.close_keys = ({pool: tuple(key) for pool, key in data["close_keys"].items()}
                                   if version == 2 else {})
                for pool, rows in restored.items():
                    self._publish(pool, rows)
        except FileNotFoundError:
            pass
        except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            log.warning("收盘快照归档读取失败: %s", sanitize_error(exc))

    def _save(self):
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.archive.with_suffix(".tmp")
        try:
            with gzip.open(tmp, "wt", compresslevel=3, encoding="utf-8") as handle:
                json.dump({"version": 2, "pools": self.pools, "close_keys": self.close_keys},
                          handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            os.replace(tmp, self.archive)
        finally:
            tmp.unlink(missing_ok=True)

    def _confirm_close(self, day):
        import daily_chart_store

        # The store is published after the close. Require both the quote and its
        # reference close to match so a forward-adjusted price cannot become a raw quote.
        if self.checked_chart_day == day or daily_chart_store.current_as_of() != day:
            return False
        if not any(self.pools.values()):
            return False
        final = daily_chart_store.final_closes(day)
        if not final:
            return False
        updated = False
        for pool in POOLS:
            rows = self.pools[pool].copy()
            pool_updated = False
            for symbol, quote in rows.items():
                prices = final.get(symbol)
                if (prices and quote["quote_trade_date"] == day and quote["quote_status"] != "close_confirmed"
                        and datetime.fromtimestamp(quote["timestamp"], CST).time() >= CLOSE_TIME
                        and quote["prev_close"] and math.isclose(prices[0], quote["last_price"], abs_tol=.0005)
                        and math.isclose(prices[1], quote["prev_close"], abs_tol=.0005)):
                    rows[symbol] = {**quote, "quote_status": "close_confirmed", "_revision": time.time_ns() // 1000}
                    pool_updated = True
            if pool_updated:
                self._publish(pool, rows)
                updated = True
        self.checked_chart_day = day
        return updated

    def _recover_midday(self, day):
        morning_end = datetime.combine(datetime.fromisoformat(day).date(), MORNING_END, CST)
        cutoff = morning_end - timedelta(seconds=MAX_LIVE_AGE)
        for pool in POOLS:
            if self.midday_keys.get(pool) == day:
                continue
            latest = self.morning_latest.get(pool)
            if ((latest is not None and cutoff <= latest <= morning_end)
                    or self._fetch(pool, day, require_today=True)):
                self.midday_keys[pool] = day

    def _refresh_close(self, day, slot):
        close_key = (day, slot)
        if self.close_attempt != close_key:
            # Keep the last available snapshots across restarts, even when a pool fails.
            self.close_attempt = close_key
            self.archive_dirty = self.archive_dirty or any(self.pools.values())
        for pool in POOLS:
            if close_key > self.close_keys.get(pool, ("", 0)) and self._fetch(pool, day, after_close=True):
                self.close_keys[pool] = close_key
                self.checked_chart_day = None
                self.archive_dirty = True
        if self._confirm_close(day):
            self.archive_dirty = True

    def _report_rounds(self):
        import memguard

        values = sorted(self.samples)
        counters = {key: value for key, value in perf.counters().items() if key.startswith("sse_")}
        sent = {key: value - self.sse_totals.get(key, 0) for key, value in counters.items()}
        self.sse_totals = counters
        memory = memguard.sample()
        samples = {symbol: self.pools[pool].get(symbol, {}).get("timestamp")
                   for pool, symbol in zip(POOLS, ("000792.SZ", "510300.SH"))}
        log.info("quote_pool_round p95_ms=%.1f rounds=%d container_bytes=%s rss_peak_kb=%s timestamps=%s sse=%s",
                 values[math.ceil(.95 * len(values)) - 1] * 1000, len(values), memory.get("usage_bytes"),
                 memory.get("hwm_kb"), samples, sent)

    def run(self):
        self._restore()
        while True:
            started = time.monotonic()
            try:
                now = market_hours.now()
                day = now.date().isoformat()
                phase = market_hours.session_phase(now)
                if phase in market_hours.LIVE_PHASES and market_hours.calendar_source() == "xshg":
                    for pool in POOLS:
                        if market_hours.is_live():
                            self._fetch(pool, day)
                    self.samples.append(time.monotonic() - started)
                    self.rounds += 1
                    if perf.ENABLED and self.rounds % 20 == 0:
                        self._report_rounds()
                elif phase == "break" and market_hours.calendar_source() == "xshg":
                    self._recover_midday(day)
                elif phase in ("closed", "pre", "non_trading") and market_hours.calendar_source() == "xshg":
                    # A restart during a weekend or pre-open can recover a missing close.
                    recent = market_hours.last_trading_day(now if phase != "pre" else now.date() - timedelta(days=1))
                    if recent:
                        close_day = recent.date().isoformat()
                        slot = (sum(now.time() >= check for check in CLOSE_CHECKS)
                                if close_day == day else len(CLOSE_CHECKS))
                        if slot:
                            self._refresh_close(close_day, slot)
                        if self.archive_dirty:
                            self._save()
                            self.archive_dirty = False
            except Exception as exc:
                log.warning("快照采集轮次失败: %s", sanitize_error(exc))
            interval = INTERVAL if market_hours.is_live() else 10
            elapsed = time.monotonic() - started
            time.sleep(interval - elapsed % interval)
