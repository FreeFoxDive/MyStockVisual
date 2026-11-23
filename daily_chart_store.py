"""visual 自管的前复权日K列式仓（主页图表预热）。

只读写 ``visual/.cache/daily_chart/``。不读取、不写入生产
``out_files/daily_qfq``。

布局与根目录日K仓相同（``symbols.npy`` + ``dates.npy`` + OHLCVA float32），
但发布方式是「新世代目录 + ``current.json`` 原子切换」：读请求持有旧
mmap，构建写另一目录，换指针后下一次请求才打开新文件。Windows 上不能
在文件仍被映射时重命名正在读的目录。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("daily_chart")

STORE_VERSION = 1
FIELDS = ("open", "high", "low", "close", "volume", "amount")
MAX_FULL_PULLS = 2
_DEFAULT_COUNT = 1006
_DEFAULT_BUILD_AT = (16, 5)
_DEFAULT_DRIFT = 0.0015

SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_ROOT = SCRIPT_DIR / ".cache" / "daily_chart"

ROOT = _DEFAULT_ROOT
CURRENT_NAME = "current.json"
META_NAME = "meta.json"

_io_lock = threading.Lock()
_reader_lock = threading.Lock()
_reader: "ChartStore | None" = None
_reader_gen: str | None = None

_align_lock = threading.Lock()
_dirty: set[str] = set()
_fail_counts: dict[str, int] = {}
_cooled: set[str] = set()
_cooled_day: str | None = None
_align_thread: threading.Thread | None = None
_align_started = False
_drift_warned = False
_required_memo: tuple[str, str] | None = None


def chart_count() -> int:
    raw = os.environ.get("DAILY_CHART_COUNT", str(_DEFAULT_COUNT)).strip()
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_COUNT


def build_at() -> tuple[int, int]:
    raw = os.environ.get("DAILY_CHART_BUILD_AT", "16:05").strip()
    try:
        hour, minute = raw.split(":")
        hour, minute = int(hour), int(minute)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    return _DEFAULT_BUILD_AT


def drift_eps() -> float:
    raw = os.environ.get("DAILY_CHART_DRIFT_EPS", str(_DEFAULT_DRIFT)).strip()
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return _DEFAULT_DRIFT


def relocate(root: Path | None) -> None:
    """测试把仓指到临时目录。``None`` 恢复默认路径。"""
    global ROOT, _reader, _reader_gen
    ROOT = Path(root) if root is not None else _DEFAULT_ROOT
    with _reader_lock:
        _reader = None
        _reader_gen = None


def _current_path() -> Path:
    return ROOT / CURRENT_NAME


def _meta_path() -> Path:
    return ROOT / META_NAME


def _empty_meta() -> dict:
    return {
        "building": False,
        "started_at": None,
        "finished_at": None,
        "elapsed_sec": None,
        "error": None,
        "progress": {"done": 0, "total": 0},
        "full_pulls_day": None,
        "full_pulls_today": 0,
        "as_of": None,
        "symbol_count": 0,
        "index_count": 0,
        "index_error": None,
        "bytes": 0,
    }


def load_meta() -> dict:
    meta = _empty_meta()
    path = _meta_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return meta
    if isinstance(raw, dict):
        meta.update(raw)
    return meta


def save_meta(meta: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(meta, ensure_ascii=False, indent=2)
    tmp = ROOT / f".meta-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, _meta_path())


def update_meta(**fields) -> dict:
    with _io_lock:
        meta = load_meta()
        meta.update(fields)
        save_meta(meta)
        return meta


def _read_pointer() -> dict | None:
    try:
        with open(_current_path(), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("gen"):
        return None
    return raw


def current_as_of() -> str | None:
    pointer = _read_pointer()
    if pointer and pointer.get("as_of"):
        return str(pointer["as_of"])
    store = open_store()
    if store is None:
        return None
    return str(store.manifest.get("as_of") or "") or None


def _today() -> str:
    import market_hours
    return market_hours.now().date().isoformat()


def pulls_today(day: str) -> int:
    meta = load_meta()
    if meta.get("full_pulls_day") != day:
        return 0
    try:
        return int(meta.get("full_pulls_today") or 0)
    except (TypeError, ValueError):
        return 0


def building_active(max_age_sec: float = 1800) -> bool:
    meta = load_meta()
    if not meta.get("building"):
        return False
    started = meta.get("started_at")
    if not started:
        return True
    try:
        age = time.time() - datetime.fromisoformat(started).timestamp()
    except (TypeError, ValueError):
        return True
    if age > max_age_sec:
        return False
    return True


def full_pull_block_reason(day: str, *, force: bool = False) -> str | None:
    """自动/手动全量是否该停。封顶对 force 同样生效。"""
    if pulls_today(day) >= MAX_FULL_PULLS:
        return "今日全量已达 2 次"
    if building_active():
        return "已有全量构建在进行"
    if not force:
        as_of = current_as_of() or ""
        if as_of >= day:
            return "已是最新"
    return None


def failed_today(day: str) -> int:
    meta = load_meta()
    if meta.get("failed_pulls_day") != day:
        return 0
    try:
        return int(meta.get("failed_pulls_today") or 0)
    except (TypeError, ValueError):
        return 0


def note_failed_pull(day: str) -> None:
    with _io_lock:
        meta = load_meta()
        if meta.get("failed_pulls_day") != day:
            meta["failed_pulls_day"] = day
            meta["failed_pulls_today"] = 0
        meta["failed_pulls_today"] = int(meta.get("failed_pulls_today") or 0) + 1
        save_meta(meta)


def note_full_pull(day: str) -> None:
    with _io_lock:
        meta = load_meta()
        if meta.get("full_pulls_day") != day:
            meta["full_pulls_day"] = day
            meta["full_pulls_today"] = 0
        meta["full_pulls_today"] = int(meta.get("full_pulls_today") or 0) + 1
        save_meta(meta)


def normalize_daily(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None or len(frame) == 0:
        return None
    data = frame.copy()
    if "trade_date" in data.columns:
        data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce")
        data = data.dropna(subset=["trade_date"]).set_index("trade_date")
    elif not isinstance(data.index, pd.DatetimeIndex):
        data.index = pd.to_datetime(data.index, errors="coerce")
    data = data[~data.index.isna()].sort_index(kind="stable")
    data.index = pd.DatetimeIndex(data.index).normalize()
    for name in FIELDS:
        if name not in data.columns:
            data[name] = np.nan if name in ("open", "high", "low", "close") else 0.0
        data[name] = pd.to_numeric(data[name], errors="coerce")
    data = data.dropna(subset=["close"])
    data = data[~data.index.duplicated(keep="last")]
    if data.empty:
        return None
    return data[list(FIELDS)]


@dataclass
class ChartStore:
    path: str
    symbols: np.ndarray
    dates: np.ndarray
    arrays: dict[str, np.ndarray]
    manifest: dict
    index: dict[str, int] = field(default_factory=dict)

    def frame(self, symbol: str) -> pd.DataFrame | None:
        col = self.index.get(symbol)
        if col is None:
            return None
        patched = _load_patch(Path(self.path), symbol)
        if patched is not None:
            return patched
        days = np.asarray(self.dates[:, col])
        return _to_frame(days, {name: self.arrays[name][:, col] for name in FIELDS})


def _to_frame(days: np.ndarray, columns: dict) -> pd.DataFrame | None:
    valid = days > 0
    if not valid.any():
        return None
    epoch = pd.Timestamp("1970-01-01")
    index = epoch + pd.to_timedelta(days[valid], unit="D")
    data = {
        name: np.array(columns[name])[valid].astype(np.float64, copy=False)
        for name in FIELDS
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(index))


def _patch_file(gen_dir: Path, symbol: str) -> Path:
    return gen_dir / "patch" / f"{symbol}.npz"


def _load_patch(gen_dir: Path, symbol: str) -> pd.DataFrame | None:
    path = _patch_file(gen_dir, symbol)
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            days = np.asarray(data["dates"])
            columns = {name: np.asarray(data[name]) for name in FIELDS}
    except (OSError, KeyError, ValueError) as exc:
        log.warning("图表日K补丁读取失败 %s: %s", symbol, exc)
        return None
    return _to_frame(days, columns)


def _load_gen(gen_dir: Path, mmap_mode: str | None) -> ChartStore:
    with open(gen_dir / "manifest.json", "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("version") != STORE_VERSION:
        raise ValueError(f"不支持的图表日K版本: {manifest.get('version')}")
    if manifest.get("adjust") != "forward":
        raise ValueError("图表日K仓不是前复权")
    symbols = np.load(gen_dir / "symbols.npy", allow_pickle=False)
    dates = np.load(gen_dir / "dates.npy", mmap_mode=mmap_mode, allow_pickle=False)
    arrays = {
        name: np.load(gen_dir / f"{name}.npy", mmap_mode=mmap_mode, allow_pickle=False)
        for name in FIELDS
    }
    shape = (int(manifest["count"]), len(symbols))
    if dates.shape != shape or any(values.shape != shape for values in arrays.values()):
        raise ValueError("图表日K数组形状与 manifest 不一致")
    index = {str(symbol): idx for idx, symbol in enumerate(symbols.tolist())}
    return ChartStore(str(gen_dir), symbols, dates, arrays, manifest, index)


def _close_store(store: ChartStore | None) -> None:
    """关掉 mmap，否则 Windows 上删不掉上一世代目录。"""
    if store is None:
        return
    for values in list(store.arrays.values()) + [store.dates]:
        mapped = getattr(values, "_mmap", None)
        if mapped is not None:
            try:
                mapped.close()
            except Exception:
                pass


def _release_reader() -> str | None:
    """放下当前映射并返回其目录，便于调用方在句柄关闭后再删。"""
    global _reader, _reader_gen
    with _reader_lock:
        old = _reader
        _reader = None
        _reader_gen = None
    path = old.path if old is not None else None
    _close_store(old)
    del old
    import gc
    gc.collect()
    return path


def _drop_path(path: str | None, keep: str | None) -> None:
    if not path or (keep and Path(path).name == keep):
        return
    shutil.rmtree(path, ignore_errors=True)


def open_store() -> ChartStore | None:
    """进程内单例。``current.json`` 的 gen 变了才重新映射。"""
    global _reader, _reader_gen
    pointer = _read_pointer()
    gen = str(pointer["gen"]) if pointer else None
    with _reader_lock:
        if gen == _reader_gen:
            return _reader
        old = _reader
        _reader = None
        _reader_gen = None
    old_path = old.path if old is not None else None
    _close_store(old)
    del old
    store = None
    if gen:
        gen_dir = ROOT / gen
        try:
            store = _load_gen(gen_dir, "r")
        except (OSError, ValueError) as exc:
            log.warning("图表日K仓打开失败 %s: %s", gen, exc)
            store = None
            gen = None
    with _reader_lock:
        _reader = store
        _reader_gen = gen
    _drop_path(old_path, gen)
    return store


def required_as_of(now=None) -> str:
    """仓至少要覆盖到今天之前最近一个交易日，否则中间缺的几天快照补不回来。"""
    global _required_memo
    import market_hours
    today = (now or market_hours.now()).date()
    key = today.isoformat()
    memo = _required_memo
    if memo and memo[0] == key:
        return memo[1]
    probe = today - timedelta(days=1)
    for _ in range(30):
        if market_hours.is_trading_day(probe.isoformat()):
            break
        probe -= timedelta(days=1)
    value = probe.isoformat()
    _required_memo = (key, value)
    return value


def _disabled() -> bool:
    return os.environ.get("DAILY_CHART_DISABLE", "").strip().lower() in ("1", "true", "yes", "on")


def daily_frame(symbol: str, min_as_of: str | None = None) -> pd.DataFrame | None:
    """取单只日K。``attrs["chart_capacity"]`` 是仓的根数上限，调用方据此判断是否被截断。"""
    if _disabled():
        return None
    store = open_store()
    if store is None:
        return None
    if min_as_of and str(store.manifest.get("as_of") or "") < str(min_as_of):
        return None
    frame = store.frame(str(symbol))
    if frame is None or len(frame) < 5:
        return None
    frame.attrs["chart_capacity"] = int(store.manifest.get("count") or 0)
    return frame


def anchor_close(frame: pd.DataFrame, today) -> float | None:
    """与快照昨收对比的那根收盘：当天 bar 不算，取它前面一根。"""
    if frame is None or len(frame) == 0:
        return None
    last = frame.index[-1]
    try:
        last_day = last.date()
    except AttributeError:
        return None
    if last_day == today:
        if len(frame) < 2:
            return None
        value = float(frame["close"].iloc[-2])
    else:
        value = float(frame["close"].iloc[-1])
    if not np.isfinite(value) or value <= 0:
        return None
    return value


def resample_bars(frame: pd.DataFrame, period: str) -> pd.DataFrame | None:
    """日K → 周/月K。日期取期内最后一个实际交易日，与 AlphaFeed 原生周/月K 一致。"""
    rule = {"1w": "W-FRI", "1M": "ME"}.get(period)
    if rule is None or frame is None or len(frame) == 0:
        return None
    data = frame.sort_index().copy()
    data["_day"] = data.index
    agg = data.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
        "_day": "max",
    })
    agg = agg.dropna(subset=["close", "_day"])
    if agg.empty:
        return None
    agg.index = pd.DatetimeIndex(agg.pop("_day"))
    return agg


def _epoch_days(index) -> np.ndarray:
    return (pd.DatetimeIndex(index) - pd.Timestamp("1970-01-01")).days.to_numpy(dtype=np.int32)


def _write_gen(gen_dir: Path, frames: dict[str, pd.DataFrame], *, count: int,
               as_of: str, index_symbols: set[str], index_error: str | None) -> dict:
    symbols = np.array(sorted(frames), dtype="<U16")
    shape = (count, len(symbols))
    dates = np.zeros(shape, dtype=np.int32)
    arrays = {name: np.full(shape, np.nan, dtype=np.float32) for name in FIELDS}
    kept = 0
    index_count = 0
    for col, symbol in enumerate(symbols):
        frame = normalize_daily(frames[symbol])
        if frame is None:
            continue
        frame = frame.tail(count)
        start = count - len(frame)
        dates[start:, col] = _epoch_days(frame.index)
        for name in FIELDS:
            arrays[name][start:, col] = frame[name].to_numpy(dtype=np.float32)
        kept += 1
        if str(symbol) in index_symbols:
            index_count += 1
    manifest = {
        "version": STORE_VERSION,
        "adjust": "forward",
        "role": "visual_chart",
        "count": count,
        "fields": list(FIELDS),
        "as_of": as_of,
        "symbols": int(len(symbols)),
        "symbols_with_data": int(kept),
        "symbol_count": int(kept),
        "index_count": int(index_count),
        "index_error": index_error,
        "full_pulls_today": pulls_today(as_of),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    gen_dir.mkdir(parents=True, exist_ok=True)
    np.save(gen_dir / "symbols.npy", symbols, allow_pickle=False)
    np.save(gen_dir / "dates.npy", dates, allow_pickle=False)
    for name, values in arrays.items():
        np.save(gen_dir / f"{name}.npy", values, allow_pickle=False)
    with open(gen_dir / "manifest.json", "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


def _publish_pointer(gen: str, manifest: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "gen": gen,
        "as_of": manifest.get("as_of"),
        "symbol_count": manifest.get("symbols_with_data"),
        "index_count": manifest.get("index_count"),
    }
    tmp = ROOT / f".current-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    previous = _read_pointer()
    os.replace(tmp, _current_path())
    with _reader_lock:
        held = _reader_gen
    if held and held != gen:
        _drop_path(_release_reader(), gen)
    previous_path = str(ROOT / previous["gen"]) if previous and previous.get("gen") else None
    _drop_path(previous_path, gen)


def publish_frames(frames: dict[str, pd.DataFrame], *, count: int, as_of: str,
                   index_symbols: set[str] | None = None,
                   index_error: str | None = None) -> dict:
    """把完整帧写成新世代并切换指针。空帧拒绝，避免覆盖还能用的旧仓。"""
    usable = {symbol: frame for symbol, frame in frames.items() if frame is not None and len(frame)}
    if not usable:
        raise ValueError("没有可落盘的日K")
    gen = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    gen_dir = ROOT / gen
    try:
        manifest = _write_gen(
            gen_dir, usable, count=count, as_of=as_of,
            index_symbols=set(index_symbols or ()), index_error=index_error,
        )
    except Exception:
        shutil.rmtree(gen_dir, ignore_errors=True)
        raise
    _publish_pointer(gen, manifest)
    nbytes = disk_bytes()
    update_meta(
        as_of=as_of,
        symbol_count=manifest.get("symbols_with_data") or 0,
        index_count=manifest.get("index_count") or 0,
        index_error=index_error,
        bytes=nbytes,
        error=None,
    )
    return manifest


def disk_bytes() -> int:
    pointer = _read_pointer()
    if not pointer:
        return 0
    gen_dir = ROOT / str(pointer["gen"])
    total = 0
    if not gen_dir.is_dir():
        return 0
    for path in gen_dir.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def patch_frames(frames: dict[str, pd.DataFrame]) -> int:
    """把重拉的少量标的写成当前世代下的单标的补丁，读时优先于主数组。

    不复制主数组：Web 进程内改仓只花补丁那几 KB。补丁跟着世代目录走，
    下一次全量发布连同旧世代一起删掉，所以不会盖住更新的数据。
    """
    if not frames:
        return 0
    store = open_store()
    if store is None:
        return 0
    gen_dir = Path(store.path)
    if not (gen_dir / "manifest.json").is_file():
        return 0
    count = int(store.manifest.get("count") or chart_count())
    as_of = pd.Timestamp(str(store.manifest.get("as_of") or "2100-01-01"))
    patch_dir = gen_dir / "patch"
    patch_dir.mkdir(exist_ok=True)
    changed = 0
    for symbol, raw in frames.items():
        if symbol not in store.index:
            continue
        frame = normalize_daily(raw)
        if frame is None:
            continue
        frame = frame[frame.index <= as_of].tail(count)
        if frame.empty:
            continue
        tmp = patch_dir / f".{symbol}-{uuid.uuid4().hex[:8]}.tmp.npz"
        payload = {name: frame[name].to_numpy(dtype=np.float32) for name in FIELDS}
        np.savez(tmp, dates=_epoch_days(frame.index), **payload)
        os.replace(tmp, _patch_file(gen_dir, symbol))
        changed += 1
    return changed


def factor_batches(symbols: list[str], count: int, required_as_of: str, progress_cb=None,
                   batch_size: int = 100):
    """仓 ``as_of`` 覆盖目标日时，按批产出切片；否则返回 None（调用方回退拉 K）。"""
    if _disabled():
        return None
    store = open_store()
    if store is None:
        return None
    as_of = str(store.manifest.get("as_of") or "")
    if as_of < str(required_as_of):
        return None

    def _iter():
        chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)] or [[]]
        total = len(chunks)
        for done, chunk in enumerate(chunks, 1):
            batch = {}
            for symbol in chunk:
                frame = store.frame(symbol)
                if frame is None:
                    continue
                batch[symbol] = frame.tail(count)
            if progress_cb:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass
            yield batch, {"batches": total, "done": done, "failed_batches": 0, "source": "daily_chart"}

    return _iter()


def _roll_cooled(day: str) -> None:
    global _cooled_day
    if _cooled_day != day:
        _cooled.clear()
        _fail_counts.clear()
        _cooled_day = day


def observe_alignment(symbol: str, anchor: float | None, prev_close: float | None = None) -> None:
    """昨收相对仓内上一根收盘偏离超过阈值则标脏。不发网络。"""
    if anchor is None:
        return
    if prev_close is None:
        try:
            import market
            quote = market.quote_cache.get(market.normalize_symbol(symbol))
        except Exception:
            quote = None
        if not quote:
            return
        prev_close = quote.get("prev_close")
    try:
        prev = float(prev_close)
        anchor_v = float(anchor)
    except (TypeError, ValueError):
        return
    if not np.isfinite(prev) or not np.isfinite(anchor_v) or prev <= 0 or anchor_v <= 0:
        return
    import market_hours
    _roll_cooled(market_hours.now().date().isoformat())
    if symbol in _cooled:
        return
    if abs(prev - anchor_v) / prev <= drift_eps():
        return
    with _align_lock:
        _dirty.add(symbol)


def dirty_symbols() -> list[str]:
    with _align_lock:
        return sorted(_dirty)


def mark_cooled(symbols) -> None:
    import market_hours
    _roll_cooled(market_hours.now().date().isoformat())
    with _align_lock:
        for symbol in symbols:
            _cooled.add(symbol)
            _fail_counts.pop(symbol, None)
            _dirty.discard(symbol)


def pop_dirty(limit: int = 100) -> list[str]:
    with _align_lock:
        batch = list(_dirty)[:limit]
        for symbol in batch:
            _dirty.discard(symbol)
        return batch


def requeue_dirty(symbols) -> None:
    import market_hours
    _roll_cooled(market_hours.now().date().isoformat())
    with _align_lock:
        for symbol in symbols:
            fails = _fail_counts.get(symbol, 0) + 1
            _fail_counts[symbol] = fails
            if fails < 2:
                _dirty.add(symbol)
            else:
                _cooled.add(symbol)


def _refresh_dirty(symbols: list[str]) -> None:
    global _drift_warned
    if not symbols or building_active():
        if symbols and building_active():
            with _align_lock:
                _dirty.update(symbols)
        return
    store = open_store()
    universe = int(store.manifest.get("symbols") or 0) if store is not None else 0
    pending = len(symbols) + len(dirty_symbols())
    if universe and pending / universe > 0.05 and not _drift_warned:
        _drift_warned = True
        log.warning("日K复权漂移标的超过 5%% (%d/%d)，仍按增量重拉，不追加全量", pending, universe)
    import factors
    frames = {}
    try:
        for batch, _stats in factors.fetch_bars(symbols, count=chart_count()):
            frames.update(batch)
    except Exception as exc:
        log.warning("漂移标的重拉失败: %s", exc)
        requeue_dirty(symbols)
        return
    missing = [symbol for symbol in symbols if symbol not in frames]
    if missing:
        requeue_dirty(missing)
    if not frames:
        return
    try:
        patch_frames(frames)
    except Exception as exc:
        log.warning("漂移标的写入图表仓失败: %s", exc)
        requeue_dirty(list(frames))
        return
    mark_cooled(frames)


def _align_loop() -> None:
    while True:
        time.sleep(5)
        try:
            batch = pop_dirty(100)
            if batch:
                _refresh_dirty(batch)
        except Exception as exc:
            log.warning("图表日K对齐线程异常: %s", exc)


def start_align_worker() -> None:
    global _align_thread, _align_started
    with _align_lock:
        if _align_started and _align_thread is not None and _align_thread.is_alive():
            return
        _align_started = True
        _align_thread = threading.Thread(target=_align_loop, name="daily-chart-align", daemon=True)
        _align_thread.start()


def status() -> dict:
    meta = load_meta()
    pointer = _read_pointer() or {}
    store = open_store()
    manifest = store.manifest if store is not None else {}
    return {
        "as_of": pointer.get("as_of") or manifest.get("as_of"),
        "building": building_active(),
        "progress": meta.get("progress") or {"done": 0, "total": 0},
        "elapsed_sec": meta.get("elapsed_sec"),
        "error": meta.get("error"),
        "full_pulls_day": meta.get("full_pulls_day"),
        "full_pulls_today": pulls_today(_today()),
        "full_pull_cap": MAX_FULL_PULLS,
        "symbol_count": manifest.get("symbols_with_data") or meta.get("symbol_count") or 0,
        "index_count": manifest.get("index_count") if store is not None else meta.get("index_count") or 0,
        "index_error": manifest.get("index_error") if store is not None else meta.get("index_error"),
        "bytes": disk_bytes(),
        "count": manifest.get("count") or chart_count(),
        "build_at": "%02d:%02d" % build_at(),
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
    }
