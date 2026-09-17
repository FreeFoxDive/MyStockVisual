# -*- coding: utf-8 -*-
"""左侧栏 (基本信息+五档) 前端测试: 静态结构断言 + panelGridLeft 行为镜像。

关键回归点:
  * 五档所有周期可用: depthSecOn() 不再按周期门控, 轮询/显隐统一走它;
  * 基本信息面板可收起/展开 (sp-fold / side-reopen);
  * 网格左边距 = 面板宽度 + 12 + Y 轴刻度预留, 避免面板遮挡左侧刻度;
  * 分时下切换复权不再静默改写 STATE.period (漏态根因)。

运行:
    venv/Scripts/python.exe -u visual/test/test_side_panel_js.py
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
MARKET_CLOCK_JS = _VISUAL_DIR / "static" / "js" / "market-clock.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
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


class SidePanelStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_panel_structure(self):
        for token in ('id="side-panel"', 'id="info-sec"', 'id="info-body"',
                      'id="depth-sec"', 'id="depth-body"', 'id="chk-info"'):
            self.assertIn(token, self.src, token)

    def test_old_depth_panel_removed(self):
        self.assertNotIn("depthPanelOn", self.src)
        self.assertNotIn("showDepthPanel", self.src)
        self.assertNotIn('id="depth-panel"', self.src)

    def test_phone_depth_dock(self):
        """手机: 五档搬到右缘独立容器 (仅分时); 桌面/平板仍在左侧基本信息框内。"""
        self.assertIn('id="depth-dock"', self.src)
        # 容器常驻 DOM 但默认不显示 → 桌面渲染不受影响
        css = self.src[self.src.index('#depth-dock {'):]
        css = css[:css.index('}')]
        self.assertIn("display: none", css)
        self.assertIn("right: 0", css, "手机五档栏贴右缘")
        # 归属可逆: 手机挂右缘容器, 否则挂回左侧信息框
        host = _extract_fn(self.src, "syncDepthHost")
        self.assertIn("depth-dock", host)
        self.assertIn("info-sec", host)
        self.assertIn("depth-sec", host)
        # 可见性: 勾选框为总闸门, 手机再叠一层「只分时」
        vis = _extract_fn(self.src, "depthPanelVisible")
        self.assertIn("depthSecOn()", vis)
        self.assertIn("isPhoneUi()", vis)
        self.assertIn("intraday", vis)

    def test_depth_section_not_gated_by_period(self):
        body = _extract_fn(self.src, "depthSecOn")
        self.assertNotIn("intraday", body, "五档应所有周期可用, 不再按周期门控")
        self.assertIn("chk-depth", body)

    def test_side_panels_apply_in_one_place(self):
        """显隐统一走 applySidePanels, 各处不再各写一遍 (手机/桌面分支集中在一处)。"""
        self.assertIn("function applySidePanels()", self.src)
        self.assertEqual(self.src.count("showSidePanel(sidePanelOn())"), 1,
                         "只允许 applySidePanels 内部出现一次")
        self.assertEqual(self.src.count("showDepthSection("), 2,
                         "一处定义 + 只允许 applySidePanels 里调用一次")
        self.assertNotIn("showDepthSection(depthSecOn())", self.src)
        body = _extract_fn(self.src, "applySidePanels")
        for token in ("syncDepthHost()", "showSidePanel(sidePanelOn())",
                      "depthPanelVisible()", "depth-dock"):
            self.assertIn(token, body)

    def test_depth_has_header_row(self):
        # 五档盘口补列头 (基本信息每行自带 sp-label, 五档此前缺失)
        head = self.src.index('id="depth-sec"')
        body = self.src.index('id="depth-body"')
        self.assertLess(head, body, "表头应位于 depth-body 之前")
        seg = self.src[head:body]
        self.assertIn('dp-head', seg)
        for col in ('档位', '价格', '量'):
            self.assertIn(col, seg, col)
        self.assertIn('#side-panel .dp-head', self.src)
        # 字号随信息面板 (#side-panel 基准 12px), 不单独设 font-size
        css = self.src[self.src.index('#side-panel .dp-head'):]
        css = css[:css.index('}')]
        self.assertNotIn('font-size', css, "五档表头不应单独设字号 (须与信息一致)")

    def test_history_and_search_switch_refresh_panels(self):
        # 历史标签/搜索下拉换股必须与回车一致地刷新 基本信息 + 五档
        for name in ('switchToHistory', 'selectResult'):
            self.assertIn('loadCurrent()', _extract_fn(self.src, name),
                          f'{name} 换股须经 loadCurrent 刷新信息/五档')

    def test_load_current_resets_panel_state(self):
        # 换股先作废旧股面板缓存并解除节流, 否则信息/五档停留上一只
        body = _extract_fn(self.src, 'loadCurrent')
        self.assertIn('resetSidePanelData()', body)
        self.assertIn('ensureQuoteStream()', body)
        reset = _extract_fn(self.src, 'resetSidePanelData')
        for token in ('STATE.stockInfo = null', 'STATE.depth = null',
                      'STATE._lastInfoAt = 0', 'STATE._lastDepthAt = 0'):
            self.assertIn(token, reset, token)

    def test_fetch_data_no_longer_restarts_stream(self):
        # 快照流启动统一由 loadCurrent/ensureQuoteStream 管理 (含分时换股)
        body = _extract_fn(self.src, 'fetchData')
        self.assertNotIn('startQuoteStream()', body)
        self.assertIn('ensureQuoteStream', self.src)

    def test_fetch_depth_no_period_gate(self):
        body = _extract_fn(self.src, "fetchDepth")
        self.assertNotIn("STATE.period", body, "五档拉取不应再按周期拦截")

    def test_panel_fold_controls_present(self):
        self.assertIn('id="side-reopen"', self.src)
        self.assertIn('id="sp-fold-info"', self.src)
        self.assertIn('onclick="foldSidePanel()"', self.src)
        self.assertIn('onclick="unfoldSidePanel()"', self.src)
        show = _extract_fn(self.src, "showSidePanel")
        self.assertIn("'side-reopen'", show)
        self.assertIn("'block'", show)
        # 收起按钮与展开恢复都要保留信息/五档开关
        fold = _extract_fn(self.src, "foldSidePanel")
        self.assertIn("_sidePrev", fold)
        unfold = _extract_fn(self.src, "unfoldSidePanel")
        self.assertIn("_sidePrev", unfold)

    def test_panel_grid_left_reserves_axis_labels(self):
        body = _extract_fn(self.src, "panelGridLeft")
        self.assertIn("AXIS_LABEL_RESERVE", body)
        self.assertIn("offsetWidth", body)

    def test_info_and_depth_folded_into_more_group(self):
        # 信息/五档 收进「更多」折叠组 (默认收起), 不再常显在面板栏最前
        for anchor in ('id="lbl-info"', 'id="lbl-depth"'):
            tag_start = self.src.rindex('<label', 0, self.src.index(anchor))
            tag = self.src[tag_start:self.src.index('>', self.src.index(anchor))]
            self.assertIn('extra-ind', tag, f'{anchor} 应带 extra-ind 折叠类')
        more_group = self.src[self.src.index('id="indicator-bar"'):self.src.index('id="btn-ind-more"')]
        for anchor in ('id="lbl-info"', 'id="lbl-depth"', 'id="cfg-ma"', 'id="chk-cross"'):
            self.assertIn(anchor, more_group, f'{anchor} 应在「更多」按钮之前')

    def test_cross_default_on(self):
        anchor = self.src.index('id="chk-cross"')
        tag = self.src[self.src.rindex('<input', 0, anchor):self.src.index('>', anchor)]
        self.assertIn('checked', tag, '金叉死叉默认开启')
        self.assertIn("cfg.cross !== false", _extract_fn(self.src, 'applyConfig'))

    def test_impulse_channel_gap_after_chip(self):
        # 动力系统/通道/缺口 移到筹码之后, 排在 MACD 之前
        bar = self.src[self.src.index('id="indicator-bar"'):self.src.index('id="btn-ind-more"')]
        chip = bar.index('id="lbl-chip"')
        self.assertLess(bar.index('id="chk-volume"'), chip, "筹码 应排在成交量之后")
        for anchor in ('id="lbl-impulse"', 'id="lbl-channel"', 'id="lbl-gap"'):
            self.assertGreater(bar.index(anchor), chip, f'{anchor} 应排在筹码之后')
        self.assertLess(bar.index('id="lbl-gap"'), bar.index('id="chk-macd"'),
                        "缺口 应排在 MACD 之前")

    def test_depth_nested_in_info_box(self):
        # 五档不再是独立的 sp-sec 块, 也不是独立带标题的框; 并入信息框内且无 label
        seg = self.src[self.src.index('id="info-sec"'):self.src.index('id="side-reopen"')]
        self.assertIn('id="depth-sec"', seg, "五档区块应包含在信息区块内")
        self.assertNotIn('class="sp-sec" id="depth-sec"', seg, "五档不应再是独立 box")
        depth_seg = seg[seg.index('id="depth-sec"'):]
        self.assertNotIn("sp-subtitle", depth_seg, "五档不再显示 label")
        self.assertNotIn("<span>五档</span>", depth_seg, "五档不应再有文字标题")

    def test_depth_section_uses_block_display(self):
        # 回归: 五档块已无 sp-sec 的 column 方向, 若改回 flex 会变 row 使盘口行压到标题
        body = _extract_fn(self.src, "showDepthSection")
        self.assertIn("'block'", body)
        self.assertNotIn("'flex'", body)

    def test_panel_visibility_follows_info(self):
        # 五档并入信息框后, 侧栏由「信息」控制 (五档只控框内子块)
        body = _extract_fn(self.src, "sidePanelOn")
        self.assertIn("infoPanelOn()", body)
        self.assertNotIn("depthSecOn", body)

    def test_no_depth_close_button(self):
        # 五档只由面板栏勾选框开关, 标题栏不再提供 ✕ (避免收起后找不到恢复入口)
        self.assertNotIn("sp-fold-depth", self.src)
        self.assertNotIn("closeDepthSection", self.src)

    def test_stock_info_periodic_refresh(self):
        # 基本信息须随行情定时刷新 (否则盘中量比/成交额停留在加载时快照)
        self.assertIn("_lastInfoAt", self.src)
        self.assertGreaterEqual(self.src.count("fetchStockInfo()"), 3)

    def test_stock_info_shows_and_live_updates_last_price(self):
        # 行规格由 renderStockInfo 与截图绘制共用 (见 test_side_panel_shot_js)
        render = _extract_fn(self.src, "infoPanelRows")
        self.assertIn("label: '现价'", render)
        self.assertIn("d.last_price", render)
        self.assertIn("priceColor", render)
        self.assertIn("infoPanelRows", _extract_fn(self.src, "renderStockInfo"))
        live = _extract_fn(self.src, "updateLiveStockInfo")
        self.assertIn("d.last_price = Number(q.last_price)", live)
        self.assertIn("d.prev_close = Number(q.prev_close)", live)

    def test_stock_info_shows_code_and_name(self):
        # 基本信息面板补 代码/名称 (此前只有工具栏显示), 排在字段最前
        render = _extract_fn(self.src, "infoPanelRows")
        self.assertLess(render.index("label: '名称'"), render.index("label: '现价'"))
        self.assertLess(render.index("label: '代码'"), render.index("label: '现价'"))
        self.assertIn("panelStockName", render)
        self.assertIn("panelStockCode", render)
        # stock-info 未返回时用 K 线 name / 当前 symbol 兜底, 否则面板先闪 "—"
        self.assertIn("STATE.klineData", _extract_fn(self.src, "panelStockName"))
        self.assertIn("STATE.symbol", _extract_fn(self.src, "panelStockCode"))

    def test_new_rows_and_live_plumbing(self):
        """新增 流通/份额、质押、ETF溢价 三行: 数据都要从后端并进 stockInfo (面板与截图共用那份)。

        顶栏挤不下时这几项会被收起 —— 面板是它们的落处, 所以更新链路不能断:
        流通 随现价每 tick 变, 溢价 随延迟算出的后端溢价 + 缓存过期状态, 质押 随接口返回。
        """
        body = _extract_fn(self.src, "infoPanelRows")
        for label in ("label: '份额'", "label: '流通'", "label: '溢价'", "label: '质押'"):
            self.assertIn(label, body, label)
        self.assertIn(".filter(r => r)", body, "无数据的行要筛掉 (不画 — 空行)")
        # 流通/份额 与顶栏 updateFundInfo 同一套算法文案
        self.assertIn("fmtCN(fs * lastPrice)", body)
        self.assertIn("(fs / 1e8).toFixed(2) + '亿份'", body)
        # 元数据 (float_shares/is_etf/is_index) 并进 stockInfo
        live = _extract_fn(self.src, "updateLiveStockInfo")
        self.assertIn("d.is_etf = !!kd.is_etf", live)
        self.assertIn("d.float_shares = Number(kd.float_shares)", live)
        # 溢价缓存过期状态随 tick 重算, 否则面板会一直显示"不再是待更新"
        self.assertIn("d.premium_stale", live)
        self.assertIn("cached.expiresAt", live)
        # 溢价值来自写顶栏那段 (同一份 p/stale, 不另算一遍)
        info = _extract_fn(self.src, "updateInfo")
        self.assertIn("premium_pct: p, premium_stale: stale", info)
        self.assertIn("premium_at:", info)
        # 质押
        pledge = self.src[self.src.index("/api/pledge"):]
        pledge = pledge[:pledge.index("}).catch")]
        self.assertIn("pledge_ratio:", pledge)
        self.assertIn("setReadout(el, '质押'", pledge, "顶栏那段保持原样")

    def test_info_row_positions(self):
        """面板排列 (分块): ①标识 ②价格边界 ③成交与资金 ④股本结构 ⑤估值 ⑥区间表现 ⑦状态。"""
        body = _extract_fn(self.src, "infoPanelRows")
        arr = body[body.index("return ["):]     # 只看返回数组里的顺序 (变量定义在前面)
        # 名称/代码/现价 必须最前且连续 (截图与几何测试都依赖)
        self.assertTrue(arr.index("label: '名称'") < arr.index("label: '代码'")
                        < arr.index("label: '现价'") < arr.index("label: '行业'"))
        # 涨停/跌停 是现价的价格边界 → 紧跟 行业, 且在成交类之前
        self.assertLess(arr.index("label: '行业'"), arr.index("label: '涨停价'"))
        self.assertLess(arr.index("label: '跌停价'"), arr.index("label: '总手'"))
        # 股本结构 (流通/份额、质押) 在成交类之后; PE/PB/溢价 同属估值块且在股本之后
        self.assertLess(arr.index("label: '量比'"), arr.index("sharesRow"))
        self.assertLess(arr.index("sharesRow"), arr.index("pledgeRow"))
        self.assertLess(arr.index("pledgeRow"), arr.index("label: 'PE'"))
        self.assertLess(arr.index("label: 'PB'"), arr.index("premiumRow"))
        # 区间涨幅紧随估值, 状态收尾
        self.assertLess(arr.index("premiumRow"), arr.index("label: '3日涨幅'"))
        self.assertLess(arr.index("label: '10日涨幅'"), arr.index("label: '状态'"))

    def test_next_open_row_sits_below_status(self):
        """「下一开盘」排在「状态」下面 (状态说现在是什么时段, 它说下一次几点)。"""
        body = _extract_fn(self.src, "infoPanelRows")
        arr = body[body.index("return ["):]
        self.assertLess(arr.index("label: '状态'"), arr.index("marketHintRow"),
                        "下一开盘 必须收在 状态 之后")

    def test_next_open_row_updates_in_place_every_second(self):
        """值每秒变 (倒计时), 所以只就地改数值与悬停说明, 不重绘整个面板。"""
        row = self.src[self.src.index("label: '下一开盘'"):]
        row = row[:row.index("},")]
        self.assertIn("id: 'info-market-hint'", row, "要有稳定 id 供就地更新")
        self.assertIn("VisualMarketClock.openHintText", row, "文案口径来自共享时钟")
        self.assertIn("marketCountdownSec()", row)
        self.assertIn("title: marketHintTitle()", row, "完整描述走悬停, 不占值区")
        fn = _extract_fn(self.src, "renderMarketHint")
        self.assertIn("info-market-hint", fn)
        self.assertIn(".sp-val", fn, "只写数值层")
        self.assertNotIn("innerHTML", fn, "不得整块重绘 (会打断滚动/选中)")
        self.assertIn("!== text", fn, "值没变就不写 DOM (盘中恒为 '—', 否则每秒白标脏)")
        self.assertIn("row.title !== title", fn,
                      "说明也要跟着相位就地更新 (只靠面板重绘会滞后 60s)")
        clock = self.src[self.src.index("// 时间 1s 刷新"):]
        clock = clock[:clock.index("}, 1000);")]
        self.assertIn("renderMarketHint()", clock)

    def test_hint_title_carries_what_the_value_box_cannot(self):
        """值区 129px 放不下"是集合竞价还是连续竞价开盘", 这段语义只能在悬停里。"""
        fn = _extract_fn(self.src, "marketHintTitle")
        self.assertIn("openHintDetail", fn)
        self.assertIn("degraded()", fn, "日历降级优先说降级")
        # 降级提示要排在完整描述之前 (否则降级时会被描述盖掉)
        self.assertLess(fn.index("degraded()"), fn.index("openHintDetail"))

    def test_next_open_row_is_conditional_on_halt(self):
        body = _extract_fn(self.src, "infoPanelRows")
        self.assertIn("d.trade_status === 'halt' ? null", body,
                      "条件要在行构造处 (与 sharesRow/premiumRow 同一套 null 过滤)")
        self.assertIn("marketHintRow,", body, "行要进返回数组")
        self.assertIn("].filter(r => r)", body, "空行由既有 filter 丢掉")

    def test_reopen_button_doubled(self):
        seg = self.src[self.src.index("#side-reopen {"):]
        css = seg[:seg.index("}")]
        self.assertIn("padding: 16px 4px", css)
        self.assertIn("font-size: 20px", css)
        # 侧栏宽度不变 → grid.left / 画线面板偏移不受牵连
        panel = self.src[self.src.index("#side-panel {"):]
        self.assertIn("width: 204px", panel[:panel.index("}")])

    def test_stock_info_loads_core_before_enrichment(self):
        body = _extract_fn(self.src, "fetchStockInfo")
        self.assertIn("&core=1", body)
        self.assertIn("fetchStockInfo(true)", body)
        self.assertIn("resp.status === 429 ? 2000 : 4000", body)

    def test_trade_close_defaults_exit_date_and_validates_fields(self):
        src = (INDEX_HTML.parent / "trades.html").read_text(encoding="utf-8")
        status = _extract_fn(src, "onStatusChange")
        submit = _extract_fn(src, "saveTrade")
        self.assertIn("exitDate.value = todayISO()", status)
        for message in ("请填写有效的退出价", "请填写卖出日期", "请选择卖出理由"):
            self.assertIn(message, submit)

    def test_stream_uses_depth_visibility(self):
        """五档订阅跟随「是否可见」: 手机非分时不该白拉五档, 桌面仍按勾选框。"""
        stream = _extract_fn(self.src, "ensureQuoteStream")
        self.assertIn("depthPanelVisible()", stream)
        vis = _extract_fn(self.src, "depthPanelVisible")
        self.assertIn("depthSecOn()", vis, "总闸门仍是勾选框")

    def test_live_quote_patches_price_line_for_all_views(self):
        on_quote = _extract_fn(self.src, "updateLivePriceLine")
        self.assertIn("STATE.period === 'intraday'", on_quote)
        self.assertIn("STATE.klineData", on_quote)
        self.assertIn("setOption", on_quote)
        # SSE quote callback must invoke the lightweight line patch without
        # rebuilding the whole chart on every 1.25s snapshot.
        self.assertIn("updateLivePriceLine(q, symbol)", self.src)

    def test_grid_uses_panel_grid_left(self):
        self.assertIn("panelGridLeft()", self.src)
        calc = _extract_fn(self.src, "calcGridLayout")
        self.assertIn("gLeft", calc, "calcGridLayout 应使用 panelGridLeft 计算左边距")

    def test_panel_labels_use_unit_aware_grid_left(self):
        # 回归: 侧栏展开时 grid.left 是 "288px", 面板名标签若按 parseFloat(left)/100 算
        # 会被画到画布外 (≈2.88×宽度), VOL/MACD 等标签整片消失。
        body = _extract_fn(self.src, "updateChart")
        self.assertIn("spanPx(g.left", body, "指标面板名标签 x 须按单位换算")
        self.assertNotIn("parseFloat(g.left)", body, "不得再把 grid.left 当百分比")
        self.assertIn("spanPx(vg.left", body, "VOL 标签 x 须按单位换算")
        self.assertNotIn("parseFloat(vg.left)", body, "不得再把 VOL grid.left 当百分比")

    def test_panel_labels_survive_draw_visibility_toggle(self):
        # 回归: 👁 隐藏画线只该隐藏画线, 面板名标签/指标数值图例应常显
        body = _extract_fn(self.src, "_renderDrawingsNow")
        gate = body.index("!STATE.draw.visible")
        self.assertLess(body.index("paintLabels"), gate, "paintLabels 应先于可见性早退")
        self.assertLess(body.index("paintLegend"), gate, "paintLegend 应先于可见性早退")

    def test_fetchdata_period_coercion_routed_through_apply(self):
        fetch = _extract_fn(self.src, "fetchData")
        self.assertNotIn("STATE.period = '1d'", fetch, "分时切换复权不得静默改写周期")
        self.assertIn("applyPeriodUI('1d')", fetch)
        self.assertIn("function applyPeriodUI", self.src)

    def test_drawer_new_tabs(self):
        for tab in ("exchange-announcement", "zljlr", "holder-change",
                    "top-holders", "float-holders", "unlock"):
            self.assertIn(f"showCnTab('{tab}')", self.src, tab)

    def test_drawer_highlight_and_int_format(self):
        self.assertIn("highlight_symbol", self.src)
        self.assertIn("#cn-body tr.hl", self.src)
        self.assertIn("Number.isInteger(v)", self.src)

    def test_drawer_kv_mode(self):
        # 单票键值视图 (主力净流入): showCnTab 需支持 data.kv 分支与样式
        self.assertIn("Array.isArray(data.kv)", self.src)
        self.assertIn(".cn-kv-row", self.src)
        self.assertIn("cn-kv-label", self.src)

    def test_drawer_tab_race_guard(self):
        # tab 快速切换: 过期响应不得覆盖新 tab (请求序号 + 股票比对)
        fn = _extract_fn(self.src, "showCnTab")
        self.assertIn("_cnSeq", fn)
        self.assertIn("seq !== _cnSeq", fn)
        self.assertIn("STATE.symbol !== symbol", fn)
        self.assertIn("seq === _cnSeq", fn, "finally 也需按序号守卫")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class PanelGridLeftBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        fns = "\n".join(_extract_fn(src, n) for n in
                        ("infoPanelOn", "depthSecOn", "sidePanelOn", "panelGridLeft"))
        cls.script = (
            fns + "\n"
            + "const c = JSON.parse(process.argv[1]);"
            + "globalThis.STATE = {period: c.period};"
            + "globalThis.document = {getElementById: (id) => c.el[id] || null};"
            + "process.stdout.write(JSON.stringify({"
            + " side: sidePanelOn(), depth: depthSecOn(), left: panelGridLeft()}));"
        )

    def _run(self, period, el):
        proc = subprocess.run(["node", "-e", self.script, json.dumps({"period": period, "el": el})],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def test_info_on_daily(self):
        out = self._run("1d", {"chk-info": {"checked": True}, "side-panel": {"offsetWidth": 204}})
        self.assertTrue(out["side"])
        self.assertFalse(out["depth"], "未勾选五档则不出五档")
        # 204(面板) + 12(间距) + 72(Y轴刻度预留)
        self.assertEqual(out["left"], "288px")

    def test_depth_on_daily_narrow(self):
        out = self._run("1d", {"chk-info": {"checked": True}, "chk-depth": {"checked": True},
                               "side-panel": {"offsetWidth": 150}})
        self.assertTrue(out["side"])
        self.assertTrue(out["depth"], "五档在日K也应显示")
        self.assertEqual(out["left"], "234px", "窄屏按实际面板宽度让位 + 刻度预留")

    def test_intraday_depth_on_narrow(self):
        out = self._run("intraday", {"chk-info": {"checked": True}, "chk-depth": {"checked": True},
                                     "side-panel": {"offsetWidth": 150}})
        self.assertTrue(out["side"])
        self.assertTrue(out["depth"])
        self.assertEqual(out["left"], "234px")

    def test_both_off_no_space(self):
        out = self._run("1d", {"chk-info": {"checked": False}, "chk-depth": {"checked": False}})
        self.assertFalse(out["side"], "信息与五档都关闭 → 侧栏隐藏")
        self.assertEqual(out["left"], "8%")

    def test_depth_alone_does_not_keep_panel_on_daily(self):
        out = self._run("1d", {"chk-info": {"checked": False}, "chk-depth": {"checked": True},
                               "side-panel": {"offsetWidth": 204}})
        self.assertFalse(out["side"], "五档已并入信息框, 信息关闭则整框隐藏")
        self.assertEqual(out["left"], "8%")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class InfoPanelRowsBehaviorTest(unittest.TestCase):
    """面板行规格 (抽真源码跑): 新排列 / 新增三行 / 无数据不出行。"""

    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        fns = "\n".join(_extract_fn(src, n) for n in
                        ("infoPanelRows", "panelStockName", "panelStockCode",
                         "fmtLots", "fmtCN", "fmtPrice3", "normPct",
                         "marketCountdownSec", "marketHintTitle"))
        # 下一开盘行读的是共享时钟 (真模块 + 可控状态), 不是服务端返回的字段
        cls.script = (
            MARKET_CLOCK_JS.read_text(encoding="utf-8") + "\n"
            + fns + "\n"
            + "globalThis.C = () => ({ up: 'up', down: 'down' });\n"
            + "globalThis.VisualLive = { price: (v) => (v == null ? '—' : String(+Number(v).toFixed(3))) };\n"
            + "const c = JSON.parse(process.argv[1]);\n"
            + "globalThis.STATE = { symbol: c.symbol, period: c.period, klineData: null };\n"
            # 真实载荷一定带 time (→ today); 用例不写就默认"目标时刻就在今天"
            + "const mk = Object.assign({ phase: 'trading', at: Date.now() }, c.market || {});\n"
            + "if (!mk.today) mk.today = String(mk.nextOpenAt || mk.nextLiveAt || '').slice(0, 10);\n"
            + "globalThis.marketClock = { state: mk, degraded: () => !!c.degraded };\n"
            + "const rows = infoPanelRows(c.info);\n"
            + "process.stdout.write(JSON.stringify({ rows: rows || null,"
            + " labels: (rows || []).map(r => r.label) }));"
        )

    def _rows(self, info, period="1d", symbol="000001.SZ", market=None, degraded=False):
        proc = subprocess.run(["node", "-e", self.script,
                               json.dumps({"info": info, "period": period, "symbol": symbol,
                                           "market": market, "degraded": degraded})],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    STOCK = {"symbol": "000001.SZ", "name": "平安银行", "last_price": 11.5, "prev_close": 11.2,
             "industry": "银行", "volume": 123456, "amount": 1.4e8, "turnover_rate": 0.51,
             "vol_ratio": 1.2, "limit_up": 12.32, "limit_down": 10.08, "chg_3d": 1.5,
             "chg_5d": -2.25, "chg_10d": 0, "pe": 5.5, "pb": 0.55,
             "trade_status": "trading", "trade_status_text": "连续竞价",
             "float_shares": 1.2e10, "pledge_ratio": 3.4}

    def test_stock_order_and_new_rows(self):
        out = self._rows(self.STOCK)
        self.assertEqual(out["labels"], [
            '名称', '代码', '现价', '行业', '涨停价', '跌停价', '总手', '成交额', '换手', '量比',
            '流通', '质押', 'PE', 'PB', '3日涨幅', '5日涨幅', '10日涨幅', '状态', '下一开盘'])
        rows = {r["label"]: r for r in out["rows"]}
        # 流通 = float_shares × 现价 (与顶栏同算法/文案): 1.2e10 × 11.5 = 1.38e11 → 1380.00亿
        self.assertEqual(rows["流通"]["text"], "1380.00亿")
        self.assertEqual(rows["质押"]["text"], "3.40%")
        self.assertFalse(rows["现价"]["color"] == rows["质押"]["color"], "质押不带涨跌色")

    def test_etf_has_shares_and_premium_but_no_pledge(self):
        etf = dict(self.STOCK, is_etf=True, float_shares=2.38e10,
                   premium_pct=-0.37, premium_stale=True, premium_at=1700000000)
        out = self._rows(etf)
        self.assertIn("份额", out["labels"])
        self.assertIn("溢价", out["labels"])
        self.assertNotIn("流通", out["labels"], "ETF 用份额, 不用流通市值")
        self.assertNotIn("质押", out["labels"], "ETF 没有质押 → 直接不出行")
        rows = {r["label"]: r for r in out["rows"]}
        self.assertEqual(rows["份额"]["text"], "238.00亿份")
        self.assertEqual(rows["溢价"]["text"], "-0.37%（待更新）", "过期的后端溢价要标出来")
        self.assertEqual(rows["溢价"]["color"], "down")
        self.assertIn("计算时间", rows["溢价"]["title"])
        # 缓存新鲜时不带后缀
        fresh = self._rows(dict(etf, premium_stale=False))
        self.assertEqual({r["label"]: r for r in fresh["rows"]}["溢价"]["text"], "-0.37%")

    def test_index_shows_none_of_the_three(self):
        idx = dict(self.STOCK, is_index=True, float_shares=1.2e10)
        out = self._rows(idx)
        for label in ("流通", "份额", "溢价", "质押"):
            self.assertNotIn(label, out["labels"], f"指数不该出 {label} 行")

    def test_hides_rows_without_backend_data(self):
        """面板行只在拿到后端数据后才出现 (不是画 — 的空行)。"""
        bare = {k: v for k, v in self.STOCK.items()
                if k not in ("float_shares", "pledge_ratio")}
        out = self._rows(bare)
        self.assertEqual(out["labels"][:10], ['名称', '代码', '现价', '行业', '涨停价', '跌停价',
                                              '总手', '成交额', '换手', '量比'])
        for label in ("流通", "份额", "溢价", "质押"):
            self.assertNotIn(label, out["labels"])
        # 无数据整体 → 返回 null (面板走 "无数据" 占位)
        self.assertIsNone(self._rows(None)["rows"])


    def test_next_open_row_renders_real_clock_state(self):
        """盘外给出下一次开盘时刻 (+ 同日倒计时); 盘中给占位符。"""
        for market, expect in (
            # 同日: 时刻 + 「余 <剩余>」; 倒计时值随本机时钟变, 只断言前缀
            ({"phase": "break", "nextOpenAt": "2026-09-17 13:00:00", "nextOpenInSec": 3600},
             "13:00 余 "),
            # 跨日: 只给 月-日 时:分 (值区 129px 放不下"下一交易日 … 集合竞价")
            ({"phase": "closed", "today": "2026-09-17",
              "nextLiveAt": "2026-09-18 09:15:00", "nextLiveInSec": 64800},
             "09-18 09:15"),
            ({"phase": "trading"}, "—"),
        ):
            with self.subTest(phase=market["phase"]):
                rows = {r["label"]: r for r in
                        self._rows(self.STOCK, market=market)["rows"]}
                self.assertIn("下一开盘", rows)
                self.assertTrue(rows["下一开盘"]["text"].startswith(expect),
                                f"{market['phase']} → {rows['下一开盘']['text']!r}")

    def test_next_open_row_title_has_the_full_description(self):
        """值被压短后, "是集合竞价还是连续竞价开盘"必须还能查到 (悬停 title)。"""
        for market, expect in (
            ({"phase": "break", "nextOpenAt": "2026-09-17 13:00:00"}, "连续竞价开盘"),
            ({"phase": "pre", "nextLiveAt": "2026-09-17 09:15:00"}, "集合竞价开始"),
            ({"phase": "closed", "today": "2026-09-17", "nextLiveAt": "2026-09-18 09:15:00"},
             "下一交易日 09-18 09:15 集合竞价开始"),
        ):
            with self.subTest(phase=market["phase"]):
                rows = {r["label"]: r for r in
                        self._rows(self.STOCK, market=market)["rows"]}
                self.assertIn(expect, rows["下一开盘"]["title"],
                              f"{market['phase']} 的悬停说明 → {rows['下一开盘']['title']!r}")

    def test_next_open_row_explains_calendar_degradation(self):
        """日历降级 (缺 pandas_market_calendars) 要在界面上说, 不能只在日志里 warning。"""
        rows = {r["label"]: r for r in self._rows(self.STOCK, degraded=True)["rows"]}
        self.assertIn("降级", rows["下一开盘"]["title"])

    def test_next_open_row_hidden_while_halted(self):
        """停牌时不显示「下一开盘」: 这只票当日就不交易, 给"13:00 开盘"是误导。

        (601995.SH 这类当日停牌标的即为此例 —— 服务端 trade_status='halt' 时,
        该行整行消失, 而不是显示一个不会发生的开盘时间。)
        """
        rows = {r["label"]: r for r in
                self._rows(dict(self.STOCK, trade_status="halt",
                                trade_status_text="停牌", volume=0))["rows"]}
        self.assertNotIn("下一开盘", rows, "停牌不该再提示下一次开盘")
        self.assertEqual(rows["状态"]["text"], "停牌")
        # 同时确认非停牌时那一行还在 (否则这条断言会因为别的原因通过)
        ok = {r["label"] for r in self._rows(self.STOCK)["rows"]}
        self.assertIn("下一开盘", ok)

if __name__ == "__main__":
    unittest.main(verbosity=2)
