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

## 文件结构

```
visual/
├── server.py          # HTTP服务器 (http.server) + AlphaFeed API 代理
├── index.html         # 前端单页面 (ECharts 5 CDN)
├── indicators.py      # 从 triple_screen_v5.py 提取的指标函数
│                      #   ema / atr / macd / kdj / rsi / force_index
├── echarts.min.js     # ECharts 5.5.0 本地副本 (1MB)
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
- **服务端 Python** 计算，复用 `triple_screen_v5.py` 中已与东财验证过的算法
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
- 前端: ECharts 5.5.0 (本地文件 `echarts.min.js`)
- 无额外 pip 依赖 (仅用 Python 标准库 `http.server`)

## 配色

| 元素 | 亮色 | 暗色 |
|------|------|------|
| K线阳线 | `#ef232a` | `#ef5350` |
| K线阴线 | `#14b143` | `#26a69a` |
| MA5 | `#f5a623` | `#ffd54f` |
| MA10 | `#4a90d9` | `#42a5f5` |
| MA20 | `#ab47bc` | `#ab47bc` |
| MACD DIF | `#4a90d9` | `#42a5f5` |
| MACD DEA | `#f5a623` | `#ffd54f` |
