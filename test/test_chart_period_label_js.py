# -*- coding: utf-8 -*-
"""图表左上角周期标题: 文案表 + 行位避让 (真源码镜像)。

两件事一起钉住:
  1. 周期中文名只有一个来源 (PERIOD_LABELS), 且必须与 #btn-<period> 按钮文案逐一一致 ——
     以前周期只体现在按钮高亮上, 图内没有文字, 截图导出后认不出当前看的是哪个周期;
     两张表各写一份就会漂移 (按钮改文案, 标题还写着旧的)。
  2. 标题行与 MA5/MA10 图例行**永不同行**: 图内左上角可用空间被图表顶部 2..18px 的
     dataZoom 滑块切掉一截 (K 线主图网格顶只有 2%), 矮屏上网格顶到滑块下沿不足一行,
     所以标题行要按下沿钳住, 图例行再往下让一行 —— mainRowOffsets 的两条不变量
     (标题不被滑块压住 / 标题盒下沿在图例盒上沿之上) 在这里逐视口验证。

运行:
    venv/Scripts/python.exe -u visual/test/test_chart_period_label_js.py
"""

import json
import re
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR / "test") not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR / "test"))

from js_test_util import require_node, run_node, run_node_json  # noqa: E402

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"

# 图表顶部 dataZoom 滑块的像素占位 (建图处 slider: top 2, height 16)
SLIDER_TOP, SLIDER_HEIGHT = 2, 16
SLIDER_BOTTOM = SLIDER_TOP + SLIDER_HEIGHT
TITLE_FONT_PX = 13                  # 周期标题字号 (MAIN_TITLE_FONT)
MA_LEGEND_FONT_PX = 11              # MA·BOLL 图例字号 (putSegs 里写死)


def _src() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# 逐帧路径里 visibleBarIndex 走 chartModel() 取 dataZoom 组件 (不能用 getOption: 那是
# 整份 option 深拷贝)。测试脚本里按同一形状造替身: mkChart(endValue) → STATE.chart。
CHART_STUB_JS = """
function mkChart(endValue) {
  return { getModel: () => ({ getComponent: (t, i) => (t === 'dataZoom' && i === 0 ? { option: { endValue } } : null) }) };
}
"""


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


def _intraday_axis_fns(src: str) -> str:
    """分时的「按槽位取数」那一套 (固定窗口 240 槽)。

    分时启用固定窗口后, 可见末位是**槽位**坐标而不是 bars 下标 (中间夹着未走到的空槽),
    所以标题日期与均价读数都要经这组函数落到槽位上, 不能再拿 bars[i] 直接取。
    """
    return "\n".join(_extract_fn(src, n) for n in (
        "intradaySlotCount", "intradayBarAt", "intradayBarAtOrBefore",
        "intradayVisibleBar"))


def _session_bars(n: int):
    """当日分时 payload 的前 n 根 (末相位: 09:31-11:30 + 13:01-15:00), 标题用例只要 time。

    按槽位顺序生成, 所以 120 根就是"午休半场"、121 根是第一根下午的数据。
    """
    times = []
    for start, end in (((9, 31), (11, 30)), ((13, 1), (15, 0))):
        m, stop = start[0] * 60 + start[1], end[0] * 60 + end[1]
        while m <= stop:
            times.append(f"{m // 60:02d}:{m % 60:02d}")
            m += 1
    return [{"time": t} for t in times[:n]]


def _extract_const_obj(src: str, name: str) -> str:
    """抽 `const NAME = { ... };` 整段 (值都是字面量, 可直接交给 node 执行)。"""
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\{", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name} = {{")
    start = m.start()
    end = src.index("\n};", m.end())
    return src[start:end + 3]


def _extract_const_line(src: str, name: str) -> str:
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*[^;]+;", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 const {name}")
    return m.group(0)


class PeriodLabelJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = _src()

    def _button_labels(self, periods):
        """#btn-<period> 的可见文案 (HTML 解析式匹配, 与按钮标签写法解耦)。

        只认 ALL_PERIODS 里的 id —— 工具栏还有 btn-alerts 这类非周期按钮。
        """
        out = {}
        for m in re.finditer(r'<button[^>]*id="btn-([\w]+)"[^>]*>([^<]+)</button>', self.src):
            if m.group(1) in periods:
                out[m.group(1)] = m.group(2).strip()
        self.assertTrue(out, "没解析到周期按钮, HTML 结构变了?")
        return out

    def test_period_labels_cover_all_periods_and_match_buttons(self):
        js = _extract_const_obj(self.src, "PERIOD_LABELS")
        labels = json.loads(run_node(js + "\nprocess.stdout.write(JSON.stringify(PERIOD_LABELS));"))
        all_periods = json.loads(run_node(
            _extract_const_line(self.src, "MINUTE_PERIODS")
            + "\n" + _extract_const_line(self.src, "ALL_PERIODS")
            + "\nprocess.stdout.write(JSON.stringify(ALL_PERIODS));"
        ))
        for p in all_periods:
            self.assertIn(p, labels, f"PERIOD_LABELS 缺周期 {p}")
        buttons = self._button_labels(set(all_periods))
        self.assertEqual(sorted(buttons), sorted(all_periods), "周期按钮与 ALL_PERIODS 对不上")
        for p, text in buttons.items():
            self.assertEqual(labels[p], text,
                             f"周期 {p} 的标题文案与按钮不一致 (按钮「{text}」/ 标题「{labels[p]}」)")

    def test_period_label_falls_back_to_key(self):
        js = _extract_const_obj(self.src, "PERIOD_LABELS") + "\n" + _extract_fn(self.src, "periodLabel")
        out = json.loads(run_node(js + """
process.stdout.write(JSON.stringify([
  periodLabel('1d'), periodLabel('intraday'), periodLabel('1M'), periodLabel('1m'),
  periodLabel('3d'), periodLabel(null), periodLabel(undefined),
]));"""))
        self.assertEqual(out, ["日K", "分时", "月K", "1分", "3D", "", ""],
                         "未知/空周期要有确定性兜底 (不得抛错、不得输出 undefined)")

    def test_main_row_offsets_never_overlap_slider_or_legend(self):
        """逐视口验证行位: 标题不被 dataZoom 滑块压住, 且在图例行之上一行。

        标题字号 (13px) 比图例 (11px) 大, 盒高因此不同 —— 两条不变量都要按各自字号算。
        """
        js = (_extract_const_line(self.src, "CHART_SLIDER_BOTTOM")
              + "\n" + _extract_const_line(self.src, "MAIN_TITLE_BOX_TOP")
              + "\n" + _extract_const_line(self.src, "ROW_PITCH")
              + "\n" + _extract_const_line(self.src, "MAIN_TITLE_FONT")
              + "\n" + _extract_fn(self.src, "mainRowOffsets")
              + "\n" + _extract_fn(self.src, "labelFontSize"))
        # K 线主图网格顶 = 2% (calcGridLayout topGap), 分时 12%; 4 档视口高
        cases = [{"h": h, "gridTop": round(h * pct)} for h in (400, 600, 900, 1200) for pct in (0.02, 0.12)]
        out = json.loads(run_node(js + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c => {
  const o = mainRowOffsets(c.gridTop);
  const box = (y, font) => [y - (labelFontSize(font) + 1), y + 4];
  return { ...c, ...o,
    titleFontPx: labelFontSize(MAIN_TITLE_FONT),
    titleBox: box(c.gridTop + o.title, MAIN_TITLE_FONT),
    legendBox: box(c.gridTop + o.legend, '11px monospace'),
    sliderBottom: CHART_SLIDER_BOTTOM };
})));
""", json.dumps(cases)))
        for c in out:
            tag = f"H={c['h']} gridTop={c['gridTop']}"
            self.assertGreater(c["titleFontPx"], MA_LEGEND_FONT_PX,
                               f"{tag}: 标题字号要比 MA 图例大")
            self.assertGreaterEqual(c["titleBox"][0], c["sliderBottom"],
                                    f"{tag}: 标题行被 dataZoom 滑块压住 ({c['titleBox']} vs 滑块下沿 {c['sliderBottom']})")
            self.assertLess(c["titleBox"][1], c["legendBox"][0],
                            f"{tag}: 标题行与 MA/BOLL 图例行重叠 ({c['titleBox']} vs {c['legendBox']})")
            # 网格顶离滑块足够远时, 标题行与面板名标签同一口径 (网格顶 + 15)
            if c["gridTop"] >= c["sliderBottom"]:
                self.assertEqual(c["title"], 15, f"{tag}: 分时/低网格顶应回到 +15 口径")

    def test_title_box_top_matches_font_size(self):
        """标题盒上沿常量必须跟字号挂钩 (改字号忘了改行位 → 又压滑块/压图例)。"""
        self.assertIn("const MAIN_TITLE_FONT = 'bold %dpx monospace';" % TITLE_FONT_PX, self.src)
        self.assertIn("const MAIN_TITLE_BOX_TOP = %d;" % (TITLE_FONT_PX + 1), self.src)
        self.assertIn("CHART_SLIDER_BOTTOM + MAIN_TITLE_BOX_TOP - gridTopPx", self.src)
        # paintLabels 的底衬同样按字号推导 (盒高 = 字号 + 5)
        labels = _extract_fn(self.src, "paintLabels")
        self.assertIn("labelFontSize(c2.font)", labels)
        self.assertIn("l.y - (fs + 1)", labels)
        self.assertIn("fs + 5", labels)

    def test_title_format_follows_visible_bar(self):
        """标题格式 = 「周期 · 日期」, 日期取当前可见末根 bar (与下面那行 MA 数值同一根)。

        这里只测**格式**; 所以把 marketClock 设成休市 (isTradingDay=false), 让
        「日期以本地时钟为准」的升级不生效 —— 否则用例会随跑测试的日期变。本地时钟
        那条规则单独在 test_title_uses_local_clock_end_to_end 里用注入的 today 测。
        """
        js = (_extract_const_obj(self.src, "PERIOD_LABELS") + "\n"
              + _extract_const_line(self.src, "TITLE_NARROW_GRID_PX") + "\n"
              + _extract_fn(self.src, "periodLabel") + "\n"
              + _extract_fn(self.src, "localDayStr") + "\n"
              + _extract_fn(self.src, "titleDateWithLocalClock") + "\n"
              + _extract_fn(self.src, "chartModel") + "\n"
              + _extract_fn(self.src, "visibleBarIndex") + "\n"
              + _intraday_axis_fns(self.src) + "\n"
              + _extract_fn(self.src, "chartTitleText"))
        cases = [
            # 日/周/月 K: date 只有日期
            {"period": "1d", "grid": [0, 0, 800, 300], "bars": [{"date": "2026-09-17"}, {"date": "2026-09-18"}],
             "want": "日K · 2026-09-18"},
            {"period": "1w", "grid": [0, 0, 800, 300], "bars": [{"date": "2026-09-11"}],
             "want": "周K · 2026-09-11"},
            # 分钟 K: date 自带时间
            {"period": "5m", "grid": [0, 0, 800, 300],
             "bars": [{"date": "2026-09-18 10:00"}, {"date": "2026-09-18 10:05"}],
             "want": "5分 · 2026-09-18 10:05"},
            # 窄网格: 整段去掉年份 (不是截断成「2026-09-…」)
            {"period": "5m", "grid": [0, 0, 180, 300],
             "bars": [{"date": "2026-09-18 10:05"}], "want": "5分 · 09-18 10:05"},
            {"period": "1d", "grid": [0, 0, 180, 300],
             "bars": [{"date": "2026-09-18"}], "want": "日K · 09-18"},
            # 分时: bar 只有 HH:MM, 日期用响应级 date; 只画一天, 时间不上标题
            {"period": "intraday", "grid": [0, 0, 800, 300], "intraday": {"date": "2026-09-18",
             "bars": [{"time": "09:30"}, {"time": "11:30"}]}, "want": "分时 · 2026-09-18"},
            {"period": "intraday", "grid": [0, 0, 180, 300], "intraday": {"date": "2026-09-18",
             "bars": [{"time": "11:30"}]}, "want": "分时 · 09-18"},
            # 旧缓存 payload 没有 date: 降级成只有周期名, 不编造日期
            {"period": "intraday", "grid": [0, 0, 800, 300],
             "intraday": {"bars": [{"time": "11:30"}]}, "want": "分时"},
            # 数据还没到
            {"period": "1d", "grid": [0, 0, 800, 300], "bars": [], "want": "日K"},
        ]
        out = json.loads(run_node(CHART_STUB_JS + js + """
const cases = JSON.parse(process.argv[1]);
let STATE = {};
let marketClock = { state: { isTradingDay: false } };   // 只测格式: 关掉本地时钟升级
process.stdout.write(JSON.stringify(cases.map(c => {
  STATE.period = c.period;
  STATE.klineData = { klines: c.bars || [] };
  STATE.intradayData = c.intraday || null;
  STATE._gridRects = [c.grid ? { left: c.grid[0], right: c.grid[2], top: c.grid[1], bottom: c.grid[3] } : null];
  // 缩放窗口: 第一例故意把可见末端停在倒数第二根之前, 验证日期跟着 endValue 走
  STATE.chart = mkChart(c.endValue);
  return chartTitleText();
})));
""", json.dumps(cases)))
        for c, got in zip(cases, out):
            self.assertEqual(got, c["want"], f"周期 {c['period']} 的标题格式不对")

    def test_intraday_title_across_session_phases(self):
        """分时标题的日期 = 响应下发的那天 (也就是图上那批 bar 的日子), 与本地时钟无关。

        日K 的「本地时钟升级」是为"盘中当日 bar 还没补上来, 标题别慢一天"设计的; 分时
        不存在这种滞后 —— 后端的 date 就是它截出来的那批 bar 的日期。所以盘前 / 非交易日
        看到的上一交易日那批, 标题必须写那一天, 不能改写成今天。
        """
        js = (_extract_const_obj(self.src, "PERIOD_LABELS") + "\n"
              + _extract_const_line(self.src, "TITLE_NARROW_GRID_PX") + "\n"
              + _extract_fn(self.src, "periodLabel") + "\n"
              + _extract_fn(self.src, "localDayStr") + "\n"
              + _extract_fn(self.src, "titleDateWithLocalClock") + "\n"
              + _extract_fn(self.src, "chartModel") + "\n"
              + _extract_fn(self.src, "visibleBarIndex") + "\n"
              + _extract_fn(self.src, "intradaySessionSlots") + "\n"
              + _intraday_axis_fns(self.src) + "\n"
              + _extract_fn(self.src, "chartTitleText"))
        cases = [
            # 盘前 (交易日 09:15-09:31): 源里当日还没有 bar, 图上还是上一交易日那批
            {"phase": "盘前", "date": "2026-09-17", "bars": 240, "isTradingDay": True,
             "want": "分时 · 2026-09-17"},
            # 长假后首个交易日盘前: 数据是节前最后一天
            {"phase": "长假后盘前", "date": "2026-09-30", "bars": 240, "isTradingDay": True,
             "want": "分时 · 2026-09-30"},
            # 盘中 (数据就是今天)
            {"phase": "盘中", "date": "2026-09-18", "bars": 60, "isTradingDay": True,
             "want": "分时 · 2026-09-18"},
            # 午休 (半场)
            {"phase": "午休", "date": "2026-09-18", "bars": 120, "isTradingDay": True,
             "want": "分时 · 2026-09-18"},
            # 盘后 (全天)
            {"phase": "盘后", "date": "2026-09-18", "bars": 240, "isTradingDay": True,
             "want": "分时 · 2026-09-18"},
            # 非交易日 (周六): 服务端已说今天不是交易日, 一律用数据日期
            {"phase": "非交易日", "date": "2026-09-11", "bars": 240, "isTradingDay": False,
             "want": "分时 · 2026-09-11"},
        ]
        for c in cases:
            c["bars"] = _session_bars(c["bars"])
        out = json.loads(run_node_json(CHART_STUB_JS + js + """
const cases = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
let STATE = {};
let marketClock = { state: { isTradingDay: true } };
// chartTitleText 内部无参调用 localDayStr(): 固定"今天"为 2026-09-18, 让用例与跑测试的
// 日期无关 (周中/周末/跨月跑都是同一组期望值)
function localDayStr() { return '2026-09-18'; }
process.stdout.write(JSON.stringify(cases.map(c => {
  STATE.period = 'intraday';
  STATE.intradayData = { date: c.date, bars: c.bars };
  STATE.intradayAxis = intradaySessionSlots('002472.SZ', c.bars);
  STATE._gridRects = [{ left: 0, right: 800, top: 0, bottom: 300 }];
  STATE.chart = mkChart(null);
  marketClock.state = { isTradingDay: c.isTradingDay };
  return chartTitleText();
})));
""", cases))
        for c, got in zip(cases, out):
            self.assertEqual(got, c["want"],
                             f"{c['phase']} 的分时标题日期不对 (图上数据是 {c['date']})")

    def test_intraday_avg_readout_follows_line(self):
        """分时左上角均价读数: 取可见末根 bar 的 avg_price, 线一动数字就跟着动。"""
        # fmtPrice3 在真页面上走 VisualLive.price, 这里按同口径给个等价替身
        stub = ("function fmtPrice3(v) { return v == null || !Number.isFinite(Number(v)) "
                "? '—' : String(Number(Number(v).toFixed(3))); }\n")
        js = (stub + _extract_fn(self.src, "chartModel")
              + "\n" + _extract_fn(self.src, "visibleBarIndex")
              + "\n" + _intraday_axis_fns(self.src)
              + "\n" + _extract_fn(self.src, "intradayAvgText"))
        cases = [
            # 可见末端 = 最后一根
            {"bars": [{"avg_price": 17.5}, {"avg_price": 17.831}], "want": "均价 17.831"},
            # 缩放把末端停在第 2 根: 读数跟可见末端, 不是永远取最新
            {"bars": [{"avg_price": 17.5}, {"avg_price": 17.831}], "endValue": 0, "want": "均价 17.5"},
            # 末根均价为 null (停牌/缺字段那根)
            {"bars": [{"avg_price": 17.5}, {"avg_price": None}], "want": "均价 —"},
            # 旧缓存 payload 整个没有 avg_price
            {"bars": [{"close": 17.5}], "want": "均价 —"},
            # 数据还没到
            {"bars": [], "want": "均价 —"},
        ]
        out = json.loads(run_node(CHART_STUB_JS + js + """
const cases = JSON.parse(process.argv[1]);
let STATE = {};
process.stdout.write(JSON.stringify(cases.map(c => {
  STATE.intradayData = { bars: c.bars };
  STATE.chart = mkChart(c.endValue);
  return intradayAvgText();
})));
""", json.dumps(cases)))
        for c, got in zip(cases, out):
            self.assertEqual(got, c["want"], f"均价读数不对 (bars={c['bars']}, endValue={c.get('endValue')})")

    def test_intraday_avg_readout_uses_legend_row_and_line_color(self):
        """读数行落在图例行位 (标题行下一行)、颜色与均价线一致、无均价时不画。"""
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertIn("const hasAvg = bars.some(b => b.avg_price != null);", intra,
                      "均价线与读数要共用同一条判据")
        self.assertIn("const iRows = mainRowOffsets(iMainTop);", intra, "两行行位要共用一处")
        self.assertIn("y: iMainTop + iRows.title", intra, "标题行没走共享行位")
        self.assertIn("y: iMainTop + iRows.legend", intra, "均价读数没落在图例行位")
        self.assertIn("text: intradayAvgText", intra, "分时没画均价读数")
        self.assertIn("if (hasAvg) {", intra, "没有均价时不该出「均价 —」的读数行")
        # 读数的颜色是均价线那支, 不是随便一个灰
        readout = intra[intra.index("text: intradayAvgText"):]
        readout = readout[:readout.index("});")]
        self.assertIn("fill: C().avgPrice", readout, "读数颜色要和均价线一致")

    def test_title_date_with_local_clock(self):
        """日期以本地时钟为准的窄条件: 只看最新一根 + 今天是交易日 + 数据还落在后面。

        用户口径: 2026-09-18 盘中当日 bar 还没补上来时, 标题不该还写着 09-17。
        反向条件同样要钉住 —— 拖缩放看历史区间、周末/节假日都不能被改成本地当天。
        """
        js = (_extract_fn(self.src, "localDayStr")
              + "\n" + _extract_fn(self.src, "titleDateWithLocalClock"))
        cases = [
            # 最新一根 + 交易日 + 数据落后 → 换成本地当天
            {"raw": "2026-09-17", "atLatest": True, "today": "2026-09-18", "want": "2026-09-18"},
            # 分钟 K 的日期自带时间: 只换日期段, 时间要留着
            {"raw": "2026-09-17 10:05", "atLatest": True, "today": "2026-09-18",
             "want": "2026-09-18 10:05"},
            # 数据没落后 (bar 就是今天) → 不动
            {"raw": "2026-09-18", "atLatest": True, "today": "2026-09-18", "want": "2026-09-18"},
            # 分时同日 (bar 的日期 + 时间) → 不动
            {"raw": "2026-09-18 14:01", "atLatest": True, "today": "2026-09-18",
             "want": "2026-09-18 14:01"},
            # 拖缩放看历史区间 → 一律用 bar 的日期 (否则与旁边 MA 数值对不上)
            {"raw": "2024-03-28", "atLatest": False, "today": "2026-09-18", "want": "2024-03-28"},
            # 没有日期 (旧缓存/无数据) → 原样返回
            {"raw": "", "atLatest": True, "today": "2026-09-18", "want": ""},
        ]
        out = json.loads(run_node(js + """
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(c =>
  titleDateWithLocalClock(c.raw, c.atLatest, c.today))));
""", json.dumps(cases)))
        for c, got in zip(cases, out):
            self.assertEqual(got, c["want"], f"日期口径不对: {c}")

    def test_title_date_keeps_bar_date_when_not_trading_day(self):
        """周末/节假日 (服务器说 isTradingDay=false) 不改成本地当天: 那天没有 bar。"""
        js = (_extract_fn(self.src, "localDayStr")
              + "\n" + _extract_fn(self.src, "titleDateWithLocalClock"))
        out = json.loads(run_node(js + """
let marketClock = { state: { isTradingDay: false } };
const closed = [titleDateWithLocalClock('2026-09-17', true, '2026-09-19'),
                titleDateWithLocalClock('2026-09-17 10:05', true, '2026-09-19')];
marketClock = { state: { isTradingDay: true } };
const open = titleDateWithLocalClock('2026-09-17', true, '2026-09-18');
marketClock = { state: { isTradingDay: null } };
const unknown = titleDateWithLocalClock('2026-09-17', true, '2026-09-18');
process.stdout.write(JSON.stringify([closed, open, unknown]));
"""))
        self.assertEqual(out[0], ["2026-09-17", "2026-09-17 10:05"], "休市日不该被改成本地当天")
        self.assertEqual(out[1], "2026-09-18", "交易日+数据落后 → 本地当天")
        self.assertEqual(out[2], "2026-09-18", "交易日未知时按可交易处理 (与 marketClock.live 同口径)")

    def test_local_day_str_is_local_not_utc(self):
        """localDayStr 必须按本地时区拼: toISOString 是 UTC, 北京 08:00 前会串到前一天。"""
        js = _extract_fn(self.src, "localDayStr")
        out = json.loads(run_node(js + """
const d = new Date(2026, 8, 18, 0, 30);            // 本地 2026-09-18 00:30
const p = (v) => (v < 10 ? '0' : '') + v;
const fromLocalParts = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
process.stdout.write(JSON.stringify({
  got: localDayStr(d),
  fromLocalParts,
  iso: d.toISOString().slice(0, 10),
  offsetMin: d.getTimezoneOffset(),
}));
"""))
        self.assertEqual(out["got"], out["fromLocalParts"], "localDayStr 不是按本地时区拼的")
        if out["offsetMin"] != 0:
            # 非 UTC 时区下, 本地午夜的 UTC 日期是前一天 —— 这正是不许用 toISOString 的原因
            self.assertNotEqual(out["got"], out["iso"], "本地午夜串到了 UTC 日期")

    def test_local_stamp_str_has_seconds_and_no_colon(self):
        """截图文件名后缀 = 日期_时分秒 (本地时区): 同一天连拍多张才不会互相覆盖。

        时分秒不能带冒号 —— Windows 文件名不允许 ':' (拷贝到 NTFS 会失败)。
        """
        js = (_extract_fn(self.src, "localDayStr")
              + "\n" + _extract_fn(self.src, "localStampStr"))
        out = json.loads(run_node(js + """
const d = new Date(2026, 8, 18, 9, 5, 3);          // 本地 2026-09-18 09:05:03
const p = (v) => (v < 10 ? '0' : '') + v;
const fromLocalParts = '2026-09-18_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
process.stdout.write(JSON.stringify({
  got: localStampStr(d),
  fromLocalParts,
  midnight: localStampStr(new Date(2026, 8, 18, 0, 0, 0)),
  offsetMin: d.getTimezoneOffset(),
}));
"""))
        self.assertEqual(out["got"], out["fromLocalParts"], "localStampStr 不是按本地时区拼的")
        self.assertRegex(out["got"], r"^\d{4}-\d{2}-\d{2}_\d{6}$", "后缀必须是 日期_时分秒")
        self.assertNotIn(":", out["got"], "文件名里不能有冒号")
        self.assertEqual(out["midnight"], "2026-09-18_000000", "个位数时分秒要补零")

    def test_title_uses_local_clock_end_to_end(self):
        """chartTitleText 的整合口径: 最新一根且数据落后 → 本地当天; 否则用 bar 日期。"""
        js = (_extract_const_obj(self.src, "PERIOD_LABELS") + "\n"
              + _extract_const_line(self.src, "TITLE_NARROW_GRID_PX") + "\n"
              + _extract_fn(self.src, "periodLabel") + "\n"
              + _extract_fn(self.src, "localDayStr") + "\n"
              + _extract_fn(self.src, "titleDateWithLocalClock") + "\n"
              + _extract_fn(self.src, "chartModel") + "\n"
              + _extract_fn(self.src, "visibleBarIndex") + "\n"
              + _intraday_axis_fns(self.src) + "\n"
              + _extract_fn(self.src, "chartTitleText"))
        out = json.loads(run_node(CHART_STUB_JS + js + """
let STATE = {};
let marketClock = { state: { isTradingDay: true } };
const day = localDayStr();
const grid = { left: 0, right: 800, top: 0, bottom: 300 };
const title = (bars, endValue) => {
  STATE.period = '1d';
  STATE.klineData = { klines: bars };
  STATE.intradayData = null;
  STATE._gridRects = [grid];
  STATE.chart = mkChart(endValue);
  return chartTitleText();
};
// 最新一根 + 数据落后 (最后一根固定在很久以前) → 标本地当天
const lagging = title([{ date: '2020-01-02' }, { date: '2020-01-03' }], undefined);
// 同一份数据但拖到历史窗口 (末端不在最后一根) → 用那根的日期
const historical = title([{ date: '2020-01-02' }, { date: '2020-01-03' }], 0);
// 数据本身就含今天 → 不动
const fresh = title([{ date: day }], undefined);
process.stdout.write(JSON.stringify({ lagging, historical, fresh, day }));
"""))
        self.assertEqual(out["lagging"], f"日K · {out['day']}",
                         "看最新行情且当日 bar 未补上时, 标题该用本地当天")
        self.assertEqual(out["historical"], "日K · 2020-01-02", "历史窗口必须用那根 bar 的日期")
        self.assertEqual(out["fresh"], f"日K · {out['day']}", "数据已含当天时保持 bar 日期")

    def test_visible_bar_index_clamps_end_value(self):
        """visibleBarIndex: endValue 越界/非有限要钳住或退到最新, 且不得走 getOption()。"""
        js = (_extract_fn(self.src, "chartModel") + "\n" + _extract_fn(self.src, "visibleBarIndex"))
        out = json.loads(run_node(CHART_STUB_JS + js + """
let STATE = { chart: mkChart(undefined) };
const plain = visibleBarIndex(10);
STATE = { chart: mkChart(3) };
const set = visibleBarIndex(10);
STATE = { chart: mkChart(999) };
const high = visibleBarIndex(10);
STATE = { chart: mkChart(-5) };
const low = visibleBarIndex(10);
STATE = { chart: mkChart(NaN) };
const nan = visibleBarIndex(10);
STATE = { chart: mkChart(Infinity) };
const inf = visibleBarIndex(10);
STATE = { chart: mkChart('不是数字') };
const str = visibleBarIndex(10);
STATE = { chart: { getModel: () => { throw new Error('未建图'); } } };
const boom = visibleBarIndex(10);
STATE = { chart: { getModel: () => null } };
const noModel = visibleBarIndex(10);
STATE = { chart: { getModel: () => ({ getComponent: () => null }) } };
const noComp = visibleBarIndex(10);
STATE = {};
process.stdout.write(JSON.stringify([
  plain, set, high, low, nan, inf, str, boom, noModel, noComp, visibleBarIndex(0),
]));
"""))
        self.assertEqual(out, [9, 3, 9, 0, 9, 9, 9, 9, 9, 9, None],
                         "可见末端下标口径漂移 (越界/非有限/异常/空数据都要有确定结果)")
        # 逐帧路径不得回退到 chart.getOption() (整份 option 深拷贝)
        self.assertNotIn("getOption", _extract_fn(self.src, "visibleBarIndex"),
                         "visibleBarIndex 又用回 getOption 了 (逐帧路径禁止深拷贝整份 option)")
        self.assertIn("chartModel()", _extract_fn(self.src, "visibleBarIndex"))
        self.assertIn("Number.isFinite", _extract_fn(self.src, "visibleBarIndex"))

    def test_slider_bottom_constant_matches_datazoom_config(self):
        """CHART_SLIDER_BOTTOM 是行位的锚点, 必须跟建图处的 slider 几何一致。"""
        self.assertIn("const CHART_SLIDER_BOTTOM = %d;" % SLIDER_BOTTOM, self.src)
        # 两个建图入口 (K 线 / 分时) 的滑块都是 top: 2, height: 16
        sliders = re.findall(r"type: 'slider'[^\n]*top: \d+, height: \d+", self.src)
        self.assertEqual(sliders, ["type: 'slider', xAxisIndex: zoomIndices, top: 2, height: 16",
                                   "type: 'slider', xAxisIndex: allXAxisIndices, top: 2, height: 16"],
                         "dataZoom 滑块的 top/height 变了: CHART_SLIDER_BOTTOM 要跟着改")

    def test_period_title_painted_on_both_charts(self):
        """两个建图入口都要画周期标题, 且走 overlay 标签通道 (paintLabels)。

        文本给的是函数 chartTitleText (日期要跟着可见 bar 走), 不是静态字符串。
        """
        chart = _extract_fn(self.src, "updateChart")
        self.assertIn("text: chartTitleText", chart, "K 线主图没有周期标题")
        self.assertIn("font: MAIN_TITLE_FONT", chart, "标题没走共享字号")
        self.assertIn("mainRowOffsets(spanPx(grids[0].top, ch, false))", chart, "标题行位没走共享口径")
        self.assertIn("bg: true", chart, "标题要有底衬 (压在蜡烛上也能读)")
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertIn("text: chartTitleText", intra, "分时主图没有周期标题")
        self.assertIn("font: MAIN_TITLE_FONT", intra)
        self.assertIn("mainRowOffsets(iMainTop)", intra, "分时的标题行位没走共享口径")
        self.assertIn("bg: true", intra)
        # 两个标题都交给 overlay 的 paintLabels 画 (一处绘制路径), 而 paintLabels 要认 bg
        labels = _extract_fn(self.src, "paintLabels")
        self.assertIn("if (l.bg)", labels, "paintLabels 不支持底衬")

    def test_legend_row_sits_below_title_row(self):
        """MA/BOLL 图例行不能再写死 31: 它必须落在标题行下面一行。"""
        legend = self.src[self.src.index("function paintLegend"):self.src.index("function _renderDrawingsNow")]
        self.assertIn("putSegs(0, mainSegs, mainRowOffsets(STATE._gridRects[0].top).legend)", legend,
                      "图例行没用共享行位 (会和周期标题叠在一起)")
        self.assertNotIn("putSegs(0, mainSegs, 31)", legend, "旧的写死行位应已移除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
