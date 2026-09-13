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


## L2 数据（十档/逐笔）获取方式调研

- 结论：**网页版扫码登录方案不可行**。扫码只能获得网页会话，L2 十档/逐笔走的是各平台（东财/同花顺/富途）非公开 WebSocket 协议且绑定付费会员账号——需要逆向私有协议、维持易失效会话，稳定性差且有合规风险。
- 替代建议：接正规付费 L2 数据源（东财全行通、捷利交易宝、券商 Level-2 API），或在 AlphaFeed 升级套餐开放 L2 后经 `feed.py` 扩展。
- 状态：暂不实现（2026-09-12）。
