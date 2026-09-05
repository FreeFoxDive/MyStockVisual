# Visual — 本地股票K线可视化

基于 **Flask + Waitress** + ECharts 5 的实时K线图表，AlphaFeed / 麦蕊数据源。

## 快速启动

```bash
cd D:/projects/stock_pick
venv/Scripts/pip.exe install -r visual/requirements.txt
venv/Scripts/python.exe -u visual/server.py
# 浏览器打开 http://localhost:8888
```

用项目 venv 启动（系统 Python 缺 `mairui` 等依赖）。自动加载 `visual/.env`，没有则回退项目根 `.env`（`AF_API_KEY` / 钉钉 / ntfy / 管理员口令）。

## 功能

| 功能 | 说明 |
|------|------|
| K线视图 | 日K / 周K / 月K (AlphaFeed 原生) |
| 默认三屏 | K线主图 + 成交量 + MACD |
| 均线 | MA5(黄) MA10(蓝) MA20(紫) |
| 可选指标面板 | MACD、KDJ、RSI、ATR，网页 checkbox 开关 |
| 成交量面板 | 可开关，关闭时 K线自动拉高 |
| 动力系统 | Elder Impulse System — EMA13方向 + MACD柱方向决定蜡烛颜色(红多/绿空/蓝中性)，仅日K |
| ATR 通道 | EMA13 ± 1/2/3 ATR 共6条虚线，仅日K，默认关闭 |
| 自适应提示框 | 鼠标在不同面板显示对应数据；MACD跟随面板开关，RSI/KDJ/ATR 独立提示框开关 |
| 股票搜索 | 模糊匹配代码/名称，实时下拉 + 键盘↑↓导航 |
| 搜索历史 | 跟账号持久化（兼浏览器本地），刷新不丢失 |
| 配置持久化 | 主题/面板开关/提示框选项自动保存 |
| 主题 | 共享 `visual-theme`（`/css/theme.css` + `/js/theme.js`），亮/暗同步各页 |
| 实时刷新 | 交易时段每30秒自动拉取快照 |
| 数据缩放 | 鼠标滚轮 + 滑块，底部可拖动 |
| 请求频率限制 | 服务端令牌桶 120次/分钟 |
| 安全 | CSP、同源 Cookie 会话、CSRF 双提交、登录爆破锁定；SQL 参数化；日志密钥脱敏 |
| 交易记录 | 多账户登录，买卖记录增删改查；录入校验日期/日K振幅/成交量；按周/月/年统计盈亏与胜率（默认周，详见 [docs/trades.md](docs/trades.md)） |
| 持仓监控 | 授权用户填齐止盈/保本/止损（止盈>保本>止损）后盘中监控，钉钉 + ntfy 推送；关联模型的持仓在推荐周期到期日 10:00/14:00 提醒平仓 |

## 文件结构

```
visual/
├── server.py          # 入口: Flask create_app + Waitress
├── app.py             # Flask 工厂 / 静态页鉴权 / 后台任务启动
├── auth_routes.py     # /api/auth/* Blueprint
├── api_routes.py      # 其余 /api/* Blueprint
├── security.py        # CSP / 限流 / 登录锁定 / 会话 Cookie / CSRF
├── market.py          # 行情代理、缓存、质押等数据层
├── logger.py          # 日志配置 + 密钥脱敏
├── indicators.py      # 指标计算
├── trades.py          # 交易记录后端 (DB / 鉴权 / CRUD / 统计)
├── monitor.py         # 持仓监控循环 (快照序列 + 告警 + 钉钉/ntfy)
├── feed.py            # AlphaFeed REST 行情接入 (令牌桶)
├── dingtalk.py        # 钉钉薄包装 (myappnotify)
├── ntfy.py            # ntfy 薄包装 (myappnotify)
├── market_hours.py    # A 股交易日历与时段
├── probe_feed.py      # 探测快照刷新频率 / 接口权限
├── test/
│   ├── test_trades.py
│   ├── test_monitor.py
│   ├── test_pledge.py
│   ├── test_logger_redact.py
│   ├── test_flask_auth.py
│   └── fixtures/
├── static/
│   ├── css/theme.css  # 共享亮暗主题变量
│   ├── js/theme.js    # 主题读写 (visual-theme)
│   ├── js/api.js      # fetch + CSRF 头
│   ├── index.html
│   ├── trades.html
│   ├── admin.html
│   └── login.html
├── Dockerfile
├── docker-compose.yml
├── requirements.txt   # Python 依赖
├── docs/
│   └── trades.md      # 交易记录功能文档 (数据表 / API / 统计口径)
└── README.md          # 本文件
```

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /` | 提供 index.html |
| `GET /api/kline?symbol=600519.SH&period=1d&count=1006` | K线数据 + 全部预计算指标（日K默认 1006≈3年可见+RSI250 warmup） |
| `GET /api/quote?symbol=600519.SH` | 实时快照 |
| `GET /api/search?q=茅台` | 模糊搜索 (全量A股+ETF，内存+磁盘双层缓存，24h刷新) |
| `GET /api/ping` | 健康检查 |
| `POST /api/auth/login` | 登录，返回 `Set-Cookie: session` |
| `POST /api/auth/logout` | 登出（需登录） |
| `GET /api/auth/me` | 当前用户，返回 `{username,is_admin,monitor_enabled}`（未登录 401） |
| `GET /api/admin/users` | 用户列表（仅管理员） |
| `POST /api/admin/users` | 添加用户 `{username,password}`（仅管理员） |
| `DELETE /api/admin/users/{id}` | 删除用户（仅管理员） |
| `POST /api/admin/users/{id}/reset-password` | 重置密码 `{password}`（仅管理员） |
| `POST /api/admin/users/{id}/monitor` | `{enabled}` 授权持仓监控（仅管理员） |
| `GET /api/monitor/status` | 监控线程状态 + 当前用户最近告警 |
| `GET /api/trades` | 交易记录列表（需登录，`status/symbol/q/from/to/model_id/limit/offset`） |
| `POST /api/trades` | 新建交易记录 |
| `PUT /api/trades/{id}` | 更新交易记录 |
| `DELETE /api/trades/{id}` | 删除交易记录 |
| `GET /api/trades/stats?from=&to=` | 盈亏/胜率统计（按周/月/年分桶 + 按股票汇总 + 按模型汇总） |
| `GET /api/models` | 量化模型列表（需登录，含停用项） |
| `POST /api/models` | 新增量化模型 `{name,description,hold_days}`（仅管理员） |
| `PUT /api/models/{id}` | 更新量化模型（仅管理员） |
| `DELETE /api/models/{id}` | 软删除量化模型（仅管理员，交易记录不受影响） |
| `POST /api/models/{id}/restore` | 恢复停用量化模型（仅管理员） |
| `GET /api/trade-reasons` | 预设买卖理由分类 |

## 技术细节

### 指标计算
- **服务端 Python** 计算
- 主图 OHLC 与 MA/MACD/RSI/KDJ/ATR 均为**未复权**；动力系统蜡烛色优先用**前复权** impulse
- MA5/MA10/MA20 使用 **SMA** (简单移动平均，算术平均)，对标东方财富/同花顺/通达信标准
- MACD/KDJ/RSI/ATR 算法详见 `indicators.py`，已逐项与东财 PC 端验证
- Elder 动力系统: EMA13 方向 + MACD 柱方向 → 蜡烛颜色（红=多/绿=空/蓝=中性）
- Elder ATR 通道: EMA13 ± 1×/2×/3× ATR，虚线叠加在 K线主图
- 浏览器端纯展示，不重复计算

### 自适应提示框
- 用 ECharts 底层 zrender canvas 的 `mousemove` 事件追踪鼠标像素坐标
- 与各 grid 像素位置比对，判断鼠标在哪个面板 → 显示对应数据

### 缓存策略
- 日K / 分钟K: 盘中 60s, 盘后 300s (交易日 1d 跳过内存缓存, 直接走磁盘缓存)
- 周/月K: 600s
- 快照: 30s TTL
- 全量股票搜索列表: 24h TTL，内存 + 磁盘双层缓存；过期后 stale-while-revalidate（立即返回旧数据 + 后台刷新，不阻塞请求）
- 全市场质押数据: 24h 保鲜兜底 + 每日 15:30 定时刷新 (stale-while-revalidate，磁盘保留最近 7 个交易日)
- K 线磁盘缓存响应不携带 quote，每次返回前现挂快照（避免命中缓存拿到旧快照）

### 并发
- Waitress 多线程处理请求；行情与交易 API 并行互不阻塞

### 限流
- AlphaFeed 30次/分钟硬限制
- 服务端缓存减少 API 调用
- 前端交易时段30秒刷新间隔 → 安全范围

### 配置持久化
- `localStorage` key: `visual_chart_config` — 主题/面板
- 搜索历史：服务端跟账号存（`users.search_history`），前端可与本地合并同步

## 依赖

- Python: `flask`, `waitress`, `alphafeed`, `numpy`, `pandas`, `akshare`, `mairui`, `pandas_market_calendars`（见 `requirements.txt`）
- 前端: ECharts 5.5.0 (通过 CDN `https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js` 加载)

### 持仓监控配置

写在 `visual/.env`（独立部署时不必依赖项目根）：

```
AF_API_KEY=...
DINGDING_WEB_HOOK_TOKEN=...
DINGDING_BOT_SIGN=...
NTFY_URL=https://ntfy.example.com
NTFY_TOPIC=stock
NTFY_USER=...
NTFY_PASSWORD=...
```

未配置的通道会跳过并打错误日志，不中断监控。

### 日志

标准库 `logging`（`visual/logger.py` 统一配置），每行 `时间戳 [级别] 模块名 消息`，时间戳固定为**北京时间**（与容器时区无关）。默认输出到 stdout（Docker 下由 `docker compose logs` 捕获）；可用环境变量覆盖：

- `LOG_LEVEL`：DEBUG / INFO / WARNING / ERROR，默认 INFO
- `LOG_FILE`：设了则额外写本地文件（RotatingFileHandler，5MB×3 轮转），便于非 Docker 持久化

监控只用 `quotes.get(symbols=...)` 按代码查询，令牌桶 6 次/分钟（额度 60/min 的 10%），不走 `universes=` 池查询。管理员在 `/admin.html` 给普通用户打开「监控」开关。

校准 / 探测 / 测试：

```
venv/Scripts/python.exe -u visual/probe_feed.py
venv/Scripts/python.exe -u visual/monitor.py --replay 603698.SH:2026-08-19 603118.SH:2026-08-13
venv/Scripts/python.exe -m unittest discover -s visual/test
```

`discover` 会跑指标/patch 的 **Node 镜像测试**（`test_recalc_tail_ma.py`、`test_indicators_js_unit.py`、`test_patch_today_bar_js.py`、`test_chart_patch_js.py`），需本机安装 **Node.js**；无 Node 时这些用例 skip，其余 Python mock 测试仍可通过。

钉钉 / ntfy 真连通（会发一条测试消息，平时不要跑）：

```
set DINGTALK_LIVE=1
venv/Scripts/python.exe -u visual/test/test_monitor.py TestDingTalk.test_live_robot_reachable
set NTFY_LIVE=1
venv/Scripts/python.exe -u visual/test/test_monitor.py TestNtfy.test_live_reachable
```

## 安全性

### 速率限制
服务端实现令牌桶算法，默认 120 次/分钟。超限返回 HTTP 429。登录失败另有 IP 锁定。

### 会话与 CSRF
会话令牌存 SQLite，经 Flask `set_cookie` 下发（HttpOnly / SameSite=Lax）。变更 API 须 CSRF Cookie + `X-CSRF-Token` 头双提交。

### CORS
限制为同源请求，避免跨域滥用。默认监听 localhost。

### Content-Security-Policy
所有响应包含 `Content-Security-Policy` 头，限制脚本来源仅为 `self` 和 `cdn.jsdelivr.net`。

### SQL
`trades.py` 对用户输入使用参数化查询（`?` 占位），不拼接请求字符串进 SQL。

### 日志与错误脱敏
`logger.py` 对 password/token/api_key 等脱敏；接口错误经 `sanitize_error` 过滤敏感片段。

## 交易记录

主页左上角「📒 交易记录」入口，未登录须先登录，数据按账户隔离。支持录入股票代码/名称、买入价、退出价、数量、买卖日期、买卖理由；`closed` 平仓记录须填全卖出字段才算一笔交易完整结束。

**录入校验**：日期不得晚于今天；买卖价须在当日日 K 振幅 `[low, high]` 内且成交量 > 0（无 K / 停牌拒绝）；逆回购只拦未来日。前端日期控件 `max=今天`，具体错误展示在弹窗。

**量化模型**：每条交易可关联一个量化模型（A–E，对齐回测管线），默认「无」。模型带推荐持仓交易日（A/B/C/D 默认 3/20/10/7，E 不提醒），到期日 10:00 / 14:00 钉钉提醒平仓；批次按最晚买入日起算。模型列表全局共享，由管理员增删改查（含 `name`、`description`、`hold_days`），普通用户只读；「删除」为软删除（停用，不物理删除、不动交易记录），历史统计永久可追溯。买卖理由新增「动力红转」（卖出）「动力绿转」（买入）「动力蓝转」（买入）。

**账户管理**：已关闭公开自助注册，新用户只能由管理员添加。管理员在「用户管理」区可添加 / 列表 / 删除 / 重置密码用户；仅单一管理员，由环境变量在首次启动时引导创建。

统计维度：按周/月/年查看总盈亏、胜率、盈亏比、最大单笔盈利/亏损、平均持仓天数，及按股票、按量化模型汇总（图表默认 **周**）。完整的数据表结构、API 端点、统计公式见 [docs/trades.md](docs/trades.md)。

- 数据库：SQLite，文件 `data/trades.db`（运行时自动创建，`data/` 不入库）
- 口令：PBKDF2-SHA256（20 万次迭代）+ 随机盐
- 会话：`secrets.token_hex(32)`，30 天过期，`HttpOnly` + `SameSite=Lax` Cookie（SQLite token，非 Flask 签名 session）
- 管理员：`.env` 中配置 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 作为口令权威来源；服务启动时若无管理员则自动创建，已有管理员则同步口令（改 `.env` 后重启即生效）

## Docker 部署

```bash
# 1. 创建 .env 文件 (填入 AlphaFeed API Key)
echo AF_API_KEY=your_key_here > .env

# 2. 构建并启动
docker compose up -d --build

# 3. 浏览器打开
# http://localhost:8888
```

```bash
# 查看日志
docker compose logs -f

# 停止
docker compose down
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 端口 | `127.0.0.1:8888` | 仅本地访问 |
| 缓存卷 | `.cache` | Docker volume 持久化 |
| 数据卷 | `data` | 交易记录数据库 `trades.db` 持久化 |
| 环境变量 | `.env` | 通过 `env_file` 注入 |
| 管理员账号 | `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 无管理员则自动创建；已有则启动时同步口令 |
| 用户 | `appuser` (非 root) | 降低容器逃逸风险 |

> 注：部署主机 `visual/data`、`visual/.cache` 的属主需为 UID 1000（绝大多数 Linux 首个用户即 1000）；否则构建时用 `--build-arg UID=$(id -u)` 对齐。

## 配色

对标东方财富按"系列"分组，同色系线条在不同面板含义一致：

| 系列 | 包含线条 | 亮色 | 暗色 |
|------|------|------|------|
| K线阳线 | — | `#ef232a` | `#ef5350` |
| K线阴线 | — | `#14b143` | `#26a69a` |
| 🔘 白/黑系 | MA5 / DIF / K / RSI1 | `#1a1a1a` | `#eeeeee` |
| 🟡 黄色系 | MA10 / DEA / D / RSI2 | `#d4a017` | `#f5c542` |
| 🟣 玫红系 | MA20 / J / RSI3 | `#c2185b` | `#e84698` |
| ⚪ 灰色系 | ATR | `#888888` | `#5a5a5a` |
| 🔵 蓝色系 | 动力系统中性 | `#5b8ff9` | `#5b8ff9` |
| 🩶 蓝灰系 | ATR通道 ±1/2/3 | `#90a4ae` / `#78909c` / `#546e7a` | 同亮色 |

## 主题

- **自动模式**（默认）：优先跟随系统暗色模式，回退到时间判断（6:00-18:00 亮色，其余暗色）
- **手动模式**：点击 🌙/☀️ 按钮锁定主题，再点恢复切换
- 手动操作后自动禁用跟随，按钮显示 `auto` 标识区分
