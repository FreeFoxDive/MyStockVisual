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

**残留（未处理）**：盘前（09:30 前）若某源快照仍带昨日 volume>0，可能被拼成“今日”bar。
实际盘前快照 volume 通常为 0（安全）；如需彻底杜绝，可给快照透传 `timestamp` 并校验日期。

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
