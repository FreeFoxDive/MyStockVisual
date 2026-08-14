# 交易记录统计功能文档

> 面向 `visual` 项目的多账户交易日志：登录注册、买卖记录增删改查、按周/月/年统计盈亏与胜率。
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
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

索引：`idx_trades_user_exit(user_id, exit_date)`、`idx_trades_user_symbol(user_id, symbol)`。

### 完整结束规则

- `status='closed'`：`exit_price` / `exit_date` / `exit_reason` **必填**（且 `exit_date >= entry_date`），填写完整才算一笔交易完整结束。
- `status='open'`：只要求 `entry_*` 字段完整；卖出字段置空。
- 后端在 `create_trade` / `update_trade` 中统一校验，不满足即拒绝（返回 400 + 具体错误信息）。

---

## 2. 鉴权流程

1. 口令用 **PBKDF2-SHA256**（`hashlib.pbkdf2_hmac`，200,000 次迭代）加随机盐（`secrets.token_hex(16)`）派生，密文与盐分开存储。
2. 登录成功后生成会话令牌 `secrets.token_hex(32)`，写入 `sessions` 表，30 天过期。
3. 令牌通过 `Set-Cookie` 下发：`session=<token>; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000`。
4. 受保护接口从 `Cookie` 读取令牌 → 查 `sessions` → 校验过期 → 得到当前用户（含 `is_admin`）；未登录返回 401。

### 用户与管理员

- **关闭公开注册**：不存在自助注册接口，新用户只能由管理员添加。
- **首个管理员引导**：服务启动时读取环境变量 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，若库里没有任何管理员（`is_admin=1`）则自动创建；已存在则跳过，绝不覆盖已改口令。
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
| GET | `/api/trades` | 是 | 列表，参数 `status,symbol,q,from,to,limit,offset` |
| POST | `/api/trades` | 是 | 新建（body 见 `trades` 字段） |
| PUT | `/api/trades/{id}` | 是 | 更新（部分字段合并） |
| DELETE | `/api/trades/{id}` | 是 | 删除 |
| GET | `/api/trades/stats?from=&to=` | 是 | 统计汇总 + 时序序列 + 按股票汇总 |
| GET | `/api/trade-reasons` | 否 | 返回 `{entry:[...], exit:[...]}` 预设分类 |

- `GET /api/trades` 的 `from`/`to` 作用于 `COALESCE(exit_date, entry_date)`（即平仓用卖出日、持仓用买入日作为归属日期）；`q` 对 `symbol`/`name` 模糊匹配。
- 股票代码/名称补全复用现有 `GET /api/search?q=...`。

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

---

## 5. 预设理由分类

### 买入理由 `ENTRY_REASONS`

突破买入、均线金叉、MACD金叉、回踩支撑、超跌反弹、趋势跟随、形态突破、放量上涨/资金流入、业绩增长/基本面改善、估值低估、政策利好/行业景气、题材热点/消息面、其他。

### 卖出理由 `EXIT_REASONS`

止盈(达到目标价)、止损(跌破止损位)、均线死叉/MACD死叉、跌破支撑/破位、基本面恶化、利空消息/政策风险、资金流出/放量下跌、调仓换股、时间止损(持有超期)、其他。

> 分类参考券商/交易社区（东方财富、雪球、淘股吧等）常用口径；每条记录可在分类之外补充自由文本说明。

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
| `visual/trades.py` | 后端模块：DB 建表/连接、口令哈希、会话、交易 CRUD、统计 |
| `visual/trades.html` | 前端 SPA：登录、概览卡片、盈亏图表、记录表格、录入编辑弹窗、用户管理（仅 admin） |
| `visual/server.py` | 新增 `do_POST/PUT/DELETE`、Cookie/body 解析、鉴权助手、路由分发 |
| `visual/index.html` | 左上角新增「📒 交易记录」入口按钮 |
| `visual/data/trades.db` | SQLite 数据库文件（运行时自动创建，不入库） |
