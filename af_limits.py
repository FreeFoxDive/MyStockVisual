"""AlphaFeed Pro 套餐限额 (单一来源) 与取数预算折算。

限额表 (Pro 套餐, 2026-09 确认):

| 接口 | 方式 | 限额 | 单次上限 |
|---|---|---|---|
| 实时快照 | 按标的 | 120/min | 100 标的/次 |
| 实时快照 | 标的池 | 60/min | — |
| 日K线 | 批量 | 60/min | 100 标的/次 |
| 日K线 | 按标的 | 120/min | 1 标的/次 |
| 分钟K线 | 批量 | 30/min | 100 标的/次 |
| 分钟K线 | 按标的 | 60/min | 1 标的/次 (5000 条 / 365 天) |
| 日内走势 | 按标的 | 60/min | 1 标的/次 |
| 盘口数据 | 批量 | 30/min | 100 标的/次 |
| 盘口数据 | 按标的 | 60/min | 1 标的/次 |
| 复权因子 | 按标的 | 60/min | 100 标的/次 |

关键口径: visual 的单只日K也走 ``af.klines.batch([symbol])`` (见 ``market._fetch_af_kline``),
即**单只与全市场共用「日K批量查询 60/min」这一份额度**。选股全市场拉K (第二批起按
100 只/批) 因此统一按 ``bucket_rate("kline_daily_batch")`` = 90% × 60 = 54/min 取令牌,
留 10% 给图表/监控链路; 实测见 ``docs/alphafeed-limits.md``。

**不要**因为这张表去放大既有令牌桶 (``feed.QUOTES_RATE_PER_MIN``=6、
``feed.DEPTH_GET_RATE_PER_MIN``=30 等): 它们是网页链路在总额度里的分配,
放大等于从监控/图表手里抢配额。新增取数路径才用本模块折算。
"""
from __future__ import annotations

# 各接口的 Pro 限额 (次/分钟)
AF_PRO_LIMITS = {
    "quotes_symbol": 120,       # 实时快照·按标的
    "quotes_universe": 60,      # 实时快照·标的池
    "kline_daily_batch": 60,    # 日K线·批量
    "kline_daily_symbol": 120,  # 日K线·按标的
    "kline_minute_batch": 30,   # 分钟K线·批量
    "kline_minute_symbol": 60,  # 分钟K线·按标的
    "intraday_symbol": 60,      # 日内走势·按标的
    "depth_batch": 30,          # 盘口·批量
    "depth_symbol": 60,         # 盘口·按标的
    "adj_factor_symbol": 60,    # 复权因子·按标的
}

# 单次请求的标的数上限 (批量接口)
BATCH_SIZE = 100

# 新增取数路径默认只吃 90% 额度, 留 10% 给网页/监控/告警链路
RESERVE_RATIO = 0.9


def limit(name: str) -> int:
    """该接口的 Pro 限额 (次/分钟)。未知名字抛 KeyError (拼错要立刻暴露)。"""
    return AF_PRO_LIMITS[name]


def bucket_rate(name: str, ratio: float = RESERVE_RATIO) -> int:
    """该接口按 ratio 折算的令牌桶速率 (次/分钟, 至少 1)。

    ratio 默认 0.9; 传 1.0 表示吃满额度 (仅限探测/离线任务显式选择)。
    """
    if ratio <= 0:
        raise ValueError("ratio 必须大于 0")
    return max(1, int(AF_PRO_LIMITS[name] * ratio))


def bucket_interval(name: str, ratio: float = RESERVE_RATIO) -> float:
    """两次请求之间的最小间隔秒数 (令牌桶速率换算, 便于串行任务直接 sleep)。"""
    return 60.0 / bucket_rate(name, ratio)


def describe() -> str:
    """限额表 → 一段可打印文本 (探测脚本/日志用)。"""
    rows = [f"  {name:<20} {per_min:>4}/min  (90% → {bucket_rate(name):>3}/min)"
            for name, per_min in sorted(AF_PRO_LIMITS.items())]
    return "\n".join([f"AlphaFeed Pro 限额 (批量单次上限 {BATCH_SIZE} 标的):"] + rows)
