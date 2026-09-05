"""实测 AF 快照 / AF 日K / 麦蕊日K 的 volume 单位是否一致 (股 vs 手)。

_likely_append_today_bar 用 AF 快照在盘中/收盘后拼「今日 bar」写进日K序列,
若快照 volume 与 K 线 volume 单位差 100 倍, 该日量能与量均线会失真。
取最近交易日 (收盘后数值已定格) 对比同一天三路数据的 volume。

运行:
    python -u visual/probe_volume_units.py
    python -u visual/probe_volume_units.py 600519.SH 000001.SZ 510300.SH
"""
from __future__ import annotations

import os
import sys
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

SYMBOLS = ["600519.SH", "000001.SZ", "510300.SH"]


def _mr_last_bar(symbol):
    """麦蕊日K最后一根 bar (股票走 stock_history, ETF 走 jj/lskx)。"""
    import market
    if market._is_etf(symbol):
        df = market._fetch_fund_kline(symbol, "1d", 3)
    else:
        rows = market.get_mr().stock_history(symbol, "d", "n", lt=3)
        import pandas as pd
        df = pd.DataFrame(rows).rename(columns={
            "o": "open", "h": "high", "l": "low", "c": "close",
            "a": "amount", "v": "volume", "t": "trade_date",
        })
        df = market._normalize(df)
    if df is None or len(df) == 0:
        return None, None
    last = df.iloc[-1]
    return str(df.index[-1].date()), float(last["volume"])


def _af_kline_last_bar(symbol):
    """AlphaFeed 前复权无关, 取未复权日K最后一根。"""
    import market
    af = market.get_af()
    dfs = af.klines.batch([symbol], period="1d", count=3, adjust="none",
                          to_dataframe=True)
    df = dfs.get(symbol)
    df = market._normalize(df)
    if df is None or len(df) == 0:
        return None, None
    last = df.iloc[-1]
    return str(df.index[-1].date()), float(last["volume"])


def _af_quote_volume(symbol):
    """AlphaFeed 快照 volume (收盘后 = 当日累计)。"""
    import market
    q = market.fetch_quotes([symbol], fresh=True).get(symbol)
    if not q:
        return None
    return q.get("volume")


def main():
    symbols = sys.argv[1:] or SYMBOLS
    print(f"{'symbol':<12}{'date':<12}{'麦蕊日K':>16}{'AF日K':>16}{'AF快照':>16}"
          f"{'AF日K/麦蕊':>12}{'快照/麦蕊':>12}")
    for sym in symbols:
        try:
            mr_date, mr_vol = _mr_last_bar(sym)
        except Exception as e:
            print(f"{sym:<12}麦蕊失败: {e}")
            continue
        try:
            af_date, af_vol = _af_kline_last_bar(sym)
        except Exception as e:
            af_date, af_vol = None, None
            print(f"  (AF日K失败: {e})")
        try:
            q_vol = _af_quote_volume(sym)
        except Exception as e:
            q_vol = None
            print(f"  (AF快照失败: {e})")

        def _fmt(v):
            return f"{v:,.0f}" if v is not None else "—"

        def _ratio(a, b):
            if a and b and b > 0:
                return f"{a / b:.4f}"
            return "—"

        date_show = mr_date or af_date or "?"
        print(f"{sym:<12}{date_show:<12}{_fmt(mr_vol):>16}{_fmt(af_vol):>16}"
              f"{_fmt(q_vol):>16}{_ratio(af_vol, mr_vol):>12}{_ratio(q_vol, mr_vol):>12}")

    print("\n判读: 比值 ≈1.0 → 单位一致; ≈100 → 差一手/股换算。\n"
          "2026-09-05 实测结论: 快照/股票日K volume 均为「手」, 唯麦蕊基金日K\n"
          "(jj/lskx) 为「股」(510300: 麦蕊日K 841,465,543 股 ↔ 快照 8,414,655 手,\n"
          "fund_real_time v=手/pv=股, cje 39.06 亿交叉验证; 600519: AF 快照与\n"
          "AF 日K同为 45,416 手, cje/价格交叉验证)。ETF 合成今日 bar 已在\n"
          "market._daily_bar_from_quote 内 ×100 对齐。")


if __name__ == "__main__":
    main()
