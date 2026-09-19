# 已知问题 / 数据源缺陷记录

> 记录 `visual` 项目外部数据源导致、且已在应用侧规避的问题，以及尚未处理的遗留项。

---

## 1. 麦蕊股票日K「当日 bar」盘中为滞后/部分成交快照

**状态**：成交校验已修复（2026-09-10）；日K图表遗留（见「遗留待办」）。

### 现象

录入交易时，买入/卖出价落在当日真实振幅内却被误拒。

- 标的：`601058.SH`（赛轮轮胎）
- 日期：`2026-09-10`
- 录入买入价 `14.20`，报错：`买入价 14.20 不在当日振幅 [14.30, 14.64] 内`
- 当日实际最低价 `14.18`

### 根因

股票日K默认源链为 `mairui,alphafeed,akshare`（`kline_source.DEFAULT_CHAINS`），**麦蕊为主源**。
麦蕊返回的当日 bar 是盘中某时刻的部分成交快照，OHLC/成交量均滞后于收盘终值：

| 数据源 | open | high | low | close | volume |
|---|---|---|---|---|---|
| 麦蕊 日K(`none`) | 14.62 | 14.64 | **14.30** | 14.32 | 144462 |
| AlphaFeed 日K(`none`) | 14.62 | 14.64 | **14.18** | 14.22 | 264196 |
| AlphaFeed 实时快照 | 14.62 | 14.64 | **14.18** | 14.22 | 264196 |

`market.get_daily_bar` 当日路径取到麦蕊这条滞后 bar；`_maybe_append_today_bar` 只在
`market_hours.in_session()` 为真（盘中）时才用实时快照覆盖当日 bar。录入发生在收盘后
（`in_session()=False`），于是滞后 bar 被原样返回，`low=14.30` 高于真实最低价，`14.20` 被误判越界。

### 排除除权除息

- 交易所参考价 `prev_close=14.62`，恰等于 09-09 收盘，除息日昨收会被调整为除权参考价。
- 对比近 80 根 AF 日K，前复权(`qfq`)与未复权(`none`)最近一次出现差异是 **2026-06-04**
  （ratio 0.9854，约 1.46% 分红），09-10 及之后两者完全一致 → 09-10 无复权因子变化。
- 麦蕊当日 volume `144462` ≈ 全天 `264196` 的 55%，是典型的盘中部分成交快照。

### 修复（应用侧规避）

`market.get_daily_bar`：当日（`target == today`）**优先实时快照**
（`_daily_bar_from_quote`，自带「交易日 + volume>0」校验），快照失败再退回历史 bar。
历史源当日 bar 不再直接用于当日成交校验。

- 代码：`visual/market.py` `get_daily_bar`
- 回归测试：`visual/test/test_trades.py`
  - `TestGetDailyBarTodayFallback.test_today_prefers_quote_over_history`
  - `TestGetDailyBarTodayFallback.test_today_stale_history_bar_overridden_by_quote`
  - `TestGetDailyBarTodayFallback.test_today_trade_entry_stale_history_passes`

### 图表当日 bar 快照校准（原方案 B，已修复）

日K图表（`/api/kline?period=1d`，前复权）此前只在盘中用快照覆盖当日 bar：收盘后
`_maybe_append_today_bar` 若见源已含今日 bar 便直接返回，于是**收盘后图表当日 K 线仍是
历史源的值**（麦蕊/指数链会滞后，如本例 low=14.30）。

现改为：交易日只要快照可用就覆盖当日 bar（`market._maybe_append_today_bar` 去掉
`last_date == today and not in_session` 早退），图表与成交校验口径一致。守卫：

- 非交易日：`is_trading_day` 早退，且 `_daily_bar_from_quote` 再查一次 → 不拼快照。
- 全天停牌：快照 `volume == 0` → `_daily_bar_from_quote` 返回 None → 保留源 bar，
  不覆盖；成交校验仍按「成交量为0」拒绝。
- `last_date > today`（脏数据）与空 df：原样返回。
- 快照 `amount` 一并透传，当日成交额不再为 NaN。

前复权口径安全：前复权的「今日」即原始实时价，覆盖不破坏序列连续性（除权日亦然）。

- 代码：`visual/market.py`（`_maybe_append_today_bar`、`_daily_bar_from_quote`、
  `_apply_quote_bar_to_df`）
- 测试：`visual/test/test_market_kline.py`
  - `test_refresh_when_history_has_today_after_close`
  - `test_no_override_when_snapshot_unavailable`
  - `test_no_override_on_non_trading_day`
  - `test_reject_stale_quote_timestamp` / `test_no_append_before_open`

**盘前残留（已修复，2026-09-13）**：此前若某源快照盘前仍带昨日 `volume>0`，可能被拼成
“今日”bar。现由两道守卫堵住：

- 时段：`market_hours.session_phase()` 统一判定，`pre`（未开盘）直接早退，不拼当日 bar。
- 时间戳：`market._daily_bar_from_quote` 校验快照 `timestamp` 的交易所日期是否为今天
  （AlphaFeed 原始快照自带 epoch 秒；麦蕊无则退化为时段守卫）。

同日另修：`_strip_today_bar_df` 原用 `not in_session()` 判“收盘终值”，午休时
`in_session()=False` 会把不完整的当日 bar 当终值写进磁盘缓存；改用
`session_phase() == "closed"`。

---

## 1b. 契约：当日 bar 只由后端产出，前端不得合成

**状态**：已确立（2026-09-13）。

**背景**：主页图表曾在 `static/index.html` 的 `patchTodayBarFromQuote` 里按实时快照
自己拼/改当日 bar（`volume>0` + 日期更晚即追加），与后端 `_maybe_append_today_bar`
是两套口径。非交易日快照仍是上一交易日残留（`volume>0`），前端据此凭空多出一根
“今日”bar（如周六出现周六的 K 线）。

**契约**：

- 当日 bar 的 OHLCV **与全部指标**只由后端产出；前端不再派生 K 线数据、不再本地
  重算指标（`static/js/indicators.js` 已删除）。
- 前端通过 `GET /api/kline/tail?symbol=&period=1d&count=<与图表同>&n=2` 取权威末
  N 根（与 `/api/kline` 同一 `fetch_kline_ex` + `compute_all_indicators` 口径），
  按 `date` 合并/追加（`applyServerBars`）；非交易日/盘前后端本就不含“今日”bar，
  合并即 no-op。
- `count` 必须与图表一致，否则 OBV 等全序列指标会漂移。
- 交易日/时段判定只保留 `market_hours.session_phase()` 一处；接口 meta 透传
  `is_trading_day` / `session_phase`。

**护栏测试**：`visual/test/test_kline_tail_api.py`
`test_tail_last_bar_matches_kline_last_bar`（末根与 `/api/kline` 逐字段一致）；
`visual/test/test_daily_tail_js.py`（前端只应用服务端 bar，非交易日不新增）。

---

## 2. 磁盘缓存 key 不含数据源 → 切源后仍命中旧源数据

**状态**：已修复。

### 现象

`KLINE_SOURCE_STOCK` 从 `mairui,alphafeed,akshare` 改为 `alphafeed,akshare` 并重启后，
601058.SH 日K图提示框当日 bar 最低仍显示 `14.30`（麦蕊滞后值），而非 `14.18`。

### 根因

- `market.DiskCache._key` 只拼 `symbol_period_count_adjust`，**不含数据源**。
- `api_routes.kline` 的 `skip_1d_cache` 只跳过内存响应缓存，跳不过 `fetch_kline_ex`
  里的磁盘缓存；收盘后 `_strip_today_bar_df` 会把当日 bar 一并落盘。
- 于是 `.cache/klines/601058.SH_1d_1006_qfq.json.gz`（`source: mairui`）在 TTL 内继续被返回。
- 叠加因素：`.env` 仅启动时加载（`app.py:_load_dotenv`），改配置必须重启，否则仍按旧链取数。

### 修复

磁盘缓存 key 纳入当前类别数据源链标识：

- `kline_source.chain_tag(category)` = `_chain(category)` 的 8 位 sha1（改链即变）。
- `market.DiskCache.get/set/_key` 增加 `chain_tag` 参数，`fetch_kline_ex` 传入。
- 依据「配置的链」而非实际服务源：实际源要拉完才知道；改配置正是需要失效缓存的场景。
  同链内主源故障回退到次源时仍复用缓存（TTL 短，可接受）。
- 文件名形如 `601058.SH_1d_1006_qfq_<chain_tag>.json.gz`；旧命名文件成为孤儿，
  由 `DiskCache.cleanup()` 判定 >24h 后自动删除。

- 代码：`visual/kline_source.py`（`chain_tag`）、`visual/market.py`（`DiskCache`、`fetch_kline_ex`）
- 测试：`visual/test/test_kline_source.py`
  - `TestChainTag.test_chain_tag_reflects_env_and_is_stable`
  - `TestChainTag.test_disk_cache_key_includes_chain_tag`
  - `TestChainTag.test_disk_cache_isolates_across_chain_tag`
  - `TestFetchKlineEx.test_chain_tag_change_invalidates_cache`

### 运维提示

改 `KLINE_SOURCE_*` 后仍需**重启服务**（`.env` 仅启动时加载）；重启后旧缓存 key 自然不再命中，
无需手动删 `.cache/klines`。

---

## 3. 股票周/月K被 AlphaFeed 适配器挡掉 → 只剩抖动的 akshare → 间歇 404

**状态**：已修复（2026-09-10）。

### 现象

切换到周K（`/api/kline?period=1w`）时提示
`❌ 获取数据失败: 无法获取 688617.SH 的K线数据`（HTTP 404）。

### 根因

- 股票链为 `KLINE_SOURCE_STOCK=alphafeed,akshare`（`.env`；麦蕊因问题 1 被移出）。
- `AlphaFeedSource.supports` 只放行 `period == "1d"`，周/月K被跳过 —— 尽管 AlphaFeed
  SDK 原生支持 `1w`/`1M`（README 亦如此标注）。
- 于是周K实际只剩 akshare（东财）单源，其接口间歇失败（实测 5 次约 2 次
  `RemoteDisconnected`；本机系统代理 `127.0.0.1:10808` 会放大抖动）。
- `kline_source.fetch_kline_df` 每源只尝试一次，akshare 那次抖动即返回 `(None, None)` → 404。

### 修复

放行 AlphaFeed 原生周/月K，使其成为股票/ETF 周月K主源：

- `kline_source.AlphaFeedSource.supports`：`category in ("stock","fund")` 且
  `period in ("1d","1w","1M")`。
- `market._fetch_af_daily_kline` 泛化为 `_fetch_af_kline(symbol, period, count, adjust)`，
  `af.klines.batch(..., period=period, ...)`；ETF 溢价等日K调用显式传 `"1d"` 保持原行为。
- 日K仍以 AlphaFeed 为首源，不影响问题 1 的规避。

- 代码：`visual/kline_source.py`、`visual/market.py`、`visual/api_routes.py`
- 测试：`visual/test/test_kline_source.py`
  - `TestDefaultRouting.test_stock_weekly_defaults_to_alphafeed`
  - 原 `_fetch_af_daily_kline` mock/断言同步更新为 `_fetch_af_kline(symbol, period, ...)`


## 4. 东财 push2his（个股资金流历史）在部分网络整段不可达

**状态**：已修复（应用侧改走可达主机回退）。

### 现象

「市场数据 → 资金流」恒显示 `加载失败: 数据源暂时不可用, 请稍后重试`，与标的无关。

### 根因

数据源 `akshare.stock_individual_fund_flow` 硬编码 `push2his.eastmoney.com`。实测本网络该主机
连续 6/6 全部 `ConnectionError: RemoteDisconnected`（对端直接断开连接，无响应）：

| 主机 | IP | 结果 |
|---|---|---|
| push2his.eastmoney.com | 103.220.167.80 | 0/6 |
| push2.eastmoney.com | 61.129.129.196 | 0/6 |
| 1.push2his.eastmoney.com | 101.42.128.173 | 失败 |
| 82.push2.eastmoney.com | 47.112.165.11 | 失败 |
| **push2delay.eastmoney.com** | 101.226.30.136 | **6/6 可用** |
| datacenter-web.eastmoney.com | 202.168.181.147 | 6/6 可用 |

无代理配置（`requests.getproxies()` 为空），故非代理问题，而是 push2his/push2 这批边缘 IP 被网络阻断。
`api/cn_data._retry` 重试 3 次后仍失败 → 返回笼统的「数据源暂时不可用」，前端只看到这一句。

### 修复（应用侧规避）

`api/cn_data._fetch_fund_flow` 不再走 akshare，改为自己请求东财，按 `push2his → push2delay` 顺序尝试：

- `push2delay` 与 akshare 的行格式完全一致（15 字段/行），但**无论 `lmt` 传多少只返回最新一个交易日**，
  因此仅作降级源；命中降级时响应带 `hint`：「历史接口 (push2his) 当前不可达, 仅显示最新一个交易日」。
- 东财净额字段单位是「元」，界面列名标的是「万」，先前未换算（差 10⁴）；现在统一换算为万元。
- `_cached` 的失败文案改为附带脱敏截断后的真实原因：`数据源暂时不可用 (<原因>)`。

- 代码：`visual/api/cn_data.py`（`_fetch_fund_flow`、`cn_fund_flow`、`_cached`）
- 测试：`visual/test/test_cn_data_api.py`
  - `test_fund_flow_falls_back_to_delay_host`（回退 + 元→万元 + 主机顺序）
  - `test_fund_flow_both_hosts_fail_returns_error`（文案带真实原因）
  - `test_fund_flow_maps_and_caches`（映射/缓存/参数）

### 运维提示

若所在网络放行 push2his，则自动恢复全量历史，无需改配置；降级提示只在回退时出现。


## 5. change_pct 单位不一致 → 「当日涨跌幅%」预警两条路径都失效

**状态**：已修复（2026-09-15，AF 官方文档确认 `ext.change_pct` 为小数，如 0.01 表示 1%）。

### 现象

价格预警中「当日涨跌幅」条件（如 `<= -2` 表示跌 2%）从不触发；而页头涨跌幅显示正常 —— 该 bug
因此长期未被发现。

### 根因

标准 quote 约定 `change_pct` 为百分数（麦蕊 `pc` 即百分数），但 AlphaFeed 官方返回**小数**。
`feed._row_to_quote` 作为 AF 行情唯一出口原样透传小数，而 `_af_quote_to_std` 只对
amplitude/turnover_rate 做了 ×100（`_af_pct`），唯独漏了 change_pct。后果按路径分：

| 路径 | change_pct 实际值 | 预警比较 | 结果 |
|---|---|---|---|
| AF 主路径（monitor `RestFeed.quotes`） | 小数：跌 5% 时为 `-0.05` | `-0.05 <= -2` 恒 False | 永不触发 |
| 麦蕊回退路径 | 原实现刻意置 `None`（"避免差 100 倍"） | `val is None` 条件不满足 | 同样不触发 |

前端主显示不受影响：`updateQuoteDisplay` 用 `last_price/prev_close` 重算覆盖了原始值；
但初次 K 线加载时 `index.html` 曾直读原始值，会闪现 100 倍偏差。

### 修复

- `feed._row_to_quote`：在唯一出口把 `ext.change_pct` ×100 统一为百分数（None 保持 None），
  monitor 与 market 两条路径同时修正；
- `feed.RestFeed._fallback`：回退源 `market.fetch_quotes` 输出已是百分数，直接透传，不再置 None；
- `market._af_quote_to_std`：直通保持（入口已统一），amplitude/turnover_rate 的 `_af_pct` 不动。

- 测试：`test_market_quote.py::test_row_to_quote_change_pct_decimal_to_pct`（单位契约 + None 透传）、
  `test_af_chain_percent_end_to_end`（AF 原始行 → std 全线百分数）、
  `test_monitor.py::test_quotes_parses_dataframe`（监控路径 0.05 → 5.0）、
  `test_monitor_trendline.py`（回退路径透传断言更新）。


## 6. 健壮性批量修复（2026-09-15 review）

| # | 问题 | 修复 | 测试 |
|---|---|---|---|
| 1 | 麦蕊快照回退无主动限速：AF 未配置/故障时 1.25s 轮询把免费额度在几分钟内打光，只剩被动 429 退避 | `_mr_quote_budget`（`MR_QUOTE_RATE_PER_MIN`，默认 20/min），计数粒度=麦蕊 HTTP 调用，桶空本轮沿用缓存 | `test_market_quote.py::test_mairui_fallback_budget_exhausted_skips` / `test_mairui_fallback_budget_bounds_calls` |
| 2 | kline 磁盘缓存 `DiskCache.set` 直写终态路径，并发写/崩溃留截断 `.json.gz` | 唯一临时文件（`.tmp-<pid>-<hex>`）+ `os.replace`；`screener_last.json` 同模式 | `test_market_caches.py::DiskCacheAtomicWriteTest` |
| 3 | 磁盘缓存清理（24h/50MB）只在进程启动执行，常驻容器永不清理 | 6h `_search_index_scheduler` 循环顺带 `cleanup()`；并清理 `.cache` 根下 >24h 的空 `tmp*` 目录（`cleanup_cache_tmp_dirs`）与 klines 下 >1h 的 `.tmp-*` 残留 | `test_market_caches.py::CleanupTmpDirsTest` |
| 4 | `monitor_alerts` 表 append-only 无限增长 | `prune_monitor_alerts()`（`MONITOR_ALERT_RETENTION_DAYS=365`），monitor 每日首轮调用 | `test_trades.py::test_prune_monitor_alerts_retention` / `test_prune_monitor_alerts_custom_days` |
| 5 | `api/cn_data` 失败无负缓存：上游挂掉时每请求阻塞 3 次重试 ~2.4s | 失败记 `_fail_ts`，`FAIL_TTL=300s` 窗口内直接回退旧数据或报错 | `test_cn_data_api.py::test_negative_cache_skips_retry_within_fail_ttl` / `test_negative_cache_expires_after_fail_ttl` |
| 6 | AF 预算中断时 deferred 集合误伤已请求未返回的标的（典型指数），跳过麦蕊回退一轮 | 只 defer 尚未请求的批次 | `test_market_quote.py`（回退用例回归覆盖） |
| 7 | `sessions` 过期行只在原 token 复现时删除，被遗弃登录无限累积 | `create_session` 顺带 `DELETE FROM sessions WHERE expires_at < ?` | `test_trades.py::test_create_session_sweeps_expired_rows` |
| 8 | 前端 `pctLabel` 等不归一负零，`-0.004` 渲染成 `▼ -0.00%` | 新增 `normPct`（\|v\|<0.005 → 0），应用于 pctLabel/股票信息/溢价/tooltip/canvas 各格式化点 | — |
| 9 | ETF 换手回算在 `amount` 缺失时默认按"股"猜单位，可能差 100 倍 | `amount` 缺失时不回算不显示（单位自证） | — |
| 10 | `quote_cache` TTL 从抓取开始计时，慢上游（~1s）时缓存有效期只剩 ~0.25s | emit 时取当前时间作 TTL 起点 | — |

### 已知取舍（本次不修）

- 港/美股 AF 令牌桶耗尽会清空整轮抓取集（`market.py` `_fetch_quotes_locked` 的 `af_set = []`），
  拖累 A 股快照走麦蕊回退 —— 港/美股功能暂缓，只要盯盘集合不含港/美标的即不触发；
- 港/美股量比沿用 A 股 240 分钟会话折算，数值仅近似；
- 会话令牌明文存 SQLite（见 README 安全节，库文件泄露风险自担）；
- `compute_stats` 全量加载 + 批次交易逐条开连接（个人规模无感）。


## 7. 前端 ECharts 时序：未建 model 就取像素 / 全量重建跨帧

**状态**：已修复（2026-09-16）。回归测试 `visual/test/test_chart_ready_js.py`（用真实 vendored
echarts-5.5.0 复现两个报错，报错文案逐字一致）。

### 现象

主图控制台两条 TypeError：

1. `Cannot read properties of undefined (reading 'queryComponents')`，栈底是
   `priceYMapper → tagColumnCtx → (Vue)`；冷启动/换股时偶发，分时冷启动更严重。
2. `Cannot read properties of undefined (reading 'getRawIndex')`，栈底是
   zrender mousemove → ECharts 事件桥接；鼠标划过 K 线时偶发。

### 根因

ECharts 5.5.0 内部实现（反编译 vendored 包确认）：

- **报错 1**：`ECharts.prototype.convertToPixel` 走 `Fv(this, ...)`，而 `Fv` 只挡了 `_disposed`，
  **没挡 `_model`**。`_model` 在首次 `setOption` 之前是 `undefined`（`getModel()` 返回 undefined），
  于是 `Yo` 里 `t.queryComponents(...)` 抛错。可达路径：
  - 日/周/月K 冷启动：`fetchData` 先写 `STATE.klineData` 再 `await loadTrades(...)`，这个窗口里
    任一快照 tick 都会走 `flush:'sync'` 的 watcher → `updateLivePriceLine` → `convertToPixel`；
  - 分时冷启动：`fetchIntraday` 写 `STATE.intradayData` 后先 `applyQuoteData(quote)`（同步触发同一
    watcher）再 `renderIntraday` —— 抛错被外层 catch 吞掉，**分时图永久白屏且每 tick 复现**。
- **报错 2**：`setOption(option, {notMerge:true, lazyUpdate:true})` 会**同步**换掉 model 与全部
  seriesModel（实测 `before !== after`），但 `lazyUpdate` 把数据管线推迟到下一帧；画布上仍是带旧
  数字 `seriesIndex` 的旧元素。这中间 mousemove 会让 `_initEvents` 的桥接拿旧索引查新 series 的
  `getDataParams` → `getData()` 返回 `undefined` → `n.getRawIndex(t)` 抛错。`zr.flush()` 关不掉这个
  窗口，只有非 lazy 的 `setOption`/`resize` 或跨帧才行。merge 语义的 lazy 补丁（模型实例不变、
  旧数据仍可达）不受影响。

### 修复

两条不变量，改动都在 `static/index.html`：

1. **取像素/取轴之前先探 model**：新增 `chartModel()`（`chart.getModel() || null`，O(1)；不用
   `getOption()`，它每次深拷贝整份 option）。接入 `priceYMapper`、`drawMapper`、
   `updateLivePriceLine`（整只 tick 让路，避免往未初始化实例推 markLine）、
   `refreshPriceTagLayout`（入口 + 80ms 兜底各一次）、`syncChipAxis`（替掉未保护的
   `chart.getOption()`）。拿不到 model 就返回 null / 直接 return —— 这正是这些函数原本文档化的
   契约（"取不到投影返回 null，调用方跳过本次绘制"），只是原先没覆盖"model 还没建"这种状态。
2. **全量重建同步提交**：`updateChart` / `renderIntraday` 的 `setOption(option, ...)` 去掉
   `lazyUpdate`（保留 `notMerge` + 不 `clear()` —— 防白屏闪烁靠的是旧 canvas 留到同 tick 内原子
   替换，不是 lazyUpdate）。增量补丁（`patchLastBarOnChart` / markLine / 筹码轴，merge 语义）继续
   用 `lazyUpdate`。

若将来真观察到周期切换闪烁，回退方案是保留 `lazyUpdate` 并在其后紧跟一次非 lazy 的空
`setOption`（实测能在同 tick 内关掉窗口，`renderIntraday` 原本就靠后面的 graphic 补丁顺带做到）。

- 相关测试：`test_chart_ready_js.py`（探针顺序 / 整只 tick 让路 / 未建 model 时两个 mapper 返回
  null 且建图后恢复 / 复用源码里的重建选项在真实 echarts 上验证无空窗）；
  `test_draw_repaint_js.py`、`test_tag_layout_js.py`、`test_risk_lines_js.py` 的假 chart 已补
  `getModel`；`test_quote_poll_js.py::test_chart_replace_does_not_clear_canvas_first` 改为断言新的
  同步提交写法。


## 8. 分时图柱子早盘很大、随分钟数一路变小（横轴不固定）

**状态**：已修复（2026-09-18）。回归测试 `visual/test/test_intraday_window_js.py`
（含真实 echarts 的槽宽几何断言）。

### 现象

分时图刚开盘时一根 K 线柱子占满整幅图，之后每过一分钟所有柱子都窄一点，到收盘才稳定下来；
东财分时是固定窗口 —— 槽宽全天不变，右侧空白随开盘推进。

### 根因

横轴的 category `data` 只装**已出现的分钟**（`times = bars.map(b => b.time)`），而 dataZoom
是 `start: 0, end: 100`，于是 ECharts 把这 N 个槽拉满整个网格：09:31 时 N=1，一根柱子占了
600px 网格的全部宽度；每 60s 刷新一次，N 加一，槽宽就乘 N/(N+1)。柱子本身没动，是**槽宽由
"已出现多少分钟"决定**，而那个数每个交易日都在变。

### 修复

`static/index.html` 里给分时加一层固定 240 槽窗口（东财口径）：

1. `intradaySessionSlots(symbol, bars)` 生成全天 240 个 1 分钟槽（末相位 09:31–11:30 + 13:01–15:00
   优先，起相位 09:30–11:29 + 13:00–14:59 兼容；判据是**哪个相位装得下全部 bar**，不是看首根
   —— 否则首根不是整点第一分钟的低流动性/复牌标的会静默退回旧轴，返回 `{slots, barAt}`；
2. `intradayPad(axis, bars, fn, empty)` 让**所有** series（K线/均价/成交量/MACD/KDJ/RSI/ATR）
   与三处 grid 的横轴都用同一份 240 槽，未走到的槽位留空 → 槽宽 = 网格宽/240，与已出现多少
   分钟无关；第一根 bar 贴左缘；
3. tooltip 的 `dataIndex` 从此是**槽位**坐标，取值改走 `intradayBarAt(slot)`（空槽不弹提示框），
   涨跌比色与均价读数走 `intradayBarAtOrBefore()`；标题的日期**只取后端下发的 `date`**（图上那批
   bar 的日子），不走 `titleDateWithLocalClock` 的"本地时钟升级"——那条是为日K「盘中当日 bar 还没
   补上来」设计的，而分时的 `date` 不存在这种滞后：盘前（09:15–09:31）与非交易日看到的都是上一
   交易日那批，让本地时钟改写会把"昨天的分时"标成今天（`test_intraday_title_across_session_phases`
   六个相位钉住）。

两条踩坑记录：

- **空槽不能填 `null`**：ECharts 5.5.0 的 candlestick 遇到 `null`/`undefined` 会在建图时抛
  `TypeError: Cannot read properties of null (reading 'value')`（整张图不出来）；`'-'` 才是它认的
  空值标记。line/bar 两种都吃，所以只有 K 线传 `INTRADAY_EMPTY_CANDLE = '-'`。
  几何测试真跑 echarts 就是在守这条。
- **护栏**：只要有一根 bar 的时间戳落不进槽位表（源换了口径/半天市）、或 bar 比槽位还多，
  就整体放弃固定窗口退回旧行为 —— 宁可观感照旧，也不能静默丢 bar。

### 已知取舍（本次不覆盖）

- **港/美股分时不启用固定窗口**：港股 330 分钟、美股 390 分钟，会话长度与 A 股不同，且
  AlphaFeed 给它们的分钟时间戳是交易所本地时间还是北京时间还没实测过，猜错会整体错位。
  这两个市场的分时仍是「只画已出现分钟」（柱子照旧会变宽）—— 等实测到时间戳口径再单独改。
- 分时**末相位**优先（09:31~15:00，AlphaFeed 实测口径）；相位判据是"哪个槽位表装得下全部
  bar"，所以将来数据源换成 09:30 起相位也不用改代码。
- 相关文档：接口/额度口径与「能力缺口不计源故障」（分时是单源链，冷却它等于全市场 404）
  见 [`alphafeed-limits.md`](alphafeed-limits.md) 的「实测：日内走势 vs 分钟K批量」一节。


## 9. 收盘集合竞价的塌陷盘口被当成「当天最后一份五档」缓存下来

**状态**：已修复（2026-09-19）。回归测试 `visual/test/test_market_quote.py`
（`DepthDayCacheTest.test_closing_auction_snapshot_does_not_replace_replayable_book`、
`test_stale_disk_entry_with_zero_levels_is_cleaned_on_read`）与
`visual/test/test_market_hours.py`（`ClosingAuctionTest`）。

### 现象

线上 `/api/depth?symbol=588200.SH`（科创芯片ETF嘉实）返回：

```json
{"bid_prices": [1.184, 0.0, 0.0, 0.0, 0.0], "bid_volumes": [174867, 656, 0, 0, 0],
 "ask_prices": [1.184, 0.0, 0.0, 0.0, 0.0], "ask_volumes": [174867, 0, 0, 0, 0],
 "timestamp": 1789714754000}
```

侧栏五档把「买2」画成价格 `0`（前端 `VisualLive.price(0)` 返回 `"0"`，只有 `null` 才显示 `—`），
而且这条快照从收盘一直报到周末；本地 server 查同一只票却是完整五档。

### 根因

两层叠加，缺一层都不会有这么难看的结果：

1. **上游在收盘集合竞价期给的就是塌陷盘口**。`timestamp` 换算回来是
   `2026-09-18 14:59:14` —— 沪深两市 14:57–15:00 是收盘集合竞价，交易所只给一个虚拟撮合价，
   上游把其余档位补 `0.0`（`bid1 == ask1 == 1.184` 是竞价虚拟价、`174867` 是累计匹配量，
   买2 那 `656` 手挂在 0 价上）。同一时刻的分钟K可佐证：14:58/14:59 两根 bar 成交量为
   `510 / 0`，全天成交都落在 15:00 那根（`223275`）。
2. **这段被算进了 `market_hours.in_session()`**（13:00–15:00 闭区间），于是
   `_depth_day_set` 把这份残缺盘口存成「当天盘中最后一份」，覆盖掉 14:56 那份完整五档。
   收盘后 `is_live()` 为假 → 所有请求都走 `_depth_day_get` 回放，周末也一样。
   本地看着正常只是因为本地 `.cache/depth_day.json` 里没有这只票，直接回源拿到了盘后那份完整盘口。

### 修复（应用侧规避）

- `market_hours` 新增 `in_closing_auction()`（14:57–15:00）与 `depth_book_replayable()`
  （= `in_session()` 减去收盘集合竞价）。**刻意不并进 `session_phase()`** —— 那会把这段从
  `trading` 拆出来，牵动量比、`BAR_READY_PHASES`、前端相位分支一大片；这里只回答
  「盘口是不是虚拟撮合队列」。开盘集合竞价 09:15–09:30 本就不在 `in_session()` 里，无需再减。
- `market` 新增 `_clean_depth_levels` / `_clean_depth_payload`：价格 `<= 0` 的档位视为不存在，
  **价与量一起置 `None`**（那 `656` 手是无主数据），长度沿用上游档数不做补齐。AF 与麦蕊两条
  路径都过一遍；全 `0` 的盘口（停牌/未开盘的上游占位）当「没拿到数据」处理，回退麦蕊而不是
  当成「五档是空的」下发。
- 日缓存写入门槛由 `in_session()` 改为 `depth_book_replayable()`；读出时再过一次清洗
  （`_clean_depth_entry`），修复前写进磁盘的补零盘口不必等它自己过期。

### 第二道判据：形态（与时段无关）

时段判据只覆盖已知的集合竞价窗口；再按盘口**形态**兜底一层（`_is_collapsed_depth`，
判据收在 `_depth_day_set` 内部，AF / 麦蕊 / 将来新增的取数路径共用一道门）：

| 判据 | 依据 |
|---|---|
| 买1价 == 卖1价（容差 `1e-6`） | 真实盘口同价挂两侧会立即撮合，这是集合竞价虚拟撮合价的签名（实测两侧都是 `1.184`、量都是 `174867`）。容差不放大：上游浮点带 `17.650000000000002` 这种噪声，而最小 tick 是 `0.001` |
| 有效档位总数 `<= 1` | 单档快照没有「当天最后一份」的回放价值 |

**不能误判的边界**：涨停/跌停的单边空（一侧 5 档、另一侧被上游补 `0` → 清洗成全 `None`）
有效档数正常，照常入库 —— 判据写成「两侧都必须非空」会让所有封板票永远没有当日快照。
被拒时打一条 `log.warning` + `perf.bump("depth_day_reject")`（成功写 `depth_day_write`）：
全仓此前没有任何 depth 埋点，这是「上游又在某个时刻给了不是五档的东西」唯一的留痕。
不接 `source_alert` —— 集合竞价塌陷是预期内的市场状态，每天推一条会变噪音。

实时语义不变：14:57–15:00 拉到的仍是当时的真相（竞价队列，只是空档位成了 `—`），
变的只是「这份值不值得当收盘盘口存下来回放」。

### 快照语义与「读到收盘前抓的那份就回源换掉」

写入是**请求驱动**的（不是定时任务），同一天每次成功取数都覆盖同一个键，所以快照原本
代表的是**最后一次成功取数的时刻**，而不是「当天收盘附近那份」。服务器上真实的
`.cache/depth_day.json`（2026-09-18，7 只票）就是这个形状，现在读它们会发生什么：

| 标的 | 修复前的快照 | 抓取时刻 | 现在读它会怎样 |
|---|---|---|---|
| 300943.SZ | 10:14:21 `33.13` | 收盘前 | 回源 → `15:30` 那份（`33.99`） |
| 002472.SZ | 10:33:42 | 收盘前 | 回源 → `15:30` |
| 000059.SZ | 11:08:57 | 收盘前 | 回源 → `15:30` |
| 002466.SZ | 14:24:57 | 收盘前 | 回源 → `15:30` |
| 002714.SZ | 14:52:27 | 收盘前 | 回源 → `15:30` |
| 000070.SZ | 14:56:24 | 收盘前 | 回源 → `15:30`（`17.71` → `17.70`） |
| 588200.SH | 14:59:14（补零） | 收盘前 | 形态塌陷 + 收盘前 → 回源 → `15:30:53` |

7 只里 3 只是上午 10–11 点的：早盘看过、关掉网页，晚上打开看到的就是上午那份。

**读侧刷新**（`_fetch_depth_locked` 里的 `refresh`）：非 live 时段读到快照时，若它
**是所属交易日收盘前抓的**（条目自己的 `_revision` 落在**该交易日**的 **15:00** 之前）**或**
形态塌陷，就花一个令牌回源，拿到有效盘口就顶掉内存与磁盘上那份 —— 之后所有视图都直接回放，
不再花令牌（每个标的每个交易日最多回源一次）。

判据必须**同时看时刻和日期**：抓取日期晚于所属交易日 ⇒ 那是上一个交易日收盘之后抓的，
已经是"收盘后那份"，不能再判陈旧。否则周末/假期 15:00 之前刷出来的那份会永远算"收盘前抓的"，
每个视图都回源一次、永不收敛（实测连看 3 次 = 3 次上游调用；回归用例
`test_weekend_morning_refresh_converges`）。

**判据为什么是「抓取时刻 vs 当天 15:00」而不是「离收盘几分钟」**：后者是拍出来的容差，而且
形状就不对 —— 实测差距与钟点无关（同一批票，快照时刻 → 与盘后那份的价差）：

| 快照时刻 | 标的 | 价差(tick) | 买1量 快照 → 盘后 |
|---|---|---|---|
| 10:14 | 300943.SZ | 86 | 14 → 67 |
| 10:33 | 002472.SZ | **1** | 44 → 84 |
| 11:08 | 000059.SZ | **0** | 2179 → 840 |
| 14:24 | 002466.SZ | 19 | 78 → 506 |
| 14:52 | 002714.SZ | 17 | 134 → 509 |
| 14:56 | 000070.SZ | 1 | 201 → 2735 |
| 14:59 | 588200.SH | 1（且塌陷） | 174867 → 30354 |

上午 10:33 那份只差 1 tick、下午 14:52 那份差 17 tick（`41.52 → 41.35`，1.7%），而且
**七只票的五档量全都变了** —— 差多少取决于「这之后价格动没动过」，跟离收盘多近无关。
所以唯一说得通的门槛是「当天连续竞价结束了没有」。

判据用**我们自己记的抓取时刻**（`_revision`，`time_ns()//1000` 微秒），不用上游 timestamp：
盘后上游给的是它自己 15:30 的刷新时刻，那是供应商行为、不该当协议依赖（今天它给 15:30，
明天可能给最后一笔的 14:56，那样按上游时间戳判就会**每次视图都回源**）。老条目没有可用的
`_revision` 时退回上游 timestamp 判。

依据（实测，不是猜）：盘后上游给的**就是当天最后一份连续竞价盘口** ——

| 标的 | 盘中快照 | 盘后上游 |
|---|---|---|
| 000070.SZ | 14:44 `17.66 / 17.67`，量 773 / 245 | 15:30 `17.70 / 17.71`，量 2735 / 2243 |
| 601555.SH | 14:52 `8.29 / 8.30` | 15:31 `8.30 / 8.31` |
| 300943.SZ | 10:14 `33.13`（收盘 33.99） | 15:30 `33.99 / 34.00` |

所以这不是「多打一次上游」，是换掉手里那份旧的。只在 **A股** 做这个判断：港美股不适用
A股 时段口径，且它们的盘中在本口径里本就落在「非 live」。

**写入/读取/失效规则**（改这块前先读这里）：

| | 条件 |
|---|---|
| 写入 | 请求驱动。取数成功 + 「当天是交易日，或非交易日能定位到最近一个已收盘的交易日」（`_depth_snapshot_key` = 当天 → `market_hours.last_trading_day()` → 文件里那个日期兜底；**不能只靠文件里的日期**：文件缺失时它是 `None`，落档会被 early-return 掉，于是每次查看都回源）+ 过**形态判据**，再看时段：`depth_book_replayable()` —— 留档窗口是「除两个集合竞价窗口与盘前」= 09:30–11:30 / 13:00–14:56 / 11:31–12:59 / 15:01 之后 / 非交易日。午休与盘后取到的正是「当天最后一份连续竞价盘口」，所以也留（这就是回源刷新那份能落到内存与磁盘上的原因）。另有**只新不旧**：新盘口 timestamp 早于已存那份就丢弃（`depth_day_stale_skip`，仅同一天内比较）。写入的是一份**副本**（`_copy_depth_entry`）：TTL 缓存与 `/api/depth` 的响应持有同一个 dict，日缓存必须跟它们解耦。落档 = 内存 + 整个字典 tmp+replace 原子写盘 |
| 读取 | 仅在 `not is_live()` 时（09:00–09:15 / 11:31–12:59 / 15:01 之后 / 非交易日）；命中即回放（**所属交易日收盘前抓的**或塌陷则先试一次回源刷新，失败退回它），**miss 则照旧回源上游**（这就是「本地看着正常、服务器不正常」的原因：本地文件里没这只票）。脏行（非序列的档位）只当"这份盘口没有档位"，不会把接口打成 500（`_depth_array`） |
| 失效 | 跨交易日按 `trade_date` 比对：内存不一致即清空、磁盘不一致直接返回 `None`（连载入都不做）。周末/假期 `today is None` 故**不比较**，上一个交易日那份继续有效（刷新时把 key 定到最近一个已收盘的交易日，不会新开一份、也不会被跨日失效清掉）。进程重启后磁盘仍在（compose 挂了 `./.cache`）。无 TTL、无条数上限 |

### 性能：盘中落盘的开销特征

五档当日快照是给非盘中回放用的，但**写盘发生在盘中**（每次成功取数都写）。结论：当前规模下
可以忽略，且频率是令牌桶定的、不是流量定的 —— 但也确实有两处无谓的写入。

**频率上界**（写盘次数 = 成功取数次数）：AF 桶 `PacedBudget(24)`（最小间隔 2.5s、无突发）
→ ≤24/min；AF 拿不到时走麦蕊回退桶 24/min，所以绝对天花板 ≤48/min；每标的 2.5s 的 TTL 缓存
让多个客户端看同一只票只取一次数；客户端只在 live 且页面可见时轮询（2.5s 一次、一次一只票）。
**实际 ≤24 次/分钟。**

**实测**（各规模 200 次，报 p50 / p95 / max，单位 ms）：

| 规模 | 文件 | 本地全量调用 `_depth_day_set`（Win/NTFS） | 生产容器 I/O 原语（Linux） | 其中 `json.dumps` | 只更内存（判据+赋值） |
|---|---|---|---|---|---|
| 1 只 | 336 B | 0.534 / 0.783 / 2.13 | 0.39 / 0.73 / 6.2（overlay）· 0.57 / 2.68 / 6.1（绑定盘） | 0.013 | 0.006 |
| 50 只 | 14.9 KB | 0.722 / 0.955 / 1.63 | 0.45 / 0.89 / 3.2 · 0.60 / 1.00 / 3.8 | 0.128 | 0.006 |
| 300 只 | 89.4 KB | 1.399 / 2.726 / 5.46 | 0.92 / 3.39 / 13.5 · 0.59 / 1.39 / 2.4 | 0.681 | 0.008 |

- 生产容器处只量了 I/O 原语（`json.dumps` + `write_text` + `os.replace`，落点 `/tmp` 与
  `/app/.cache`）：线上跑的仍是修复前代码，不为量数据去部署。判据部分与本地一致（纯 CPU，
  0.006 ms 量级）。
- 复跑：本地压**生产函数** `market._depth_day_set`（把 `_DEPTH_DAY_CACHE_FILE` 指到临时目录），
  规模 1/50/300 只 × 200 次。
- 关键读法：**落盘一次 ≈ 0.4–0.9 ms，且与文件大小几乎无关**（89 KB 也是 0.6 ms —— 写的是页
  缓存、没有 fsync，`os.replace` 只是 rename）；尖峰（3–13 ms）来自 overlay 首次写把文件
  copy-up。24 次/分 × 0.6 ms ≈ **14 ms/分钟，占空比约 0.02%**。

**锁的边界**：`_depth_day_cache_lock` 只被盘口读写路径用，持锁时间 = 整个 `_depth_day_set`
（判据 + 序列化 + 落盘，上表第一列就是它）；`_depth_fetch_lock` 是单飞锁，抢不到的请求直接吃
TTL 缓存、不等待。所以最坏情况下被挡住的只有盘口自己的请求，行情/K线/分时都不经过这两个锁。

**两处无谓的写入**（本次不治，先记着）：

1. **全量重写而非增量**：每次取数都把整个字典重新序列化落盘，成本是 O(当天看过的标的数)。
   300 只票时序列化 0.68 ms 已是成本大头（落盘 I/O 反而平）。
2. **内容没变也写**：`_revision`（抓取时刻）每次都变，所以上游返回同一个盘口（冷门票、封板票
   静静挂着）也照样重写一遍。一个面板开一下午 ≈ 5.8k 次小写入/天，全是重复内容。

**为什么现在不那么要紧**：盘后读到「收盘前抓的」那份会回源换掉，所以盘中落盘那份的**精度**
已经不重要了，它的价值退化成「下游桶空了/上游挂了时，盘后还有东西可回放」。将来真要治这两处
浪费，「内容没变就不写盘」（内存仍更新）和「落盘节流（如 10s 一次）」都是安全的 —— 代价只是
故障重启时丢掉最多 10s 前的盘口，而那份盘后本来就会被回源刷新覆盖。

### 运维提示

修复前的 `.cache/depth_day.json` 里若有补零或早盘/下午的条目，**部署后不必删文件**：读出时
补零会被清洗、**收盘前抓的**会被回源换掉（线上那条 14:59 补零条目就是这样自愈成
`15:30:53` 的完整盘口的）。注意每个标的每天最多回源一次（换来的那份是收盘后抓的，之后
所有视图直接回放），但快速连翻时仍受 24/min 节流。

### 已知取舍

- 14:57–15:00 期间侧栏五档只有买1/卖1（同一虚拟价），其余档位显示 `—`：这是真实盘口状态，
  不是缺陷。塌陷判据只挡**留档**、不挡实时展示；若将来希望这段时间直接冻结在 14:56 那份，
  改 `fetch_depth` 的实时分支即可，与本次的缓存口径无关。
- **回源刷新受 24/min 节流；被限流时退回手里那份旧快照**（完整的用户体验）：
  - 何时会看到旧那份：非 live 时段读到「收盘前抓的」→ 尝试回源 → 那一刻令牌被占满
    （盘口桶 24/min、无突发、与监控 feed 的 30/min 合起来贴着「盘口按标的 60/min」那一份，
    不能靠放大桶解决，见 AGENTS.md）或上游异常 → 按设计**退回手里那份**，也就是早盘那五档。
    盘后首次翻自选表时，超出令牌间隔（2.5s）的那几只就属于这种情况。
  - 为什么退回而不是给空面板：宁可给一份明确的旧盘口，也不要「无五档数据」；而且判据仍然
    成立（那份还是「收盘前抓的」），下次查看还能再换。回归用例：
    `test_market_quote.DepthStaleSnapshotRefreshTest.test_stale_snapshot_kept_when_upstream_unavailable`
    （上游抛错 + 无麦蕊 key → 返回手里那份，不是 `None`）。
  - 怎么恢复：**下一次查看 / 换股 / 重开页面都会重试**。客户端在非 live 时段不自动轮询
    （`live-market.js` 的 `active` 闸门），所以它不会自己变新，需要一次交互；成功换到收盘那份
    之后就永久免费（那份的抓取时刻在所属交易日 15:00 之后、或是隔日抓的，不再判陈旧）。
    翻第二遍就是全免费。
  - 边界：港美股不参与这个判断（不适用 A股 时段口径，它们的盘中在本口径里本就落在非 live）；
    停牌票上游天然就给几天前的盘口，那种「旧」是正确的、也不该被反复回源。
- **午休期间每次查看都会回源**：11:30 那份是"当天收盘前抓的"，所以 11:31–12:59 每只票每次
  查看最多花一个令牌换一份 11:30 的盘口（内容一样，换的是"证明它是最新的"）；13:00 后盘中
  取数会覆盖它，收盘后再换一次收盘那份。一屏自选表翻一遍的代价在个位数令牌。
- **只新不旧按上游 timestamp 比**：一次滞后的响应不会顶掉更好的那份；但没有"档位数不回退"
  的判据 —— 若上游在收盘前给出一份更薄但更新鲜的盘口，它仍会覆盖较厚的那份（真实盘口本就
  会变薄，硬性比较档位数会冻结一份旧的）。
- **面板不显示快照时刻**（表头只有「档位/价格/量」）：刷新后盘后看到的已基本是收盘那份，
  但仍无法从面板判断手上这份是几点几分（比如午休时的 11:30 那份）。本次未实现（要动面板
  表头 + 截图导出模型 + JS 用例），后续若要再加，可一并考虑 `/api/depth?fresh=1` 手动强制回源。


## L2 数据（十档/逐笔）获取方式调研

- 结论：**网页版扫码登录方案不可行**。扫码只能获得网页会话，L2 十档/逐笔走的是各平台（东财/同花顺/富途）非公开 WebSocket 协议且绑定付费会员账号——需要逆向私有协议、维持易失效会话，稳定性差且有合规风险。
- 替代建议：接正规付费 L2 数据源（东财全行通、捷利交易宝、券商 Level-2 API），或在 AlphaFeed 升级套餐开放 L2 后经 `feed.py` 扩展。
- 状态：暂不实现（2026-09-12）。
