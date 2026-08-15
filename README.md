# Visual — 本地股票K线可视化

基于 Python HTTP Server + ECharts 5 的实时K线图表，AlphaFeed 数据源。

## 快速启动

```bash
cd D:/projects/stock_pick
python -u visual/server.py
# 浏览器打开 http://localhost:8888
```

自动加载项目根目录 `.env` 中的 `AF_API_KEY`。

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
| MACD 模式 | 默认(12/26/9)、MACD(13/30/10)，随视图自动切换 |
| 自适应提示框 | 鼠标在不同面板显示对应数据；MACD跟随面板开关，RSI/KDJ/ATR 独立提示框开关 |
| 股票搜索 | 模糊匹配代码/名称，实时下拉 + 键盘↑↓导航 |
| 搜索历史 | localStorage 持久化最近10条，刷新不丢失 |
| 配置持久化 | 主题/面板开关/MACD模式/提示框选项自动保存 |
| 主题 | 默认亮色 / 可切换暗色 |
| 实时刷新 | 交易时段每30秒自动拉取快照 |
| 数据缩放 | 鼠标滚轮 + 滑块，底部可拖动 |
| 请求频率限制 | 服务端令牌桶 120次/分钟 |
| 安全头 | CSP 限制脚本来源 + CORS 同源限制 |
| 速率限制 | 服务端令牌桶 120次/分钟，超限返回 429 |
| 交易记录 | 多账户登录，买卖记录增删改查，按周/月/年统计盈亏与胜率（详见 [docs/trades.md](docs/trades.md)） |

## 文件结构

```
visual/
├── server.py          # HTTP服务器 (ThreadingHTTPServer 多线程) + AlphaFeed API 代理
├── indicators.py      # 指标计算函数 (ema / atr / macd / kdj / rsi / force_index)
├── trades.py          # 交易记录后端 (DB / 鉴权 / CRUD / 统计)
├── static/            # 前端静态文件 (URL 仍为干净路径, 如 /trades.html /admin.html)
│   ├── index.html     # 前端单页面 (ECharts 5 CDN)
│   ├── trades.html    # 交易记录前端 (登录注册 + 统计界面)
│   └── admin.html     # 管理后台 (用户管理 + 量化模型管理, 仅 admin)
├── Dockerfile         # Docker 构建文件
├── docker-compose.yml # Docker Compose 配置
├── requirements.txt   # Python 依赖
├── docs/
│   └── trades.md      # 交易记录功能文档 (数据表 / API / 统计口径)
└── README.md          # 本文件
```

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /` | 提供 index.html |
| `GET /api/kline?symbol=600519.SH&period=1d&count=300` | K线数据 + 全部预计算指标 |
| `GET /api/quote?symbol=600519.SH` | 实时快照 |
| `GET /api/search?q=茅台` | 模糊搜索 (全量A股+ETF，内存+磁盘双层缓存，24h刷新) |
| `GET /api/ping` | 健康检查 |
| `POST /api/auth/login` | 登录，返回 `Set-Cookie: session` |
| `POST /api/auth/logout` | 登出（需登录） |
| `GET /api/auth/me` | 当前用户，返回 `{username,is_admin}`（未登录 401） |
| `GET /api/admin/users` | 用户列表（仅管理员） |
| `POST /api/admin/users` | 添加用户 `{username,password}`（仅管理员） |
| `DELETE /api/admin/users/{id}` | 删除用户（仅管理员） |
| `POST /api/admin/users/{id}/reset-password` | 重置密码 `{password}`（仅管理员） |
| `GET /api/trades` | 交易记录列表（需登录，`status/symbol/q/from/to/limit/offset`） |
| `POST /api/trades` | 新建交易记录 |
| `PUT /api/trades/{id}` | 更新交易记录 |
| `DELETE /api/trades/{id}` | 删除交易记录 |
| `GET /api/trades/stats?from=&to=` | 盈亏/胜率统计（按周/月/年分桶 + 按股票汇总 + 按模型汇总） |
| `GET /api/models` | 量化模型列表（需登录，含停用项） |
| `POST /api/models` | 新增量化模型 `{name,description}`（仅管理员） |
| `PUT /api/models/{id}` | 更新量化模型（仅管理员） |
| `DELETE /api/models/{id}` | 软删除量化模型（仅管理员，交易记录不受影响） |
| `POST /api/models/{id}/restore` | 恢复停用量化模型（仅管理员） |
| `GET /api/trade-reasons` | 预设买卖理由分类 |

## 技术细节

### 指标计算
- **服务端 Python** 计算
- MA5/MA10/MA20 使用 **SMA** (简单移动平均，算术平均)，对标东方财富/同花顺/通达信标准
- MACD/KDJ/RSI/ATR 算法详见 `indicators.py`，已逐项与东财 PC 端验证
- Elder 动力系统: EMA13 方向 + MACD 柱方向 → 蜡烛颜色（红=多/绿=空/蓝=中性）
- Elder ATR 通道: EMA13 ± 1×/2×/3× ATR，虚线叠加在 K线主图
- 浏览器端纯展示，不重复计算

### 自适应提示框
- 用 ECharts 底层 zrender canvas 的 `mousemove` 事件追踪鼠标像素坐标
- 与各 grid 像素位置比对，判断鼠标在哪个面板 → 显示对应数据

### 缓存策略
- 日K: 120s TTL
- 周/月K: 300s TTL
- 快照: 30s TTL
- 全量股票搜索列表: 24h TTL，内存 + 磁盘双层缓存；过期后 stale-while-revalidate（立即返回旧数据 + 后台刷新，不阻塞请求）

### 并发
- 服务端 `ThreadingHTTPServer` 多线程处理请求，多个 K线/搜索/快照请求并行互不阻塞

### 限流
- AlphaFeed 30次/分钟硬限制
- 服务端缓存减少 API 调用
- 前端交易时段30秒刷新间隔 → 安全范围

### 配置持久化
- `localStorage` key: `visual_chart_config` — 主题/面板/模式
- `localStorage` key: `visual_search_history` — 最近10条搜索记录

## 依赖

- Python: `alphafeed`, `numpy`, `pandas` (已存在于项目 venv)
- 前端: ECharts 5.5.0 (通过 CDN `https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js` 加载)
- 无额外 pip 依赖 (仅用 Python 标准库 `http.server`)

## 安全性

### 速率限制
服务端实现令牌桶算法，默认 120 次/分钟。超限返回 HTTP 429。

### CORS
限制为同源请求，避免跨域滥用。默认监听 localhost。

### Content-Security-Policy
所有响应包含 `Content-Security-Policy` 头，限制脚本来源仅为 `self` 和 `cdn.jsdelivr.net`。

### 错误信息过滤
敏感关键词（api key, token, auth 等）在错误响应中被过滤。

## 交易记录

主页左上角「📒 交易记录」入口，未登录须先登录，数据按账户隔离。支持录入股票代码/名称、买入价、退出价、数量、买卖日期、买卖理由；`closed` 平仓记录须填全卖出字段才算一笔交易完整结束。

**量化模型**：每条交易可关联一个量化模型（A–E，对齐回测管线），默认「无」。模型列表全局共享，由管理员增删改查（含 `name` 与 `description` 策略描述），普通用户只读；「删除」为软删除（停用，不物理删除、不动交易记录），历史统计永久可追溯。买卖理由新增「动力红转」（卖出）「动力绿转」（买入）「动力蓝转」（买入）。

**账户管理**：已关闭公开自助注册，新用户只能由管理员添加。管理员在「用户管理」区可添加 / 列表 / 删除 / 重置密码用户；仅单一管理员，由环境变量在首次启动时引导创建。

统计维度：按周/月/年查看总盈亏、胜率、盈亏比、最大单笔盈利/亏损、平均持仓天数，及按股票、按量化模型汇总。完整的数据表结构、API 端点、统计公式见 [docs/trades.md](docs/trades.md)。

- 数据库：SQLite，文件 `data/trades.db`（运行时自动创建，`data/` 不入库）
- 口令：PBKDF2-SHA256（20 万次迭代）+ 随机盐
- 会话：`secrets.token_hex(32)`，30 天过期，`HttpOnly` + `SameSite=Lax` Cookie
- 管理员：`.env` 中配置 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，服务启动时若无管理员则自动创建（仅一次，不覆盖已改口令）

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
| 管理员账号 | `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 首次启动自动创建管理员（仅一次） |
| 用户 | `appuser` (非 root) | 降低容器逃逸风险 |

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
