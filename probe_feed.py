"""探测 AlphaFeed Starter 套餐下监控所需接口的可用性与快照刷新频率。

温和限速 (6 次/分钟), 不裸跑。盘中连续采样看 timestamp 前进间隔,
据此决定 POLL_INTERVAL; 顺带探 instruments / depth / intraday_batch / WebSocket。

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=60, help="快照刷新探测时长")
    parser.add_argument("--interval", type=float, default=10.0, help="采样间隔秒 (温和, 远低于 60/min)")
    args = parser.parse_args()

    key = os.environ.get("AF_API_KEY", "")
    if not key:
        print("need AF_API_KEY")
        sys.exit(1)
    from alphafeed import AlphaFeed
    import feed as feed_mod
    af = AlphaFeed(api_key=key)

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

    print("探测结束。监控循环将使用 quotes.get(symbols=) + 令牌桶 6/min。", flush=True)


if __name__ == "__main__":
    main()
