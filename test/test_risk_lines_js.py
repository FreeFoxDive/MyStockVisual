# -*- coding: utf-8 -*-
"""持仓风控线 (止盈/保本/止损) 前端测试: 静态结构断言 + 取价/建线行为镜像。

覆盖点:
  * 工具栏三个档位数值 + 面板栏「风控线」勾选框 (非持仓时动态隐藏, 不占位);
  * 取价口径: 取最新一笔有风控价的持仓, 排除已平仓与逆回购, 多笔不跨交易拼线;
  * 主图虚线: 与现价线同款样式, 配色走 --risk-* 亮暗两套变量;
  * markLine 只接受单个对象 (传数组会被整段忽略), 故风控线与现价线合并进同一个
    markLine 的 data, 并用真实 ECharts 渲染断言 4 条标注都画得出来;
  * 现价线调用点全部改走 buildKlineMarkLines, 保证 data 项顺序与 index 一致。

运行:
    venv/Scripts/python.exe -u visual/test/test_risk_lines_js.py
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
THEME_CSS = _VISUAL_DIR / "static" / "css" / "theme.css"
ECHARTS_JS = _VISUAL_DIR / "static" / "vendor" / "echarts-5.5.0.min.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 function {name}")
    start = src.index("{", m.end() - 1)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    raise AssertionError(f"function {name} 大括号不配对")


def _extract_const(src: str, name: str) -> str:
    """抽取 `const NAME = ...;` (数组字面量按方括号配平, 标量取到分号)。"""
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name}")
    if src[m.end()] == "[":
        start = m.end()
        depth = 0
        for i in range(start, len(src)):
            if src[i] == "[":
                depth += 1
            elif src[i] == "]":
                depth -= 1
                if depth == 0:
                    return src[m.start():i + 1] + ";"
        raise AssertionError(f"const {name} 方括号不配对")
    end = src.index(";", m.end())
    return src[m.start():end] + ";"


class RiskLinesStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.css = THEME_CSS.read_text(encoding="utf-8")

    def test_toolbar_has_no_risk_values_but_holding_badge(self):
        """风险数值不再占工具栏 (只在图上与徽章悬停里), 代码/名称旁改标「持仓中」。"""
        group = self.src[self.src.index('class="info-group"'):self.src.index('class="toolbar-right"')]
        self.assertIn('id="info-holding"', group, "缺少持仓中徽章")
        badge = group[group.index('id="info-holding"'):]
        badge = badge[:badge.index("</span>")]
        self.assertIn("持仓中", badge)
        self.assertIn('style="display:none"', badge, "非持仓须隐藏")
        # 徽章用主题变量 (亮暗自动适配), 与交易记录页 .badge.open 同一观感
        css = self.src[self.src.index(".info-group .hold-badge"):]
        css = css[:css.index("}")]
        self.assertIn("rgba(var(--up-rgb), 0.12)", css)
        self.assertIn("var(--up-text)", css)
        # 旧的风控数值标记/样式必须清干净, 否则又变成两处维护
        for gone in ("info-tp", "info-be", "info-sl", "risk-val", "risk-label",
                     ".info-group .risk-tp", ".info-group .risk-be", ".info-group .risk-sl"):
            self.assertNotIn(gone, self.src, f"{gone} 应已移除")
        # 图上线仍要三条: RISK_ITEMS 只保留线与标签需要的字段
        items = _extract_const(self.src, "RISK_ITEMS")
        for gone in ("boxId", "valId"):
            self.assertNotIn(gone, items, f"RISK_ITEMS 不再需要 {gone}")

    def test_price_tag_prefixed(self):
        """四个签都自带名称: 避让把谁推开十几像素也能一眼对上 (现价签原本只有裸价格)。"""
        body = _extract_fn(self.src, "buildLastPriceLine")
        self.assertIn("'现价 ' + fmtPrice3(price)", body)

    def test_all_price_tags_share_fg_token(self):
        """现价签与风控签共用 --risk-fg: 亮色白字 / 暗色深字, 两主题下四签同色。"""
        self.assertIn("'last', C().riskFg, rising ? C().upChip : C().downChip)",
                      _extract_fn(self.src, "buildLastPriceLine"),
                      "现价签: 字色走 --risk-fg, 底色走签专用的 --up-chip/--down-chip")
        self.assertIn("it.key, C().riskFg)", _extract_fn(self.src, "buildRiskLineItems"))
        # 画线层左缘框签 (水平线价签/监控名称签) 同色, 免得同一列两种字色
        self.assertIn("C().riskFg", _extract_fn(self.src, "paintTag"))
        self.assertNotIn("'#fff'", _extract_fn(self.src, "paintTag"))

    def test_symbol_link_replaces_guba_tag(self):
        """「股吧 ↗」独立标签去掉, 代码·名称整体成一个链接: 点代码复制, 点名称跳股吧。"""
        group = self.src[self.src.index('class="info-group"'):self.src.index('class="toolbar-right"')]
        self.assertNotIn("info-guba", self.src, "独立股吧标签应已移除")
        link_start = group.index("<a")
        link = group[link_start:group.index("</a>", link_start)]
        self.assertIn('id="info-sym"', link)
        self.assertIn('id="info-code"', link, "代码在链接内")
        self.assertIn('id="info-name"', link, "名称在链接内")
        self.assertIn('title="点击复制代码"', link, "代码保留自己的复制提示 (就近生效)")
        self.assertIn("noopener", link, "外链安全属性")
        # 初始标记不带 href/title: 首屏渲染前 (或首拉失败时) 点名称不得开一个空白 # 页,
        # 两者都等 updateSymbolLink 拿到股吧地址后再挂上
        self.assertNotIn('href="', link)
        self.assertNotIn('title="去股吧"', link)
        self.assertIn("el.href = url", _extract_fn(self.src, "updateSymbolLink"))
        self.assertIn("el.title = '去股吧'", _extract_fn(self.src, "updateSymbolLink"))
        # 持仓徽章在链接之外: 点它不该跳股吧
        self.assertNotIn("info-holding", link)
        self.assertLess(group.index("</a>"), group.index('id="info-holding"'),
                        "徽章应在链接闭合之后 (链接的兄弟节点)")
        # 样式: 默认不画下划线, 悬停只给名称加; 且只在真有 href 时才暗示可点
        css = self.src[self.src.index(".info-group .sym-link"):]
        css = css[:css.index(":hover") + 200]
        self.assertIn("text-decoration: none", css)
        self.assertIn(".info-group .sym-link[href]:hover .name", css)
        self.assertNotIn(".sym-link:hover #info-code", css)
        # 窄屏不再连代码/名称一起藏 (原来是藏「股吧」标签)
        self.assertNotIn("#toolbar #info-guba", self.src)
        for tag in ("#toolbar .oh, #toolbar #info-time", "#toolbar #info-code,"):
            self.assertIn(tag, self.src, f"{tag} 的响应式规则应保留")

    def test_toggle_is_dynamic_label(self):
        anchor = self.src.index('id="lbl-risk"')
        tag = self.src[self.src.rindex("<label", 0, anchor):self.src.index(">", anchor)]
        self.assertIn('style="display:none"', tag, "非持仓须隐藏勾选框")
        self.assertNotIn("extra-ind", tag, "extra-ind 由 CSS 控制显隐, 内联 display 会被覆盖")
        chk_anchor = self.src.index('id="chk-risk"')
        chk_tag = self.src[self.src.rindex("<input", 0, chk_anchor):self.src.index(">", chk_anchor)]
        self.assertIn("checked", chk_tag, "风控线默认开启")

    def test_config_key_wired_both_ends(self):
        self.assertIn("risk: document.getElementById('chk-risk').checked",
                      _extract_fn(self.src, "buildConfig"))
        self.assertIn("cfg.risk !== false", _extract_fn(self.src, "applyConfig"))

    def test_chart_colors_mapped(self):
        chart_map = self.src[self.src.index("const CHART_VAR_MAP"):]
        chart_map = chart_map[:chart_map.index("};")]
        for key, var in (("riskTp", "--risk-tp"), ("riskBe", "--risk-be"),
                         ("riskSl", "--risk-sl"), ("riskFg", "--risk-fg")):
            self.assertIn(f"{key}: '{var}'", chart_map, key)
        # 亮暗两套数值都定义在 theme.css (canvas 侧从计算值读取)
        light = self.css[self.css.index(":root {"):self.css.index('[data-theme="dark"]')]
        dark = self.css[self.css.index('[data-theme="dark"]'):]
        for var in ("--risk-tp", "--risk-be", "--risk-sl", "--risk-fg"):
            self.assertIn(f"{var}:", light, f"亮色缺 {var}")
            self.assertIn(f"{var}:", dark, f"暗色缺 {var}")
        # 字色跟主题正文同向: 亮色黑字 / 暗色白字 (用户指定)。暗色底色为此压深,
        # 用 WCAG 对比度实算而不是"平均亮度大小"的粗略判据。
        dark_fg = re.search(r"--risk-fg:\s*(#[0-9a-f]{6})", dark).group(1)
        light_fg = re.search(r"--risk-fg:\s*(#[0-9a-f]{6})", light).group(1)
        self.assertEqual(light_fg.lower(), "#000000", "亮色价格签为黑字")
        self.assertEqual(dark_fg.lower(), "#ffffff", "暗色价格签为白字")
        for var in ("--risk-tp", "--risk-be", "--risk-sl"):
            fill = re.search(re.escape(var) + r":\s*(#[0-9a-f]{6})", dark).group(1)
            self.assertGreaterEqual(self._contrast(dark_fg, fill), 4.5,
                                    f"暗色 {var}={fill} 配白字对比不足 4.5:1")

    @staticmethod
    def _contrast(fg: str, bg: str) -> float:
        """WCAG 相对亮度对比度 (1~21)。"""
        def lum(h):
            ch = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        a, b = lum(fg), lum(bg)
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    def test_all_markline_sites_use_builder(self):
        # 4 处调用点 + buildKlineMarkLines/refreshPriceTagLayout 各一处; buildLastPriceLine
        # 仅剩定义与 buildKlineMarkLines 内部调用
        self.assertEqual(self.src.count("buildKlineMarkLines("), 6)
        self.assertEqual(self.src.count("buildLastPriceLine("), 2)

    def test_sync_called_where_trades_change(self):
        fetch = _extract_fn(self.src, "fetchData")
        self.assertIn("setTrades(null)", fetch, "换股清空交易记录须同步持仓状态")
        self.assertIn("klineRenderKey(STATE.klineData) === STATE._fetchedKlineRenderKey", fetch,
                      "缓存命中已渲染过, 拿到交易记录后须补一次重绘")
        load = _extract_fn(self.src, "loadTrades")
        self.assertNotIn("STATE.trades = ", load, "交易记录写入须经 setTrades 统一刷新")
        self.assertEqual(load.count("setTrades("), 3, "成功/未授权/异常三条路径都要刷新")


class RiskLinesBehaviorTest(unittest.TestCase):
    """用真实 index.html 源码跑 pickOpenRiskPrices / buildRiskLines。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, payload):
        script = (
            _extract_const(self.src, "RISK_ITEMS") + "\n"
            + _extract_fn(self.src, "openPositions") + "\n"
            + _extract_fn(self.src, "pickOpenRiskPrices") + "\n"
            + _extract_fn(self.src, "priceLineLabel") + "\n"
            + _extract_fn(self.src, "priceLineItem") + "\n"
            + _extract_fn(self.src, "buildRiskLineItems") + "\n"
            + "const ENV = { on: true };\n"
            + "function riskOn() { return ENV.on; }\n"
            + "function C() { return { riskTp: '#c97a00', riskBe: '#1565c0', riskSl: '#7b3fa0', riskFg: '#f0f0f0' }; }\n"
            + "function fmtPrice3(v) { return Number(v).toFixed(3); }\n"
            + "const STATE = { risk: null, _tagSlots: null };\n"
            + "const p = JSON.parse(process.argv[1]);\n"
            + "const out = { picked: p.picks.map(t => pickOpenRiskPrices(t)), lines: [] };\n"
            + "for (const c of p.lines) {\n"
            + "  STATE.risk = c.risk; ENV.on = c.on;\n"
            + "  out.lines.push(buildRiskLineItems().map(l => ({ text: l.label.formatter,"
            + "    color: l.lineStyle.color, fg: l.label.color, dash: l.lineStyle.type,"
            + "    pos: l.label.position, ys: l.yAxis })));\n"
            + "}\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(
            ["node", "-e", script, json.dumps(payload)],
            capture_output=True, check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    def _picks(self, cases):
        return self._run({"picks": cases, "lines": []})["picked"]

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_pick_requires_open_position(self):
        picks = self._picks([
            [],
            None,
            [{"id": 1, "status": "closed", "entry_date": "2026-01-01",
              "take_profit": 11.2, "breakeven": 10.05, "stop_loss": 9.8}],
            # 逆回购 status 恒为 open, 与监控页一致地排除
            [{"id": 2, "status": "open", "type": "reverse_repo", "entry_date": "2026-02-01",
              "take_profit": 11.2}],
            [{"id": 3, "status": "open", "type": "batch", "entry_date": "2026-02-01",
              "take_profit": None, "breakeven": None, "stop_loss": None}],
        ])
        for i, got in enumerate(picks):
            self.assertIsNone(got, f"case {i} 不该产出风控价")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_pick_returns_all_filled_levels(self):
        picks = self._picks([[
            {"id": 1, "status": "open", "type": "simple", "entry_date": "2026-01-01",
             "take_profit": 11.2, "breakeven": 10.05, "stop_loss": 9.8},
        ]])
        self.assertEqual(picks[0], {"take_profit": 11.2, "breakeven": 10.05, "stop_loss": 9.8})

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_pick_prefers_latest_entry_and_never_mixes_trades(self):
        older = {"id": 1, "status": "open", "entry_date": "2026-01-01",
                 "take_profit": 11.2, "breakeven": 10.05, "stop_loss": 9.8}
        newer = {"id": 2, "status": "open", "entry_date": "2026-03-01",
                 "take_profit": 12.5, "breakeven": 11.0, "stop_loss": None}
        picks = self._picks([[older, newer]])
        self.assertEqual(picks[0], {"take_profit": 12.5, "breakeven": 11.0},
                         "多笔持仓取最新一笔, 不得跨交易补止损")
        # 同日按 id 大者优先 (列表顺序不影响结果)
        same_day = [
            {"id": 5, "status": "open", "entry_date": "2026-03-01", "stop_loss": 9.0},
            {"id": 7, "status": "open", "entry_date": "2026-03-01", "stop_loss": 8.5},
        ]
        self.assertEqual(self._picks([same_day])[0], {"stop_loss": 8.5})

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_pick_parses_strings_and_drops_invalid(self):
        picks = self._picks([[
            {"id": 1, "status": "open", "entry_date": "2026-01-01",
             "take_profit": "11.20", "breakeven": None, "stop_loss": 0},
        ]])
        self.assertEqual(picks[0], {"take_profit": 11.2}, "字符串价转数值; 0/空值不产出档位")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_lines_shape_and_colors(self):
        lines = self._run({"picks": [], "lines": [
            {"risk": {"take_profit": 11.2, "breakeven": 10.05, "stop_loss": 9.8}, "on": True},
        ]})["lines"][0]
        self.assertEqual([l["text"] for l in lines], ["止盈 11.200", "保本 10.050", "止损 9.800"])
        self.assertEqual([l["color"] for l in lines], ["#c97a00", "#1565c0", "#7b3fa0"])
        self.assertEqual([l["ys"] for l in lines], [11.2, 10.05, 9.8])
        for line in lines:
            self.assertEqual(line["dash"], "dashed", "样式与现价线一致")
            self.assertEqual(line["pos"], "insideStartBottom", "避让现价签")
            self.assertEqual(line["fg"], "#f0f0f0", "字色走 --risk-fg (暗色浅底翻深字)")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_lines_only_filled_levels_and_respect_toggle(self):
        out = self._run({"picks": [], "lines": [
            {"risk": {"take_profit": 11.2}, "on": True},
            {"risk": {"take_profit": 11.2}, "on": False},
            {"risk": None, "on": True},
        ]})["lines"]
        self.assertEqual([l["ys"] for l in out[0]], [11.2], "只画填了值的档位")
        self.assertEqual(out[1], [], "开关关闭不出线")
        self.assertEqual(out[2], [], "无持仓不出线")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_merged_markline_renders_in_real_echarts(self):
        """markLine 必须是单个对象: 早期实现返回 [风控..., 现价] 数组, ECharts 5 会把
        整个 markLine 忽略 —— 一条线都画不出来。这里用真实 ECharts 渲染 SVG 断言
        4 条标注都出现, 且各自的配色/档位对得上。"""
        payload = {
            # 三档风控价都落在 K 线区间内 (区间外的线 ECharts 本就不渲染)
            "bars": [{"open": 10, "close": 10.5, "low": 9.7, "high": 12.2},
                     {"open": 10.5, "close": 11.2, "low": 9.6, "high": 12.4}],
            "live": 11.3,
            "risk": {"take_profit": 12.0, "breakeven": 10.05, "stop_loss": 9.8},
        }
        script = (
            _extract_const(self.src, "RISK_ITEMS") + "\n"
            + _extract_fn(self.src, "priceLineLabel") + "\n"
            + _extract_fn(self.src, "priceLineItem") + "\n"
            + _extract_fn(self.src, "buildLastPriceLine") + "\n"
            + _extract_fn(self.src, "buildRiskLineItems") + "\n"
            + _extract_fn(self.src, "buildKlineMarkLines") + "\n"
            + "function riskOn() { return true; }\n"
            + "function C() { return { up: '#ef5350', down: '#26a69a',"
            + " riskTp: '#c97a00', riskBe: '#1565c0', riskSl: '#7b3fa0', riskFg: '#f0f0f0' }; }\n"
            + "function fmtPrice3(v) { return Number(v).toFixed(3); }\n"
            + "function resolveLastPrice(bars, livePrice) { return livePrice != null ? livePrice : bars[bars.length - 1].close; }\n"
            + "const echarts = require(process.argv[2]);\n"
            + "const p = JSON.parse(process.argv[1]);\n"
            + "const STATE = { symbol: 'X', risk: p.risk, _tagSlots: null };\n"
            + "const markLine = buildKlineMarkLines(p.bars, p.live);\n"
            + "const chart = echarts.init(null, null,"
            + " { renderer: 'svg', ssr: true, width: 600, height: 400 });\n"
            + "chart.setOption({ xAxis: { type: 'category',"
            + "   data: p.bars.map((_, i) => 'd' + i) },"
            + " yAxis: { type: 'value', scale: true },"
            + " series: [{ name: 'K线', type: 'candlestick',"
            + "   data: p.bars.map(b => [b.open, b.close, b.low, b.high]), markLine }] });\n"
            + "const svg = chart.renderToSVGString();\n"
            + "chart.dispose();  // SSR 下 zrender 动画循环会让 node 永不退出\n"
            + "process.stdout.write(JSON.stringify({ isArray: Array.isArray(markLine),"
            + " items: markLine.data.length, silent: markLine.silent, svg,"
            + " fgs: markLine.data.map(d => d.label.color),"
            + " texts: markLine.data.map(d => d.label.formatter) }));"
        )
        proc = subprocess.run(
            ["node", "-e", script, json.dumps(payload), str(ECHARTS_JS)],
            capture_output=True, check=True,
        )
        out = json.loads(proc.stdout.decode("utf-8"))
        svg = out.pop("svg")
        self.assertFalse(out["isArray"], "series.markLine 传数组会被 ECharts 整段忽略")
        self.assertEqual(out["items"], 4, "风控三条 + 现价线合并进一个 markLine")
        self.assertTrue(out["silent"])
        for text in ("止盈 12.000", "保本 10.050", "止损 9.800"):
            self.assertEqual(svg.count(text), 1, f"{text} 未画出")
        for color in ("#c97a00", "#1565c0", "#7b3fa0"):
            self.assertGreaterEqual(svg.count(color), 1, f"{color} 未用上")
        # 现价线在最右一根 close 之上 → 涨色
        self.assertGreaterEqual(svg.count("#ef5350"), 1, "现价线未画")
        # 四个签的字色必须同源 (亮色白字 / 暗色深字都由 --risk-fg 给), 不再现价签固定白字
        self.assertTrue(any("现价" in t for t in out["texts"]), "现价签文案丢了")
        self.assertEqual(set(out["fgs"]), {"#f0f0f0"}, f"四个签字色应一致: {out['fgs']}")


    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_position_badge_wiring(self):
        """syncPosition 的 DOM 接线: 徽章显隐 + 悬停文案 + 风控线开关显隐。

        口径: 「持仓中」徽章只看有没有 open 持仓 (不要求填风控价);
        「风控线」勾选框仍要求填了风控价 (没有价位就没有线可开关)。
        """
        payload = {"cases": [
            {"trades": None},
            {"trades": [{"id": 1, "status": "closed", "entry_date": "2026-01-01",
                         "take_profit": 11.2}]},
            {"trades": [{"id": 2, "status": "open", "type": "reverse_repo",
                         "entry_date": "2026-02-01", "take_profit": 11.2}]},
            # 持仓但没填风控价: 徽章显示, 开关不显示
            {"trades": [{"id": 3, "status": "open", "type": "simple", "entry_date": "2026-03-01",
                         "cost_price": 10.2, "quantity": 1000, "hold_days": 12}]},
            {"trades": [{"id": 4, "status": "open", "type": "simple", "entry_date": "2026-03-05",
                         "cost_price": 10.2, "quantity": 1000, "hold_days": 12,
                         "take_profit": 11.2, "breakeven": 10.05, "stop_loss": 9.8}]},
        ]}
        script = (
            _extract_const(self.src, "RISK_ITEMS") + "\n"
            + "".join(_extract_fn(self.src, n) + "\n"
                      for n in ("openPositions", "pickOpenPosition", "pickOpenRiskPrices",
                                "holdingTip", "syncPosition"))
            + "const nodes = { 'info-holding': { style: {}, title: '' }, 'lbl-risk': { style: {} } };\n"
            + "function document$getElementById(id) { return nodes[id] || null; }\n"
            + "const document = { getElementById: document$getElementById };\n"
            + "function fmtPrice3(v) { return Number(v).toFixed(2); }\n"
            + "const STATE = { trades: null, risk: null, position: null };\n"
            + "const out = [];\n"
            + "for (const c of JSON.parse(process.argv[1]).cases) {\n"
            + "  STATE.trades = c.trades; syncPosition();\n"
            + "  out.push({ risk: STATE.risk, posId: STATE.position && STATE.position.id,"
            + " badge: nodes['info-holding'].style.display, tip: nodes['info-holding'].title,"
            + " toggle: nodes['lbl-risk'].style.display });\n"
            + "}\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(
            ["node", "-e", script, json.dumps(payload)],
            capture_output=True, check=True,
        )
        out = json.loads(proc.stdout.decode("utf-8"))

        self.assertIsNone(out[0]["posId"], "无交易记录不显示徽章")
        self.assertEqual(out[0]["badge"], "none")
        self.assertEqual(out[0]["toggle"], "none")
        self.assertEqual(out[0]["tip"], "")
        self.assertIsNone(out[1]["posId"], "已平仓不算持仓")
        self.assertEqual(out[1]["badge"], "none")
        self.assertIsNone(out[2]["posId"], "逆回购 status 恒为 open, 但不算持仓")
        self.assertEqual(out[2]["badge"], "none")

        self.assertEqual(out[3]["posId"], 3, "持仓即显示徽章, 不要求填风控价")
        self.assertEqual(out[3]["badge"], "")
        self.assertEqual(out[3]["toggle"], "none", "没风控价就没有线可开关")
        self.assertIsNone(out[3]["risk"])
        for token in ("持仓中", "买入均价 10.20", "1000 股", "建议持有 12 交易日"):
            self.assertIn(token, out[3]["tip"], f"悬停文案缺 {token}")

        self.assertEqual(out[4]["risk"], {"take_profit": 11.2, "breakeven": 10.05, "stop_loss": 9.8})
        self.assertEqual(out[4]["badge"], "")
        self.assertEqual(out[4]["toggle"], "", "有风控价时勾选框可见")
        for token in ("买入均价 10.20", "止盈 11.20", "保本 10.05", "止损 9.80"):
            self.assertIn(token, out[4]["tip"], f"悬停文案缺 {token}")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_hold_days_null_is_not_shown(self):
        """回归: 后端未关联模型时 hold_days 返回 null, 不能落进 Number(null)===0
        显示成「持有 0 天」(口径也不是已持有天数, 是模型建议的持仓交易日)."""
        script = (
            _extract_const(self.src, "RISK_ITEMS") + "\n"
            + _extract_fn(self.src, "holdingTip") + "\n"
            + "function fmtPrice3(v) { return Number(v).toFixed(2); }\n"
            + "const p = JSON.parse(process.argv[1]);\n"
            + "process.stdout.write(JSON.stringify([holdingTip(p.nullDays), holdingTip(p.days)]));"
        )
        payload = {"nullDays": {"cost_price": 10.2, "quantity": 1000, "hold_days": None},
                   "days": {"cost_price": 10.2, "quantity": 1000, "hold_days": 5}}
        proc = subprocess.run(["node", "-e", script, json.dumps(payload)],
                              capture_output=True, check=True)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertNotIn("天", out[0], f"null 不该出现天数: {out[0]}")
        self.assertEqual(out[0], "持仓中 · 买入均价 10.20 · 1000 股")
        self.assertIn("建议持有 5 交易日", out[1])
        self.assertNotIn("持有 5 天", out[1], "不是已持有天数")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_badge_uses_latest_open_position(self):
        """多笔持仓时徽章跟最新一笔 (与图上风控价的取数同源, 都走 openPositions)。"""
        payload = {"cases": [{"trades": [
            {"id": 1, "status": "open", "entry_date": "2026-01-01", "cost_price": 9.0,
             "quantity": 100},
            {"id": 2, "status": "open", "entry_date": "2026-03-01", "cost_price": 10.2,
             "quantity": 1000, "stop_loss": 9.8},
        ]}]}
        script = (
            _extract_const(self.src, "RISK_ITEMS") + "\n"
            + "".join(_extract_fn(self.src, n) + "\n"
                      for n in ("openPositions", "pickOpenPosition", "pickOpenRiskPrices",
                                "holdingTip", "syncPosition"))
            + "const nodes = { 'info-holding': { style: {}, title: '' }, 'lbl-risk': { style: {} } };\n"
            + "const document = { getElementById: (id) => nodes[id] || null };\n"
            + "function fmtPrice3(v) { return Number(v).toFixed(2); }\n"
            + "const STATE = { trades: null, risk: null, position: null };\n"
            + "STATE.trades = JSON.parse(process.argv[1]).cases[0].trades;\n"
            + "syncPosition();\n"
            + "process.stdout.write(JSON.stringify({ posId: STATE.position.id,"
            + " tip: nodes['info-holding'].title, risk: STATE.risk }));"
        )
        proc = subprocess.run(["node", "-e", script, json.dumps(payload)],
                              capture_output=True, check=True)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(out["posId"], 2, "徽章取最新一笔持仓")
        self.assertIn("买入均价 10.20", out["tip"])
        self.assertEqual(out["risk"], {"stop_loss": 9.8}, "风控价取最新一笔带风控价的")


class PositionTradesSyncTest(unittest.TestCase):
    """交易记录/持仓在两条加载路径上的同步。

    回归 1: 分时换股不拉交易记录 → 「持仓中」徽章与图上风控线还是上一只股票的。
    回归 2: 迟到响应的竞态 —— 旧请求失败不能清掉新请求刚拿到的持仓。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_intraday_path_loads_trades_and_switch_clears(self):
        self.assertIn("loadTrades(symbol)", _extract_fn(self.src, "fetchIntraday"),
                      "分时也要拉交易记录 (分时图会画风控线/显示持仓徽章)")
        self.assertIn("setTrades(null)", _extract_fn(self.src, "resetSidePanelData"),
                      "换股先作废上一只的持仓")
        # 换股入口确实调用了它
        self.assertIn("resetSidePanelData()", _extract_fn(self.src, "loadCurrent"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_stale_response_does_not_clobber_new_data(self):
        script = (
            _extract_fn(self.src, "loadTrades") + "\n"
            + "const calls = []; const pending = [];\n"
            + "function setTrades(v) { calls.push(v); }\n"
            + "function fetch() { return new Promise((res) => pending.push(res)); }\n"
            + "const STATE = { _tradesSeq: 0, symbol: 'B', trades: null };\n"
            + "(async () => {\n"
            + "  const p1 = loadTrades('B');\n"
            + "  const p2 = loadTrades('B');\n"
            + "  pending[1]({ ok: true, json: () => Promise.resolve({ trades: [{ id: 9 }] }) });\n"
            + "  await p2;\n"
            + "  const afterNew = calls.length;\n"
            + "  pending[0]({ ok: false });\n"          # 旧请求迟到且失败
            + "  await p1;\n"
            + "  const out = { calls, afterNew };\n"
            + "  process.stdout.write(JSON.stringify(out));\n"
            + "})();"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, check=True)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(out["calls"], [[{"id": 9}]],
                         "迟到的失败响应不得清掉新请求拿到的持仓")
        self.assertEqual(out["afterNew"], 1)


class MarkLineLayoutRenderTest(unittest.TestCase):
    """真实 ECharts + 真实签位逻辑: 现价与风控价几乎同价时, 两个签必须错开。

    走的就是生产路径: 建图 → 用真实投影算签位 → 只推一次 markLine (增量 setOption,
    同 updateLivePriceLine)。早期实现四签同列重叠, 就是这里要盯住的回归点。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, payload):
        fns = ["stackTags", "tagLabelPos", "priceLineLabel", "priceLineItem",
               "resolveLastPrice", "buildLastPriceLine", "buildRiskLineItems",
               "buildKlineMarkLines", "spanPx", "cacheGridRects", "chartModel", "priceYMapper",
               "tagBars", "tagColumnCtx", "layoutPriceTagSlots"]
        script = (
            "".join(_extract_const(self.src, n) + "\n"
                    for n in ("TAG_PITCH", "TAG_HUG", "TAG_MAX_SHIFT", "TAG_BOX_H"))
            + _extract_const(self.src, "RISK_ITEMS") + "\n"
            + "".join(_extract_fn(self.src, n) + "\n" for n in fns)
            + "const echarts = require(process.argv[2]);\n"
            + "const p = JSON.parse(process.argv[1]);\n"
            + "function riskOn() { return true; }\n"
            + "function C() { return { up: '#ef5350', down: '#26a69a', riskTp: '#c97a00',"
            + " riskBe: '#1565c0', riskSl: '#7b3fa0' }; }\n"
            + "function fmtPrice3(v) { return String(+Number(v).toFixed(3)); }\n"
            + "const GRIDS = [{ top: '12%', height: '60%', left: '8%', right: '2%' }];\n"
            + "const STATE = { symbol: 'X', period: '1d', risk: p.risk, _tagSlots: null,"
            + " _gridRects: null, klineData: { klines: p.bars } };\n"
            + "const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 600, height: 400 });\n"
            + "STATE.chart = chart;\n"
            + "chart.setOption({ grid: GRIDS[0], xAxis: { type: 'category',"
            + " data: p.bars.map((_, i) => 'd' + i) }, yAxis: { type: 'value', scale: true },"
            + " series: [{ name: 'K线', type: 'candlestick',"
            + " data: p.bars.map(b => [b.open, b.close, b.low, b.high]) }] });\n"
            + "cacheGridRects(GRIDS);   // 与实际 grid 一致, 否则列边界与 ECharts 对不上\n"
            + "const shots = [];\n"
            + "for (const tick of p.ticks) {\n"
            + "  layoutPriceTagSlots(p.bars, tick.live);\n"
            + "  // SSR 没有动画循环可 flush lazyUpdate, 这里同步提交 (合并语义与线上一致)\n"
            + "  chart.setOption({ series: [{ name: 'K线', markLine: buildKlineMarkLines(p.bars, tick.live) }] },"
            + " { silent: true });\n"
            + "  const svg = chart.renderToSVGString();\n"
            + "  const labels = {};\n"
            + "  for (const [t, name] of Object.entries(tick.labels)) {\n"
            + "    const i = svg.indexOf('>' + t + '<');\n"
            + "    if (i < 0) { labels[name] = null; continue; }\n"
            + "    const head = svg.slice(0, i);\n"
            + "    const tr = [...head.matchAll(/transform=\"translate\\((-?[0-9.]+) (-?[0-9.]+)\\)\"/g)].pop();\n"
            + "    const ly = [...head.matchAll(/<text[^>]*\\sy=\"(-?[0-9.]+)\"/g)].pop();\n"
            + "    labels[name] = (tr && ly) ? +(Number(tr[2]) + Number(ly[1])).toFixed(2) : null;\n"
            + "  }\n"
            + "  shots.push({ labels, slots: STATE._tagSlots });\n"
            + "}\n"
            + "chart.dispose();\n"
            + "process.stdout.write(JSON.stringify(shots));"
        )
        proc = subprocess.run(["node", "-e", script, json.dumps(payload), str(ECHARTS_JS)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def _payload(self, live_price):
        # 三档风控价都在 K 线区间内 (区间外的线 ECharts 本就不渲染); 11.02/10.98 只差
        # 0.04 (~3px) → 两个风控签天然叠在一起, 必须靠错位分开。
        risk = {"take_profit": 12.0, "breakeven": 11.02, "stop_loss": 10.98}
        names = {"保本 11.02": "保本签", "止损 10.98": "止损签", "止盈 12": "止盈签"}
        return {
            "bars": [{"open": 10, "close": 10.5, "low": 9.7, "high": 12.2},
                     {"open": 10.5, "close": 11.2, "low": 9.6, "high": 12.4}],
            "risk": risk,
            "ticks": [{"live": live_price,
                       "labels": dict({self._price_label(live_price): "现价签"}, **names)}],
        }

    @staticmethod
    def _price_label(price):
        """与镜像里的现价签口径一致: 「现价 <最多 3 位小数, 去尾随 0>」"""
        return "现价 " + f"{price:.3f}".rstrip("0").rstrip(".")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_close_levels_are_pushed_apart(self):
        # 现价 11.05, 保本 11.02, 止损 10.98 三档挤在 0.07 内 → 不错开就是几个签叠在一起
        shots = self._run(self._payload(11.05))
        labels = shots[0]["labels"]
        for name in ("现价签", "保本签", "止损签", "止盈签"):
            self.assertIsNotNone(labels[name], f"{name} 未渲染")
        ys = sorted(labels.values())
        for a, b in zip(ys, ys[1:]):
            self.assertGreaterEqual(round(b - a, 1), 14, f"标签盒重叠 (盒高 14): {ys}")
        # 现价签钉在自己的线上 (最高优先级, 不许被挪走)
        slot = shots[0]["slots"]["last"]
        self.assertFalse(slot.get("dropped"))
        self.assertLess(abs(slot["centerY"] - slot["lineY"]), 20)
        # 让位的是与邻居挤在一起的保本签
        self.assertGreater(abs(shots[0]["slots"]["breakeven"]["centerY"]
                               - shots[0]["slots"]["breakeven"]["lineY"]), 1)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_labels_follow_price_when_it_moves(self):
        """现价波动 → 下一 tick 重新错开 (不能只在建图时算一次)。"""
        payload = self._payload(11.05)
        risk_names = {"保本 11.02": "保本签", "止损 10.98": "止损签", "止盈 12": "止盈签"}
        payload["ticks"] = [
            {"live": 11.05, "labels": dict({self._price_label(11.05): "现价签"}, **risk_names)},
            {"live": 12.04, "labels": dict({self._price_label(12.04): "现价签"}, **risk_names)},  # 贴到止盈上
        ]
        shots = self._run(payload)
        for shot in shots:
            ys = sorted(v for v in shot["labels"].values() if v is not None)
            self.assertEqual(len(ys), 4, f"每 tick 四签都要在: {shot['labels']}")
            for a, b in zip(ys, ys[1:]):
                self.assertGreaterEqual(round(b - a, 1), 14, f"有 tick 又叠上了: {ys}")
        # 第二 tick 主动权交给止盈价 (它和现价挤在一起)
        first, second = shots[0]["slots"], shots[1]["slots"]
        self.assertGreater(abs(first["breakeven"]["centerY"] - first["breakeven"]["lineY"]), 1)
        self.assertGreater(abs(second["take_profit"]["centerY"] - second["take_profit"]["lineY"]), 1)
        # 现价签在两个 tick 都跟着自己的线走 (及时调整)
        for shot in shots:
            self.assertLess(abs(shot["slots"]["last"]["centerY"]
                                - shot["slots"]["last"]["lineY"]), 20)


    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_symbol_link_wiring(self):
        """股吧地址口径 + 无代码时摘掉链接 (名称不可点、无提示)。"""
        payload = {"cases": [
            {"symbol": "000001.SH", "is_index": True},
            {"symbol": "399001.SZ", "is_index": True},
            {"symbol": "510300.SH", "is_etf": True},
            {"symbol": "002472.SZ"},
            {"symbol": "", "is_index": True},
        ]}
        script = (
            "".join(_extract_fn(self.src, n) + "\n" for n in ("gubaUrl", "updateSymbolLink"))
            + "const nodes = { 'info-sym': { href: '#', title: '', removed: [],"
            + " removeAttribute(a) { this.removed.push(a); delete this[a]; } } };\n"
            + "const document = { getElementById: (id) => nodes[id] || null };\n"
            + "const out = [];\n"
            + "for (const c of JSON.parse(process.argv[1]).cases) {\n"
            + "  nodes['info-sym'].href = '#'; nodes['info-sym'].title = ''; nodes['info-sym'].removed = [];\n"
            + "  updateSymbolLink(c);\n"
            + "  out.push({ url: gubaUrl(c.symbol, !!c.is_index, !!c.is_etf),"
            + "    href: nodes['info-sym'].href, title: nodes['info-sym'].title,"
            + "    removed: nodes['info-sym'].removed.slice() });\n"
            + "}\n"
            + "process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(["node", "-e", script, json.dumps(payload)],
                              capture_output=True, check=True)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(out[0]["url"], "https://guba.eastmoney.com/list,zssh000001.html",
                         "沪市指数 zs+市场前缀")
        self.assertEqual(out[1]["url"], "https://guba.eastmoney.com/list,zssz399001.html",
                         "深市指数 zssz")
        self.assertEqual(out[2]["url"], "https://guba.eastmoney.com/list,sh510300.html",
                         "ETF 小写市场前缀")
        self.assertEqual(out[3]["url"], "https://guba.eastmoney.com/list,002472.html",
                         "个股裸 6 位")
        for i in (0, 1, 2, 3):
            self.assertEqual(out[i]["href"], out[i]["url"], "有代码就写进 href")
            self.assertEqual(out[i]["title"], "去股吧")
            self.assertEqual(out[i]["removed"], [])
        self.assertEqual(out[4]["url"], "", "无代码没有股吧页")
        self.assertEqual(out[4]["removed"], ["href", "title"], "无股吧页要摘掉 href/title")
        # 点代码要阻止跳转, 否则复制被链接导航盖掉
        self.assertIn("ev.preventDefault()", self.src)
        self.assertIn("ev.stopPropagation()", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
