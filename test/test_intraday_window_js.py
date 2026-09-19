# -*- coding: utf-8 -*-
"""分时固定窗口 (东财口径): 240 槽横轴 + bar 按时落到槽位 (真源码镜像)。

现象 (改之前): 分时横轴的 category 只装「已出现的分钟」, dataZoom 又是 0~100,
ECharts 把 N 个槽拉满整个网格 —— 早盘 09:31 一根 bar 就占满整幅图, 之后每过一分钟
槽宽都变窄一点, 到收盘才稳定在 1/240。东财是固定 240 槽窗口: 槽宽全天不变, 右侧
空白随开盘推进。

这里钉住三组不变量:
  1. 槽位表: 240 个、含午休跳变、相位由数据首根决定 (09:30 起 / 09:31 末);
  2. 护栏: 有 bar 落不进槽位表 / bar 比槽位多 / 非 A股 → 返回 null 退回旧行为
     (宁可观感照旧, 也不能把 bar 悄悄丢掉);
  3. 对齐: 所有 series 走 intradayPad, 未出现的槽位是 null (不是 0/undefined),
     且空槽能把 tooltip/均价读数落到「槽位之前最近一根真 bar」。

运行:
    venv/Scripts/python.exe -u visual/test/test_intraday_window_js.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR / "test") not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR / "test"))

from js_test_util import require_node, run_node, run_node_json  # noqa: E402

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
ECHARTS_JS = _VISUAL_DIR / "static" / "vendor" / "echarts-5.5.0.min.js"

# 逐帧路径里 visibleBarIndex 走 chartModel() 取 dataZoom 组件
CHART_STUB_JS = """
function mkChart(endValue) {
  return { getModel: () => ({ getComponent: (t, i) => (t === 'dataZoom' && i === 0 ? { option: { endValue } } : null) }) };
}
"""

# 被测函数 + 它们的依赖 (顺序即依赖顺序; 函数声明会提升, 顺序只为可读)
WINDOW_FNS = ["intradaySessionSlots", "intradayPad", "intradaySlotCount",
              "intradayBarAt", "intradayBarAtOrBefore",
              "chartModel", "visibleBarIndex", "intradayVisibleBar"]


def _src() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


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
    raise AssertionError(f"function {name} 括号不闭合")


def _bars(*times):
    return [{"time": t, "close": 10.0, "avg_price": 10.0} for t in times]


class IntradayWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = _src()
        cls.fns = CHART_STUB_JS + "\n" + "\n".join(
            _extract_fn(cls.src, n) for n in WINDOW_FNS)

    def _run(self, body, payload):
        return json.loads(run_node(self.fns + "\n" + body, json.dumps(payload)))

    def test_slots_cover_the_whole_session(self):
        """240 个槽、无重复、午休是 11:30 → 13:01 的跳变 (末相位, AlphaFeed 实测口径)。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
const ax = intradaySessionSlots('002472.SZ', bars);
process.stdout.write(JSON.stringify({
  n: ax.slots.length,
  uniq: new Set(ax.slots).size,
  first: ax.slots[0], mid: ax.slots[119], after: ax.slots[120], last: ax.slots[239],
}));
""", _bars("09:31"))
        self.assertEqual(out["n"], 240, "固定窗口必须是全天 240 个 1 分钟槽")
        self.assertEqual(out["uniq"], 240, "槽位不得重复")
        self.assertEqual([out["first"], out["mid"], out["after"], out["last"]],
                         ["09:31", "11:30", "13:01", "15:00"],
                         "末相位槽位表不对 (11:30 之后直接跳到 13:01)")

    def test_start_phase_used_when_data_uses_it(self):
        """首根 09:30 的数据用起相位 (09:30-11:29 + 13:00-14:59)。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
const ax = intradaySessionSlots('600519.SH', bars);
process.stdout.write(JSON.stringify({
  first: ax.slots[0], mid: ax.slots[119], after: ax.slots[120], last: ax.slots[239],
}));
""", _bars("09:30"))
        self.assertEqual([out["first"], out["mid"], out["after"], out["last"]],
                         ["09:30", "11:29", "13:00", "14:59"])

    def test_late_first_bar_still_gets_the_window(self):
        """首根不是整点第一分钟也要进固定窗口 (低流动性开盘无成交 / 停牌后复牌)。

        回归点: 早先按 `bars[0].time` 判定相位, 首根是 09:35 这种就直接放弃固定窗口,
        观感又回到"柱宽随分钟数变" —— 且没有任何提示, 用户只觉得"有的票分时图会缩"。
        现在判据是"哪个相位装得下全部 bar"。
        """
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
const ax = intradaySessionSlots('002472.SZ', bars);
const pad = ax ? intradayPad(ax, bars, b => b.close) : null;
process.stdout.write(JSON.stringify({
  firstSlot: ax.slots[0],
  at4: (ax.barAt[4] || {}).time, at9: (ax.barAt[9] || {}).time,
  filled: ax.barAt.filter(Boolean).length,
  padLen: pad.length,
  nullsBefore: pad.slice(0, 4).every(v => v === null),
}));
""", _bars("09:35", "09:36", "09:37", "09:38", "09:39", "09:40"))
        self.assertEqual(out["firstSlot"], "09:31", "默认用末相位 (实测口径)")
        self.assertEqual(out["at4"], "09:35", "槽 0=09:31, 所以 09:35 落在槽 4")
        self.assertEqual(out["at9"], "09:40")
        self.assertEqual(out["filled"], 6)
        self.assertEqual(out["padLen"], 240, "照样是 240 槽的固定窗口")
        self.assertTrue(out["nullsBefore"], "前面没走到的槽留空")

    def test_bars_land_on_their_own_slot(self):
        """bar 按时间落到槽位: 09:31/09:32/13:01 → 0/1/120 (午休不占槽)。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
const ax = intradaySessionSlots('002472.SZ', bars);
process.stdout.write(JSON.stringify(ax.barAt.map(b => (b ? b.time : null))));
""", _bars("09:31", "09:32", "13:01"))
        self.assertEqual(out[0], "09:31")
        self.assertEqual(out[1], "09:32")
        self.assertEqual(out[119], None, "11:30 之后不该有 bar")
        self.assertEqual(out[120], "13:01", "下午第一根要落在第 121 个槽上")
        self.assertEqual(len(out), 240)

    def test_hk_us_keep_the_old_axis(self):
        """港/美股会话长度不同 (330/390 分钟) 且时间戳口径未实测 → 不启用固定窗口。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify([
  intradaySessionSlots('00700.HK', bars),
  intradaySessionSlots('AAPL.US', bars),
  intradaySessionSlots('002472.SZ', bars) === null ? 'cn-null' : 'cn-ok',
]));
""", _bars("09:31"))
        self.assertEqual(out, [None, None, "cn-ok"], "只有 A股/ETF/指数才启用固定窗口")

    def test_offgrid_bar_disables_fixed_window(self):
        """有 bar 落不进槽位表就整体退回旧行为: 宁可观感照旧, 也不能丢 bar。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify([
  intradaySessionSlots('002472.SZ', bars),
  intradaySessionSlots('002472.SZ', bars.slice(1)) === null ? 'lone-null' : 'lone-ok',
]));
""", _bars("09:31", "11:31"))
        # 11:31 不在任何相位的槽位表里 (上午到 11:30 为止, 下午从 13:01 起)
        self.assertIsNone(out[0], "时间戳不合口径时必须整体放弃固定窗口")
        self.assertEqual(out[1], "lone-null", "首根 11:31 定不出相位, 也要放弃")

    def test_more_bars_than_slots_disables_fixed_window(self):
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(intradaySessionSlots('002472.SZ', bars)));
""", _bars("09:31") * 241)
        self.assertIsNone(out)

    def test_pad_fills_missing_slots_with_null(self):
        """对齐后的数组长度 = 槽位数, 未出现的槽位是 null (不是 0/undefined)。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
const ax = intradaySessionSlots('002472.SZ', bars);
const vals = intradayPad(ax, bars, b => b.close);
process.stdout.write(JSON.stringify({
  n: vals.length, first: vals[0], second: vals[1],
  nulls: vals.filter(v => v === null).length,
  noUndef: vals.every(v => v !== undefined),
  custom: intradayPad(ax, bars, b => b.close, '-')[2],
}));
""", _bars("09:31", "09:32"))
        self.assertEqual(out["n"], 240)
        self.assertEqual(out["first"], 10.0)
        self.assertEqual(out["second"], 10.0)
        self.assertEqual(out["nulls"], 238, "未出现的槽位留空 (线在末根断开)")
        self.assertTrue(out["noUndef"], "空槽必须是 null, 不能是 undefined")
        self.assertEqual(out["custom"], "-", "空槽取值要能按 series 类型覆盖 (K 线用 '-')")

    def test_candlestick_empty_slot_uses_dash(self):
        """K 线的空槽必须是 '-'。

        ECharts 5.5.0 实测: candlestick 的 data 里放 null/undefined 会在建图时抛
        `Cannot read properties of null (reading 'value')` —— 整张分时图不出来。
        几何测试 (FixedWindowGeometryTest) 用真 echarts 跑的就是这条路径。
        """
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertIn("}, INTRADAY_EMPTY_CANDLE);", intra, "K 线没传空槽标记")
        self.assertIn("const INTRADAY_EMPTY_CANDLE = '-';", self.src)

    def test_pad_falls_back_to_plain_map(self):
        """axis 为 null 时 pad 等价于原来的 bars.map(fn) (旧行为逐字保留)。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(intradayPad(null, bars, b => b.time)));
""", _bars("09:31", "09:32", "09:33"))
        self.assertEqual(out, ["09:31", "09:32", "09:33"])

    def test_empty_slot_falls_back_to_last_real_bar(self):
        """空槽取 bar 要回退到「该槽之前最近一根真 bar」: 均价读数/涨跌配色靠它。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
let STATE = {};
STATE.intradayAxis = intradaySessionSlots('002472.SZ', bars);
STATE.intradayData = { bars: bars };
        process.stdout.write(JSON.stringify({
  onBar: intradayBarAt(1) && intradayBarAt(1).time,
  emptySlot: intradayBarAt(5),
  before: intradayBarAtOrBefore(5) && intradayBarAtOrBefore(5).time,
  last: STATE.intradayAxis.barAt.reduce((acc, b, i) => (b ? i : acc), -1),
  count: intradaySlotCount(),
}));
""", _bars("09:31", "09:32"))
        self.assertEqual(out["onBar"], "09:32")
        self.assertIsNone(out["emptySlot"], "空槽本身没有 bar")
        self.assertEqual(out["before"], "09:32", "要落到槽位之前最近一根真 bar")
        self.assertEqual(out["last"], 1, "末根槽位 = 最后一根已出现 bar 的位置")
        self.assertEqual(out["count"], 240, "槽位数是 240, 不是已出现 bar 数")

    def test_visible_bar_follows_zoom(self):
        """可见末位落在右侧空槽时, 读数要退到最后一根真 bar (不是 null)。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
let STATE = {};
STATE.intradayAxis = intradaySessionSlots('002472.SZ', bars);
STATE.intradayData = { bars: bars };
const out = [];
STATE.chart = mkChart(null);                 // 未交互: 可见末端 = 轴末位 (空槽)
out.push(intradayVisibleBar() && intradayVisibleBar().time);
STATE.chart = mkChart(0);                    // 缩放停在第一根
out.push(intradayVisibleBar() && intradayVisibleBar().time);
STATE.intradayAxis = null;                   // 未启用固定窗口: 旧的末根口径
STATE.chart = mkChart(null);
out.push(intradayVisibleBar() && intradayVisibleBar().time);
process.stdout.write(JSON.stringify(out));
""", _bars("09:31", "09:32", "09:33"))
        self.assertEqual(out, ["09:33", "09:31", "09:33"])

    def test_old_axis_reads_are_unchanged(self):
        """没启用固定窗口 (intradayAxis 为 null) 时按老口径取数, 行为逐字不变。"""
        out = self._run("""
const bars = JSON.parse(process.argv[1]);
let STATE = {};
STATE.intradayAxis = null;
STATE.intradayData = { bars: bars };
process.stdout.write(JSON.stringify({
  count: intradaySlotCount(), last: intradaySlotCount() - 1,
  at0: intradayBarAt(0) && intradayBarAt(0).time,
  before1: intradayBarAtOrBefore(1) && intradayBarAtOrBefore(1).time,
  before2: intradayBarAtOrBefore(2),
  minusOne: intradayBarAtOrBefore(-1),
  oob: intradayBarAt(99),
}));
""", _bars("09:31", "09:32"))
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["last"], 1)
        self.assertEqual(out["at0"], "09:31")
        self.assertEqual(out["before1"], "09:32", "槽位即下标时取的是本槽")
        # 越界与「第 0 根之前」都返回 null: 老口径下可见下标本来就钳在范围内,
        # tooltip 里 slot-1 为 -1 时也不能绕回末根当"上一根"
        self.assertIsNone(out["before2"])
        self.assertIsNone(out["minusOne"])
        self.assertIsNone(out["oob"])

    def test_render_intraday_pads_every_series(self):
        """源码级: renderIntraday 里所有 series 数据都必须按槽位对齐, 不能用 bars.map。"""
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertIn("const pad = (fn, empty) => intradayPad(axis, bars, fn, empty);", intra)
        # 横轴那一行本身就是「轴 = 槽位表 / 否则已出现序列」的取舍; 注释里提到的旧写法
        # 也不算数 —— 两个都摘掉再查「还有没有 series 直接吃已出现序列」
        rest = "\n".join(
            l for l in intra.replace(
                "const times = axis ? axis.slots : bars.map(b => b.time);", "").splitlines()
            if not l.strip().startswith("//"))
        self.assertNotIn("bars.map(", rest, "还有 series 直接吃已出现序列 → 会与 K 线错位")
        self.assertIn("const times = axis ? axis.slots : bars.map(b => b.time);", intra)
        # 三处 grid 的横轴 (主图 K线 / 成交量 / 指标面板) 共用同一份槽位表, 否则
        # 子面板与主图槽宽不一致 —— 这也正是「柱子自己会缩」的同一类错位
        self.assertEqual(intra.count("data: times"), 3,
                         "主图/成交量/指标面板的横轴都要吃固定槽位表")
        # tooltip 按槽位取数 (dataIndex 是槽位坐标, 不再是 bars 下标)
        self.assertIn("const b = intradayBarAt(slot);", intra)
        self.assertNotIn("data.bars[idx]", intra)

    def test_window_state_cleared_when_leaving_intraday(self):
        """换股/换周期都要清掉槽位表, 否则会用上一只股票的 240 槽去画新标的。"""
        self.assertIn("STATE.intradayAxis = null; // 固定窗口槽位表同属上一只股票的数据",
                      self.src)
        switch = _extract_fn(self.src, "switchPeriod")
        self.assertIn("STATE.intradayAxis = null;", switch)

    def test_axis_label_interval_fits_full_window(self):
        """轴标签每 30 槽一个 (240 槽 → 半小时一个刻度), 与东财的疏密一致。"""
        intra = _extract_fn(self.src, "renderIntraday")
        self.assertEqual(intra.count("interval: 29"), 2)


class SessionProgressionTest(unittest.TestCase):
    """一根根推进一整个交易日: 槽宽、落槽、末根槽位都不许随分钟数变化。

    真实盘中就是「09:31 一根 → 11:30 半场 120 根 → 13:01 第 121 根 → 15:00 收满 240 根」,
    这条链上任何一处改成按"已出现根数"算几何 (而不是按时间落槽), 表现就是柱子又变宽
    变窄。盘中/午休/盘后三种状态在数据上只差"前 n 根有值", 所以这里按 n 推进。
    """

    @classmethod
    def setUpClass(cls):
        require_node()
        if not ECHARTS_JS.exists():
            raise unittest.SkipTest(f"缺少 vendored echarts: {ECHARTS_JS}")
        cls.src = _src()
        cls.fns = "\n".join(_extract_fn(cls.src, n) for n in
                            ("intradaySessionSlots", "intradayPad"))
        cls.slots = _all_slot_times()          # 末相位全天 240 个槽位时刻
        cls.counts = [1, 2, 30, 60, 120, 121, 180, 239, 240]

    def _payload(self, n):
        """模拟"已走到第 n 分钟"的当日 payload (时间取槽位表前 n 个)。"""
        return [{"time": t, "open": 10.0, "close": 10.0 + i * 0.01,
                 "low": 9.9, "high": 10.1, "avg_price": 10.0, "volume": 100 + i,
                 "macd_hist": 0.01 if i % 2 else -0.01}
                for i, t in enumerate(self.slots[:n])]

    def _measure(self):
        """一次 node 运行里把每个 n 的几何都量出来 (真 echarts, SSR)。"""
        script = self.fns + """
const echarts = require(process.argv[4]);
const all = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
const counts = JSON.parse(process.argv[2]);
const EMPTY = process.argv[3];
const W = 600, H = 400;
const measure = (bars) => {
  const ax = intradaySessionSlots('002472.SZ', bars);
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: W, height: H });
  chart.setOption({
    animation: false,
    grid: { left: 0, right: 0, top: 0, bottom: 0 },
    xAxis: { type: 'category', data: ax.slots },
    yAxis: { type: 'value', scale: true },
    dataZoom: [{ type: 'inside', xAxisIndex: [0], start: 0, end: 100 }],
    series: [
      { name: 'K线', type: 'candlestick',
        data: intradayPad(ax, bars, b => [b.open, b.close, b.low, b.high], EMPTY) },
      { name: '均价', type: 'line', data: intradayPad(ax, bars, b => b.avg_price), symbol: 'none' },
      { name: '成交量', type: 'bar', data: intradayPad(ax, bars, b => b.volume) },
    ],
  }, { notMerge: true, silent: true });
  const px = (i) => chart.convertToPixel({ xAxisIndex: 0 }, i);
  const out = {
    slotW: px(1) - px(0),
    gridW: px(239) - px(0),
    filled: ax.barAt.filter(Boolean).length,
    firstFilled: ax.barAt.findIndex(Boolean),
    lastFilled: ax.barAt.reduce((acc, b, i) => (b ? i : acc), -1),
    padLen: intradayPad(ax, bars, b => b.close).length,
  };
  chart.dispose();
  return out;
};
const out = {};
counts.forEach(n => { out[n] = measure(all.slice(0, n)); });
process.stdout.write(JSON.stringify(out));
"""
        return json.loads(run_node_json(script, self._payload(240),
                                        json.dumps(self.counts), self.empty,
                                        str(ECHARTS_JS)))

    @property
    def empty(self):
        return re.search(r"const INTRADAY_EMPTY_CANDLE = ('[^']*');", self.src).group(1)

    def test_slot_width_identical_from_first_minute_to_close(self):
        """槽宽从第 1 根到第 240 根必须一模一样 —— 这就是"横轴不拉伸"的可执行定义。"""
        out = self._measure()
        base = out["1"]["slotW"]
        self.assertAlmostEqual(base, out["1"]["gridW"] / 239, delta=0.2,
                               msg="单根 bar 时槽宽就该是网格宽/239")
        for n in self.counts:
            m = out[str(n)]
            self.assertAlmostEqual(m["slotW"], base, delta=0.05,
                                   msg=f"{n} 根时槽宽 {m['slotW']:.2f} != 开盘时的 {base:.2f}")
            self.assertEqual(m["padLen"], 240, f"{n} 根时 series 数组仍要是 240 长")
            self.assertEqual(m["filled"], n, f"{n} 根时落槽数不对")
            self.assertEqual(m["firstFilled"], 0, "第一根永远贴左缘")
            self.assertEqual(m["lastFilled"], n - 1, f"{n} 根时末根槽位不对")

    def test_afternoon_first_bar_lands_after_the_lunch_gap(self):
        """午休: 120 根是半场 (末槽 119), 第 121 根 (13:01) 必须是槽 120。"""
        out = self._measure()
        self.assertEqual(out["120"]["lastFilled"], 119, "半场末槽 = 119")
        self.assertEqual(out["121"]["lastFilled"], 120, "13:01 落在槽 120, 不因午休前移")
        # 午休不是"数据缺失", 是轴上的固定跳变: 11:30 之后直接接 13:01
        self.assertEqual(self.slots[119], "11:30")
        self.assertEqual(self.slots[120], "13:01")

    def test_half_day_and_full_day_share_the_same_axis(self):
        """午休半场 vs 收盘全天: 轴长与槽宽都相同, 只是填充长度不同。"""
        out = self._measure()
        self.assertEqual(out["120"]["padLen"], out["240"]["padLen"], "半场/全天轴长必须一致")
        self.assertAlmostEqual(out["120"]["slotW"], out["240"]["slotW"], delta=0.05)

    def test_missing_minute_does_not_shift_later_bars(self):
        """中间缺一分钟 (那分钟无成交/停牌, 源没给) 时, 后面的 bar 不许前移。

        这是"按时间落槽"与"按下标落槽"的分水岭: 按下标铺的话, 缺根之后整段左移一格,
        K线/均价/成交量彼此看还对得上, 但时间已经全错了 (tooltip 报的也是错的时间)。
        """
        payload = self._payload(4)          # 09:31 09:32 09:33 09:34
        del payload[2]                      # 源少给 09:33
        out = json.loads(run_node(self.fns + """
const bars = JSON.parse(process.argv[1]);
const ax = intradaySessionSlots('002472.SZ', bars);
const closes = intradayPad(ax, bars, b => b.close);
process.stdout.write(JSON.stringify({
  at2: ax.barAt[2], at3: (ax.barAt[3] || {}).time, at4: ax.barAt[4],
  v2: closes[2], v3: closes[3], filled: ax.barAt.filter(Boolean).length,
}));
""", json.dumps(payload)))
        self.assertIsNone(out["at2"], "缺的那分钟要留空, 不能由后一根补位")
        self.assertEqual(out["at3"], "09:34", "09:34 仍在槽 3")
        self.assertIsNone(out["at4"])
        self.assertIsNone(out["v2"], "空槽的 series 值必须是空 (null), 不是 0")
        self.assertEqual(out["filled"], 3, "真实根数 = 3 (缺一根)")


class FixedWindowGeometryTest(unittest.TestCase):
    """真实 echarts (SSR): 固定窗口下的槽宽 = 网格宽 / 240, 与已出现多少分钟无关。

    这是用户直接看到的那条现象: 旧轴只装「已出现分钟」, 09:31 一根 bar 就占满整幅图
    (600px 网格 → 槽宽 ~600px), 之后每过一分钟变窄一点。下面同时跑新旧两种轴:
    新轴 3 根 bar 的槽宽必须 ≈ 网格宽/239, 且与 240 根时**一模一样** —— 这就是
    「槽宽全天不变」的可执行定义。
    """

    @classmethod
    def setUpClass(cls):
        require_node()
        if not ECHARTS_JS.exists():
            raise unittest.SkipTest(f"缺少 vendored echarts: {ECHARTS_JS}")
        src = _src()
        cls.fns = "\n".join(_extract_fn(src, n)
                            for n in ("intradaySessionSlots", "intradayPad"))
        # 空槽标记直接取自源码: 这里跑的就是真页面的那条路径 (K 线用 '-' 而非 null)
        cls.empty = re.search(r"const INTRADAY_EMPTY_CANDLE = ('[^']*');", src).group(1)

    def _run(self, bars, minutes):
        script = self.fns + """
const echarts = require(process.argv[4]);
const bars = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'))
                   .slice(0, Number(process.argv[2]));
const minutes = Number(process.argv[2]);
const W = 600, H = 400;
// measure 按真页面分时的 option 形状建图: K线(candlestick) + 均价(line) + 成交量(bar) +
// MACD柱(bar, 空槽为 null) + 三轴 dataZoom —— 三种 series 类型与 dataZoom 都要过一遍,
// 空槽取值对不上 (candlestick 吃 null) 会在这里直接抛出来。
const measure = (slots, avg, candle, vol, hist) => {
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: W, height: H });
  chart.setOption({
    animation: false,
    grid: { left: 0, right: 0, top: 0, bottom: 0 },
    xAxis: { type: 'category', data: slots },
    yAxis: { type: 'value', scale: true },
    dataZoom: [{ type: 'inside', xAxisIndex: [0], start: 0, end: 100 }],
    series: [
      { name: 'K线', type: 'candlestick', data: candle,
        itemStyle: { color: '#ef232a', color0: '#14b143',
                     borderColor: '#ef232a', borderColor0: '#14b143' } },
      { name: '均价', type: 'line', data: avg, symbol: 'none',
        lineStyle: { color: '#f5a623' } },
      { name: '成交量', type: 'bar', data: vol },
      { name: 'MACD柱', type: 'bar', data: hist },
    ],
  }, { notMerge: true, silent: true });
  const px = (i) => chart.convertToPixel({ xAxisIndex: 0 }, i);
  const svg = chart.renderToSVGString();
  const out = {
    slotW: px(1) - px(0),
    gridW: px(239) - px(0),
    firstX: px(0),
    // 真有内容画出来: 涨幅色 + 均价线色都出现在 SVG 里 (空槽没把 series 吃掉)
    svgHasCandle: svg.includes('#ef232a'),
    svgHasAvg: svg.includes('#f5a623'),
  };
  chart.dispose();
  return out;
};
// 轴 = 固定 240 槽 (本仓做法) vs 旧做法 (轴只装已出现分钟)
const EMPTY = process.argv[3];
const ax = intradaySessionSlots('002472.SZ', bars);
const fill = (fn, empty) => intradayPad(ax, bars, fn, empty);
const fixed = measure(ax.slots, fill(b => b.close), fill(b => [b.open, b.close, b.low, b.high], EMPTY),
                      fill(b => b.volume), fill(b => b.macd_hist));
const legacy = measure(bars.map(b => b.time), bars.map(b => b.close),
                       bars.map(b => [b.open, b.close, b.low, b.high]),
                       bars.map(b => b.volume), bars.map(b => b.macd_hist));
process.stdout.write(JSON.stringify({ fixed, legacy }));
"""
        # payload 走临时文件: 240 根 bar 的 JSON 直接当命令行参数会撞 Windows 32K 上限
        return json.loads(run_node_json(script, bars, str(minutes), self.empty,
                                        str(ECHARTS_JS), timeout=120))

    def _bar(self, i):
        return {"time": f"09:{31 + i:02d}", "open": 10.0 + i, "close": 10.5 + i,
                "low": 9.9 + i, "high": 10.6 + i}

    def test_slot_width_is_session_fixed_not_elapsed_dependent(self):
        early = self._run([self._bar(i) for i in range(3)], 3)
        # 3 根 bar: 槽宽 ≈ 网格宽/239 (~2.5px @600px), 而不是铺满整幅图
        self.assertAlmostEqual(early["fixed"]["slotW"],
                               early["fixed"]["gridW"] / 239, delta=0.2)
        self.assertLess(early["fixed"]["slotW"], 5,
                        "早盘单根 bar 必须还是很窄 —— 变宽就是回到了旧观感")
        # 第一根贴左缘: category 轴的点落在槽中心, 所以是半个槽宽 (~1.25px), 不是网格中心
        self.assertLess(early["fixed"]["firstX"], early["fixed"]["slotW"],
                        "第一根 bar 要贴左缘 (东财口径), 不是居中")
        self.assertTrue(early["fixed"]["svgHasCandle"], "空槽不该把 K 线画没了")
        self.assertTrue(early["fixed"]["svgHasAvg"], "均价线也要画出来")
        # 对照组: 旧轴 3 个槽铺满网格 → 槽宽 ~200px, 正是「早盘柱子很大」的成因
        self.assertGreater(early["legacy"]["slotW"], 100,
                           "旧做法 (轴只装已出现分钟) 会铺满整幅图, 这是被修掉的现象")

    def test_slot_width_stable_from_open_to_close(self):
        """同一幅图先画 3 根、再画 240 根, 槽宽必须一致 (全天不随分钟数变化)。"""
        bars = [{"time": t, "open": 10.0, "close": 10.5, "low": 9.9, "high": 10.6}
                for t in ("09:31", "09:32", "09:33")]
        three = self._run(bars, 3)
        full = self._run([{"time": t, "open": 10.0, "close": 10.5, "low": 9.9,
                           "high": 10.6} for t in _all_slot_times()], 240)
        self.assertAlmostEqual(three["fixed"]["slotW"], full["fixed"]["slotW"], delta=0.1,
                               msg="槽宽随已出现分钟数变化 → 又回到「柱子越来越小」")


def _all_slot_times():
    """末相位全天 240 个槽位时刻 (09:31-11:30 + 13:01-15:00)。"""
    out = []
    for start, end in (((9, 31), (11, 30)), ((13, 1), (15, 0))):
        m = start[0] * 60 + start[1]
        stop = end[0] * 60 + end[1]
        while m <= stop:
            out.append(f"{m // 60:02d}:{m % 60:02d}")
            m += 1
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
