# 条件选股 (screener) 设计与运维

页面 `/screener.html`，后端 `api/screener.py`，任务表 `screener_runs`（`trades.py`），
条件注册表 `screener_metrics.py`，因子库 `factors.py`（见 [factors.md](factors.md)）。

## 两条主线

1. **任务化 + 队列**：一次扫描 = 一行 `screener_runs`，单 worker 线程 FIFO 执行；
   多用户隔离、关页/重启不丢、管理员可见队列。
2. **读因子库，不拉K线**：判定读交易日 18:00 构建的全市场因子快照 → 秒级；同一天
   同条件直接复用结果（不重扫、不取数）。

## 扫描模式与命中数（第二批）

- `mode="and"`：全部条件满足才入选；
- `mode="or"`：满足任一条件即入选，结果带 `hit_count`（满足了几项）与逐条件明细
  `hits:[{metric,label,ok,value,unit}]`，页面据此做「满足 N 项」筛选与 ✅/❌ 明细列；
- `price_mode`：`close`（默认，用因子库=收盘口径）/ `live`（盘中用批量快照补当日 bar
  后重算，10 分钟共享缓存；不可用时自动回退收盘口径并记录实际 `data_version`）。

条件由 `screener_metrics.METRICS` 定义（key/label/group/kind/field/unit/scale/op），
前后端共用：`GET /api/screener/metrics` 把目录发给页面渲染，扫描时按同一份定义判定，
**页面能选的指标与实际口径不可能漂移**。新增指标 = 注册表加一条 + 因子库快照加一列。

四种条件类型：`bool`（无参数）/ `num`（op + 数值）/ `range`（上下限闭区间）/
`text`（等值，如行业；可选值由快照实时生成）。`scale` 处理单位折算（市值列存元、
界面按亿元填）。

## 结果去重（当日同条件不重扫）

`cache_key = sha1(mode | 条件 | 数据版本 | 价格口径 | 标的范围上限 | 指标口径指纹)`：

- **数据版本** = 因子库快照的交易日（盘中口径带时段桶，形如 `2026-09-18+live@1234`）；
- **标的范围上限** = `SCREENER_MAX_SYMBOLS`（改了不会把"只扫 N 只"的结果当全市场复用）；
- **指标口径指纹** = `screener_metrics.VERSION`（注册表 field/kind/scale/op 改动即失效）。

提交时若已有同 key 的 `done` 任务（跨用户，结果只由条件+数据决定），直接为请求者落一条
`from_cache` 的完成记录并复用其结果：不扫描、不取数。

**盘中口径（`price_mode=live`）特殊处理**：它的实际数据版本要等 `live_snapshot()` 才知道，
所以键由 worker 在 `_execute` 里按**真实口径**计算；live 取不到数据而回退收盘时，会把
`price_mode` 一并改回 `close`、数据版本记成收盘日 —— 否则收盘结果会被标成"盘中"，
并让当天后续所有 live 请求都命中这条缓存。

## 结果上限与截断

`SCREENER_RESULT_MAX`（默认 3000）是单次任务落库的结果行上限。触顶时 `truncated=1`
落库并一路透出：`/status`、`/runs/{id}` 的 `truncated` 字段、页面 run 行提示
「已达单次上限，仅保留前 N 只」、推送文案也标出 —— 用户能区分"共 N 只"与"截断到 N 只"。

## 为什么要任务化

旧实现是「一个模块级全局 job + 一个线程」：任何人提交都能覆盖/取消别人的扫描，
结果只落一个 `.cache/screener_last.json`，而且**只在有结果时**才被页面回显 ——
0 命中和「已取消」下次登录就和没扫一样。现在改成任务行 + 单 worker 队列：

```
POST /api/screener/run ──► screener_runs(status=queued)
                                │
                worker 线程（单进程唯一）: claim → running
                                │
              逐只取数 + 令牌桶 ──► progress 落库
                                │
                        done / stopped / error
```

- **有序**：FIFO，同一时刻只有一个任务在跑。取数只在**因子库构建**那一步
  （`SCREENER_KLINE_PER_MIN`，默认 54/min），扫描本身不取数。
- **隔离**：`/status`、`/runs/{id}`、`/stop` 全部按 `user_id` 过滤；非本人任务
  返回 403（管理员例外，`/admin.html` 的「选股队列」可代停）。
- **可恢复**：任务与页面解耦，关页/换设备后重新登录仍能看到条件、进度与结果；
  进程重启后遗留的 `running` 会被标成 `error`（「服务重启中断」），`queued` 继续跑。

## 状态机

| 状态 | 含义 | 出口 |
|---|---|---|
| `queued` | 已提交排队 | worker 认领 → `running`；取消 → `stopped` |
| `running` | 正在扫描 | 正常结束 → `done`；取消 → `stopped`（保留已扫到的部分结果）；异常 → `error` |
| `done` | 完成（可能 0 命中） | 终态 |
| `stopped` | 已取消（`results` 为部分结果） | 终态 |
| `error` | 失败（`error` 字段给原因） | 终态 |

取消语义：`trades.stop_screener_run()` 返回 `cancelled`（排队中，直接置 stopped）/
`stopping`（运行中，置内存取消标志，worker 收尾）/ 已结束状态 / `None`（不存在）。
排队中的取消**不会**进扫描；运行中的取消在下一个「取令牌」检查点生效（秒级）。

## 历史与保留

- 页面显示最近 **3** 次（`HISTORY_LIMIT`），含进行中任务；
- 库里每用户保留最近 **10** 条已结束任务（`trades.SCREENER_RUN_KEEP`，每次任务结束
  后 `prune_screener_runs` 清理）；排队/运行中的永不被清理。
- 条件以**带 label 的快照**存库（`{"metric","op","value","label"}`），以后改指标
  名称也不会让历史回显错乱。

## 通知

全部先落库（`monitor_alerts`，每人一行，监控中心「最近告警」可见），再入队推送：

| alert_type | 时机 | 说明 |
|---|---|---|
| `screener_start` | 任务开始执行（不是提交时） | 排队久时用户能看到真正开跑的时刻 |
| `screener_progress` | 进度跨过 `SCREENER_NOTIFY_PCTS`（默认 50） | 任务运行 < `MILESTONE_MIN_SEC`(30s) 不发，免得秒级任务刷屏 |
| `screener_done` | 完成或取消 | 命中数 + 前 5 只；取消时标明「保留部分结果」 |
| `screener_error` | 执行异常 | 文案「扫描失败」，详情进日志 |

推送走 `notify.py`（见下），扫描线程只入队，不会被 HTTP 推送阻塞。

## notify.py：有序非阻塞通知队列

`myappnotify` 的 `urlopen(timeout=10)` 是同步的，两个通道最坏阻塞 20 秒。原来的
四处推送（持仓监控/到期提醒/价格预警/趋势线）都在监控循环里同步调用 —— 新加的
选股与因子通知如果照抄，会把扫描 worker 与构建线程一起拖住。因此：

- 生产者 `notify.notify(title, text, key=..., kind=...)` 只做 `put_nowait`，永不阻塞；
- 单个守护线程 FIFO 消费 → **严格保序**（进度 → 完成），一条消息内先钉钉后 ntfy、
  各自 try/except（单通道失败不影响另一通道，也不打乱后续顺序）；
- `kind="progress"` + `key`：入队时同 key 只留最新一条；出队时入队超过
  `NOTIFY_PROGRESS_TTL_SEC`（默认 300s）的进度消息直接丢弃；事件类消息不丢；
- 失败原地重试一次（`NOTIFY_RETRY_BACKOFF_SEC`），仍失败只计数 + 经
  `error_notify` 上报（带窗口聚合），**不无限重试、不堆积**；
- 队列满时丢最旧一条（优先丢进度）并计数；`notify.stats()` 可查。
- 站内为准：业务事件**先写库再入队**，推送丢了页面照样正确；进程重启会丢队列里
  未发出的推送（刻意的简单性，不引入持久化 outbox）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCREENER_MAX_SYMBOLS` | 0（全市场） | 扫描标的数上限（调试用；也进 cache_key） |
| `SCREENER_MAX_CONDITIONS` | 20 | 条件条数上限（判定 O(行数×条件数)，唯一 worker 会被单任务独占） |
| `SCREENER_TEXT_MAX_LEN` | 24 | 文本类取值长度上限（取值会进共享群推送文案） |
| `SCREENER_RESULT_MAX` | 3000 | 单次任务落库的结果行上限（触顶标 `truncated`） |
| `SCREENER_PUSH_MAX_PER_HOUR` | 10 | 扫描推送每小时每用户上限（0=不推外部；站内告警不受影响） |
| `SCREENER_NOTIFY_PCTS` | 50 | 阶段通知节点，逗号分隔；空串 = 不发阶段通知 |
| `RISK_REMINDER_AT` / `RISK_REMINDER_WINDOW_MIN` | 09:35 / 330 | 「未设风控价」提醒时刻与窗口长度（窗口外不触发，避免晚间重启补推） |
| `NOTIFY_DISABLED` | — | `1` 全部静默（测试/CI） |
| `NOTIFY_QUEUE_MAX` | 200 | 通知队列容量 |
| `NOTIFY_PROGRESS_TTL_SEC` | 300 | 进度消息过期秒数 |
| `NOTIFY_RETRY_BACKOFF_SEC` | 2.0 | 推送失败重试退避（0 = 不重试） |

拉K与限速、因子库构建相关的变量见 [factors.md](factors.md) 与
[alphafeed-limits.md](alphafeed-limits.md)。

## 接口细节（容易踩的两点）

- `GET /api/screener/status` **默认不下发 `results`**（首页徽章每 10~60s 轮询一次，
  带结果就是每次数 MB JSON）：只给摘要与 `n_results`/`truncated`；要详情用 `?full=1`
  或 `GET /api/screener/runs/<id>`，页面按需取并缓存。
- `POST /api/screener/stop` 只接受正整数 `run_id`（非数字/浮点/列表一律 400），缺省取消
  自己的当前任务；取消请求与 worker「认领→写内存态」之间的窗口用 `_pending_stop` 记账，
  不会丢取消。

## 已知边界

- 扫描依赖因子库：当日快照缺失时 `/api/screener/run` 返回 503（不会静默退回逐只拉K）；
  交易日 18:00 自动构建，也可由管理员手动重建（`POST /api/factors/rebuild`）。
- `price_mode=live` 用未复权实时价拼前复权序列，除权日会有一日偏差（18:00 重建纠正）；
  筹码列在盘中口径下沿用收盘值（重算太贵）。
- 阶段通知只在任务确实跑 >30 秒时才发（秒级扫描没必要刷屏）；开始与完成必发。
- 需求条件里的「股东户数变化」「解禁」等未纳入本次目录（数据源为逐票接口，
  成本高），后续可在注册表按同一模式补。

## 测试

```
venv/Scripts/python.exe -u visual/test/test_screener_api.py      # 目录/队列/隔离/历史/去重/读快照扫描/因子库路由
venv/Scripts/python.exe -u visual/test/test_screener_metrics.py  # 条件注册表与四种类型的判定
venv/Scripts/python.exe -u visual/test/test_screener_page_js.py  # 页面锁定/回显/历史/OR 命中数筛选与明细
venv/Scripts/python.exe -u visual/test/test_factors.py           # 因子库 (计算/存储/快照/调度/构建)
venv/Scripts/python.exe -u visual/test/test_notify.py            # 通知队列保序/去重/降级
```

约束：路由用例只落库（worker 不在测试里启动），执行路径用例用 mock 的因子库快照，
绝不触发真实扫描与取数。
