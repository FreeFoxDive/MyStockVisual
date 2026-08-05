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
| MACD 模式 | 默认(12/26/9)、MACD(13/30/10)，随视图自动切换 |
| 自适应提示框 | 鼠标在不同面板显示对应数据(非全部指标) |
| 股票搜索 | 模糊匹配代码/名称，实时下拉 + 键盘↑↓导航 |
| 搜索历史 | localStorage 持久化最近10条，刷新不丢失 |
| 配置持久化 | 主题/面板开关/MACD模式/提示框选项自动保存 |
| 主题 | 默认亮色 / 可切换暗色 |
| 实时刷新 | 交易时段每30秒自动拉取快照 |
| 数据缩放 | 鼠标滚轮 + 滑块，底部可拖动 |
| 请求频率限制 | 服务端令牌桶 120次/分钟 |
| 安全头 | CSP 限制脚本来源 + CORS 同源限制 |
| 速率限制 | 服务端令牌桶 120次/分钟，超限返回 429 |

## 文件结构

```
visual/
├── server.py          # HTTP服务器 (http.server) + AlphaFeed API 代理
├── index.html         # 前端单页面 (ECharts 5 CDN)
├── indicators.py      # 从 triple_screen_v5.py 提取的指标函数
│                      #   ema / atr / macd / kdj / rsi / force_index
├── Dockerfile         # Docker 构建文件
├── docker-compose.yml # Docker Compose 配置
├── requirements.txt   # Python 依赖
└── README.md          # 本文件
```

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /` | 提供 index.html |
| `GET /api/kline?symbol=600519.SH&period=1d&count=300` | K线数据 + 全部预计算指标 |
| `GET /api/quote?symbol=600519.SH` | 实时快照 |
| `GET /api/search?q=茅台` | 模糊搜索 (内存缓存全量A股，1小时刷新) |
| `GET /api/ping` | 健康检查 |

## 技术细节

### 指标计算
- **服务端 Python** 计算
- MA5/MA10/MA20 使用 **SMA** (简单移动平均，算术平均)，对标东方财富/同花顺/通达信标准
- MACD/KDJ/RSI/ATR 算法详见 `indicators.py`，已逐项与东财 PC 端验证
- 浏览器端纯展示，不重复计算

### 自适应提示框
- 用 ECharts 底层 zrender canvas 的 `mousemove` 事件追踪鼠标像素坐标
- 与各 grid 像素位置比对，判断鼠标在哪个面板 → 显示对应数据

### 缓存策略
- 日K: 120s TTL
- 周/月K: 300s TTL
- 快照: 30s TTL
- 全量股票搜索列表: 1小时 TTL

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

## Docker 部署

```bash
# 1. 创建 .env 文件 (填入 AlphaFeed API Key)
echo AF_API_KEY=your_key_here > visual/.env

# 2. 构建并启动
docker compose -f visual/docker-compose.yml up -d --build

# 3. 浏览器打开
# http://localhost:8888
```

```bash
# 查看日志
docker compose -f visual/docker-compose.yml logs -f

# 停止
docker compose -f visual/docker-compose.yml down
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 端口 | `127.0.0.1:8888` | 仅本地访问 |
| 缓存卷 | `.cache` | Docker volume 持久化 |
| 环境变量 | `visual/.env` | 通过 `env_file` 注入 |
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

## 主题

- **自动模式**（默认）：优先跟随系统暗色模式，回退到时间判断（6:00-18:00 亮色，其余暗色）
- **手动模式**：点击 🌙/☀️ 按钮锁定主题，再点恢复切换
- 手动操作后自动禁用跟随，按钮显示 `auto` 标识区分
