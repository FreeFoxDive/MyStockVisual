"""实测 akshare(东财) 日/周/月K 与分钟K接口: 列名、成交量单位、覆盖范围。

kline_source.AkshareSource 依赖这些接口做兜底源, 需确认:
1) 各接口返回的中文列名 (映射到 open/high/low/close/volume/amount);
2) volume 单位与麦蕊口径是否一致 (股票日K=手; 麦蕊基金日K=股, 差100倍);
3) 分钟K的 1m 是否只有近 5 个交易日 (东财 ndays=5);
4) 北交所 (833533) 与指数 (000300) 的覆盖。

运行:
    python -u visual/probe_akshare_source.py
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


def _retry(fn, tries=3, pause=4.0):
    """东财接口偶发 ProxyError/限流, 小退避重试。"""
    import time
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(pause)
    raise last


def _brief(df, label, n=2):
    if df is None or len(df) == 0:
        print(f"  {label:<34} -> 空数据")
        return
    cols = list(df.columns)
    tail = df.tail(n)
    print(f"  {label:<34} -> {len(df)} 行, 列: {cols}")
    for _, row in tail.iterrows():
        keep = {c: row[c] for c in cols if c not in ("振幅", "涨跌幅", "涨跌额", "换手率", "均价", "股票代码")}
        print(f"  {'':<34}    {keep}")


def main():
    import akshare as ak
    import market
    import pandas as pd

    start = "20260801"

    print("[1] stock_zh_a_hist 日/周/月 (600519, 不复权)")
    for period, tag in (("daily", "日"), ("weekly", "周"), ("monthly", "月")):
        try:
            _brief(ak.stock_zh_a_hist(symbol="600519", period=period,
                                      start_date=start, end_date="20991231", adjust=""),
                   f"600519 {tag}K")
        except Exception as e:
            print(f"  600519 {tag}K 失败: {type(e).__name__} {market._sanitize_error(e)}")

    print("[2] stock_zh_a_hist_min_em 分钟 (600519)")
    for period in ("5", "1"):
        try:
            _brief(ak.stock_zh_a_hist_min_em(symbol="600519", period=period, adjust=""),
                   f"600519 {period}m")
        except Exception as e:
            print(f"  600519 {period}m 失败: {type(e).__name__} {market._sanitize_error(e)}")

    print("[3] fund_etf_hist_em / fund_etf_hist_min_em (510300)")
    try:
        _brief(ak.fund_etf_hist_em(symbol="510300", period="daily",
                                   start_date=start, end_date="20991231", adjust=""),
               "510300 日K")
    except Exception as e:
        print(f"  510300 日K 失败: {type(e).__name__} {market._sanitize_error(e)}")
    try:
        _brief(ak.fund_etf_hist_min_em(symbol="510300", period="5", adjust=""),
               "510300 5m")
    except Exception as e:
        print(f"  510300 5m 失败: {type(e).__name__} {market._sanitize_error(e)}")

    print("[4] index_zh_a_hist / index_zh_a_hist_min_em (000300)")
    try:
        _brief(ak.index_zh_a_hist(symbol="000300", period="daily",
                                  start_date=start, end_date="20991231"), "000300 日K")
    except Exception as e:
        print(f"  000300 日K 失败: {type(e).__name__} {market._sanitize_error(e)}")
    try:
        _brief(ak.index_zh_a_hist_min_em(symbol="000300", period="5"), "000300 5m")
    except Exception as e:
        print(f"  000300 5m 失败: {type(e).__name__} {market._sanitize_error(e)}")

    print("[5] 北交所覆盖 (833533)")
    try:
        _brief(ak.stock_zh_a_hist(symbol="833533", period="daily",
                                  start_date=start, end_date="20991231", adjust=""),
               "833533 日K")
    except Exception as e:
        print(f"  833533 日K 失败: {type(e).__name__} {market._sanitize_error(e)}")
    try:
        _brief(ak.stock_zh_a_hist_min_em(symbol="833533", period="5", adjust=""),
               "833533 5m")
    except Exception as e:
        print(f"  833533 5m 失败: {type(e).__name__} {market._sanitize_error(e)}")

    print("[6] volume 单位交叉验证 (同一天 akshare vs 麦蕊)")
    last_day = None
    for sym, ak_sym, fetch_ak, fetch_mr in (
        ("600519.SH", "600519",
         lambda: ak.stock_zh_a_hist(symbol="600519", period="daily", start_date=start,
                                    end_date="20991231", adjust=""),
         lambda: market.get_mr().stock_history("600519.SH", "d", "n", lt=5)),
        ("510300.SH", "510300",
         lambda: ak.fund_etf_hist_em(symbol="510300", period="daily", start_date=start,
                                     end_date="20991231", adjust=""),
         lambda: market._fetch_fund_kline("510300.SH", "1d", 5)),
    ):
        try:
            ak_df = fetch_ak()
            ak_df = ak_df.rename(columns={"日期": "trade_date", "成交量": "volume"})
            ak_df["trade_date"] = pd.to_datetime(ak_df["trade_date"]).dt.strftime("%Y-%m-%d")
            ak_last = ak_df.iloc[-1]
            day = str(ak_last["trade_date"])
            mr_rows = fetch_mr()
            mr_df = pd.DataFrame(mr_rows).rename(columns={
                "o": "open", "h": "high", "l": "low", "c": "close",
                "a": "amount", "v": "volume", "t": "trade_date",
            }) if isinstance(mr_rows, list) else None
            if mr_df is None or len(mr_df) == 0:
                print(f"  {sym}: 麦蕊无数据, 跳过")
                continue
            mr_day = mr_df[mr_df["trade_date"] == day]
            if len(mr_day) == 0:
                print(f"  {sym}: 麦蕊无 {day} 数据, 跳过")
                continue
            ak_v, mr_v = float(ak_last["volume"]), float(mr_day.iloc[-1]["volume"])
            ratio = ak_v / mr_v if mr_v else None
            print(f"  {sym} {day}: akshare={ak_v:,.0f} 麦蕊={mr_v:,.0f} "
                  f"比值={ratio:.4f}" if ratio else f"  {sym}: 麦蕊 volume=0")
        except Exception as e:
            print(f"  {sym} 交叉验证失败: {type(e).__name__} {market._sanitize_error(e)}")

    print("\n判读: 比值≈1.0 单位一致; ≈0.01 = akshare 为「手」麦蕊基金为「股」。\n"
          "列名映射基准: 日期/时间→trade_date/trade_time, 开盘/最高/最低/收盘→\n"
          "open/high/low/close, 成交量→volume, 成交额→amount。\n"
          "2026-09-05 实测结论:\n"
          "- stock_zh_a_hist 日/周/月K 列名 [日期,股票代码,开盘,收盘,最高,最低,\n"
          "  成交量,成交额,...]; 与麦蕊同日逐位一致 (600519 0904: 45416 手 /\n"
          "  6,022,594,729 元) -> 股票日K兜底无单位换算。\n"
          "- fund_etf_hist_em 日K: 510300 0904 volume=8,414,655 (手) vs 麦蕊\n"
          "  jj/lskx 841,465,543 (股) -> 恰差 100 倍。AkshareSource 基金日K\n"
          "  volume ×100 对齐麦蕊'股'口径, 保证今日bar合成 (_daily_bar_from_quote\n"
          "  的 ETF ×100) 与主图跨源一致。\n"
          "- stock_zh_a_hist_min_em 5m: 1488 行(东财上限), 数据到 2026-09-04\n"
          "  15:00 (当前!); 1m 走 trends2 ndays=5 (源码确认近 5 个交易日)。\n"
          "- 东财 push2his 连续请求后会限流 (RemoteDisconnected/ProxyError),\n"
          "  指数/北交所/ETF分钟三项未跑通, 运行时若失败回退链自动下沉;\n"
          "  AkshareSource 需在配置为默认主源时才承受高频, 兜底频率无虞。\n")


if __name__ == "__main__":
    main()
