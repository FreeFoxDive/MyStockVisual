# -*- coding: utf-8 -*-
"""主页顶栏 + 平板布局回归测试 (静态结构断言 + 判定口径真源码镜像)。

覆盖的几起「布局事故」:
  * 顶栏入口文案收短 (监控中心→监控, 交易记录→交易), 刷新按钮只留图标;
  * 顶栏不得因行情数字/秒级时钟变宽窄而在一行/两行之间反复跳 —— 信息组可收缩并
    裁掉尾部次要字段, 宽屏右侧功能组不换行, 秒级时钟定宽;
  * 顶栏/记录栏/指标栏高度变化时图表容器尺寸跟着变, 必须触发 chart.resize
    (否则 ECharts 画布与 overlay 画线层一起和容器错位);
  * 平板周期条默认只显示 3 个周期, 翻页按真实按钮宽度而不是固定像素;
  * 平板 (触屏 或 ≤1024px) 顶栏口径: 不显示秒级时钟、入口只留图标、指标栏固定单行横向滚动
    (换行时栏高随周期在 1/2 行之间跳)、搜索框按 symbol.ex 收窄、下拉长名称限宽省略;
  * isTabletUi / isWeeklyPlus 的判定口径 (从 index.html 抽真源码用 node 执行)。

运行:
    venv/Scripts/python.exe -u visual/test/test_home_toolbar_js.py
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


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


def _css_block(src: str, selector: str) -> str:
    """取第一个 `selector { ... }` 规则正文 (CSS 无嵌套, 按大括号配平即可)。"""
    i = src.index(selector + " {")
    start = src.index("{", i)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"{selector} 大括号不配对")


def _extract_arrow(src: str, name: str) -> str:
    """抽取 `const NAME = (...) => { ... };` (箭头函数版 _extract_fn)。"""
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\(", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name} = (...) =>")
    start = src.index("{", src.index("=>", m.end()))
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1] + ";"
    raise AssertionError(f"const {name} 箭头函数大括号不配对")


class ToolbarStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.toolbar = cls.src[cls.src.index('<div id="toolbar">'):cls.src.index('<div id="history-bar"')]

    def test_entry_labels_shortened(self):
        """顶栏入口文案收短; 全称保留在 title 里, 悬浮仍能看全。"""
        for anchor, short, full in (("monitor-entry", "监控", "监控中心"),
                                    ("trades-entry", "交易", "交易记录")):
            i = self.toolbar.index(f'id="{anchor}"')
            open_tag = self.toolbar[i:self.toolbar.index(">", i)]
            text_end = self.toolbar.index("</button>", i)
            text = self.toolbar[self.toolbar.index(">", i) + 1:text_end]
            self.assertIn(short, text, f"{anchor} 文案应为 {short}")
            self.assertNotIn(full, text, f"{anchor} 顶栏文案应收短")
            self.assertIn(full, open_tag, f"{anchor} 的 title 应保留全称")

    def test_refresh_button_icon_only(self):
        i = self.toolbar.index('id="btn-refresh"')
        open_tag = self.toolbar[i:self.toolbar.index(">", i)]
        body = self.toolbar[self.toolbar.index(">", i) + 1:self.toolbar.index("</button>", i)]
        self.assertIn('class="icon-btn"', open_tag, "刷新按钮走统一图标按钮样式")
        self.assertIn('aria-label="刷新"', open_tag, "文字去掉后必须有无障碍名")
        self.assertIn("title=", open_tag)
        self.assertIn("<svg", body)
        self.assertNotIn("刷新", body, "按钮内不应再有文字节点")
        # 旧的字+图标样式已无引用, 不该留在样式表里
        self.assertNotIn("icon-text-btn", self.src)

    def test_toolbar_cannot_flip_row_count_on_text_width(self):
        """顶栏行数只由断点决定, 不随行情文字宽度跳 (核心回归点)。

        实测几何: 信息行最长约 1000px (代码/名称/成交额/换手/…), 功能组约 950~1200px
        (搜索+周期条+入口+图标), 而成交额 万↔亿、换手出现/消失、秒级时钟变宽都会改变
        信息行宽度 —— 只要总宽压在阈值上, 功能组就会在一行/两行之间反复切。

        例外: 大宽度触屏 (iPad Pro 13 横屏 1376 这类) 改成实测争取单行, 由 .tb-2row 兜底
        (见 test_wide_touch_can_merge_into_one_row) —— 桌面各断点的规则仍然是定死的。
        """
        narrow = _css_block(self.src, "@media (min-width: 1000px) and (max-width: 1919px)")
        self.assertIn("#toolbar .info-group", narrow)
        self.assertIn("overflow: hidden", narrow, "信息行须可裁掉尾部次要字段")
        self.assertIn("min-width: 0", narrow)
        self.assertIn("flex: 1 1 100%", narrow, "窄于 1920 时功能组独占一行")
        self.assertIn("flex-wrap: nowrap", narrow, "功能组内部不换行")

        wide = _css_block(self.src, "@media (min-width: 1920px)")
        self.assertIn("flex: 1 1 0", wide, "够宽时信息行取剩余宽度, 永不挤走功能组")
        self.assertIn("min-width: 0", wide)
        self.assertIn("overflow: hidden", wide)
        self.assertIn("flex: 0 0 auto", wide, "功能组回到第一行右端")
        self.assertIn("margin-left: auto", wide)
        # 1920 及以上必须是一行: 断点不得再往后拖 (曾经写成 2049, 1920 屏白白多占一行)
        self.assertNotIn("@media (min-width: 2049px)", self.src)

    def test_narrow_screen_keeps_wrapping(self):
        """窄屏保持原有「信息组换行」策略: 宁可多占一行也不裁字段。"""
        info = _css_block(self.src, "#toolbar .info-group")
        self.assertIn("flex-shrink: 0", info, "基础规则仍是窄屏口径")
        self.assertNotIn("overflow: hidden", info)
        narrow = _css_block(self.src, "@media (max-width: 900px)")
        self.assertIn("flex-wrap: wrap", narrow)
        self.assertIn("white-space: normal", narrow)
        self.assertIn("#toolbar .oh, #toolbar #info-time", narrow,
                      "平板宽度就先摘掉次要行情字段, 给功能组留余量")
        self.assertNotIn("@media (max-width: 768px)", self.src, "该断点整块上移到 900px")

    def test_volatile_readouts_have_stable_width(self):
        """逐 tick 重写的字段必须定宽, 否则每次刷新都在重排。"""
        self.assertIn("width: 9ch", _css_block(self.src, "#info-time"))
        self.assertIn("text-align: right", _css_block(self.src, "#info-time"))

    def test_chart_resizes_when_top_strips_change_height(self):
        body = _extract_fn(self.src, "initChart")
        self.assertIn("ResizeObserver", body, "容器尺寸变化必须被监听")
        self.assertIn("STATE.chart.resize", body)
        self.assertIn("redrawDrawings()", body, "overlay 画线层要跟着重投影")


class TabletPeriodStripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_visible_counts_declared(self):
        self.assertIn("const PERIOD_VISIBLE_TABLET = 3;", self.src)
        self.assertIn("const PERIOD_VISIBLE_DESKTOP = 5;", self.src)
        # 桌面仍是 CSS 的 252px 固定宽度, 平板才由 JS 量出 3 个按钮的宽
        self.assertIn("width: 252px", _css_block(self.src, "#toolbar .period-viewport"))

    def test_layout_viewport_measures_buttons(self):
        body = _extract_fn(self.src, "layoutPeriodViewport")
        self.assertIn("isTabletUi()", body, "只有平板才收窄")
        self.assertIn("periodBtnOffset", body, "按真实按钮宽度算, 不写死像素")
        self.assertIn("innerWidth", body, "极窄视口要有兜底上限")
        # 非平板清掉内联宽度, 回落 CSS
        self.assertIn("viewport.style.width = ''", body)

    def test_shared_offset_helper(self):
        """量宽与滚入视野必须用同一套偏移口径 (两者 offsetParent 相同)。"""
        helper = _extract_fn(self.src, "periodBtnOffset")
        self.assertIn("offsetLeft", helper)
        self.assertIn("periodBtnOffset", _extract_fn(self.src, "ensurePeriodVisible"))
        self.assertIn("periodBtnOffset", _extract_fn(self.src, "initPeriodScroller"))

    def test_scroll_step_follows_button_width(self):
        body = _extract_fn(self.src, "initPeriodScroller")
        self.assertIn("PERIOD_VISIBLE_TABLET", body)
        self.assertIn("PERIOD_VISIBLE_DESKTOP", body)
        self.assertNotIn("Math.max(120, viewport.clientWidth * .8)", body,
                         "旧的固定像素翻页会把按钮滚偏")
        self.assertIn("orientationchange", body, "转屏后要重量宽度")
        self.assertIn("layoutPeriodViewport()", body)
        # 折叠式「更多周期」是废弃方案, 不得回归
        self.assertNotIn("togglePeriodMore", self.src)
        self.assertNotIn('class="extra-period"', self.src)


class TabletLayoutTest(unittest.TestCase):
    """平板口径: 触屏 或 ≤1024px (与 isTabletUi() 同一条判定)。

    覆盖的事故:
      * 768~1024 的平板掉在 900px 与 1000px 两条断点之间: 秒级时钟照显示、指标栏照换行、
        搜索框跟着触控按钮放大到 38px/16px (比同排按钮还显眼);
      * 指标栏换行时 pill 数量随周期变 (分时少 筹码/动力系统/通道 三个), 栏高就在
        「日K 两行 / 分时 一行」之间跳, 图表跟着上下一截 → 平板改成固定单行 + 横向滚动;
      * 入口文案 (监控/选股/交易) 在平板/手机上仍占位 → 只留图标, 进度只能写文字层。
      * 信息行的 开/高/低 在平板被截断/被整条摘掉 → 改成「实测一行放不下才收次要读数」
        (见 test_info_row_drops_stats_only_when_it_does_not_fit 与 test_info_drop_plan)。
    """

    TABLET_MQ = "@media (max-width: 1024px), (pointer: coarse)"

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.tablet = _css_block(cls.src, cls.TABLET_MQ)

    def test_same_criteria_and_source_order(self):
        """口径与 isTabletUi() 一致, 且必须排在 640 手机块之前 (手机块要能覆盖它)。"""
        js = _extract_fn(self.src, "isTabletUi")
        self.assertIn("(pointer: coarse)", js)
        self.assertIn("innerWidth <= 1024", js)
        self.assertIn("(pointer: coarse)", self.TABLET_MQ)
        self.assertIn("(max-width: 1024px)", self.TABLET_MQ)
        self.assertLess(self.src.index(self.TABLET_MQ + " {"),
                        self.src.index("@media (max-width: 640px) {"),
                        "平板块排在手机块之后就会把手机的浮层/字号盖坏")

    def test_hides_clock_and_entry_text(self):
        """平板: 不占秒级时钟的空间; 入口只留图标 (文字层隐藏, 图标仍可点)。"""
        self.assertIn("#toolbar #info-time { display: none; }", self.tablet)
        self.assertIn("#toolbar .nav-txt { display: none; }", self.tablet)
        self.assertIn("row-gap: 2px", self.tablet, "两行时行距要收紧 (原本 6px)")
        self.assertIn("#toolbar .nav-btn { padding: 6px 9px; }", self.tablet,
                      "只剩图标后要收成图标按钮的尺寸 (触控那条 8px 14px 会把图标按钮撑胖)")
        # 扫描进度是实时状态, 收起来就看不见了
        self.assertIn("#toolbar .nav-btn.scanning .nav-txt { display: inline; }", self.tablet)

    def test_entry_buttons_are_two_layers_with_accessible_name(self):
        """图标/文字必须是两个节点: 文字层被 CSS 藏掉后, 无障碍名要落在标签上。"""
        for anchor, ico, txt, full in (("monitor-entry", "🛡", "监控", "监控中心"),
                                       ("screener-entry", "🎯", "选股", "条件选股"),
                                       ("trades-entry", "📒", "交易", "交易记录统计")):
            i = self.src.index(f'id="{anchor}"')
            open_tag = self.src[i:self.src.index(">", i)]
            body = self.src[self.src.index(">", i) + 1:self.src.index("</button>", i)]
            self.assertIn('class="nav-btn"', open_tag, anchor)
            self.assertIn(f'aria-label="{full}"', open_tag, f"{anchor} 缺无障碍名 (文字层会隐藏)")
            self.assertIn(f'<span class="nav-ico" aria-hidden="true">{ico}</span>', body, anchor)
            self.assertIn(f'<span class="nav-txt">{txt}</span>', body, anchor)
        self.assertIn("gap: 4px", _css_block(self.src, "#toolbar .nav-btn"),
                      "图标与文字的间距由容器给: 文字层隐藏后间距自动消失")

    def test_screener_badge_only_writes_text_layer(self):
        """进度只写文字层: innerHTML 整体覆写会把图标/文字两层抹平, 平板上又冒出「选股」两字。"""
        fn = _extract_fn(self.src, "refreshScreenerBadge")
        self.assertNotIn("innerHTML", fn)
        self.assertIn("setNavText(btn, `${pct}%`)", fn)
        self.assertIn("setNavText(btn, '选股')", fn)
        self.assertIn('classList.add("scanning")', fn, "扫描态仍要驱动 CSS 把进度露出来")
        setter = _extract_fn(self.src, "setNavText")
        self.assertIn("querySelector('.nav-txt')", setter)

    def test_indicator_bar_single_row(self):
        """指标栏固定单行 + 隐藏滚动条: 栏高恒定, 切周期不再上下跳。

        换行布局下, 分时比日K 少 筹码/动力系统/通道 三个 pill (见 applyPeriodUI),
        1 行与 2 行之间切换会把图表高度来回改。"""
        bar = self.tablet[self.tablet.index("#indicator-bar {"):]
        self.assertIn("flex-wrap: nowrap", bar)
        self.assertIn("overflow-x: auto", bar)
        self.assertIn("scrollbar-width: none", bar)
        self.assertIn("-webkit-overflow-scrolling: touch", bar)
        self.assertIn("#indicator-bar::-webkit-scrollbar { display: none; }", bar,
                      "藏不住滚动条的话 Windows 的 17px 横条自己就把栏顶高了")
        self.assertIn("#indicator-bar label,", bar, "nowrap 下 pill 必须先定住宽度")
        self.assertIn("#indicator-bar .cfg-mini,", bar)
        self.assertIn("#indicator-bar select { flex-shrink: 0; }", bar)
        # 触发换行的三个 pill 仍由周期决定显隐 (单行只是把结果藏进横向滚动里)
        ui = _extract_fn(self.src, "applyPeriodUI")
        for lbl in ("lbl-chip", "lbl-impulse", "lbl-channel"):
            self.assertIn(lbl, ui)

    def test_phone_block_still_wins(self):
        """手机 (≤640) 不能被平板块带坏: 浮层照旧、不被 overflow 裁掉、字号仍 16px。"""
        narrow = _css_block(self.src, "@media (max-width: 640px)")
        self.assertIn("flex-wrap: nowrap", narrow)
        self.assertIn("overflow: visible", narrow,
                      "浮层 #ind-body 是绝对定位子节点, 被 overflow 裁掉就点不到")
        self.assertIn("#indicator-bar.sheet-open #ind-body { display: flex; }", narrow)
        self.assertIn('#toolbar input[type="text"] { width: 120px; font-size: 16px; min-height: 38px; }',
                      narrow, "手机保留 16px 防 iOS 聚焦放大")

    def test_search_input_sized_for_symbol(self):
        """搜索框宽度按 symbol.ex 定 (服务端 symbol 最长 9 字符, 如 688004.SH), 不再 160px。"""
        self.assertIn("width: 120px", self.tablet)
        self.assertIn("font-size: 14px", self.tablet)
        self.assertIn("min-height: 32px", self.tablet)
        self.assertIn('#toolbar input[type="text"]:focus { font-size: 16px; }', self.tablet,
                      "聚焦抬回 16px: iOS 只在聚焦那一刻看字号决定要不要放大页面")
        # 触控块不得再写输入框尺寸: 它排在平板块之后, 会把 14px/32px 顶回 16px/38px
        self.assertNotIn('input[type="text"]', _css_block(self.src, "@media (pointer: coarse)"))
        # 120px 放不下原 placeholder → 收短
        self.assertIn('placeholder="代码/名称"', self.src)
        self.assertNotIn("输入代码或名称搜索", self.src)

    def test_info_row_drops_stats_only_when_it_does_not_fit(self):
        """顶栏信息行: 默认全显示, **实测**一行放不下才按优先级收起次要读数。

        回归点: 原先按设备档收 (平板块固定隐藏三个统计读数), 宽屏明明放得下也收; 而 开/高/低
        在 DOM 尾部, 1000~1919 那条「装不下就裁尾部」正好把它截断 (iPad Mini 横屏), 900 那条
        更是在竖屏把它整条摘掉。
        """
        # 布局侧走类, 数据侧继续写内联 display; 绝不 !important 盖掉数据侧「没数据就不显示」的闸门
        self.assertIn("#toolbar .info-group > .rd-drop { display: none; }", self.src)
        self.assertNotIn(".rd-drop { display: none !important", self.src)
        self.assertIn("const INFO_DROP_ORDER = ['info-pledge', 'info-shares', 'info-amount', "
                      "'info-premium', 'info-turnover'];", self.src, "收起顺序即优先级, 顺序变了要写进用例")
        plan = _extract_fn(self.src, "infoDropPlan")
        self.assertIn("INFO_DROP_ORDER", plan)
        self.assertIn("return null;", plan, "收光仍放不下 → 交回 CSS (窄屏换行), 而不是硬收")
        fit = _extract_fn(self.src, "fitInfoRow")
        self.assertIn("infoRowBudget(", fit, "预算来自顶栏可用宽度, 不是信息行自己的框宽")
        self.assertNotIn("group.getBoundingClientRect().width, gap", fit,
                         "拿信息行自己的框宽当预算是错的: flex-basis:auto 时它等于内容宽, "
                         "收过字段后更小 → 会把「已收」判成「放不下」, 字段再也放不回来")
        self.assertIn("el.getBoundingClientRect().width", fit,
                      "逐项宽度要用小数值: offsetWidth 取整后十来项累加能凭空多出 1px (实测过)")
        self.assertIn("r.bottom > gt.top + 1 && r.top < gt.bottom - 1", fit,
                      "只算同一行的兄弟项, 高矮不一要比纵向重叠而不是 top")
        self.assertIn("const INFO_FIT_SLACK = ", self.src, "亚像素容差常量 (取值由 test_info_drop_plan 用例锁)")
        self.assertIn("classList.remove('rd-drop')", fit, "先全放回再从头量: 幂等, 变宽会自动放回来")
        self.assertIn("classList.add('rd-drop')", fit)
        self.assertIn("scheduleFitInfoRow", _extract_fn(self.src, "wireInfoRowFit"),
                      "尺寸一变就重算 (ResizeObserver 盯信息行本身)")
        self.assertIn("wireInfoRowFit();", self.src, "启动时要接上")
        self.assertIn("scheduleFitInfoRow", _extract_fn(self.src, "scheduleFitInfoRow"),
                      "rAF 合并 (行情每秒重写读数, 不必每帧都量)")
        # 挂钩点: 行情快照与换股/换周期都过 updateFundInfo; 质押是异步到的;
        # 持仓中徽章显隐会改宽度; resize/转屏/字体就绪都走 initPeriodScroller 的 relayout
        for fn in ("updateFundInfo", "syncPosition", "initPeriodScroller"):
            self.assertIn("scheduleFitInfoRow()", _extract_fn(self.src, fn), f"{fn} 应重算顶栏信息行")
        pledge = self.src[self.src.index("/api/pledge"):]
        pledge = pledge[:pledge.index("}).catch")]   # 整个回调 (它同时还会把质押并进面板)
        self.assertIn("scheduleFitInfoRow()", pledge, "质押响应回来要重算")
        # 平板块: 开/高/低 放回来 + 901~999 那道缝补上可收缩; 但不再按断点收字段
        self.assertIn("#toolbar .oh { display: inline-block; }", self.tablet,
                      "竖屏 768 也要看得到 开/高/低")
        self.assertIn("#toolbar .info-group { flex: 0 1 auto; min-width: 0; }", self.tablet,
                      "901~999 无可收缩规则 → 整行溢出视口被 body 裁掉")
        for pid in ("info-pledge", "info-shares", "info-amount", "info-premium", "info-turnover"):
            self.assertNotIn(f"#{pid}", self.tablet, f"平板块不该再按断点收 {pid} (交给实测)")

    def test_wide_touch_can_merge_into_one_row(self):
        """大宽度触屏 (iPad Pro 13 横屏 1376): 允许把功能组并回信息行做成一行。

        桌面 <1920 是「功能组独占一行」的既定设计 (行数只由断点定); 触屏大宽度上改成实测争取
        单行 —— 为此可以收掉几个次要读数, 连收光都放不下才退回那条两行规则 (.tb-2row)。
        """
        css = _css_block(self.src, "@media (min-width: 1025px) and (pointer: coarse)")
        self.assertIn("#toolbar .toolbar-right { flex: 0 0 auto; flex-wrap: nowrap; margin-left: auto;",
                      css, "默认并排: 功能组保持自然宽, 不被压扁")
        self.assertIn("#toolbar.tb-2row .toolbar-right { flex: 1 1 100%; margin-left: 0; "
                      "justify-content: flex-end; }", css, "退回两行时与桌面 1000~1919 同口径")
        # 11 寸 (1180) 并成一行靠这段「非信息部分」挤紧约 140px: 间距/内边距/分隔线/搜索框/顶栏内边距/GitHub
        for rule in ("#toolbar { padding: 4px 8px; column-gap: 4px; }",
                     "#toolbar .info-group { column-gap: 4px; }",
                     "#toolbar .icon-btn { padding: 6px 7px; }",
                     "#toolbar .nav-btn { padding: 6px 7px; }",
                     "#toolbar .separator { display: none; }",
                     "#toolbar .github-link { display: none; }",
                     '#toolbar input[type="text"] { width: 100px; }'):
            self.assertIn(rule, css, f"11 寸一行少了这条紧凑规则: {rule}")
        self.assertNotIn("font-size: 12px", css, "这一轮不动字号 (你选的: 只收紧间距/内边距)")
        # GitHub 入口只在触屏大宽度块与 ≤640 手机块里隐藏, 桌面不得被波及
        for band in ("@media (min-width: 1000px) and (max-width: 1919px)", "@media (min-width: 1920px)"):
            self.assertNotIn("github-link", _css_block(self.src, band), band)
        self.assertIn("#toolbar .github-link { display: none; }",
                      _css_block(self.src, "@media (max-width: 640px)"), "手机一直是隐藏的")
        # JS 与样式同一条查询 (同 isPhoneUi 的做法), 免得又出现「样式生效/判定不生效」的漂移
        wide = _extract_fn(self.src, "isWideTouchUi")
        self.assertIn("(min-width: 1025px) and (pointer: coarse)", wide)
        self.assertIn("@media (min-width: 1025px) and (pointer: coarse) {", self.src,
                      "样式块的查询串要与 isWideTouchUi 逐字一致")
        # 源序: 必须排在桌面 1000~1919 与平板块之后 (要靠后者覆盖它们), 在手机块之前
        self.assertLess(self.src.index("@media (min-width: 1000px) and (max-width: 1919px)"),
                        self.src.index("@media (min-width: 1025px) and (pointer: coarse)"))
        self.assertLess(self.src.index("@media (min-width: 1025px) and (pointer: coarse)"),
                        self.src.index("@media (max-width: 640px) {"))
        row = _extract_fn(self.src, "toolbarRowPlan")
        self.assertIn("rows: 1", row)
        self.assertIn("rows: 2", row, "单行收光也放不下要能退回两行")
        self.assertIn("|| []", row, "退回两行时字段全放回 (而不是继续收)")
        fit = _extract_fn(self.src, "fitInfoRow")
        self.assertIn("isWideTouchUi()", fit, "只有大宽度触屏才争取单行, 桌面口径不变")
        self.assertIn("toolbarRowPlan(", fit)
        self.assertIn("fnNaturalWidth(", fit, "功能组的自然宽: 与当前排几行无关, 不必先改类再量")
        self.assertIn("classList.toggle('tb-2row', wideTouch && twoRows)", fit,
                      "只在「宽触屏 + 两行」时加退回类")

    def test_search_dropdown_contained(self):
        """长名称: 名称走省略号 (代码/徽章不被裁), 浮层宽度有上限且按位置限位不出屏。"""
        dd = _css_block(self.src, ".search-dropdown")
        self.assertIn("min-width: 300px", dd)
        self.assertIn("max-width: min(360px, calc(100vw - 24px))", dd)
        item = _css_block(self.src, ".search-dropdown .item")
        self.assertIn("display: flex", item)
        self.assertIn("gap: 6px", item)
        name = _css_block(self.src, ".search-dropdown .item .name")
        for rule in ("flex: 1 1 auto", "min-width: 0", "overflow: hidden",
                     "text-overflow: ellipsis", "white-space: nowrap"):
            self.assertIn(rule, name, rule)
        self.assertIn("flex: 0 0 auto", _css_block(self.src, ".search-dropdown .item .code"),
                      "代码是识别项, 不能被名称挤掉")
        render = _extract_fn(self.src, "renderDropdown")
        self.assertIn('<span class="name">', render, "名称要能单独省略号")
        self.assertIn("applyDropdownBox()", render)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class HomeToolbarBehaviorTest(unittest.TestCase):
    """抽 index.html 真源码执行判定函数 (抽不到即失败)。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, script, payload):
        proc = subprocess.run(["node", "-e", script, json.dumps(payload)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def test_is_tablet_ui(self):
        script = _extract_fn(self.src, "isTabletUi") + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => {
  globalThis.window = c.noMM ? { innerWidth: c.w }
    : { innerWidth: c.w, matchMedia: () => ({ matches: !!c.coarse }) };
  return isTabletUi();
})));
"""
        out = self._run(script, [
            {"coarse": True, "w": 1366},    # 触屏平板: 即便够宽也算平板
            {"coarse": False, "w": 1440},   # 桌面
            {"coarse": False, "w": 800},    # 窄窗口也算平板布局
            {"coarse": False, "w": 1024},   # 边界含在平板内
            {"coarse": False, "w": 1025},   # 边界外是桌面
            {"coarse": False, "w": 1440, "noMM": True},  # 无 matchMedia 时靠宽度兜底
        ])
        self.assertEqual(out, [True, False, True, True, False, False])

    def test_is_phone_ui(self):
        """手机口径 = CSS 的 @media (max-width: 640px), JS 与样式必须同一条查询。"""
        script = _extract_fn(self.src, "isPhoneUi") + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => {
  globalThis.window = c.noMM ? {} : { matchMedia: () => ({ matches: !!c.m }) };
  return isPhoneUi();
})));
"""
        out = self._run(script, [
            {"m": True},     # 命中 640px 查询
            {"m": False},    # 更宽的窗口 / 桌面
            {"m": False, "noMM": True},   # 无 matchMedia (老环境) → 不当作手机
        ])
        self.assertEqual(out, [True, False, False])
        # 同一条媒体查询出现在 CSS 断点里
        self.assertIn("@media (max-width: 640px)", self.src)
        self.assertIn("(max-width: 640px)", _extract_fn(self.src, "isPhoneUi"))

    def test_draw_entry_hidden_on_phone(self):
        """手机不提供画线入口 (窄屏点选精度不够), 且画线模式没有别的入口。"""
        narrow = _css_block(self.src, "@media (max-width: 640px)")
        self.assertIn("#toolbar #btn-draw { display: none; }", narrow)
        self.assertNotIn("draw.enabled = true", _extract_fn(self.src, "buildConfig"))
        # toggleDrawMode 只由按钮触发, 无快捷键
        self.assertIn('id="btn-draw"', self.src)
        self.assertIn('onclick="toggleDrawMode()"', self.src)

    def test_intraday_panel_labels_use_shared_path(self):
        """分时 VOL/MACD 标签与其他周期同一条绘制路径 + 同一坐标口径 (回归点)。"""
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertIn("redrawDrawings(igraphics)", intra, "交给 overlay 层统一画")
        self.assertIn("spanPx(g.left, iCw, false) + 6", intra, "x 口径与其他周期一致")
        self.assertIn("spanPx(g.top, iCh, false) + 15", intra, "y 口径与其他周期一致")
        self.assertIn("bold 11px monospace", intra)
        self.assertNotIn("type: 'text', left: 10", intra, "旧的 ECharts graphic 写死位置已删除")
        self.assertNotIn("setOption({ graphic: igraphics })", self.src)
        # 其他周期的面板标签仍是同一套 (grid.left + 6 / grid.top + 15)
        chart = _extract_fn(self.src, "updateChart")
        self.assertIn("x: gridLeft + 6, y: gridTop + 15", chart)
        # 无画线上下文时 (分时) 也要画标签, 且不能把日线图例带上分时
        now = _extract_fn(self.src, "_renderDrawingsNow")
        self.assertIn("if (!dctx) { paintLabels(c2, STATE.draw._labels);", now)
        self.assertLess(now.index("paintLabels(c2, STATE.draw._labels)"),
                        now.index("paintLegend(c2, dctx)"), "图例仍只在有画线上下文时画")

    def test_phone_history_bar_hidden_until_search_focus(self):
        """手机: 搜索记录默认不占位, 聚焦搜索框才展开。"""
        body = _extract_fn(self.src, "renderHistoryTags")
        self.assertIn("isPhoneUi()", body)
        self.assertIn("STATE._histFocus", body)
        wire = _extract_fn(self.src, "wireHistoryBarFocus")
        self.assertIn("'focus'", wire)
        self.assertIn("'blur'", wire)
        self.assertIn("isPhoneUi()", wire)
        self.assertIn("setTimeout", wire, "失焦要留宽限, 免得点标签时先收起")
        self.assertIn("wireHistoryBarFocus();", self.src)

    def test_phone_shows_ohlc_in_toolbar(self):
        """手机顶栏要显示 开/高/低 (900px 那条为宽屏防跳行摘掉的字段, 手机放回来)。"""
        narrow = _css_block(self.src, "@media (max-width: 640px)")
        self.assertIn("#toolbar .oh { display: inline-block; }", narrow)
        self.assertIn("#toolbar .oh, #toolbar #info-time", _css_block(self.src, "@media (max-width: 900px)"),
                      "宽屏那条隐藏规则保持不变")

    def test_phone_hides_github_entry(self):
        """手机不显示 GitHub 入口。"""
        narrow = _css_block(self.src, "@media (max-width: 640px)")
        self.assertIn("#toolbar .github-link { display: none; }", narrow)
        self.assertIn('class="github-link"', self.src, "桌面仍有该入口")

    def test_toolbar_readouts_split_label_and_value(self):
        """顶部栏读数: 属性名保持次要灰, 数值用强对比文本 (以前名称+数值同一个颜色)。
        时钟本身仍是强对比文本。"""
        self.assertIn("var(--strong-text)", _css_block(self.src, "#info-time"))
        self.assertIn("color: var(--text-secondary)", _css_block(self.src, "#toolbar .info-lbl"))
        self.assertIn("#toolbar .info-val { color: var(--strong-text); }", self.src)
        fn = _extract_fn(self.src, "setReadout")
        self.assertIn("info-lbl", fn)
        self.assertIn("info-val", fn)
        self.assertNotIn("innerHTML", fn, "值来自行情, 用 createElement/textContent 更稳")
        for call in ("setReadout(turnoverEl, '换手'", "setReadout(sharesEl, '份额'",
                     "setReadout(sharesEl, '流通'", "setReadout(amountEl, '额'",
                     "setReadout(el, '质押'"):
            self.assertIn(call, self.src, call)
        self.assertIn("setReadout(document.getElementById('info-premium'), `溢价", self.src)
        # 三个 span 的颜色交给子元素; OHLC 不动
        for pid in ("info-pledge", "info-shares", "info-amount"):
            i = self.src.index(f'id="{pid}"')
            tag = self.src[self.src.rindex("<span", 0, i):self.src.index(">", i)]
            self.assertNotIn("color:", tag, f"{pid} 的颜色应由 .info-lbl/.info-val 决定")
        for pid in ("info-open", "info-high", "info-low"):
            i = self.src.index(f'id="{pid}"')
            tag = self.src[self.src.rindex("<span", 0, i):self.src.index(">", i)]
            self.assertIn('class="oh"', tag, f"{pid} (OHLC) 不应被改动")

    def test_chip_summary_labels_muted_values_strong(self):
        """筹码峰: 左列标签/标题走次要灰, 右侧数值 (90%/70% 等无色的) 走强对比文本。"""
        chart = _extract_fn(self.src, "updateChart")
        chip = chart[chart.index("if (chipOn) {"):]
        chip = chip[:chip.index("loadDrawings()")]
        self.assertEqual(chip.count("fill: chipColors.muted"), 2, "标题 + 标签两处走 muted")
        self.assertIn("chipColors[row.color] || chipColors.text", chip,
                      "数值: 有语义色用语义色, 没有的 (90%/70%) 用强对比文本")
        self.assertNotIn("fill: chipColors.text,", chip, "标签不该再用强对比文本")

    def test_phone_tooltip_confined_and_wrapped(self):
        """手机提示框: 贴屏内 + 限宽换行 + 小一号字; 日K 与分时两个都要。"""
        chart = _extract_fn(self.src, "updateChart")
        self.assertIn("const phoneTip = isPhoneUi();", chart)
        self.assertIn("confine: phoneTip,", chart)
        self.assertIn("max-width: calc(100vw - 22px)", chart)
        self.assertIn("fontSize: phoneTip ? 11 : 12", chart)
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertIn("confine: isPhoneUi()", intra)
        self.assertIn("max-width: calc(100vw - 22px)", intra)
        self.assertIn("fontSize: isPhoneUi() ? 11 : 12", intra)

    def test_phone_tooltip_layout_split_by_line(self):
        """手机提示框内容重排: OHLC 两行, 指标 extras 逐项一行; 桌面分支保持一行。"""
        chart = _extract_fn(self.src, "updateChart")
        self.assertIn("开 ${cfn(k.open)} 高 ${cfn(k.high)}</div>", chart)
        self.assertIn("低 ${cfn(k.low)} 收 ${cfn(k.close)}${chgHtml}</div>", chart)
        self.assertIn("extras.map(e => `<div>${e}</div>`)", chart)
        self.assertIn("extras.join(' | ')", chart, "桌面仍是一行拼起来")
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertIn("低 ${cFn(b.low)} 收 ${cFn(b.close)}</div>", intra)
        self.assertIn("开 ${cFn(b.open)} 高 ${cFn(b.high)} 低 ${cFn(b.low)} 收 ${cFn(b.close)}</div>", intra,
                      "桌面/平板那一行不变")

    def test_chip_band_geometry(self):
        """手机筹码带按「可用宽度」取像素, 没地方时返回 null 不画。

        回归点: 手机上左侧让位是绝对 px (展开侧栏 234px), 带宽若按整屏百分比给,
        360px 屏上 234 + 0.35*360 正好相等 → K 线网格被挤成 0 宽。"""
        fn = _extract_fn(self.src, "chipBandFor")
        self.assertIn("isPhoneUi()", fn)
        self.assertIn("CHIP_MIN_BAND_PX", fn)
        script = (_extract_fn(self.src, "isPhoneUi") + "\n"
                  + _extract_fn(self.src, "spanPx") + "\n"
                  + "const CHIP_MIN_BAND_PX = 80, CHIP_MAX_BAND_PX = 140;\n"
                  + fn + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => {
  globalThis.window = { innerWidth: c.vw, matchMedia: q => ({ matches: q.indexOf('640px') >= 0 ? !!c.phone : false }) };
  return chipBandFor(c.w, c.gl);
})));
""")
        out = self._run(script, [
            {"phone": False, "vw": 1440, "w": 1440, "gl": "288px"},   # 桌面: 历史取值逐字不变
            {"phone": True, "vw": 390, "w": 390, "gl": "8%"},          # 手机 + 侧栏折叠: 地方够
            {"phone": True, "vw": 360, "w": 360, "gl": "234px"},       # 手机 + 侧栏展开: 没地方
            {"phone": True, "vw": 390, "w": 390, "gl": "234px"},       # 390px + 展开: 地方仍不够
        ])
        self.assertEqual(out[0], {"klineRight": "17%", "bandLeft": "83%", "bandRight": "3%"},
                         "桌面/平板几何不得漂移")
        self.assertTrue(out[1] and out[1]["klineRight"].endswith("px"), "手机带宽按像素给")
        bandW = int(out[1]["klineRight"][:-2]) - 6
        self.assertGreaterEqual(bandW, 80)
        self.assertLessEqual(bandW, 140)
        self.assertEqual(int(out[1]["bandLeft"][:-2]), 390 - bandW - 2, "带左缘 = 宽 - 带宽 - 右边距")
        self.assertIsNone(out[2], "没地方 → 本次不画筹码带 (否则 K 线归零)")
        self.assertIsNone(out[3], "390px + 侧栏展开同样没地方: 宁可不画带, 也不把 K 线挤没"
                                  " (折叠侧栏 / 关掉筹码后自动恢复, 见 out[1])")
        # 调用方: 「画不了」并进 chipOn, 几何传下去; 摘要列单位无关
        chart = _extract_fn(self.src, "updateChart")
        self.assertIn("chipBandFor(chart.getWidth(), panelGridLeft())", chart)
        self.assertIn("&& !!chipBand;", chart, "没地方时 chipOn 必须为假")
        self.assertIn("calcGridLayout(panels, chipOn, chipBand)", chart)
        self.assertIn("chipOn && chipBand ? chipBand.klineRight : '2%'",
                      _extract_fn(self.src, "calcGridLayout"))
        self.assertNotIn("left: '83%', right: '3%'", self.src, "几何不再写死")
        self.assertIn("spanPx(cband.bandLeft, cw, false) + cw * 0.005", chart)
        self.assertIn("spanPx(cband.bandRight, cw, true) - cw * 0.002", chart)

    def test_phone_indicator_sheet_wrappers_transparent_on_desktop(self):
        """包装层在桌面必须对布局透明 (display:contents) → 桌面 DOM 只深一层, 视觉与顺序不变。"""
        self.assertIn("#ind-body, #adj-slot, #adj-group { display: contents; }", self.src)
        self.assertIn("#btn-ind-sheet, #btn-ind-sheet-done { display: none; }", self.src)
        bar = self.src[self.src.index('id="indicator-bar"'):self.src.index('id="btn-ind-more"')]
        for anchor in ('id="ind-body"', 'id="btn-ind-sheet"', 'id="adj-slot"', 'id="adj-group"'):
            self.assertIn(anchor, bar, anchor)
        # 复权那一对仍夹在风控线与 BOLL 之间 (桌面顺序不变)
        self.assertLess(bar.index('id="lbl-risk"'), bar.index('id="adj-group"'))
        self.assertLess(bar.index('id="adj-group"'), bar.index('id="chk-boll"'))

    def test_phone_indicator_bar_no_horizontal_scroll(self):
        """手机指标栏不再横向滑动: 一行只剩「面板」+ 复权, 其余进下拉浮层。"""
        narrow = _css_block(self.src, "@media (max-width: 640px)")
        self.assertNotIn("overflow-x: auto", narrow, "手机指标栏不能再靠右滑")
        self.assertIn("#indicator-bar.sheet-open #ind-body { display: flex; }", narrow)
        self.assertIn("position: absolute", narrow, "浮层悬浮在图表上, 不推挤图表高度")
        self.assertIn("max-height: 58vh", narrow)
        self.assertIn("z-index: 30", narrow)
        self.assertIn("#btn-ind-more { display: none; }", narrow, "手机用浮层滚动替代「更多」两级折叠")
        self.assertIn("#indicator-bar .extra-ind { display: inline-flex; }", narrow)
        self.assertIn("#indicator-bar label { white-space: nowrap; flex-shrink: 0; }", narrow)

    def test_phone_indicator_sheet_interactions(self):
        """开合/关闭三个入口/分组标题/复权挪位/计数标签 接线完整。"""
        self.assertIn('onclick="toggleIndSheet()"', self.src)
        self.assertIn('aria-expanded="false"', self.src)
        self.assertIn('aria-controls="ind-body"', self.src)
        self.assertIn('onclick="closeIndSheet()"', self.src)
        wire = _extract_fn(self.src, "wireIndSheet")
        self.assertIn("bar.contains(e.target)", wire, "点浮层外才收起")
        self.assertIn("'Escape'", wire)
        groups = _extract_fn(self.src, "syncIndSheetGroups")
        self.assertIn("副图指标", groups)
        self.assertIn("高级", groups)
        self.assertIn("cur.remove()", groups, "切回桌面要移除段标题")
        apply = _extract_fn(self.src, "applyIndSheet")
        self.assertIn("bar.appendChild(group)", apply,
                      "手机: 复权挪到栏内必须用 appendChild")
        self.assertNotIn("insertBefore", apply,
                         "禁止「参照物」插入: 参照物不属于该父节点就抛 NotFoundError, "
                         "而本函数被点面板/勾选/切周期/启动调用, 抛出去会连带打断这些流程")
        self.assertIn("try {", apply, "DOM 操作要吞异常, 不能外溢打断调用方")
        self.assertIn("slot.appendChild(group)", apply, "桌面: 复权归位 (无锚点依赖)")
        self.assertIn("closeIndSheet();", apply, "切回桌面时浮层要收起")
        self.assertIn("getIndicatorPanels().length", apply)
        # 触发按钮 / 周期切换 / 启动 三处都要刷新
        self.assertIn("applyIndSheet()", _extract_fn(self.src, "onConfigChange"))
        self.assertIn("applyIndSheet()", _extract_fn(self.src, "applyPeriodUI"))
        self.assertIn("applyIndSheet();", self.src)

    def test_ind_sheet_label(self):
        """触发按钮文案: 已开副图个数 (不展开也知道状态)。"""
        script = _extract_fn(self.src, "indSheetLabel") + """
const ns = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(ns.map(n => indSheetLabel(n))));
"""
        out = self._run(script, [0, 3, 12])
        self.assertEqual(out, ["面板 ▾", "面板 3 ▾", "面板 12 ▾"])
        self.assertEqual(self._run(script, ["x"])[0], "面板 ▾", "脏值按 0 处理")

    def test_apply_ind_sheet_survives_hostile_dom(self):
        """回归: 用最小假 DOM 跑真源码 applyIndSheet, 复现线上那次 NotFoundError。

        现场: 手机上点「面板」报 NotFoundError(参照物不是该父节点的子节点), 面板打不开且
        切周期/勾选也一起断。这里构造「#ind-body 不在 #indicator-bar 之下」的宿主环境
        (假 DOM 的 insertBefore 对越界参照物显式抛错), 断言:
          ① 不抛异常  ② 复权仍被挪进栏内  ③ 计数标签照写。
        """
        helper = """
// ── 最小假 DOM: insertBefore 严格校验参照物归属, 越界即抛 NotFoundError ──
class FakeNode {
  constructor(tag) { this.tagName = tag; this.children = []; this.parentNode = null;
    this.textContent = ''; this.title = ''; this.className = ''; this.setAttribute = (k, v) => { this[k] = v; }; }
  get firstChild() { return this.children[0] || null; }
  contains(n) { return n === this || this.children.some(c => c.contains(n)); }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.children.push(n); return n; }
  removeChild(n) { this.children = this.children.filter(c => c !== n); n.parentNode = null; return n; }
  insertBefore(n, ref) {
    if (ref.parentNode !== this) {
      const e = new Error("Failed to execute 'insertBefore' on 'Node'");
      e.name = 'NotFoundError';
      throw e;
    }
    const i = this.children.indexOf(ref);
    this.children.splice(i, 0, n);
    n.parentNode = this;
    return n;
  }
}
const ids = {};
const mk = (id) => { const n = new FakeNode('div'); n.id = id; ids[id] = n; return n; };
// 真实结构: bar 里只有触发按钮; #ind-body 被放在了别处 (宿主环境异常时就是这样)
const bar = mk('indicator-bar'), btn = mk('btn-ind-sheet');
const slot = mk('adj-slot'), group = mk('adj-group'), orphanBody = mk('ind-body');
bar.appendChild(btn);
slot.appendChild(group);
const outside = new FakeNode('div');
outside.appendChild(orphanBody);
// 段标题要插在 MACD 的 label 与 lbl-info 之前 → 它们在 #ind-body 里
const body = orphanBody;
const macdLabel = new FakeNode('label'), infoLabel = new FakeNode('label');
macdLabel.appendChild(mk('chk-macd'));
infoLabel.appendChild(mk('lbl-info'));
body.appendChild(macdLabel);
body.appendChild(infoLabel);
// closest('label') 支持: 段标题插入锚点要用 (真代码路径, 不桩掉)
FakeNode.prototype.closest = function (sel) {
  let n = this;
  while (n) { if (sel === 'label' && n.tagName === 'label') return n; n = n.parentNode; }
  return null;
};
globalThis.document = {
  getElementById: (id) => ids[id] || null,
  createElement: (tag) => new FakeNode(tag),
  querySelectorAll: () => [],
};
globalThis.isPhoneUi = () => true;
globalThis.getIndicatorPanels = () => ['macd', 'kdj', 'atr'];
globalThis.closeIndSheet = () => { globalThis._closed = true; };
// 注意: 这里不桩 syncIndSheetGroups —— 跑真实现, 覆盖线上失败路径的相邻代码
"""
        script = (_extract_fn(self.src, "indSheetLabel") + "\n"
                  + _extract_fn(self.src, "syncIndSheetGroups") + "\n"
                  + _extract_fn(self.src, "applyIndSheet") + "\n"
                  + helper + """
let thrown = null;
try { applyIndSheet(); } catch (e) { thrown = e.name + ': ' + e.message; }
process.stdout.write(JSON.stringify({
  thrown,
  groupParent: group.parentNode && group.parentNode.id,
  barChildren: bar.children.map(c => c.id),
  btnText: btn.textContent,
  groupHeaders: body.children.filter(c => c.className === 'ind-group').map(c => c.textContent),
}));
""")
        out = self._run(script, {})
        self.assertIsNone(out["thrown"], f"applyIndSheet 不应把异常抛给调用方: {out['thrown']}")
        self.assertEqual(out["groupParent"], "indicator-bar", "复权仍要被挪进栏内")
        self.assertIn("adj-group", out["barChildren"])
        self.assertEqual(out["btnText"], "面板 3 ▾")
        # 真 syncIndSheetGroups 也跑过了: 两个段标题就地插在各自锚点前, 不抛异常
        self.assertEqual(out["groupHeaders"], ["副图指标", "高级"])

    def test_put_segs_backing_and_guard(self):
        """图例底衬必须盖住省略号; 网格被挤到没有宽度时不画 (否则负宽底衬 + 文字外溢)。"""
        segs = _extract_arrow(self.src, "putSegs")
        self.assertIn("const tail = truncated ? ' …' : '';", segs)
        self.assertIn("widthOf(drawn) + 10", segs, "底衬宽度要算上省略号")
        self.assertIn("if (!(maxW > 0)) return;", segs, "无宽度直接不画")
        script = """
const calls = [];
const c2 = {
  measureText: (t) => ({ width: String(t).length * 7 }),
  fillRect: (x, y, w, h) => calls.push({ op: 'rect', x, y, w, h }),
  fillText: (t, x, y) => calls.push({ op: 'text', t, x, y }),
  set fillStyle(v) {}, set globalAlpha(v) {}, get fillStyle() { return '#000'; },
};
const C = () => ({ bg: '#fff', text: '#000' });
const SEG_GAP = '  ';
""" + segs + """
const grid = JSON.parse(process.argv[1]);
STATE = { _gridRects: grid };
putSegs(0, [{ text: 'MA5 11.1', fill: '#111111' }, { text: 'MA10 11.2', fill: '#222222' }], 31);
const rects = calls.filter(c => c.op === 'rect');
const texts = calls.filter(c => c.op === 'text');
const lastText = texts[texts.length - 1];
const rect = rects[0];
process.stdout.write(JSON.stringify({
  rect, texts: texts.length,
  coversEllipsis: rect ? (rect.x + rect.w) >= (lastText.x + 7 * lastText.t.length) : null,
}));
"""
        # 窄网格: 两段放不下 → 丢第二段 + 省略号, 底衬仍要盖住省略号
        out = self._run(script, [{"top": 0, "bottom": 300, "left": 0, "right": 100}])
        self.assertEqual(out["texts"], 2, "一段文字 + 一个省略号")
        self.assertTrue(out["coversEllipsis"], "省略号必须落在底衬内")
        # 宽度为 0 (maxW = -14): 一律不画
        out0 = self._run(script, [{"top": 0, "bottom": 300, "left": 200, "right": 206}])
        self.assertEqual(out0["texts"], 0, "无宽度时不画文字")
        self.assertNotIn("rect", out0, "无宽度时连底衬都不画")

    def test_info_row_budget(self):
        """信息行这一行能用多少宽度 (纯函数): 顶栏内宽 − 内边距 − 同行其它项及间距。"""
        script = _extract_fn(self.src, "infoRowBudget") + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => infoRowBudget(c.innerW, c.pad, c.sibs, c.gap))));
"""
        out = self._run(script, [
            {"innerW": 768, "pad": 24, "sibs": [], "gap": 6},          # 信息行独占一行 (自己换行时)
            {"innerW": 768, "pad": 24, "sibs": [32], "gap": 6},        # 同行只有 GitHub 入口
            {"innerW": 1024, "pad": 24, "sibs": [32], "gap": 6},
            {"innerW": 1920, "pad": 24, "sibs": [32, 606], "gap": 6},  # ≥1920: 功能组也在同一行
            {"innerW": 390, "pad": 20, "sibs": [], "gap": 6},          # ≤640 GitHub 入口已隐藏
        ])
        self.assertEqual(out[0], 744)
        self.assertEqual(out[1], 706)
        self.assertEqual(out[2], 962)
        self.assertEqual(out[3], 1246, "1920 - 24 内边距 - (32+606) 同行两项 - 2*6 间距")
        self.assertEqual(out[4], 370)

    def test_toolbar_row_plan(self):
        """顶栏排几行 (抽真源码跑): 大宽度触屏先争取单行, 收尽量少的次要读数; 收光还不行才两行。

        信息行权重同 test_info_drop_plan (全显示 need = 854 + 11*6 = 920), 功能组自然宽取 680。
        """
        m = re.search(r"const INFO_DROP_ORDER = (\[[^\]]*\]);", self.src)
        slack = re.search(r"const INFO_FIT_SLACK = ([0-9.]+);", self.src)
        self.assertIsNotNone(m, "index.html 中找不到 INFO_DROP_ORDER")
        self.assertIsNotNone(slack, "index.html 中找不到 INFO_FIT_SLACK")
        order = json.loads(m.group(1).replace("'", '"'))
        script = ("const INFO_DROP_ORDER = " + m.group(1) + ";\n"
                  + "const INFO_FIT_SLACK = " + slack.group(1) + ";\n"
                  + _extract_fn(self.src, "infoRowBudget") + "\n"
                  + _extract_fn(self.src, "infoDropPlan") + "\n"
                  + _extract_fn(self.src, "toolbarRowPlan") + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => toolbarRowPlan(c.items, c.innerW, c.pad, c.gap, c.githubW, c.fnW))));
""")
        items = [{"id": i, "w": w, "on": True} for i, w in
                 [("info-sym", 150), ("info-holding", 50), ("info-price", 48), ("info-change", 68),
                  ("info-open", 53), ("info-high", 53), ("info-low", 53), ("info-pledge", 66),
                  ("info-shares", 98), ("info-amount", 69), ("info-premium", 66), ("info-turnover", 80)]]

        def case(innerW, fnW=680):
            return {"items": items, "innerW": innerW, "pad": 24, "gap": 6, "githubW": 32, "fnW": fnW}

        cases = [
            case(1920),          # 很宽: 一行就够, 一个都不收
            case(1376),          # iPad Pro 13 横屏: 收到只剩 换手 才刚好并排
            case(1300),          # 更窄: 连 换手 也要收 (行情字段永不收)
            case(1025),          # 一行收光也放不下 → 退回两行, 两行够住 → 字段全放回
            case(1376, 0),       # 功能组量不到 (0) → 预算更宽松, 一个都不用收
            case(1032, 680),     # 竖屏 1032: 同样退回两行
        ]
        out = self._run(script, cases)
        self.assertEqual(out[0], {"rows": 1, "plan": []}, "1920 一行就够")
        self.assertEqual(out[1], {"rows": 1, "plan": ["info-pledge", "info-shares",
                                                      "info-amount", "info-premium"]},
                         "1376: 单行预算 628 → 收到只剩 换手 才塞得进一行")
        self.assertEqual(out[2], {"rows": 1, "plan": order},
                         "1300: 单行预算 552 → 连 换手 也要收")
        self.assertEqual(out[3], {"rows": 2, "plan": []},
                         "1025: 单行最小宽 511 也塞不进 (预算 277) → 退回两行, 字段全放回来")
        self.assertEqual(out[4], {"rows": 1, "plan": []}, "功能组量不到时按 0 算, 更宽松")
        self.assertEqual(out[5], {"rows": 2, "plan": []}, "竖屏 1032 仍走两行")
        for row in out:
            self.assertIn(row["rows"], (1, 2))
            self.assertTrue(all(p in order for p in row["plan"]),
                            f"收起名单里出现了非统计字段: {row['plan']}")

    def test_info_drop_plan(self):
        """信息行收起决策 (抽真源码跑): 只在放不下时收, 且只可能收那 5 个统计读数。

        权重按实测观的量级给: 代码+名称 150 / 持仓 50 / 现价 48 / 涨跌 68 / 开高低 各 53 /
        质押 66 / 流通份额 98 / 成交额 69 / 溢价 66 / 换手 80, 项间距 6。
        12 项全显示时 need = 854 + 11*6 = 920。
        """
        m = re.search(r"const INFO_DROP_ORDER = (\[[^\]]*\]);", self.src)
        self.assertIsNotNone(m, "index.html 中找不到 INFO_DROP_ORDER")
        slack = re.search(r"const INFO_FIT_SLACK = ([0-9.]+);", self.src)
        self.assertIsNotNone(slack, "index.html 中找不到 INFO_FIT_SLACK")
        order = json.loads(m.group(1).replace("'", '"'))
        script = ("const INFO_DROP_ORDER = " + m.group(1) + ";\n"
                  + "const INFO_FIT_SLACK = " + slack.group(1) + ";\n"
                  + _extract_fn(self.src, "infoDropPlan") + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => infoDropPlan(c.items, c.avail, c.gap))));
""")
        keep = [("info-sym", 150), ("info-holding", 50), ("info-price", 48), ("info-change", 68),
                ("info-open", 53), ("info-high", 53), ("info-low", 53)]
        stats = [("info-pledge", 66), ("info-shares", 98), ("info-amount", 69),
                 ("info-premium", 66), ("info-turnover", 80)]
        items = [{"id": i, "w": w, "on": True} for i, w in keep + stats]
        no_pledge = [dict(it, on=(it["id"] != "info-pledge")) for it in items]

        def case(avail, its=None):
            return {"items": its or items, "avail": avail, "gap": 6}

        cases = [case(1000),                      # 放得下 → 一个都不收
                 case(920),                       # 正好卡住 → 也不收
                 case(919.9),                     # 0.1 的浮点/取整噪声被容差吸收
                 case(919.6),                     # 0.4 的真实短缺: 浏览器真会为这点差额折行
                 case(900),                       # 差一点 → 只收最次要的质押
                 case(800),                       # 差得多 → 质押 + 流通份额
                 case(700),                       # 再差 → 再收成交额
                 case(520),                       # 收满 5 个才刚好放下
                 case(500),                       # 收满 5 个仍放不下 → null (全放回)
                 case(900, no_pledge),            # 质押本来就没数据 (数据侧隐藏) → 不用收
                 case(10)]                        # 极端窄 → null, 且绝不能返回行情字段
        out = self._run(script, cases)
        self.assertEqual(out[0], [])
        self.assertEqual(out[1], [], "need 正好等于可用宽度就算放得下")
        self.assertEqual(out[2], [], "0.1px 的浮点噪声不能算成放不下")
        self.assertEqual(out[3], ["info-pledge"], "0.4px 的真实短缺要收 (768 实测: 0.48px 的短缺就折行)")
        self.assertEqual(out[4], ["info-pledge"])
        self.assertEqual(out[5], ["info-pledge", "info-shares"])
        self.assertEqual(out[6], ["info-pledge", "info-shares", "info-amount"])
        self.assertEqual(out[7], order, "收满 5 个才放下时顺序必须与 INFO_DROP_ORDER 一致")
        self.assertIsNone(out[8], "收光仍放不下 → 交回 CSS (窄屏换行), 而不是把行情字段也收掉")
        self.assertEqual(out[9], [], "数据侧已经隐藏的项既不计入也不该被\"收\"")
        self.assertIsNone(out[10])
        for plan in out:
            self.assertTrue(plan is None or all(p in order for p in plan),
                            f"收起名单里出现了非统计字段: {plan}")

    def test_dropdown_box_stays_in_viewport(self):
        """搜索下拉的限位几何 (抽真源码跑): 搜索框在工具栏行里的左缘随入口按钮数/断点变
        (手机上行内左缘约 150px), 只靠 CSS 的 left:0 锚不住, 300px 的浮层必然伸出右缘。"""
        script = _extract_fn(self.src, "dropdownBox") + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => dropdownBox(c.left, c.vw))));
"""
        cases = [
            {"left": 350, "vw": 1024},   # 平板: 宽度上限即装得下, 不用移位
            {"left": 148, "vw": 390},    # 手机: 右缘顶出 → 整体左移
            {"left": 148, "vw": 320},    # 极窄: 宽度先收到 vw-16
            {"left": 10, "vw": 390},     # 贴左: 不动
            {"left": 0, "vw": 390},      # 左缘为 0: 夹到左边距, 不越界
            {"left": 148, "vw": 0},      # 取不到视口宽 → 回落 CSS
        ]
        out = self._run(script, cases)
        self.assertEqual(out[0], {"width": 360, "left": 0})
        self.assertEqual(out[1], {"width": 360, "left": -126})
        self.assertEqual(out[2], {"width": 304, "left": -140})
        self.assertEqual(out[3], {"width": 360, "left": 0})
        self.assertEqual(out[4], {"width": 360, "left": 8})
        self.assertIsNone(out[5], "无视口宽度时交回 CSS (left:0 + max-width)")
        for c, box in zip(cases, out):
            if box is None:
                continue
            self.assertLessEqual(box["width"], 360, "宽度上限别涨")
            self.assertGreaterEqual(c["left"] + box["left"], 8, "左缘不得出屏")
            self.assertLessEqual(c["left"] + box["left"] + box["width"], c["vw"] - 8, "右缘不得出屏")

    def test_panel_grid_right(self):
        """K线右边距: 五档栏可见时让位, 否则回落 (供筹码/默认取值)。"""
        fn = _extract_fn(self.src, "panelGridRight")
        self.assertIn("depth-dock", fn)
        self.assertIn("offsetWidth", fn)
        script = (
            "const els = {};\n"
            "globalThis.document = { getElementById: (id) => els[id] || null };\n"
            + fn + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => {
  els['depth-dock'] = c.has ? { style: { display: c.display }, offsetWidth: c.w } : null;
  return panelGridRight();
})));
""")
        out = self._run(script, [
            {"has": False},                                   # 没有该节点
            {"has": True, "display": "none", "w": 118},       # 隐藏 (桌面/非分时)
            {"has": True, "display": "block", "w": 0},        # 还没布局出宽度
            {"has": True, "display": "block", "w": 118},      # 手机分时: 让位
        ])
        self.assertEqual(out[:3], [None, None, None], "没显示/没宽度时回落")
        self.assertEqual(out[3], "122px", "可见时返回面板宽 + 4px")

    def test_is_weekly_plus(self):
        """周K及以上折 KDJ, 日K及以下折 RSI —— 口径不能与 isElderPeriod 混。"""
        script = ("const ALL_PERIODS = [];\n"
                  + _extract_fn(self.src, "isWeeklyPlus") + """
const ps = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(ps.map(p => isWeeklyPlus(p))));
""")
        out = self._run(script, ["1d", "1w", "1M", "intraday", "1m", "5m", "15m", "30m", "60m"])
        self.assertEqual(out, [False, True, True, False, False, False, False, False, False])

    def test_folding_direction_in_period_ui(self):
        body = _extract_fn(self.src, "applyPeriodUI")
        self.assertIn("const weekPlus = isWeeklyPlus(period);", body)
        self.assertIn("lblKdj.style.display = weekPlus ? 'none' : ''", body, "周K及以上折 KDJ")
        self.assertIn("lblRsi.style.display = weekPlus ? '' : 'none'", body, "日K及以下折 RSI")
        # 折叠是纯显示层: 面板装配逻辑不得被周期折叠影响 (渲染保持原样)
        self.assertNotIn("isWeeklyPlus", _extract_fn(self.src, "getIndicatorPanels"))

    def test_folding_applied_on_first_paint(self):
        """不带 ?period= 直接开首页也要过一次 applyPeriodUI, 否则折叠不生效。"""
        idx = self.src.index("const jumpPeriod = bootParams.get('period');")
        seg = self.src[idx:idx + 400]
        self.assertIn("applyPeriodUI(jumpPeriod)", seg)
        self.assertIn("else applyPeriodUI(STATE.period)", seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
