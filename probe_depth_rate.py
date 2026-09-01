"""探测 depth_get_batch（depth.get 模拟 batch）在 30/min 令牌桶下是否被 AF 限流。

运行:
    python -u visual/probe_depth_rate.py
    python -u visual/probe_depth_rate.py --burst 15
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

from alphafeed import AlphaFeed

import feed as feed_mod

SYMS = ["600489.SH", "601995.SH", "600519.SH"]


def _load_af():
    key = os.environ.get("AF_API_KEY") or os.environ.get("ALPHAFEED_API_KEY")
    if not key:
        print("AF_API_KEY 未设置", flush=True)
        sys.exit(1)
    return AlphaFeed(api_key=key)


def probe_burst(af, n: int):
    """无本地桶，连续 n 次 depth.get，看 AF 是否 429。"""
    sym = SYMS[0]
    print(f"=== burst: {n}x depth.get({sym}) 无本地限流 ===", flush=True)
    ok = err = 0
    t0 = time.perf_counter()
    for i in range(n):
        try:
            d = af.depth.get(sym)
            ok += 1 if d else 0
            print(f"  [{i+1}/{n}] OK ask1={(d.get('ask_volumes') or [None])[0]}", flush=True)
        except Exception as e:
            err += 1
            print(f"  [{i+1}/{n}] FAIL {type(e).__name__}: {e}", flush=True)
    dt = time.perf_counter() - t0
    print(f"burst 结果: ok={ok} err={err} {dt:.1f}s\n", flush=True)


def probe_bucket_batch(af, rounds: int, syms_per_round: int):
    """用 depth_get_batch + 30/min 桶，模拟监控多轮拉取。"""
    bucket = feed_mod.TokenBucket(feed_mod.DEPTH_GET_RATE_PER_MIN)
    print(
        f"=== depth_get_batch: {rounds} 轮 x {syms_per_round} 只, "
        f"桶={feed_mod.DEPTH_GET_RATE_PER_MIN}/min ===",
        flush=True,
    )
    total_ok = total_skip = total_err = 0
    t0 = time.perf_counter()
    for r in range(rounds):
        syms = SYMS[:syms_per_round]
        try:
            out = feed_mod.depth_get_batch(af, syms, bucket, log_skip=False)
            got = len(out)
            skip = len(syms) - got
            if skip > 0:
                # 可能令牌不足或 get 失败
                total_skip += max(0, len(syms) - got)
            total_ok += got
            print(
                f"  round {r+1}: got={got}/{len(syms)} "
                f"tokens_left≈{bucket.tokens:.1f}",
                flush=True,
            )
        except Exception as e:
            total_err += 1
            print(f"  round {r+1}: FAIL {type(e).__name__}: {e}", flush=True)
        time.sleep(2.0)
    dt = time.perf_counter() - t0
    print(
        f"bucket 结果: ok_symbols={total_ok} skip≈{total_skip} err_rounds={total_err} "
        f"{dt:.1f}s\n",
        flush=True,
    )


def probe_native_batch(af):
    sym = SYMS[0]
    print(f"=== depth_batch_native([{sym}]) 对照 ===", flush=True)
    try:
        r = feed_mod.depth_batch_native(af, [sym])
        print(f"OK keys={list(r.keys())}", flush=True)
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}", flush=True)
    print(flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--burst", type=int, default=12, help="无桶连续 depth.get 次数")
    p.add_argument("--rounds", type=int, default=8, help="depth_get_batch 轮数")
    p.add_argument("--per-round", type=int, default=2, help="每轮 symbol 数")
    args = p.parse_args()

    af = _load_af()
    probe_native_batch(af)
    probe_burst(af, args.burst)
    probe_bucket_batch(af, args.rounds, args.per_round)
    print("探测结束。监控 RestFeed.depth 使用 depth_get_batch + 30/min 桶。", flush=True)


if __name__ == "__main__":
    main()
