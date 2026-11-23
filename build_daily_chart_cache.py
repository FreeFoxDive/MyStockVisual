"""交易日 16:05 后把 A股股票 + ETF（指数尽力）前复权日K写入 visual 自管仓。

不读取、不修改生产 ``out_files/daily_qfq`` 及其构建脚本。全市场批量日K与
因子库共用 54/min 令牌桶；同一自然日自动全量最多 2 次。

    python -u visual/build_daily_chart_cache.py --full
    python -u visual/build_daily_chart_cache.py --status
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

import daily_chart_store as store

log = logging.getLogger("daily_chart")

SCRIPT_DIR = Path(__file__).resolve().parent
SCHED_INTERVAL_SEC = 60
RETRY_BACKOFF_SEC = 1800
MAX_FAILED_PULLS = 3
PRE_OPEN_GUARD = (9, 5)
_thread = None
_thread_lock = threading.Lock()
_child = None
_child_lock = threading.Lock()


def _now():
    import market_hours
    return market_hours.now()


def target_day(now=None) -> str:
    import factors
    return factors.last_closed_trading_day(now or _now())


def _cn_indexes() -> list[str]:
    import market
    try:
        symbols = market._load_index_cache() or set()
    except Exception as exc:
        log.warning("指数列表不可用，本轮跳过指数: %s", exc)
        return []
    return sorted(
        symbol for symbol in symbols
        if str(symbol).endswith(".SH") or str(symbol).endswith(".SZ")
    )


def universe() -> tuple[list[str], list[str]]:
    """股票+场内基金，以及沪深指数（指数失败不影响股票列表）。"""
    import market
    rows = market._load_stock_list() or []
    stocks = []
    seen = set()
    for row in rows:
        symbol = str((row or {}).get("symbol") or "").strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            stocks.append(symbol)
    indexes = [symbol for symbol in _cn_indexes() if symbol not in seen]
    return stocks, indexes


def collect_frames(pull, stocks: list[str], indexes: list[str]):
    """先拉股票/ETF。指数单独包一层，失败只记错误，不丢掉已拉到的股票。"""
    frames = {}
    frames.update(pull(stocks))
    index_error = None
    if indexes:
        try:
            frames.update(pull(indexes))
        except Exception as exc:
            index_error = str(exc)
            log.warning("指数日K拉取失败，股票仓照常落盘: %s", exc)
    return frames, index_error


def cut_to_day(frames: dict, day: str) -> dict:
    """``--force`` 盘中测试会拉到今天未收盘的 bar，只保留目标日及以前。"""
    out = {}
    for symbol, raw in frames.items():
        frame = store.normalize_daily(raw)
        if frame is None:
            continue
        frame = frame[frame.index <= pd.Timestamp(day)]
        if len(frame):
            out[symbol] = frame
    return out


def _pull(symbols: list[str]):
    """按批交出日K。全市场大约 80 批，调用方写完一批就必须丢掉。

    因子库的内存看门狗默认 400MB，到 90% 会清空主页 K 线缓存。
    若把 8000 只 DataFrame 攒齐再落盘，峰值大约 1GB，会直接触发它。
    """
    import factors
    if not symbols:
        return

    def on_progress(done, total):
        store.update_meta(progress={"done": done, "total": total})

    yield from factors.fetch_bars(
        symbols, count=store.chart_count(), progress_cb=on_progress)


def _stream_into(writer, symbols: list[str], day: str) -> None:
    for batch, _stats in _pull(symbols):
        store.write_gen_batch(writer, batch, day)
        del batch


def _in_session(now) -> bool:
    """竞价/连续竞价/午休视为盘中；盘前最后 10 分钟也算，免得全量跑进 09:15 竞价。"""
    import market_hours
    phase = market_hours.session_phase(now)
    if phase == "pre":
        return (now.hour, now.minute) >= PRE_OPEN_GUARD
    return phase in ("auction", "trading", "break")


def _in_retry_backoff(now) -> bool:
    meta = store.load_meta()
    if not meta.get("error") or not meta.get("finished_at"):
        return False
    try:
        finished = datetime.fromisoformat(str(meta["finished_at"])).timestamp()
    except ValueError:
        return False
    return now.timestamp() - finished < RETRY_BACKOFF_SEC


def _block_reason(now, *, force: bool) -> str | None:
    """盘中默认禁止全量（仅 ``force`` 测试可破）。

    今天的目标日还要等 build_at；落后于更早的已收盘交易日则在盘后/非交易日补跑。
    """
    if not force and _in_session(now):
        return "盘中禁止落盘（测试请加 --force）"
    day = target_day(now)
    if not force and day == now.date().isoformat() and (now.hour, now.minute) < store.build_at():
        return "未到 %02d:%02d" % store.build_at()
    reason = store.full_pull_block_reason(day, force=force)
    if reason:
        return reason
    if not force and store.failed_today(day) >= MAX_FAILED_PULLS:
        return "今日失败已达 %d 次，停止自动重试（--force 可再试）" % MAX_FAILED_PULLS
    if not force and _in_retry_backoff(now):
        return "上次全量失败，%d 分钟内不自动重试" % (RETRY_BACKOFF_SEC // 60)
    return None


def should_build(now=None):
    reason = _block_reason(now or _now(), force=False)
    return (False, reason) if reason else (True, "due")


def run_full(*, force: bool = False) -> dict:
    """拉全市场并发布新世代。只有成功落盘才计入当日全量次数。"""
    now = _now()
    day = target_day(now)
    reason = _block_reason(now, force=force)
    if reason:
        return {"ok": False, "reason": reason, "day": day}
    started = time.time()
    store.update_meta(
        building=True,
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        finished_at=None,
        error=None,
        progress={"done": 0, "total": 0},
    )
    try:
        stocks, indexes = universe()
        ordered = list(dict.fromkeys([*stocks, *indexes]))
        writer = store.open_gen(
            ordered, count=store.chart_count(), as_of=day, index_symbols=set(indexes),
        )
        index_error = None
        try:
            _stream_into(writer, stocks, day)
            if indexes:
                try:
                    _stream_into(writer, indexes, day)
                except Exception as exc:
                    index_error = str(exc)
                    log.warning("指数日K拉取失败，股票仓照常落盘: %s", exc)
            if not writer.filled:
                raise RuntimeError("没有拉到可用日K")
            manifest = store.seal_gen(writer, index_error=index_error)
        except Exception as exc:
            store.abort_gen(writer)
            if not writer.sealed:
                raise
            # 指针已经切过去。再记成失败会占掉重试次数，下一轮又因「已是最新」不再补跑。
            log.warning("图表日K已发布，收尾失败: %s", exc)
            manifest = json.loads((writer.path / "manifest.json").read_text(encoding="utf-8"))
        store.note_full_pull(day)
        elapsed = round(time.time() - started, 1)
        store.update_meta(
            building=False,
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            elapsed_sec=elapsed,
            error=None,
        )
        log.info("图表日K已落盘 as_of=%s symbols=%s indexes=%s %.1fs",
                 day, manifest.get("symbols_with_data"), manifest.get("index_count"), elapsed)
        return {"ok": True, "day": day, "elapsed_sec": elapsed,
                "symbol_count": manifest.get("symbols_with_data"),
                "index_count": manifest.get("index_count"),
                "index_error": index_error}
    except Exception as exc:
        log.warning("图表日K全量失败: %s", exc)
        store.note_failed_pull(day)
        store.update_meta(
            building=False,
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            elapsed_sec=round(time.time() - started, 1),
            error=str(exc),
        )
        return {"ok": False, "day": day, "error": str(exc)}
    finally:
        # 崩溃路径也要放下 building，避免调度把过期锁当成还在跑满 30 分钟
        meta = store.load_meta()
        if meta.get("building"):
            store.update_meta(building=False)


def build_blocked_reason(*, force: bool = False) -> str | None:
    return _block_reason(_now(), force=force)


def spawn_build(*, force: bool = False) -> None:
    global _child
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--full"]
    if force:
        cmd.append("--force")
    with _child_lock:
        if _child is not None and _child.poll() is None:
            raise RuntimeError("已有全量构建在进行")
        _child = subprocess.Popen(cmd, cwd=str(SCRIPT_DIR.parent))


def _loop() -> None:
    log.info("图表日K调度已启动 (交易日 %02d:%02d 后, 全量每天最多 %d 次)",
             store.build_at()[0], store.build_at()[1], store.MAX_FULL_PULLS)
    while True:
        try:
            with _child_lock:
                alive = _child is not None and _child.poll() is None
            if not alive:
                ok, why = should_build()
                if ok:
                    log.info("图表日K开始全量 (%s)", why)
                    spawn_build(force=False)
        except Exception as exc:
            log.warning("图表日K调度异常: %s", exc)
        time.sleep(SCHED_INTERVAL_SEC)


def startup_refresh() -> dict:
    """服务启动时预热 mmap；仓过时且门控允许则立刻拉起全量，不过时只预热。

    返回 ``{warmed, spawned, reason}``，供测试断言；不抛到调用方。
    """
    out = {"warmed": False, "spawned": False, "reason": None}
    try:
        out["warmed"] = store.open_store() is not None
    except Exception as exc:
        out["reason"] = f"warm_failed:{exc}"
        log.warning("图表日K仓预热失败: %s", exc)
        return out
    try:
        ok, why = should_build()
    except Exception as exc:
        out["reason"] = f"gate_failed:{exc}"
        log.warning("图表日K启动门控失败: %s", exc)
        return out
    if not ok:
        out["reason"] = why
        log.info("启动时图表日K无需全量 (%s)", why)
        return out
    try:
        spawn_build(force=False)
        out["spawned"] = True
        out["reason"] = why
        log.info("启动时图表日K过时，已拉起全量 (%s)", why)
    except RuntimeError as exc:
        out["reason"] = str(exc)
        log.info("启动时图表日K未拉起: %s", exc)
    except Exception as exc:
        out["reason"] = str(exc)
        log.warning("启动时图表日K拉起失败: %s", exc)
    return out


def start_scheduler() -> None:
    """Web 进程里：启动预热/按需补全，再巡检到点全量，并做盘中漂移增量重拉。"""
    global _thread
    startup_refresh()
    store.start_align_worker()
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="daily-chart-scheduler", daemon=True)
        _thread.start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="visual 图表前复权日K仓")
    parser.add_argument("--full", action="store_true", help="立即全量落盘")
    parser.add_argument(
        "--force", action="store_true",
        help="测试用：盘中/未到点/已是最新也可落盘（仍受每日成功 2 次封顶）",
    )
    parser.add_argument("--status", action="store_true", help="打印仓状态")
    args = parser.parse_args()
    if args.status or not args.full:
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        if not args.full:
            return
    result = run_full(force=args.force)
    print(json.dumps(result, ensure_ascii=False, default=str))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
