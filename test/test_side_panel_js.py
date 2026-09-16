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

    def test_depth_section_not_gated_by_period(self):
        body = _extract_fn(self.src, "depthSecOn")
        self.assertNotIn("intraday", body, "五档应所有周期可用, 不再按周期门控")
        self.assertIn("chk-depth", body)

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

    def test_reopen_button_doubled(self):
        # 收起后唯一的恢复入口: 点按区域按 2 倍放大 (8px 2px → 16px 4px, 12px → 20px 字)
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

    def test_stream_uses_depth_sec_on(self):
        self.assertIn("depthSecOn()", _extract_fn(self.src, "ensureQuoteStream"))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
