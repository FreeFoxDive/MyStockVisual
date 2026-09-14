# Visual — 本地股票K线可视化

[![Tests](https://github.com/FreeFoxDive/MyStockVisual/actions/workflows/visual-test.yml/badge.svg?branch=master)](https://github.com/FreeFoxDive/MyStockVisual/actions/workflows/visual-test.yml)
[![Docker Build](https://github.com/FreeFoxDive/MyStockVisual/actions/workflows/visual-docker.yml/badge.svg?branch=master)](https://github.com/FreeFoxDive/MyStockVisual/actions/workflows/visual-docker.yml)

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
| 均线 | MA5/MA10/MA20（SMA，白/黄/玫红系） |
| 可选指标面板 | MACD、KDJ、RSI、ATR、OBV，网页 checkbox 开关 |
| OBV 面板 | OBV + MAOBV(30)（东财/通达信口径），零轴参考线 |
| 成交量面板 | 可开关；叠加 VOL MA5/MA10/MA20（东财口径）；关闭时 K线自动拉高 |
| 筹码分布 | 日/周/月 K 股票/ETF，东财原版 CYQ 算法（始终日线粒度，回看窗口 210/600/1500 根日线，前复权），右侧叠加、与主图价格轴联动；含获利比例/平均成本/90·70成本区间 |
| 左侧信息栏 | **所有周期**显示「基本信息」(行业/总手/成交额/换手/量比/涨停价/跌停价/3·5·10日涨幅/PE/PB/交易状态)；盘中每 60s 刷新，标题栏 ◀ 可收起（收起后左缘 › 展开）。同一框内下方接「五档」(买1-5/卖1-5) **所有周期**可用（盘中约 3s 刷新，独立令牌桶 2/3×30/min）；整框显隐由面板栏「信息」勾选框控制，「五档」勾选框单独控制框内五档子块 |
| 换手/流值/份额/成交额 | 顶部信息栏：股票显示换手率与流通市值；ETF 显示份额；均显示当日成交额 |
| 动力系统 | Elder Impulse System — EMA13方向 + MACD柱方向决定蜡烛颜色(红多/绿空/蓝中性)，日/周/月K |
| ATR 通道 | EMA13 ± 1/2/3 ATR 共6条虚线，日/周/月K，默认关闭 |
| 跳空缺口 | 60m/日/周/月：前端扫描未回补缺口（最近 2 个），主图灰色 markArea；十字线落在灰区时提示价差；部分回补收缩；日K 随快照重算；默认开启 |
| 自适应提示框 | 鼠标在不同面板显示对应数据；MACD跟随面板开关，RSI/KDJ/ATR 独立提示框开关 |
| 股票搜索 | 模糊匹配代码/名称，实时下拉 + 键盘↑↓导航 |
| 搜索历史 | 跟账号持久化（兼浏览器本地），刷新不丢失 |
| 配置持久化 | 面板开关/指标参数/提示框选项跟账号服务器同步（`users.panel_config`），主题走本地共享键 |
| 主题 | 共享 `visual-theme`（`/css/theme.css` + `/js/theme.js`），亮/暗同步各页 |
| 实时刷新 | 交易时段每30秒自动拉取快照 |
| 数据缩放 | 鼠标滚轮 + 滑块，底部可拖动 |
| 请求频率限制 | 服务端令牌桶 120次/分钟 |
| 安全 | CSP、同源 Cookie 会话、CSRF 双提交、登录爆破锁定；SQL 参数化；日志密钥脱敏 |
| 交易记录 | 多账户登录，买卖记录增删改查；录入校验日期/日K振幅/成交量；按周/月/年统计盈亏与胜率（默认周，详见 [docs/trades.md](docs/trades.md)） |
| 持仓监控 | 授权用户填齐止盈/保本/止损（止盈>保本>止损）后盘中监控，钉钉 + ntfy 推送；关联模型的持仓在推荐周期到期日 10:00/14:00 提醒平仓 |
| 监控中心 | `/monitor.html`（工具栏「🛡 监控中心」进入）：持仓风控一览（现价距风控价距离、止损/保本/止盈/加速/涨停/到期徽标）、价格监控（趋势线跌破监控列表可停用、条件预警列表可启停/删除）、最近告警，以及非管理员自服务监控开关 |
| 图表画线 | 主面板 14 种工具（趋势线/射线/水平线/垂直线/折线/矩形/平行通道/斐波那契/回归通道/箭头/文本/多空仓位盒/测量尺）；磁吸、自动支撑压力线、画线修正建议、未来 8 根预测延伸；悬停显示线上相交价与相对 close 的 ±%；按 账户+代码+周期 服务器同步（详见 [docs/drawing-mode.md](docs/drawing-mode.md)） |
| 指标扩展 | BOLL(20,2) 主图叠加、WR/CCI/BIAS/DMI 独立面板（通达信口径）；面板图例常显最近 bar 数值；MA 与 BOLL 参数可视化编辑（客户端重算）；对数坐标轴开关；面板栏「信息/五档/MA设置/金叉死叉」收进「▾ 更多」默认收起，「动力系统/通道/缺口」排在筹码之后，金叉死叉默认开启 |
| 信号与形态 | MACD 金叉死叉 + MA5/20 交叉标记；K线形态识别（锤头/上吊/吞没/十字星/曙光初现/乌云盖顶/红三兵/三只乌鸦，`patterns.js`） |
| 复权切换 | 前/后/不复权三档，缓存按复权隔离（分钟周期固定前复权） |
| 快照 SSE 推送 | `/api/stream/quotes` 长连接推送（间隔 `QUOTE_SSE_INTERVAL` 默认 10s，上游 ≤ 快照限额 4/5）；断线/超限自动回退轮询 |
| 港股/美股 | AlphaFeed 源 日/周/月K + 快照；专用令牌桶 8/min（额度 10/min 的 4/5，env 可调）；v1 不含分时 |
| 搜索增强 | 覆盖 A股/ETF/指数/港股/美股，结果带类型徽标（指数/ETF/港/美）；排序 股票(含港/美) > ETF > 指数，下拉最多 50 条 |
| 价格预警 | 自定义条件（现价/涨跌幅 ≥/≤ 数值，AND 组合），监控循环盘中评估，30 分钟冷却，钉钉/ntfy 推送；工具栏 🔔 管理 |
| 趋势线监控 | 选中趋势线/射线/水平线 → 画线菜单「🔔监控」→ 强制命名 + 跌破幅度%（默认 个股 2 / ETF·指数 3）→ 现价跌破线值×(1−pct%) 时盘中推送钉钉/ntfy，每条线每日一次；监控配置随画线保存（含复权口径），拖动即跟随，预警弹窗可总览；日/周/月K 支持。监控中心「去图表」跳回该监控线所在周期（`/?symbol=…&period=1w`） |
| 条件选股 | `/screener.html`：MACD金叉 / 站上MA20 / 筹码获利盘 / RSI / 5日涨幅 条件组合后台扫描（`SCREENER_MAX_SYMBOLS` 控制范围） |
| 市场数据抽屉 | 主面板 📊：个股资金流向（东财 push2his→push2delay 自动回退）、龙虎榜、分红送配、公告（akshare/东财源 + 缓存）；麦蕊源：交易所公告（日期倒序）、主力净流入（当前股单票键值视图 + 全市场排名）、股东户数变化、十大股东、十大流通股东、解禁限售（含解禁市值/占流通股%） |

## 文件结构

```
visual/
├── server.py          # 入口: Flask create_app + Waitress
├── app.py             # Flask 工厂 / 静态页鉴权 / 后台任务启动
├── api/               # 所有 /api/* 路由 (Blueprint 包)
│   ├── __init__.py    # api_bp 蓝图 + 子模块 import 注册
│   ├── common.py      # 共用: JSON 响应 / 登录态助手
│   ├── auth.py        # /api/auth/*
│   ├── market.py      # /api/ping|search|quote|quotes|depth|pledge|quota
│   ├── kline.py       # /api/kline|kline/tail|chips|intraday
│   ├── trades.py      # /api/trades*|fees|trade-reasons|repo-maturity
│   ├── models.py      # /api/models*
│   ├── admin.py       # /api/admin/users*
│   ├── me.py          # /api/me/search-history、/api/me/panel-config、/api/monitor/status
│   └── drawings.py    # /api/drawings 画线同步 (用户+代码+周期)
├── security.py        # CSP / 限流 / 登录锁定 / 会话 Cookie / CSRF
├── market.py          # 行情代理、缓存、质押等数据层
├── kline_source.py    # K线数据源注册/回退路由 (KLINE_SOURCE_* 配置)
├── logger.py          # 日志配置 + 密钥脱敏
├── indicators.py      # 指标计算
├── chips.py           # 东财筹码分布 (CYQ 算法移植 + 取数/缓存)
├── trades.py          # 交易记录后端 (DB / 鉴权 / CRUD / 统计)
├── monitor.py         # 持仓监控循环 (快照序列 + 告警 + 钉钉/ntfy)
├── feed.py            # AlphaFeed REST 行情接入 (令牌桶)
├── dingtalk.py        # 钉钉薄包装 (myappnotify)
├── ntfy.py            # ntfy 薄包装 (myappnotify)
├── market_hours.py    # A 股交易日历与时段
├── probe_feed.py      # 探测快照刷新频率 / 接口权限
├── smoke_server.py    # 冒烟测试工具 (临时 DB + 8899 端口完整服务)
├── test/
│   ├── test_trades.py
│   ├── test_monitor.py
│   ├── test_pledge.py
│   ├── test_kline_source.py
│   ├── test_logger_redact.py
│   ├── test_flask_auth.py
│   └── fixtures/
├── static/
│   ├── css/theme.css  # 共享亮暗主题变量
│   ├── js/theme.js    # 主题读写 (visual-theme)
│   ├── js/api.js      # fetch + CSRF 头
│   ├── js/gaps.js     # 缺口扫描
│   ├── js/chips.js    # 筹码分布叠加渲染
│   ├── js/drawings.js # 画线模式纯逻辑 (坐标/命中/磁吸/ZigZag/评分/回归)
│   ├── index.html
│   ├── trades.html
│   ├── admin.html
│   └── login.html
├── Dockerfile
├── docker-compose.yml
├── requirements.txt   # Python 依赖
├── docs/
│   ├── trades.md      # 交易记录功能文档 (数据表 / API / 统计口径)
│   └── drawing-mode.md # 图表画线模式文档 (工具 / 智能辅助 / 数据模型 / 同步)
└── README.md          # 本文件
```

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /` | 提供 index.html |
| `GET /api/kline?symbol=600519.SH&period=1d&count=1006` | K线数据 + 全部预计算指标（含 OBV/MAOBV/量均线）+ 流通股本元数据（日K默认 1006≈3年可见+RSI250 warmup） |
| `GET /api/kline/tail?symbol=600519.SH&period=1d&count=1006&n=2` | 末 N 根日/周/月K（含全部指标），供图表增量刷新。与 `/api/kline` 同口径，`count` 须与图表一致；短 TTL（`KLINE_TAIL_TTL`，默认 8s）。前端据此更新末根，不再自行拼 bar |
| `GET /api/quote?symbol=600519.SH` | 实时快照（含换手率，AF 小数→百分数；附 `is_trading_day`） |
| `GET /api/chips?symbol=600519.SH&period=1w` | 筹码分布（股票/ETF；`period` 1d/1w/1M 决定日线回看窗口 210/600/1500 根，返回直方图+汇总+`source`/`period`；指数返回 null）。默认 AlphaFeed 近似（`CHIPS_SOURCE=af`），`em` 切东财精确源 |
| `GET /api/depth?symbol=600519.SH` | 五档盘口（所有周期可用；独立令牌桶 2/3×30/min，失败返回 `depth=null`） |
| `GET /api/stock-info?symbol=600519.SH` | 侧栏基本信息（行业/总手/成交额/换手/量比/涨停跌停/N日涨幅/PE/PB/交易状态；麦蕊 + 日K，整包 60s 缓存，港/美股降级为 None） |
| `GET /api/cn/fund-flow?symbol=` | 个股资金流向（东财，15min 缓存） |
| `GET /api/cn/lhb?symbol=` | 龙虎榜（近 7 日，30min 缓存） |
| `GET /api/cn/dividends?symbol=` | 分红送配（1h 缓存） |
| `GET /api/cn/announcements?symbol=` | 公告（巨潮/akshare，近 90 天，30min 缓存） |
| `GET /api/cn/exchange-announcement?symbol=` | 交易所公告（麦蕊 `/hsstock/announcement`，源升序→按日期倒序展示，30min 缓存） |
| `GET /api/cn/zljlr?symbol=` | 当前股票主力净流入（单票键值视图：主力净额/率、主力流入/流出、净额/率、量价换手、全市场排名；麦蕊 `/higg/zljlr` 全市场快照，10min 缓存） |
| `GET /api/cn/holder-change?symbol=` | 股东户数变化趋势（麦蕊 `/hscp/gdbh`，6h 缓存） |
| `GET /api/cn/top-holders?symbol=` | 十大股东（最新报告期展平，麦蕊 `/hscp/sdgd`，6h 缓存） |
| `GET /api/cn/float-holders?symbol=` | 十大流通股东（麦蕊 `/hscp/ltgd`，6h 缓存） |
| `GET /api/cn/unlock?symbol=` | 解禁限售（麦蕊 `/hscp/jjxs`，12h 缓存；附 解禁市值(亿)、解禁均价(元)=市值÷数量、占流通股% = 解禁数量÷流通股本） |
| `GET /api/search?q=茅台` | 模糊搜索 (全量A股+ETF，内存+磁盘双层缓存，24h刷新；最多返回 50 条，排序 股票>ETF>指数) |
| `GET /api/ping` | 健康检查 |
| `POST /api/auth/login` | 登录，返回 `Set-Cookie: session` |
| `POST /api/auth/logout` | 登出（需登录） |
| `GET /api/auth/me` | 当前用户，返回 `{username,is_admin,monitor_enabled}`（未登录 401） |
| `GET /api/drawings?symbol=&period=` | 当前用户的图表画线（用户+代码+周期隔离） |
| `PUT /api/drawings` | 整体保存画线 `{symbol,period,drawings}`（登录+CSRF，≤500条） |
| `DELETE /api/drawings?symbol=&period=` | 清空该代码/周期的画线（登录+CSRF） |
| `GET /api/trendline-monitors` | 当前用户的趋势线监控列表（配置存于画线 `monitor` 字段，只读展示） |
| `PUT /api/trendline-monitors/{drawing_id}` | `{enabled}` 启停该画线的趋势线监控（只改 `monitor.enabled`，画线本身保留） |
| `GET /api/monitor/overview` | 监控页主数据：持仓风控 + 趋势线监控 + 条件预警 + 最近告警（均限当前用户，`?all=1` 管理员可放宽持仓范围） |
| `GET/POST /api/alerts`、`PUT/DELETE /api/alerts/{id}` | 条件价格预警 CRUD（`rule` 为 1-3 条 `{metric,op,value}`，AND 组合） |
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
- 主图 OHLC 与 MA/MACD/RSI/KDJ/ATR / 动力系统均为**前复权**（与东财默认观感一致）；成交校验 `get_daily_bar` 与监控 1m 仍用**未复权**真实价
- MA5/MA10/MA20 使用 **SMA** (简单移动平均，算术平均)，对标东方财富/同花顺/通达信标准
- MACD/KDJ/RSI/ATR/OBV 算法详见 `indicators.py`，已逐项与东财 PC 端验证
- OBV(能量潮): 涨累加量、跌累减量、平不变，首根为 0；MAOBV = OBV 的 SMA(30)（东财/通达信默认）
- OBV 从**图表加载的首根 K 线**起算（累计量）：绝对数值随数据加载窗口变化，跨平台/跨加载根数不可直接比较（比较请看走势）。东财会随滚动懒加载历史而改变 OBV 起点，故其数值与我们的默认加载根数不同属正常
- 成交量均线: VOL MA5/MA10/MA20（东财/通达信默认）
- Elder 动力系统: EMA13 方向 + MACD 柱方向 → 蜡烛颜色（红=多/绿=空/蓝=中性）
- Elder ATR 通道: EMA13 ± 1×/2×/3× ATR，虚线叠加在 K线主图
- 当日 bar 与其全部指标**只由后端产出**：前端只按 `date` 合并 `/api/kline/tail` 下发的权威末根（`applyServerBars`），不再从快照拼 bar、不再本地重算指标
- 换手率/振幅由 AlphaFeed `ext`（小数）统一 ×100 为百分数；流通/总股本取 `instruments.ext`（24h 缓存）

### 筹码分布 (CYQ)
- 服务端 `chips.py` 移植东财官网 `CYQCalculator`：150 价格分桶 + 按日换手率衰减 + 三角分布（一字板特例）
- 数据源由 `CHIPS_SOURCE` 控制：
  - `af`（**默认**）：AlphaFeed 前复权日K + 当前流通股本回算换手率（`换手=成交量/流通股本`），近似、不依赖东财；
  - `em`：东财 `push2his` 日K（`fqt=1` 前复权，`f61` 真实换手率），精确；走 `http→https→备用主机` 回退重试；
  - `auto`：先东财，失败再 AlphaFeed。
- 前复权价与图表价格轴对齐；支持股票/ETF（日/周/月 K），指数返回 `chips=null`（无份额与换手率，筹码对其无意义）；内存 TTL 300s（失败 30s）
- ETF 的换手率分母用 AlphaFeed 的**份额**（`float_shares`）回算，与东财 `f61` 口径实测一致；份额随申赎变动，历史值为近似。源未返回换手率（全 0）时视为无数据（`_has_turnover` 守卫），`auto` 模式回退 AF
- **算法粒度始终为日线**（分辨率最高）；周/月 K 只是把回看窗口加长（`WINDOW_BY_PERIOD`：1d/1w/1M = 210/600/1500 根日线），不换成周/月 bar 重算（周期 bar 会把区间成交摊在一根 H-L 上，反而失真）。因此同一只股在周/月 K 看到的筹码数值与日 K 不同属正常，摘要标题会标注「周窗口/月窗口」
- 前端 `chips.js` 右侧叠加横向直方图（基线在 K 线右缘、向右延伸），按末收价分获利(红)/套牢(绿)，随 dataZoom 与主图价格轴联动；摘要位于筹码条下方右侧窄带，`source=af` 时标注「近似」
- 摘要为**两列**（标签左对齐、数值右对齐成列；`summaryRows` 结构化返回，`paintLabels` 支持 `align`），**字号自适应 13→10px**（基准 13px，仅窄窗/矮带逐级减小以免超宽或超出画布），价格最多 3 位小数并去掉尾随 0（`4.690→4.69`、`3.4000→3.4`）；获利/均本/中位价数值分别用红/黄/紫与对应线同色。筹码带占右侧 17%（`rightPct`，overlay 83%~97%），给两列文本留出宽度（实测 1200px 图宽用 13px，1050px 降到 11px，均不溢出）
- 价格字段由服务端按 `PRICE_DP=3` 输出（`min/max/avgCost/medianCost/pct90/pct70/桶价`）：低位 ETF（0.5 元级）的第 3 位小数有意义（如 `0.332`、`0.301~0.358`）；曾统一取整到 2 位，导致前端的 3 位小数格式实际显示不出来
- 筹码 y 轴 = 主图可见价格范围（通道开启时并入可见 `EMA13±3ATR` 带），保证与主图价格轴对齐不错位
- 渲染时把 150 桶按**当前可见价格区间线性插值重采样**到「约 1.5px/根」（`TARGET_PX`），缩放/拉伸窗口时保持细密，不会因主图变高而变粗
- 汇总口径：同时返回并显示两种成本线——**均本**（`avgCost`，加权平均 `Σ价×筹码/Σ筹码`，语义更准、对齐东财 App）与**中位价**（`medianCost`，50% 分位中位成本，对齐东财网页版原生输出）；均本为黄色虚线、中位价为紫色点线，右侧数字标签与摘要中对应文本均与线同色，颜色随亮/暗主题切换；90%/70% 成本为分位区间（与口径无关）
- 汇总指标在桶内**线性插值**（不按整桶取价）：否则切换周期改变价格轴下界→分桶变粗，`profitRatio`/`pct90` 会摆动数个百分点，而筹码分布其实没变。插值后高换手标的（如 ETF）三周期数值基本一致，低换手标的仍能真实反映长历史成本

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

### K线数据源配置
K线按类别（分钟/股票/指数/基金）经 `kline_source.py` 注册表路由，主源失败自动回退下一源；`.env` 可自定义每类的源链（逗号分隔，依次尝试）：

```bash
KLINE_SOURCE_MINUTE=alphafeed,akshare      # 默认
KLINE_SOURCE_STOCK=mairui,alphafeed,akshare # 默认
KLINE_SOURCE_INDEX=mairui,akshare           # 默认
KLINE_SOURCE_FUND=alphafeed,akshare         # 默认 (麦蕊 jj/lskx 无前复权)
```

| 数据源 | 分钟K | 股票/指数/基金 日周月K | 说明 |
|---|---|---|---|
| `mairui` | 5m/15m/30m/60m（`hszbl/fsjy`，仅未复权；1m/北交所不支持） | 股票等比前复权 `fr`；指数无复权；基金 `jj/lskx` 仅未复权 | 付费证书；分钟/基金在图表前复权链上会被跳过 |
| `alphafeed` | ✓ 全周期前复权 | 仅股票/ETF 日K（指数未验证） | 付费证书 `AF_API_KEY` |
| `akshare` | ✓（东财源，1m 仅近 5 个交易日） | ✓ 免费前复权 `qfq` | 无需 key，作兜底；东财限流期可能持续失败 |

- 改 `.env` 后重启生效；启动横幅会打印各类别实际生效的链
- 想强制单源（禁用回退）: `KLINE_SOURCE_STOCK=mairui`
- 想把麦蕊加入分钟回退: `KLINE_SOURCE_MINUTE=alphafeed,mairui,akshare`（仅 `adjust=none` 时可用；数据滞后会被新鲜度守卫拦截，末根 bar 距今 >30 天视为失败）
- 基金日K口径：默认链 AlphaFeed/东财 volume 为「手」，与快照一致；麦蕊 `jj/lskx` 为「股」，仅在显式配置且未复权时可用
- ETF 溢价线用未复权收盘价对齐单位净值；股吧链接 ETF 带 `sh`/`sz` 前缀（如 `list,sh588200.html`）
- `/api/kline` 响应 `meta.source` 返回实际服务的数据源，便于观察回退是否生效
- 五档（`/api/depth`，所有周期）独立令牌桶，速率 = `AF_DEPTH_RATE_PER_MIN`（默认 30，实测 depth 限额 30/min）× 2/3 = 20/min；2s 缓存去重，仅盘中轮询
- 麦蕊特色数据（侧栏基本信息 / 抽屉新标签）：全市场榜单（`/himk/roe`、`/higg/zljlr`）单次请求 + 进程内长缓存（ROE 12h、主力净流入 10min、股东 6h、解禁 12h、公告 30min、个股基础信息 24h、行业 7d），基本信息整包 60s；`/api/stock-info` 换股/换周期即时拉取，盘中另每 60s 刷新一次。量比盘中按已过交易分钟折算，非盘中按最近一个交易日全天 240 分钟口径（休市也能看到最近量比）。付费版额度见 [MAIRUI_API.md](../MAIRUI_API.md)
- ⚠️ **麦蕊股票日K当日 bar 盘中为滞后/部分成交快照**：如 601058.SH 2026-09-10，麦蕊 `low=14.30/vol=144462`，而实时快照与 AlphaFeed 为 `low=14.18/vol=264196`（当日无除权）。成交校验与日K图表当日 bar 均已改为以实时快照为准（`market.get_daily_bar` / `_maybe_append_today_bar`）；非交易日、停牌（volume=0）不覆盖。详见 [docs/known-issues.md](docs/known-issues.md)
- 磁盘缓存按**数据源链隔离**（key 含 `kline_source.chain_tag`）：改 `KLINE_SOURCE_*` 后旧源缓存自动失效，不会串源；改配置仍需重启生效（`.env` 仅启动时加载）

### 配置持久化
- 面板设置（面板开关/指标参数/提示框/复权/折叠状态）：服务端跟账号存（`users.panel_config`，`/api/me/panel-config`），默认开启、跨设备恢复；`localStorage` key `visual_chart_config` 为本地镜像与离线兜底
- 主题：本地共享键 `visual-theme`（`/js/theme.js`），亮/暗跨页同步，不做账号同步
- 搜索历史：服务端跟账号存（`users.search_history`），前端可与本地合并同步

## 依赖

- Python: `flask`, `waitress`, `alphafeed`, `numpy`, `pandas`, `akshare`, `mairui`, `pandas_market_calendars`（见 `requirements.txt`）
- 前端: ECharts 5.5.0 (自托管于 `static/vendor/echarts-5.5.0.min.js`，来源与 sha256 见 `static/vendor/README.md`)

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

`discover` 会跑前端逻辑的 **Node 镜像测试**（`test_daily_tail_js.py`、`test_chart_patch_js.py`、`test_quote_poll_js.py`、`test_gaps_js.py`、`test_chips_js.py`、`test_patterns_js.py`、`test_drawings_js.py`），需本机安装 **Node.js**；无 Node 时这些用例 skip，其余 Python mock 测试仍可通过。

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
所有响应包含 `Content-Security-Policy` 头，脚本来源仅为 `self`（含内联脚本所需的 `'unsafe-inline'`），
无任何第三方脚本源：ECharts 自托管于 `static/vendor/`。
带版本号的 vendor 资源发 `public, max-age=31536000, immutable`，其余静态（HTML、`/js/*.js`）发 `no-store`。

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
| 🔘 白/黑系 | MA5 / DIF / K / RSI1 / OBV / VOL MA5 | `#1a1a1a` | `#eeeeee` |
| 🟡 黄色系 | MA10 / DEA / D / RSI2 / MAOBV / VOL MA10 | `#d4a017` | `#f5c542` |
| 🟣 玫红系 | MA20 / J / RSI3 / VOL MA20 | `#c2185b` | `#e84698` |
| ⚪ 灰色系 | ATR | `#888888` | `#5a5a5a` |
| 🔵 蓝色系 | 动力系统中性 | `#5b8ff9` | `#5b8ff9` |
| 🩶 蓝灰系 | ATR通道 ±1/2/3 | `#90a4ae` / `#78909c` / `#546e7a` | 同亮色 |
| 🟥🟩 筹码 | 获利盘(≤现价) / 套牢盘(>现价) / 平均成本 | `#ef232a` / `#14b143` / `#d4a017` | `#ef5350` / `#26a69a` / `#f5c542` |

## 主题

- **自动模式**（默认）：优先跟随系统暗色模式，回退到时间判断（6:00-18:00 亮色，其余暗色）
- **手动模式**：点击 🌙/☀️ 按钮锁定主题，再点恢复切换
- 手动操作后自动禁用跟随，按钮显示 `auto` 标识区分
