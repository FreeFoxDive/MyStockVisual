# AlphaFeed Pro 套餐限额与实测口径

限额的**代码单一来源**是 [`visual/af_limits.py`](../af_limits.py)（`AF_PRO_LIMITS` +
`bucket_rate()`）；本文件记录口径、各路径的额度分配与实测数据。

## 限额表（Pro，2026-09 确认）

| 接口 | 方式 | 限额 | 单次上限 |
|---|---|---|---|
| 实时快照 | 按标的 | 120/min | 100 标的/次 |
| 实时快照 | 标的池 | 60/min | — |
| 日K线 | 批量 | 60/min | 100 标的/次 |
| 日K线 | 按标的 | 120/min | 1 标的/次 |
| 分钟K线 | 批量 | 30/min | 100 标的/次 |
| 分钟K线 | 按标的 | 60/min | 1 标的/次（5000 条 K 线 / 365 天历史） |
| 日内走势 | 按标的 | 60/min | 1 标的/次 |
| 盘口数据 | 批量 | 30/min | 100 标的/次 |
| 盘口数据 | 按标的 | 60/min | 1 标的/次 |
| 复权因子 | 按标的 | 60/min | 100 标的/次 |

**关键口径**：visual 的单只日K也走批量接口 —— `market._fetch_af_kline` 调的是
`af.klines.batch([symbol], ...)`。所以「日K·按标的 120/min」这一档在本项目里用不到，
**单只与全市场共用「日K·批量 60/min」这一份额度**。

## visual 各路径的额度分配

| 路径 | 使用的接口 | 令牌桶 | 说明 |
|---|---|---|---|
| 选股全市场拉K / 因子库构建 | 日K·批量 | **54/min**（60 × 90%） | `api/screener._scan_bucket`、第二批的因子库构建；100 只/批 |
| 图表日/周/月K | 日K·批量 | 无独立桶（走磁盘缓存 TTL 60~600s） | 用户点击驱动，天然低频 |
| **分时图 `/api/intraday`** | **日内走势·按标的** | **54/min**（60 × 90%） | 默认链 `alphafeed_intraday`（不兜 akshare）；权限不可用当日熔断退回分钟K批量 |
| 监控补种 `feed.seed_intraday` | 日内走势·批量（逐只） | 无独立桶 | 只为空序列补种，与分时图共用同一份**按市场**的当日熔断 |
| 分钟K视图（1m/5m/15m/30m/60m） | 分钟K·批量 | 无独立桶 | 跨天历史（1m 要 1200 根 ≈ 5 个交易日），**不**走日内走势 |
| 监控快照 | 实时快照·按标的 | 6/min | `feed.QUOTES_RATE_PER_MIN`，自己收紧的预算 |
| 网页快照 SSE | 实时快照·按标的 | 48/min | `QUOTE_SSE_INTERVAL=1.25s`，无突发 |
| 五档盘口 | 盘口·按标的（`depth.get` 模拟 batch） | 30/min | `feed.DEPTH_GET_RATE_PER_MIN`，实测 31 次触发限流 |
| 麦蕊兜底 | 麦蕊 | 20/min | `MR_QUOTE_RATE_PER_MIN` |

**既有令牌桶一律不因上限表放大**：它们是同一份额度里的分配，放大等于从监控/图表手里
抢配额。新增取数路径统一用 `af_limits.bucket_rate(name)`（默认 ×0.9，留 10% 余量）。

## 实测（2026-09-18，`probe_feed.py --only-kline`）

```
# 持续压在目标速率上（50 只/批, count=5, 60 批）
批 60/60     66.3s  实测 54.3 批/min  有效 2998 空 2 错 0 限流 0
[OK] 无 429; 实测 54.3 批/min ≤ 文档 60/min (目标 54/min, 余量 5.7)

# 全市场彩排（100 只/批, count=130, 79 批 = 7843 只）
批 79/79     87.0s  实测 54.5 批/min  有效 7221 空 622 错 0 限流 0
单批耗时 min=0.40s median=1.16s max=2.08s
[OK] 无 429; 实测 54.5 批/min ≤ 文档 60/min (目标 54/min, 余量 5.5); 单批 100 只可用
```

结论：

1. **54/min（90%）可持续跑，无 429** —— 令牌桶按 60 × 0.9 取令牌是安全的；
2. 全市场 7843 只按 100 只/批 = 79 批，**约 87 秒**拉完（第二批 18:00 因子库构建的时间就是它）；
3. 单批耗时随标的数增长（100 只 ≈ 1.16s 中位），所以 100 只/批时**吞吐受延迟限制**
   （≈54 批/min），令牌桶刚好不成为瓶颈；把批次调小反而会被桶卡住（这正是 50 只/批
   那轮能精确压到 54.3/min 的原因）；
4. **单次上限比文档宽**：150 只/次可正常返回（1.6~3.1s）。仍按 100 只/批实现，留余量；
5. 空数据率约 8%（622/7843），集中在列表尾部的场内基金（LOF/封闭式等）—— 因子库构建
   必须把「有效/缺失」分开计数并允许个别标的长期无数据。

复跑命令：

```bash
venv/Scripts/python.exe -u visual/probe_feed.py --only-kline --kline-batches 5     # 500 只, 约 6s
venv/Scripts/python.exe -u visual/probe_feed.py --only-kline --kline-batches 60 --kline-chunk 50 --kline-count 5  # 压满 54/min
venv/Scripts/python.exe -u visual/probe_feed.py --only-kline --kline-batches 0     # 全市场彩排 (≈87s, 79 次额度)
```

`probe_feed.py` 不带 `--kline-batches` 时跳过这一节（默认不烧额度）。

## 实测：日内走势 vs 分钟K批量（2026-09-18，`probe_feed.py --only-intraday`）

分时图原来走的是**分钟K批量**（`af.klines.batch([sym], period='1m', count=240,
adjust='forward')`，再按当日截断）。Pro 开放日内走势接口后改为一句话：**优先
`/v1/klines/intraday`，权限不可用则当日退回原接口**（`market._fetch_intraday_kline`）。
两个接口在同一天实测的差异：

```
=== 8. 日内走势 /v1/klines/intraday vs 分钟K批量 (1m × 240 根) ===
       当日已过交易分钟 240/240 | 现在(北京) 2026-09-18 23:17:23
  ── 600519.SH ──
  [OK ] klines.intraday   240 根  2026-09-18 09:31:00 → 2026-09-18 15:00:00  (1 个交易日)  1.11s
       cols=['symbol','name','timestamp','trade_date','trade_time','open','high','low','close','volume','amount']
  [OK ] klines.batch(1m, forward)   240 根  2026-09-18 09:31:00 → 2026-09-18 15:00:00  (1 个交易日)  0.41s
       数值对照: 重叠 240 根  open/high/low/close/volume/amount 最大差均为 0
       相位: 首根 09:31 → 09:31 末相位 (09:31-11:30 + 13:01-15:00)
       末根 15:00 close=1257.1200 vol=1938 | 快照 last=1257.12 (差 +0.0000)
  ── 000001.SZ / 510300.SH / 000001.SH ──  同上 (股票 / ETF / 指数都支持)
  ── 00700.HK / AAPL.US ──  两个接口都 403 (见下方「权限按功能 + 市场分别授权」)
  [小结]
       耗时中位数: intraday 0.6s vs batch 0.5s (A股)
```

结论：

1. **数值完全一致**：重叠 240 根，`open/high/low/close/volume/amount` 逐列最大差 0。
   日内走势接口没有 `adjust` 参数，但**当日前复权与未复权本来就相同**，所以分时图口径
   不变 —— 换接口换的是额度账本，不是数据。
2. **额度独立**：日内走势 60/min，与「分钟K批量 30/min」不是同一份额度。分时图 60s
   轮询因此不再和 1m/5m/15m/30m/60m 分钟K视图抢配额（`market._intraday_budget` =
   60 × 90% = 54/min）；分钟K视图本身**仍走批量接口**（它们要跨天历史，日内走势只回当日）。
3. **只回当日**：返回 `trade_date` 只有一个交易日，`09:31 ~ 15:00` 恰好 240 根
   （**末相位**：上午 09:31–11:30 + 下午 13:01–15:00）。前端分时固定窗口的 240 槽就是照
   这个相位生成的（`index.html` `intradaySessionSlots`，同时兼容 09:30 起相位）。
4. **末根即最新**：盘后末根 close 与快照 last_price 一致（股票差 0.0000）；SDK 文档口径
   是「交易时段内持续更新」，即盘中末根就是**正在走**的那一分钟。
5. **品种覆盖**：股票 / ETF / 指数都正常返回（各 240 根），列里带 `amount`（均价线要用）。
6. **权限被拒当日不重试，且按市场记**：403/401/402（或 code 文案提示）记为该市场当日熔断
   （`visual/af_intraday.py`），之后该市场的分时图与监控补种都直接走分钟K批量，不再反复撞权限；
   其它失败（超时/空数据/额度桶空/429）只本次回退，下一次仍会试 —— 偶发失败不该把一整天
   降级。跨日自动恢复。

### 权限按功能 + 市场分别授权（2026-09-18 实测）

AlphaFeed 的 403 文案直接点明了授权粒度 —— `No permission for 日内分时查询 (markets: US)`。
本套餐实测：

| 标的 | `/v1/klines/intraday` | `/v1/klines/batch`（1m） |
|---|---|---|
| 600519.SH（A股） | ✅ 200，240 根，与批量逐列差 0 | ✅ 200 |
| 00700.HK（港股） | ❌ 403 日内分时查询 (markets: HK) | ❌ 403 `Access mode 'batch' not available for 日/周/月K线查询` |
| AAPL.US（美股） | ❌ 403 日内分时查询 (markets: US) | ❌ 403 同上 |

两点结论：

1. **港/美股分时在 AF 上两个接口都拿不到**，而分时默认链是 `alphafeed_intraday`（**不**兜 akshare：
   实测其分钟数据不稳、东财限流期可能整段失败，而且是静默换供应商）——所以**港/美股分时在默认
   配置下直接报无数据**（这与 known-issues 里「港/美股功能暂缓」是同一件事）。需要老行为就显式配
   `KLINE_SOURCE_INTRADAY=alphafeed_intraday,akshare`。复跑：
   `probe_feed.py --only-intraday --intraday-symbols 00700.HK,AAPL.US`。

2. 正因为授权按市场分，**熔断必须按市场记**：早先写成全局熔断，自选表里只要带一只美股
   （403 是常态）就会把 **A股** 的日内走势偏好一起关掉、当天全部退回分钟K批量 —— 反而把
   这份独立额度让出去了。现在 `af_intraday` 按 cn/hk/us 分别记，A股 不受港/美股影响
   （`test_intraday_source.py::test_latch_is_per_market` 钉住这条）；监控补种的
   `intraday_batch` / 兜底 `klines.batch` 也都**按市场分组**发请求，一只没权限的港/美股
   不会把整批（含 A股）一起 403 掉（`test_seed_mixed_markets_does_not_blame_cn`）。

### 能力缺口不计源故障（`SourceSkip`）

分时默认链只有 `alphafeed_intraday` **一个**源，而源健康计数是按**源名**记、与市场无关：
只要"取不到数据"就 `_note_fail`，累计 3 次整源冷却 60s（冷却结束后第一次尝试是 probe，
**一次失败就立刻再冷却**）。于是港/美股 403（常态）或任意一个当日没有数据的代码，都能
把整个源压在冷态 —— 冷却期内路由跳过整个源，**全站 A股 分时一起 404**，且这是进程级共享
状态、前端每 60s 自动刷新，等于把独立额度变成了单点。

修法（`kline_source.SourceSkip`，与既有 `SourceBusy` 同理）：源可以明确声明"这个市场/标的
我服务不了"，路由据此**不记失败**、直接下沉下一源。判据在 `market._fetch_intraday_kline`：

| 情形 | 处置 |
|---|---|
| 本市场当日已被判定无权限（403 熔断过） | `SourceSkip`（能力缺口） |
| 上游**答复了**（HTTP 200）但两个接口都没数据 | `SourceSkip`（这个标的没数据） |
| 上游异常 / 超时（没答复） | 返回 `None` → 记源故障，该冷却照旧冷却 |
| A股 权限被拒但原接口有数据 | 正常返回（「权限有问题就退原来的接口」不变） |

`test_kline_source.py::test_source_skip_is_not_counted_as_failure` 连造 4 次 SourceSkip 后
断言源**没有**进冷却；`test_upstream_error_is_a_real_failure` 钉住真故障仍然计失败。

### 熔断/退避会推通知（每源每天一条）

源熔断原来只在日志里，没人盯日志等于没发生；而"冷却 → probe 失败 → 再冷却"能反复触发，
所以配了**每个源每天最多一条**的告警（`visual/source_alert.py` 的当日闸门，按源名记、
跨日自动恢复；`SOURCE_ALERT_DISABLED=1` 可关）：

- **K 线源熔断**：`kline_source._note_fail` 的 `if entered:` 处推一条，正文带最后失败的那次
  请求（`symbol period (category)`，由 `_try_source` 传入）。
- **麦蕊 429 退避**：`market._mr_note_429` 处推一条；detail 由 SDK 路径给"方法名 + 标的"、
  直连路径给**去掉 licence 段**的 URL（证书 key 绝不进通知；SDK 路径只在首参是纯代码形态时
  才带上）。
- 麦蕊的 429 与连续失败**共用同一条当日配额**（闸门 key 是源名，`_gate_key()` 一行可拆）。
- 发送走 `error_notify.notify_alert`（同一队列/工作线程/每源 5 分钟聚合窗口/全局 5 条每分钟
  预算 + `redact_message` 脱敏），未配置的通道自动跳过 —— 告警绝不落在取数请求线程上。
- 测试：`test_source_alert.py`（闸门语义）、`test_error_notify.py::NotifyAlertTest`（通道）、
  `test_kline_source.py::test_cooldown_notifies_once_per_source_per_day`、
  `test_market_backoff.py::BackoffAlertTest`。全部离线打桩，不发真实通知 —— 触发熔断的测试
  基类会打桩 `source_alert.notify`（单文件单跑兜底），`test_source_alert.py` 还在模块级把
  `SOURCE_ALERT_DISABLED` 默认置 1（`unittest discover` 先 import 完所有模块再跑，覆盖整轮）。
  改这条链路时请保留这两层，否则跑一次测试就会往真实群里发消息。

### 那 intraday 到底比 K 线强在哪

厂商文档口径：K 线接口「分钟级（1m/5m/15m/30m/60m）需要分钟K线权限」，日内分时是**另一项功能**
（独立的 403 文案与独立限额）；FAQ 又写「K线数据盘中实时更新」——所以**盘后两者数据一致是预期的**，
不是接口没用。差别落在：① 独立授权（按功能 × 市场卖）；② 独立额度（60/min vs 30/min）；
③ 定位不同（日内分时只回当日、无 `adjust`/`start_time`，是给实时分时/盘口页的当日视图；
K 线是带 `start_time`、`count` 上限 10000 的历史存储查询）。

顺带说明：**本次 A/B 都在盘后做的**（当天 23:17 与 23:45），所以验的是「数据口径等价」；
盘中「末根是不是正在走的那一分钟」只有 SDK 文档与盘后末根对齐快照两条旁证。要钉死这一点，
在 A股 盘中（09:31–15:00）跑一次 `probe_feed.py --only-intraday` 即可 —— 探测脚本会打出
标的所在市场的当前墙钟，并判定末根是不是当前分钟。

注意：本节是**盘后**实测，盘中「末根是进行中那根」来自 SDK 文档口径 + 盘后末根对齐快照
这两条旁证；开盘前如果返回空，分时图会自动退回批量接口（那条路带回前一日的尾巴，路由再
截当日，于是 09:31 也能画出一根 bar）。

复跑命令（4 只 × 2 个接口 ≈ 15s，不跑 1~6 节）：

```bash
venv/Scripts/python.exe -u visual/probe_feed.py --only-intraday
venv/Scripts/python.exe -u visual/probe_feed.py --only-intraday --intraday-symbols 600519.SH,510300.SH
```
