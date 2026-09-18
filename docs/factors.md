# 因子库 (每日全市场因子) 设计与运维

模块 `visual/factors.py`，状态接口 `GET /api/factors/status`，管理员面板 `/admin.html#factors`。
选股扫描读的就是它 —— 因子库缺失时选股会返回 503（不静默退回逐只拉K）。

## 为什么需要它

原来选股逐只拉日K（300 只 ≈50 分钟）且每次重算。因子库把「全市场拉K + 因子计算」搬到
**交易日 18:00 一次**完成：

- 取数：AlphaFeed 日K批量（100 只/批，令牌 = 日K批量额度的 90% = 54/min，见
  [alphafeed-limits.md](alphafeed-limits.md)）→ 全市场 7843 只 **79 批 ≈87 秒**（实测）；
- 计算：逐只算技术/量价/筹码因子（复用 `indicators.compute_all_indicators`，与图表同口径）
  ≈1 分钟；
- 外部快照：基本面/资金/筹码 ≈30~60 秒（受各数据源响应时间影响）。

之后扫描只读快照 → **秒级**，同一天同条件连"再扫一次"都不做（结果去重，见
[screener.md](screener.md)）。

## 存储

| 位置 | 内容 |
|---|---|
| `.cache/factors.db` | SQLite：`bars`（每只标的日K blob）/ `meta`（流通/总股本，7 天刷新）/ `builds`（构建记录 = 页面进度与最近 5 交易日） |
| `.cache/factors/snapshot_<交易日>.pkl.gz` | 当日全市场因子表（扫描直接读它，内存按 mtime 缓存） |

`bars` 存 numpy blob（dates int32 天 + ohlcv float64），供 `live` 盘中口径重算与后续
加指标用；快照是列式 DataFrame，`symbol` 为主键。

## 调度（只在交易日）

```
每 5 分钟巡检:  is_trading_day(now)? 且 now >= FACTORS_BUILD_AT(18:00) ?
                 ├─ 否 → 什么都不做 (非交易日、未到点都不构建、不通知)
                 └─ 是 → 目标日 = 今天
                          已有快照/已成功 → 跳过
                          否则构建 (失败后 30 分钟内不再重试, 最多 3 次)
```

**构建只允许在 18:00 之后开始**（`due_day()` 未到点返回 `None`，`before_build_at()` 也是
`build()` 的门）。两条原因：

1. 15:00 收盘后数据还没齐 —— 龙虎榜要收盘后才发布、质押 15:30 才刷新，提前构建会让当天
   快照永久缺这些列（18:00 又因 already_done 不再构建）；
2. 明确窗口后，"今天的数据什么时候可用"只有一个答案。

`due_day()` 与 `last_closed_trading_day()` 的区别：后者在交易日 15:00 后就返回"今天"
（它是"最近已收盘交易日"的语义，用于历史/状态展示），**不能**当作构建门控。

- **不补建**：晚间进程不在（漏跑）时不做盘中补建，顺延到下一个交易日 18:00 构建当天；
  缺的那天在「最近 5 个交易日」表里显示为「未构建」。这是刻意的：补建会把"哪天的数据"
  和"什么时候跑"两个维度搅在一起，且盘中补出来的快照必然缺收盘后才有的事件数据。
- **失败退避**：失败后 `RETRY_MIN_SEC`（30 分钟）内不再重试，最多 `MAX_ATTEMPTS`(3) 次；
  用尽则当日不再试并通知（次日 18:00 重新开始）。退避对 `error` 与 `running` 同样生效。
- 幂等：目标日已有 `done` 记录或已有快照时不再构建（「强制重建」可覆盖）。
- 手动：`python -u visual/factors.py --build [--force] [--day YYYY-MM-DD]`，或管理员在
  `/admin.html` 点「手动重建 / 强制重建」。**未到 18:00 的非强制手动构建同样被拒绝**
  （提示「因子库只在 18:00 后构建」）；确需立即跑用 **force**（显式的人类决定，用于
  修复当日或历史缺口，`--day` 可指定交易日）。开不了跑时接口**如实回报原因**
  （未到点 / 已在跑 / 已完成 / 重试用尽），不会假装已触发。

## 通知（开始 / 完成 / 异常）

三类都写**管理员**的 `monitor_alerts`（`factors_build_started/done/error`，监控中心
「最近告警」可见）并推钉钉/ntfy（走 `notify.py` 的有序非阻塞队列，先落库再入队）：

- 开始：`因子库更新开始 2026-09-18`（含第几次尝试）
- 完成：`X 只入因子表 (拉取 Y 只, 缺失 Z), 用时 Ns`
- 异常：`第 N 次失败: <脱敏归类文案>`（第 3 次用尽后不再重试）

错误文案统一走 `logger.sanitize_error`（脱敏 + 限流归类），**上游异常原文只进日志**：
不入 `builds.error`、不进推送、`/api/factors/status` 对非管理员只给
「构建失败（详情见服务端日志）」。

## 页面可见

- `/admin.html#factors`：状态行 + 进度条 + **最近 5 个交易日**完成情况表
  （交易日/状态/入表/拉取/缺失/进度/完成时间/异常），构建中 5 秒刷新、平时 60 秒。
- `/screener.html` 顶部状态条：`因子库 2026-09-18 · 7221 只` / `构建中 45%（阶段 bars）` /
  红色异常提示。因子库缺失时提示「交易日 18:00 自动更新，或由管理员手动重建」。

## 数据源与降级

外部源逐个 try/except，**任何一个挂掉都不中断构建**，状态写进 `builds.sources`
（页面与状态接口都能看到 `ok:行数` / `fail:异常类型`）。当前口径：

| 列 | 主源 | 降级 |
|---|---|---|
| 流通/总市值 | 东财全市场快照 `stock_zh_a_spot_em` | 股本 × 收盘（股本来自 AlphaFeed `instruments.batch`，7 天刷新） |
| PE / PB | 同上（动态口径） | 收盘 / 每股收益、收盘 / 每股净资产（东财业绩报表，报告期口径） |
| ROE / 行业 | 东财业绩报表 `stock_yjbb_em`（最新报告期） | 无 |
| 主力净流入 | 麦蕊 `/higg/zljlr` 全市场快照（`market.mr_zljlr`） | 无 |
| 龙虎榜 | 东财 `stock_lhb_detail_em`（当日名单，上榜者为 True） | 无 |
| 质押比例 | 复用 market 每日质押缓存（15:30 刷新） | 无 |
| ETF 溢价 | 东财 `fund_etf_spot_em`（基金折价率） | 无 |
| 换手率 | 本地：成交量 × 单位换算 / 流通股本 | 无 |
| 筹码获利盘/集中度/平均成本 | 本地 `chips.compute_chips`（同一份日K，不再取数） | 无流通股本时留空 |

已知环境问题：本机（Windows）系统代理会拦掉东财 push2 系（`stock_zh_a_spot_em`），
该源会记 `fail:ProxyError`，市值/PE/PB 自动走降级路径；服务器/Docker 无此代理。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `FACTORS_BUILD_AT` | 18:00 | 交易日构建时刻（北京时间） |
| `FACTORS_BARS` | 150 | 每只标的回看的日K根数（影响 MA60/新高新低等窗口） |
| `SCREENER_KLINE_PER_MIN` | 54 | 拉K令牌速率（日K批量额度的 90%） |
| `FACTORS_LIVE_TTL_SEC` | 600 | 盘中口径（`price_mode=live`）快照的复用窗口；返回版本号带该桶 |

`live_snapshot()` 只在**连续竞价与午休**（`session_phase ∈ {trading, break}`）生效：
集合竞价（09:15-09:30）与盘前拿到的是上一交易日残留快照，拼上去会凭空多一根今日 bar
（量比/振幅/KDJ 全失真）；收盘后因子库本身就是当日收盘口径，也不该再拼。
返回的版本形如 `2026-09-18+live@<时段桶>`，让结果去重随桶滚动而不是整天冻结。

## 测试

```
venv/Scripts/python.exe -u visual/test/test_factors.py            # 因子计算/存储/快照/调度门控/外部源降级/构建
venv/Scripts/python.exe -u visual/test/test_screener_metrics.py   # 条件注册表与判定
venv/Scripts/python.exe -u visual/test/test_screener_api.py       # 含因子库状态/重建路由与读快照扫描
```

全程零网络：行情、外部源、推送一律 mock；存储指向临时目录。
