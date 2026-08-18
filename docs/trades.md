# 交易记录统计功能文档

> 面向 `visual` 项目的多账户交易日志：登录注册、买卖记录增删改查、量化模型关联、按周/月/年统计盈亏与胜率、按股票/模型归因。
> 仅使用 Python 标准库（`sqlite3` / `hashlib` / `secrets`），不引入任何新依赖。

- 入口：主页（`index.html`）左上角「📒 交易记录」按钮，点击跳转 `/trades.html`。
- 未登录访问 `/trades.html` 会先展示登录页，登录成功后才能进入统计界面。
- 数据按账户隔离：每个账户只能看到自己的交易记录。

---

## 1. 数据表设计

数据库文件：`visual/data/trades.db`（SQLite，WAL 模式）。

连接参数：`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`。每次操作使用短连接（线程安全，适配 `ThreadingHTTPServer` 多线程）。

### 表 `users` — 用户账户

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 用户 ID |
| `username` | TEXT | UNIQUE NOT NULL | 用户名（2~32 字符） |
| `password_hash` | TEXT | NOT NULL | PBKDF2-SHA256 哈希（hex） |
| `salt` | TEXT | NOT NULL | 随机盐（hex，16 字节） |
| `is_admin` | INTEGER | NOT NULL DEFAULT 0 | 是否管理员（仅单一管理员为 1） |
| `created_at` | TEXT | NOT NULL | 创建时间 ISO8601 |

### 表 `sessions` — 会话令牌

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `token` | TEXT | PRIMARY KEY | `secrets.token_hex(32)` |
| `user_id` | INTEGER | NOT NULL，FK→users(id) ON DELETE CASCADE | 所属用户 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `expires_at` | TEXT | NOT NULL | 过期时间（30 天） |

索引：`idx_sessions_user(user_id)`。

### 表 `models` — 量化模型（全局共享，软删除）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 模型 ID |
| `name` | TEXT | NOT NULL | 模型名称（启用中唯一，见下） |
| `description` | TEXT | NOT NULL DEFAULT '' | 策略描述 |
| `active` | INTEGER | NOT NULL DEFAULT 1 | 是否启用（0=已软删除） |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `deleted_at` | TEXT | 可空 | 软删除时间（恢复时置空） |

- **名称唯一性**：部分唯一索引 `idx_models_name_active`（`UNIQUE(name) WHERE active=1`），仅启用中的模型名不可重复；停用后可复用同名。
- **软删除**：管理员「删除」仅置 `active=0` 并记录 `deleted_at`，**不物理删除、不动交易记录**，历史交易与按模型统计永久可追溯；同名模型可「恢复」。
- **种子数据**：首次建库自动播种 A–E 五个模型（对应回测管线）：`A 60分钟超短`、`B 日线波段`、`C 日线波段·阳包阴`、`D 动力管线`、`E K线反转管线`；策略描述默认留空，不暴露内部策略细节。

### 表 `trades` — 交易记录

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 记录 ID |
| `user_id` | INTEGER | NOT NULL，FK→users(id) ON DELETE CASCADE | 归属账户 |
| `symbol` | TEXT | NOT NULL | 归一化代码（如 `000001.SZ`） |
| `name` | TEXT | NOT NULL | 完整名称（如 `平安银行`） |
| `status` | TEXT | NOT NULL | `open`（持仓中）/ `closed`（已平仓） |
| `entry_price` | REAL | NOT NULL | 买入价 |
| `exit_price` | REAL | 可空（open 时为空） | 退出价 |
| `quantity` | INTEGER | NOT NULL | 数量 / 股数 |
| `entry_date` | TEXT | NOT NULL | 买入日期 `YYYY-MM-DD` |
| `exit_date` | TEXT | 可空 | 卖出日期 `YYYY-MM-DD` |
| `entry_reason` | TEXT | NOT NULL | 买入理由分类（预设值） |
| `entry_note` | TEXT | 可空 | 买入理由自由文本补充 |
| `exit_reason` | TEXT | 可空（open 时为空） | 卖出理由分类 |
| `exit_note` | TEXT | 可空 | 卖出理由自由文本补充 |
| `model_id` | INTEGER | 可空，FK→models(id) ON DELETE SET NULL | 关联量化模型（`NULL`=无；软删不触发置空） |
| `type` | TEXT | NOT NULL DEFAULT `'simple'` | 交易类型：`simple`（单笔买卖，默认）/ `batch`（批次多次买卖） |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

索引：`idx_trades_user_exit(user_id, exit_date)`、`idx_trades_user_symbol(user_id, symbol)`。

### 表 `trade_legs` — 批次交易腿（多次买卖明细）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 腿 ID |
| `trade_id` | INTEGER | NOT NULL，FK→trades(id) ON DELETE CASCADE | 所属批次交易 |
| `side` | TEXT | NOT NULL | `buy`（买入）/ `sell`（卖出） |
| `price` | REAL | NOT NULL | 该腿成交价 |
| `quantity` | INTEGER | NOT NULL | 该腿成交数量（股） |
| `date` | TEXT | NOT NULL | 成交日期 `YYYY-MM-DD` |
| `time` | TEXT | 可空 | 成交时间 `HH:MM[:SS]`（用于同日内正T/反T排序） |
| `reason` | TEXT | 可空 | 该腿理由（如 加仓/止盈/做T） |
| `note` | TEXT | 可空 | 该腿自由文本备注 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

索引：`idx_trade_legs_trade(trade_id, date, id)`。

### 两种交易类型与结束规则

- **单笔 `simple`**（默认）：一行记录 = 一次完整往返。`status='closed'` 要求 `exit_price`/`exit_date`/`exit_reason` 必填（`exit_date >= entry_date`）；`status='open'` 只要求 `entry_*` 字段完整，卖出字段置空。
- **批次 `batch`**：父行只存**冗余汇总**（净持仓/加权均价/首买日/末卖日/status），明细在 `trade_legs`。每次腿写入时按时间顺序滚动重算：
  - 按 `(date, time, id)` 排序，维护净持仓 `held` 与累计买入成本 `cost_total`。
  - **买入**：`held += qty`，`cost_total += price × qty`，加权均价 = `cost_total / held`。
  - **卖出**：校验 `qty <= held`（否则「卖出数量超过当前持仓」拒绝），实现盈亏 `(price − avg) × qty`，`held -= qty`，`cost_total = held × avg`（**卖出不改变均价**）。
  - 循环结束 `held == 0` → `status='closed'`，否则 `open`。
- 后端在 `create_trade` / `update_trade` 中统一校验，不满足即拒绝（返回 400 + 具体错误信息）。

> **兼容性**：老库 `trades` 表自动补 `type` 列（默认 `'simple'`），`trade_legs` 表用 `CREATE TABLE IF NOT EXISTS` 新建；存量记录零改动，行为与历史逐位一致。

---

## 2. 鉴权流程

1. 口令用 **PBKDF2-SHA256**（`hashlib.pbkdf2_hmac`，200,000 次迭代）加随机盐（`secrets.token_hex(16)`）派生，密文与盐分开存储。
2. 登录成功后生成会话令牌 `secrets.token_hex(32)`，写入 `sessions` 表，30 天过期。
3. 令牌通过 `Set-Cookie` 下发：`session=<token>; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000`。
4. 受保护接口从 `Cookie` 读取令牌 → 查 `sessions` → 校验过期 → 得到当前用户（含 `is_admin`）；未登录返回 401。

### 用户与管理员

- **关闭公开注册**：不存在自助注册接口，新用户只能由管理员添加。
- **首个管理员引导**：服务启动时读取环境变量 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，若库里没有任何管理员（`is_admin=1`）则自动创建；已有同名管理员则同步口令（`.env` 为权威来源，改 `.env` 后重启即生效，旧口令及会话立即失效）。
- **仅单一管理员**：引导出的账号是唯一管理员；管理员通过 `POST /api/admin/users` 添加的用户一律为普通用户，无「设为管理员」入口。
- 管理员可**添加 / 列表 / 删除 / 重置密码**；删除管理员本身会被拒绝（400）。

---

## 3. API 端点

所有 `/api/*` 均受令牌桶限流（120 次/分钟，超限 429）。

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/auth/login` | 否 | 校验 → 建会话，`Set-Cookie` |
| POST | `/api/auth/logout` | 是 | 删除会话，清 Cookie |
| GET | `/api/auth/me` | 是 | 返回 `{username,is_admin}`；未登录 401 |
| GET | `/api/admin/users` | 管理员 | 用户列表 `[{id,username,is_admin,created_at}]` |
| POST | `/api/admin/users` | 管理员 | 添加用户 `{username,password}` → 普通用户，重名 409 |
| DELETE | `/api/admin/users/{id}` | 管理员 | 删除用户（管理员拒绝 400，不存在 404） |
| POST | `/api/admin/users/{id}/reset-password` | 管理员 | 重置密码 `{password}`（至少 6 位） |
| GET | `/api/models` | 是 | 模型列表 `[{id,name,description,active,created_at,deleted_at}]`（含停用项，前端过滤） |
| POST | `/api/models` | 管理员 | 新增模型 `{name,description}`，重名 409 |
| PUT | `/api/models/{id}` | 管理员 | 更新模型 `{name,description}`（不存在 404） |
| DELETE | `/api/models/{id}` | 管理员 | 软删除模型（置 `active=0`，交易记录不受影响） |
| POST | `/api/models/{id}/restore` | 管理员 | 恢复停用模型（启用中重名 409） |
| GET | `/api/trades` | 是 | 列表，参数 `status,symbol,q,from,to,limit,offset` |
| POST | `/api/trades` | 是 | 新建（body 见 `trades` 字段） |
| PUT | `/api/trades/{id}` | 是 | 更新（部分字段合并） |
| DELETE | `/api/trades/{id}` | 是 | 删除 |
| GET | `/api/trades/stats?from=&to=` | 是 | 统计汇总 + 时序序列 + 按股票汇总 + 按模型汇总 |
| GET | `/api/trade-reasons` | 否 | 返回 `{entry:[...], exit:[...]}` 预设分类 |

- `GET /api/trades` 的 `from`/`to` 作用于 `COALESCE(exit_date, entry_date)`（即平仓用卖出日、持仓用买入日作为归属日期）；`q` 对 `symbol`/`name` 模糊匹配。
- 股票代码/名称补全复用现有 `GET /api/search?q=...`。

### 请求体示例

**单笔买卖（`simple`，默认）**：`POST /api/trades`

```json
{
  "symbol": "000001.SZ",
  "name": "平安银行",
  "status": "closed",
  "quantity": 1000,
  "entry_price": 10.5,
  "entry_date": "2026-08-01",
  "entry_reason": "突破买入",
  "entry_note": "",
  "exit_price": 12.0,
  "exit_date": "2026-08-10",
  "exit_reason": "止盈(达到目标价)",
  "exit_note": "",
  "model_id": 2
}
```

**批次多次买卖（`batch`）**：`POST /api/trades`

```json
{
  "type": "batch",
  "symbol": "000001.SZ",
  "name": "平安银行",
  "model_id": null,
  "legs": [
    { "side": "buy",  "price": 10.0,  "quantity": 1000, "date": "2026-08-01", "time": "09:30", "reason": "建仓",   "note": "" },
    { "side": "buy",  "price": 12.0,  "quantity": 500,  "date": "2026-08-03", "time": "10:00", "reason": "加仓",   "note": "" },
    { "side": "sell", "price": 15.0,  "quantity": 800,  "date": "2026-08-05", "time": "14:00", "reason": "减仓",   "note": "" },
    { "side": "sell", "price": 16.0,  "quantity": 700,  "date": "2026-08-08", "time": "09:45", "reason": "清仓",   "note": "" }
  ]
}
```

- `legs` 非空且至少一条 `buy`；每条腿 `side∈{buy,sell}`、`price>0`、`quantity>0`、`date` 合法（`time`/`reason`/`note` 可选）。
- `update_trade` 对批次交易**整体替换** `legs`（客户端传完整列表），删旧插新后重算汇总。
- 列表 / 详情 / 持仓接口对批次交易额外返回 `legs`（腿明细）与 `t_stats`（做T统计）字段。

---

## 4. 统计口径（`compute_stats`）

只统计 `status='closed'` 且 `exit_date` 落在 `[from, to]` 区间内的记录。

| 指标 | 定义 |
|------|------|
| 单笔盈亏 | `(exit_price - entry_price) × quantity` |
| 单笔收益率 | `exit_price / entry_price - 1` |
| 总盈亏 | 所有平仓记录盈亏之和 |
| 总收益率 | `总盈亏 / Σ(entry_price × quantity) × 100%` |
| 胜率 | `盈利笔数 / (盈利笔数 + 亏损笔数)`，盈亏持平不计入分母 |
| 盈亏比 (profit factor) | `Σ盈利 / |Σ亏损|`，无亏损时为 ∞ |
| 平均盈利 / 平均亏损 | 盈利（亏损）笔盈亏金额的平均值 |
| 最大单笔盈利 / 最大单笔亏损 | 单笔盈亏最大正值 / 最小负值（含金额 + 收益率） |
| 平均持仓天数 | `mean(exit_date - entry_date)`（天） |

### 时序分桶序列（`series`）

按 `exit_date` 归桶，返回 `week` / `month` / `year` 三种粒度，每桶 `{label, pnl, count, win_rate}`：

- `week`：`2026-W33`（ISO 年-周）
- `month`：`2026-08`
- `year`：`2026`

### 按股票汇总（`by_symbol`）

按 `symbol` 聚合 `{symbol, name, pnl, count, win_rate}`，按盈亏金额降序。

### 按模型汇总（`by_model`）

按 `model_id` 聚合 `{model_id, name, active, pnl, count, win_rate}`，按盈亏金额降序。

- `model_id=NULL` 归入「无」。
- 停用（软删）模型的历史交易仍计入，`name` 带「（已删除）」后缀，`active=false`。
- 与 `by_symbol` 相同的盈亏/胜率口径，仅分组维度不同。

### 批次交易盈亏（移动加权平均）

批次交易通过统一的「归一化单笔指标」并入上面的统计口径（`_trade_metrics`），父行字段含义如下：

| 父行字段 | 批次含义 |
|------|------|
| `entry_price` | 当前加权均价（`cost_total / held`；已平仓时为 `0.0`） |
| `quantity` | 净持仓股数（已平仓时为 `0`） |
| `entry_date` | 首条买入腿日期 |
| `exit_date` | 末条卖出腿日期（仅平仓时） |
| `entry_reason` | 首条买入腿理由 |
| `exit_reason` | 末条卖出腿理由（仅平仓时） |

- **单笔盈亏**：滚动实现的毛盈亏之和（每笔卖出 `(卖价 − 当时加权均价) × 数量`）扣除合计费用。
- **收益率分母**：`cost = Σ(买入价 × 数量)`（累计买入金额），`return_pct = pnl / cost × 100%`。完全平仓时累计卖出金额 == 累计买入金额，与现有 `pnl / (entry_price × quantity)` 口径一致。
- **费用**：每条腿各自计一次单边费用（每次下单的佣金各算一次最低佣金 5 元），合计后计入 `pnl` 与 `fees`。
- **持仓中的批次**：浮盈亏由前端用 `(现价 − 加权均价) × 净持仓` 计算（与单笔持仓一致）。

> 举例（扣费前）：买 1000@10 → 买 500@12（加权均价 16000/1500=10.667）→ 卖 800@15（实现 `(15−10.667)×800=3466.67`）→ 卖 700@16（实现 `(16−10.667)×700=3733.33`）。毛盈亏 7200，累计买入金额 16000，收益率 45%。

### 做T 统计（正T / 反T / 成功率）

批次交易的腿按 `date` 分组，**同一天既有买入又有卖出**记一次做T：

- 配对数量 `matched_qty = min(当日Σ买入量, 当日Σ卖出量)`；超出部分按加仓/减仓计，不计入做T。
- 方向：当日按 `(time, id)` 排序的**首条腿**为买 → **正T**（先买后卖）；首条腿为卖 → **反T**（先卖后买）。
- T 盈亏 = `(当日平均卖价 − 当日平均买价) × matched_qty`（毛差价）；`> 0` 记为成功。

汇总返回 `t_stats = {count, positive, reverse, success, success_rate, pnl}`：
- `count` 做T次数，`positive`/`reverse` 正T/反T次数，`success_rate` 成功率（成功次数 / 总次数 × 100%，无做T时为 `null`），`pnl` 做T盈亏合计。

> 口径与同花顺/东方财富一致：只要同一天既有买又有卖即记一次做T，**不要求先有非零底仓**（建仓当日「先买后卖」的日内往返也计一次正T）。
>
> 例：09:30 买 500@10.00 → 14:00 卖 500@10.50 ⇒ 正T，T盈亏 = (10.50−10.00)×500 = +250（成功）。

---

## 5. 预设理由分类

### 买入理由 `ENTRY_REASONS`

突破买入、均线金叉、MACD金叉、回踩支撑、超跌反弹、趋势跟随、形态突破、放量上涨/资金流入、业绩增长/基本面改善、估值低估、政策利好/行业景气、题材热点/消息面、动力绿转、动力蓝转、其他。

### 卖出理由 `EXIT_REASONS`

止盈(达到目标价)、止损(跌破止损位)、均线死叉/MACD死叉、跌破支撑/破位、基本面恶化、利空消息/政策风险、资金流出/放量下跌、调仓换股、时间止损(持有超期)、动力红转、其他。

> 分类参考券商/交易社区（东方财富、雪球、淘股吧等）常用口径；每条记录可在分类之外补充自由文本说明。
>
> 「动力红转 / 动力绿转 / 动力蓝转」来自管线 D（动力管线）的颜色转换信号：红转=卖出，绿转=买入，蓝转=买入。

---

## 6. Docker 持久化

- `docker-compose.yml` 挂载 `./data:/app/data`，SQLite 数据库持久化到宿主机 `visual/data/trades.db`。
- `Dockerfile` 中 `RUN mkdir -p .cache data && chown -R appuser:appuser /app` 确保目录可写。
- 容器重启后数据仍在；删除 `data/` 目录即清空全部账户与记录（慎用）。
- `data/` 已加入 `.gitignore` / `.dockerignore`，不纳入版本控制与镜像。

---

## 7. 文件清单

| 文件 | 说明 |
|------|------|
| `visual/trades.py` | 后端模块：DB 建表/连接、口令哈希、会话、交易 CRUD、模型 CRUD、统计 |
| `visual/static/trades.html` | 前端 SPA：登录、概览卡片、盈亏图表（含按模型）、记录表格、录入编辑弹窗 |
| `visual/static/admin.html` | 管理后台：用户管理 + 量化模型管理（仅 admin，软删/恢复） |
| `visual/server.py` | 新增 `do_POST/PUT/DELETE`、Cookie/body 解析、鉴权助手、路由分发 |
| `visual/static/index.html` | 左上角新增「📒 交易记录」入口按钮 |
| `visual/data/trades.db` | SQLite 数据库文件（运行时自动创建，不入库） |
