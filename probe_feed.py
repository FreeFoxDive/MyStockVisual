"""探测 AlphaFeed 套餐下监控所需接口的可用性与快照刷新频率。

温和限速 (6 次/分钟), 不裸跑。盘中连续采样看 timestamp 前进间隔,
据此决定 POLL_INTERVAL; 顺带探 instruments / depth / intraday_batch / WebSocket。

另含第 7 节日K批量拉取限额实测 (默认跳过, 会真烧额度; 见 af_limits.py):
    python -u visual/probe_feed.py                 # 只跑 1~6 节 (温和)
    python -u visual/probe_feed.py --kline-batches 5      # 5 批 (500 只) 验速率
    python -u visual/probe_feed.py --kline-batches 0      # 全市场 (≈79 批 ≈1.5min)

运行:
    python -u visual/probe_feed.py
    python -u visual/probe_feed.py --seconds 90
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import af_limits  # noqa: E402

for env_dir in (SCRIPT_DIR, SCRIPT_DIR.parent):
    env_file = env_dir / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        break

PROBE_SYMBOLS = ["600519.SH", "000001.SZ", "000001.SH", "510300.SH"]
# 涨跌停价探测: 股票 / 10% ETF / 20cm ETF / 跨境 ETF (无涨跌幅限制) / 货币 ETF (无)
PROBE_LIMIT_SYMBOLS = ["600519.SH", "300750.SZ", "688111.SH",
                       "510300.SH", "159915.SZ", "512880.SH",
                       "513100.SH", "511990.SH"]
# 第 5b 节港/美股探测用 (此前漏定义, 该节一直抛 NameError 静默失效)
PROBE_HKUS_SYMBOLS = ["00700.HK", "AAPL.US"]


def _ok(name, ok, extra=""):
    flag = "OK " if ok else "FAIL"
    print(f"  [{flag}] {name}{('  ' + extra) if extra else ''}", flush=True)


def _universe_symbols(limit=None, af=None):
    """标的池: 优先本地缓存列表 (零额度), 取不到再回退 AF 标的池查询。"""
    try:
        import market
        syms = [s["symbol"] for s in (market._load_stock_list() or []) if s.get("symbol")]
        if syms:
            print(f"       标的池来自本地列表: {len(syms)} 只", flush=True)
            return syms[:limit] if limit else syms
    except Exception as e:
        print(f"       本地列表不可用 ({e}), 回退 AF universe", flush=True)
    if af is None:
        return []
    try:
        df = af.quotes.get(universes=["CN_Stock"], to_dataframe=True)
        syms = sorted(str(s) for s in (df.index if df is not None else []))
        print(f"       标的池来自 AF universe: {len(syms)} 只 (消耗 1 次池查询)", flush=True)
        return syms[:limit] if limit else syms
    except Exception as e:
        print(f"       AF universe 失败: {e}", flush=True)
        return []


def _kline_ceiling_check(af, symbols):
    """7a. 「100 标的/次」是否硬约束: 用 150 只试一次 (一支额外请求)。"""
    import feed as feed_mod
    print("=== 7a. 单次批量上限 (文档: 100 标的/次) ===", flush=True)
    if len(symbols) <= af_limits.BATCH_SIZE:
        print(f"       标的不足 {af_limits.BATCH_SIZE + 1} 只, 跳过", flush=True)
        return
    t0 = time.time()
    try:
        dfs = af.klines.batch(symbols, period="1d", count=5, adjust="forward", to_dataframe=True)
        n = len(dfs or {})
        print(f"       {len(symbols)} 只/次: 通过, 返回 {n} 只 ({time.time() - t0:.2f}s) "
              f"→ 实际上限比文档更宽; {af_limits.BATCH_SIZE} 仍是安全批量", flush=True)
    except Exception as e:
        wait = feed_mod._retry_after_ms(e)
        extra = f" retry_after_ms={wait}" if wait is not None else ""
        print(f"       {len(symbols)} 只/次: 被拒 [{type(e).__name__}] {str(e)[:120]}{extra} "
              f"→ 证实单次上限 {af_limits.BATCH_SIZE} 只", flush=True)


def _kline_batch_rate_probe(af, batches=0, count=130, ratio=af_limits.RESERVE_RATIO,
                            chunk=af_limits.BATCH_SIZE):
    """7. 日K批量拉取限额实测: 按 chunk 只/批 + 令牌桶, 看是否撞 429。

    batches=0 表示全市场 (≈79 批); 其余按 batches×chunk 只取样。
    chunk 小于 100 时单批延迟更低, 令牌桶才会真正成为瓶颈 (能压到目标速率跑)。
    """
    import feed as feed_mod
    target = af_limits.bucket_rate("kline_daily_batch", ratio)
    hard = af_limits.limit("kline_daily_batch")
    interval = af_limits.bucket_interval("kline_daily_batch", ratio)
    print(f"=== 7. 日K批量拉取限额实测 (Pro {hard}/min × {ratio:g} → 目标 {target}/min, "
          f"批间隔 ≥{interval:.2f}s, 单批 {chunk} 只) ===", flush=True)
    symbols = _universe_symbols(batches * chunk if batches else None, af)
    chunks = [symbols[i:i + chunk] for i in range(0, len(symbols), chunk)]
    if not chunks:
        print("       无标的可测, 跳过", flush=True)
        return
    _kline_ceiling_check(af, symbols[:af_limits.BATCH_SIZE + 50])

    print(f"       计划 {len(chunks)} 批 / {len(symbols)} 只, "
          f"预计 {len(chunks) / target * 60:.0f}s", flush=True)
    bucket = feed_mod.TokenBucket(rate_per_min=target)
    ok = empty = err = limited = 0
    lat = []
    t_start = time.time()
    for i, chunk in enumerate(chunks, 1):
        while not bucket.try_acquire():
            time.sleep(0.05)
        t0 = time.time()
        try:
            dfs = af.klines.batch(chunk, period="1d", count=count, adjust="forward",
                                  to_dataframe=True)
            got = sum(1 for s in chunk
                      if (dfs or {}).get(s) is not None and len(dfs[s]) > 0)
            ok += got
            empty += len(chunk) - got
        except Exception as e:
            wait = feed_mod._retry_after_ms(e)
            if wait is not None:
                limited += 1
                print(f"       [{i}] 429 限流 retry_after_ms={wait}", flush=True)
            else:
                err += 1
                print(f"       [{i}] 失败 {type(e).__name__}: {str(e)[:120]}", flush=True)
        lat.append(time.time() - t0)
        if i % 10 == 0 or i == len(chunks):
            elapsed = time.time() - t_start
            print(f"       批 {i}/{len(chunks)}  {elapsed:6.1f}s  实测 {i / elapsed * 60:5.1f} 批/min"
                  f"  有效 {ok} 空 {empty} 错 {err} 限流 {limited}", flush=True)

    elapsed = time.time() - t_start
    eff = len(chunks) / elapsed * 60 if elapsed else 0
    lat.sort()
    print(f"       单批耗时 min={lat[0]:.2f}s median={lat[len(lat) // 2]:.2f}s max={lat[-1]:.2f}s", flush=True)
    if limited:
        print(f"       [FAIL] 出现 {limited} 次限流: {target}/min 超上游容忍 —— "
              f"把 ratio 降到 {ratio * 0.7:.2f} 左右重测, 并把结论写进 docs/alphafeed-limits.md", flush=True)
    elif err:
        print(f"       [WARN] 无 429 但有 {err} 批异常 (非限流), 检查报文后重测", flush=True)
    elif eff > hard:
        print(f"       [FAIL] 实测 {eff:.1f} 批/min 高于文档 {hard}/min 却未被拒: 口径存疑", flush=True)
    else:
        print(f"       [OK] 无 429; 实测 {eff:.1f} 批/min ≤ 文档 {hard}/min "
              f"(目标 {target}/min, 余量 {hard - eff:.1f}); 单批 {af_limits.BATCH_SIZE} 只可用", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=60, help="快照刷新探测时长")
    parser.add_argument("--interval", type=float, default=10.0, help="采样间隔秒 (温和, 远低于 60/min)")
    parser.add_argument("--kline-batches", type=int, default=-1,
                        help="日K批量限速实测的批数 (0=全市场; 默认 -1 = 跳过, 避免烧额度)")
    parser.add_argument("--kline-count", type=int, default=130, help="实测每只拉取的日K根数")
    parser.add_argument("--kline-chunk", type=int, default=af_limits.BATCH_SIZE,
                        help="实测单批标的数 (默认 100; 取小值可让令牌桶真正成为瓶颈)")
    parser.add_argument("--kline-ratio", type=float, default=af_limits.RESERVE_RATIO,
                        help="实测额度比例 (默认 0.9 = Pro 限额的 90%%)")
    parser.add_argument("--only-kline", action="store_true",
                        help="只跑第 7 节日K批量限速实测 (跳过 1~6 节, 不占用快照采样时间)")
    args = parser.parse_args()

    key = os.environ.get("AF_API_KEY", "")
    if not key:
        print("need AF_API_KEY")
        sys.exit(1)
    from alphafeed import AlphaFeed
    import feed as feed_mod
    af = AlphaFeed(api_key=key)

    if args.only_kline:
        _kline_batch_rate_probe(af, batches=max(0, args.kline_batches),
                                count=args.kline_count, ratio=args.kline_ratio,
                                chunk=max(1, args.kline_chunk))
        print("探测结束 (仅日K批量限速)。", flush=True)
        return

    print("=== 1. quotes.get(symbols=) ===", flush=True)
    t0 = time.time()
    try:
        df = af.quotes.get(symbols=PROBE_SYMBOLS, to_dataframe=True)
        dt = time.time() - t0
        cols = list(df.columns) if df is not None else []
        n = 0 if df is None else len(df)
        _ok("quotes.get(symbols=)", n > 0, f"{n} 行 {dt:.2f}s cols={cols[:12]}")
        if n:
            row = df.iloc[0]
            print(f"       sample symbol={row.get('symbol')} last={row.get('last_price')} "
                  f"ts={row.get('timestamp')} prev_close={row.get('prev_close')}", flush=True)
    except Exception as e:
        _ok("quotes.get(symbols=)", False, str(e))
        df = None

    print("=== 2. instruments.batch (limit_up) ===", flush=True)
    try:
        insts = af.instruments.batch(PROBE_LIMIT_SYMBOLS)
        has_up = False
        sample = None
        for it in insts or []:
            ext = (it or {}).get("ext") or {}
            if ext.get("limit_up") is not None:
                has_up = True
                sample = (it.get("symbol"), ext.get("limit_up"), ext.get("limit_down"))
                break
        _ok("instruments.batch", bool(insts), f"n={len(insts or [])} limit_up={has_up} sample={sample}")
        # 逐标的列出, 用于判断 ETF 是否也有涨跌停价 (不能只看第一只有值的)
        by_sym = {it.get("symbol"): it for it in (insts or []) if isinstance(it, dict)}
        print(f"       {'symbol':<12} {'name':<12} {'type':<7} {'ext.type':<10} "
              f"{'limit_up':>9} {'limit_down':>10}", flush=True)
        for sym in PROBE_LIMIT_SYMBOLS:
            it = by_sym.get(sym)
            if not it:
                print(f"       {sym:<12} (未返回)", flush=True)
                continue
            ext = it.get("ext") or {}
            name = str(it.get("name") or "")[:10]
            print(f"       {sym:<12} {name:<12} {str(it.get('type')):<7} "
                  f"{str(ext.get('type')):<10} {str(ext.get('limit_up')):>9} "
                  f"{str(ext.get('limit_down')):>10}", flush=True)
    except Exception as e:
        _ok("instruments.batch", False, str(e))

    print("=== 3. depth_get_batch (depth.get 模拟, 30/min) ===", flush=True)
    try:
        bucket = feed_mod.TokenBucket(feed_mod.DEPTH_GET_RATE_PER_MIN)
        depths = feed_mod.depth_get_batch(af, PROBE_SYMBOLS[:2], bucket, log_skip=False)
        n = len(depths or {})
        extra = ""
        if n:
            d0 = next(iter(depths.values()))
            extra = f"ask1_vol={(d0.get('ask_volumes') or [None])[0]}"
        _ok("depth_get_batch", n > 0, f"n={n} {extra}")
    except Exception as e:
        _ok("depth_get_batch", False, str(e))

    print("=== 3b. depth.batch 原生 (高套餐, Starter 预期 FAIL) ===", flush=True)
    try:
        depths = af.depth.batch(PROBE_SYMBOLS[:1])
        n = len(depths or {})
        extra = ""
        if n:
            d0 = next(iter(depths.values()))
            extra = f"ask1_vol={(d0.get('ask_volumes') or [None])[0]}"
        _ok("depth.batch", n > 0, f"n={n} {extra}")
    except Exception as e:
        _ok("depth.batch", False, str(e))

    print("=== 4. klines.intraday_batch ===", flush=True)
    try:
        dfs = af.klines.intraday_batch(PROBE_SYMBOLS[:1], to_dataframe=True)
        df1 = (dfs or {}).get(PROBE_SYMBOLS[0])
        n = 0 if df1 is None else len(df1)
        cols = list(df1.columns) if df1 is not None and n else []
        _ok("klines.intraday_batch", n > 0, f"{n} 根 cols={cols[:8]}")
    except Exception as e:
        _ok("klines.intraday_batch", False, str(e))

    print("=== 5. 快照刷新频率 (timestamp 前进间隔) ===", flush=True)
    stamps = []
    deadline = time.time() + max(20, args.seconds)
    while time.time() < deadline:
        try:
            qdf = af.quotes.get(symbols=PROBE_SYMBOLS[:1], to_dataframe=True)
            if qdf is not None and len(qdf):
                ts = qdf.iloc[0].get("timestamp")
                last = qdf.iloc[0].get("last_price")
                stamps.append((time.time(), ts, last))
                print(f"       t+{stamps[-1][0] - stamps[0][0]:5.1f}s  exch_ts={ts}  last={last}", flush=True)
        except Exception as e:
            print(f"       采样失败: {e}", flush=True)
        time.sleep(args.interval)
    deltas = []
    prev = None
    for _, ts, _ in stamps:
        if ts is None:
            continue
        if prev is not None and ts != prev:
            deltas.append((ts - prev) / (1000.0 if ts > 1e12 else 1.0))
        prev = ts
    if deltas:
        print(f"       timestamp 前进 {len(deltas)} 次, 间隔 min={min(deltas):.2f}s "
              f"median={sorted(deltas)[len(deltas)//2]:.2f}s max={max(deltas):.2f}s", flush=True)
    else:
        print("       探测期内 timestamp 未前进 (盘后/延迟/分钟级刷新?)", flush=True)

    print("=== 5b. 港/美股 quotes + klines ===", flush=True)
    try:
        hk_df = af.quotes.get(symbols=PROBE_HKUS_SYMBOLS, to_dataframe=True)
        print(hk_df, flush=True)
        for sym in PROBE_HKUS_SYMBOLS:
            try:
                dfs = af.klines.batch([sym], period="1d", count=30, adjust="qfq", to_dataframe=True)
                d = (dfs or {}).get(sym)
                print(sym, "日K:", ("OK rows=" + str(len(d))) if d is not None and len(d) else "空 (AF 不支持该代码/市场?)", flush=True)
            except Exception as e:
                print(sym, "日K失败:", e, flush=True)
        print("限频口径: 若 429, 记录 retry_after; 令牌桶预设 8/min (HKUS_AF_QUOTE_PER_MIN 可调)", flush=True)
    except Exception as e:
        print("港/美股探测失败 (AF 不支持或额度不足):", e, flush=True)

    print("=== 6. WebSocket (可选, 无权限则 FAIL) ===", flush=True)
    try:
        import websocket  # noqa: F401
        has_ws_lib = True
    except ImportError:
        has_ws_lib = False
        _ok("websocket-client 库", False, "未安装, 跳过 (REST 轮询不受影响)")
    if has_ws_lib:
        try:
            import json
            from websocket import create_connection
            url = f"wss://api.tickflow.org/v1/ws/stream?api_key={key}"
            ws = create_connection(url, timeout=8)
            ws.send(json.dumps({"op": "subscribe", "channel": "quotes", "symbols": PROBE_SYMBOLS[:1]}))
            msg = ws.recv()
            ws.close()
            _ok("ws quotes channel", True, str(msg)[:180])
        except Exception as e:
            _ok("ws quotes channel", False, str(e))

    if args.kline_batches >= 0:
        _kline_batch_rate_probe(af, batches=args.kline_batches, count=args.kline_count,
                                ratio=args.kline_ratio, chunk=max(1, args.kline_chunk))
    else:
        print("=== 7. 日K批量限速实测: 已跳过 (加 --kline-batches 5 试 500 只, 0 = 全市场) ===",
              flush=True)

    print("探测结束。监控循环将使用 quotes.get(symbols=) + 令牌桶 6/min。", flush=True)


if __name__ == "__main__":
    main()
