# vendor/ — 自托管第三方库

## echarts-5.5.0.min.js

| 项 | 值 |
|---|---|
| 上游 | `https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js`（npm 包 `echarts@5.5.0`） |
| 构建 | 全量构建（非 common/simple） |
| 字节数 | 1,029,203 |
| sha256 | `42f8329d989b6f6539dd2b15bbdf0d82025762ac112fbb60dc57b27d7bcf3946` |

引入处：`static/index.html`、`static/trades.html` 的 `<script src="/vendor/echarts-5.5.0.min.js">`。

### 为什么自托管

此前图表运行时只依赖 ECharts。此前走 jsdelivr：CDN 不可达时（国内偶发 DNS 污染/超时）
图表页会白屏，而这是个「图表挂了就没意义」的应用；且浏览器 HTTP 缓存早已按顶层站点分区，
「多站共享 CDN 缓存」的老红利不复存在。自托管后 CSP 的 `script-src` 才能收成
`'self' 'unsafe-inline'`（第三方脚本源清零）。

### 为什么用全量构建

`echarts.common.min.js`(660,988 B)、`echarts.simple.min.js`(467,183 B) 的图表类型只有
折线/柱状/饼/散点，**不含 candlestick**，本项目 K 线必须用全量。

### 不放 `.map`

该文件不带 `sourceMappingURL` 注释（已核），浏览器不会去取 source map，所以 vendor 目录
不需要放 `.map`，也无需为 `/vendor/*.js.map` 加 404 守卫。

## Vue 状态运行时

| 文件 | 上游 npm 包 | 字节数 | sha256 |
|---|---|---:|---|
| `vue-3.5.13.global.prod.js` | `vue@3.5.13` | 157,924 | `c459ba7cc8db65c982589fa5d64c7ff478877e8e5b0fd75683207cec6a4e89e8` |
| `vue-demi-0.14.10.iife.js` | `vue-demi@0.14.10` | 3,669 | `783e3e694be8ccc60b911b8100696a5b12284b2a64fa67199fabf00fa94a042f` |
| `pinia-2.2.6.iife.prod.js` | `pinia@2.2.6` | 6,069 | `eec33c1f90b55cc2c9df85b022f067c5a3855f74c0dc399b7d57101552117ff2` |

它们只从本站 `/vendor/` 加载，供 `static/js/market-store.js` 建立统一响应式行情状态；ECharts 仍负责图表绘制。

### 缓存

文件名带版本号 → `app.py:_send_static` 对它下发 `public, max-age=31536000, immutable`
（见 `_IMMUTABLE_VENDOR`）；HTML 与无版本的 `/js/*.js` 保持 `no-store`，改了立刻生效。

## 升级步骤

1. 下载新版本全量构建（文件名带新版本号）：
   ```bash
   curl -fL -o static/vendor/echarts-5.6.0.min.js \
     https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js
   ```
2. 改 `static/index.html`、`static/trades.html` 两处 `<script src>`。
3. 更新本文件的字节数与 sha256：`sha256sum static/vendor/echarts-5.6.0.min.js`
4. 确认线上已换用新文件名后，删除旧版本文件。
5. `python -m unittest discover -s visual/test` —— `test/test_static_assets.py` 会校验
   版本号、字节数与 sha256 与本文件记录一致（照抄上游、防替换/损坏）。
