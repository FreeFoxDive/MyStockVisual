"""主页 K 线补页时保留视窗，重选当前周期时恢复首屏日期和柱宽。"""

import json
import sys
import unittest
from pathlib import Path

VISUAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VISUAL / "test"))
from js_test_util import run_node  # noqa: E402


# 真实 loadOlderKlines → renderFetchedKline → updateChart → ECharts 5.5。
# 屏蔽与平移无关的侧栏、标签及网络请求，分页游标仍由主页生成。
PROBE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = process.argv[1];
const realEcharts = require(path.join(root, 'vendor', 'echarts-5.5.0.min.js'));
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
for (const name of [
  'loadOlderKlines', 'unpackChartRows', 'renderFetchedKline', 'klineRenderKey',
  'readOlderKlinePage', 'prependOlderKlinePage', 'rebuildHistoryIndicators',
  'chartWindowKey', 'klineZoomWindow', 'updateChart', 'applyFutureSlots', 'buildFutureZoneMarkArea',
  'buildOhlcPoint', 'isHaltBar', 'buildVolPoint', 'calcGridLayout', 'fmtVolume',
  'initChart', 'chartModel', 'switchPeriod', 'resetKlineZoom', 'klineDataZoomOptions', 'scheduleChartZoomSync',
]) {
  const source = html.match(new RegExp('^(?:async )?function ' + name + '\\([\\s\\S]*?^}', 'm'));
  if (!source) throw new Error('Missing homepage function ' + name);
  vm.runInThisContext(source[0]);
}
const noop = () => {};
Object.assign(globalThis, {
  STATE: {}, window: { Drawings: { FUTURE_BARS: 8 }, addEventListener: noop }, Drawings: { FUTURE_BARS: 8 },
  document: { createElement: () => null, getElementById: id => ({ checked: id === 'chk-volume' }) },
  C: () => ({ up: '#f00', down: '#090' }), panelGridLeft: () => 60, panelGridRight: () => 20,
  getIndicatorPanels: () => [], effectiveMaBoll: () => ({ ma: [] }), maLineColors: () => [],
  isChipPeriod: () => false, drawNeedsFuture: () => STATE.draw.autoLines,
  isPhoneUi: () => false, mainRowOffsets: () => ({ title: 0 }), MAIN_TITLE_FONT: '',
  applySidePanels: noop, chipBandFor: noop, fmtPrice3: noop, buildTradeMarkers: noop,
  buildGapMarkArea: noop, buildKlineMarkLines: noop, drawPanLocked: noop,
  markFirstChart: noop, cacheGridRects: noop, refreshPriceTagLayout: noop,
  spanPx: noop, chartTitleText: noop, loadDrawings: noop, redrawDrawings: noop, fetchPremium: noop,
  initDrawEvents: noop, bindChartWheelPan: noop, requestAnimationFrame: setImmediate,
});
const nextFrame = () => new Promise(resolve => setImmediate(resolve));
let onKeyDown;
document.addEventListener = (type, listener) => { if (type === 'keydown') onKeyDown = listener; };
vm.runInThisContext(html.match(/^  const PERIOD_KEYS = [\s\S]*?^  \}\);/m)[0]);
const ALL_PERIODS = Object.values(PERIOD_KEYS);

function viewport() {
  const model = STATE.chart.getModel();
  const axis = model.getComponent('xAxis', 0).axis;
  const dates = axis.scale.getOrdinalMeta().categories;
  const [left, right] = axis.scale.getExtent();
  const bars = model.getSeriesByIndex(0).getData();
  return {
    first: dates[left], last: dates[Math.min(right, dates.length - 1)],
    future: Math.max(0, right - dates.length + 1),
    slots: right - left + 1, candles: bars.count(), width: bars.getLayout('candleWidth'),
  };
}

function dragSlider(chart, heightFraction, destination, resize = false) {
  const zr = chart.getZr();
  zr.storage.getDisplayList(true);
  const zoom = chart.getModel().getComponent('dataZoom', 1);
  const view = chart.getViewOfComponentModel(zoom);
  const { filler, handles } = view._displayables;
  const element = resize ? handles[0] : filler;
  const box = element.getBoundingRect();
  const [x, y] = element.transformCoordToGlobal(box.x + box.width / 2, box.y + box.height * heightFraction);
  const selected = filler.shape;
  const dx = destination === 'left' ? -selected.x - 2
    : destination === 'right' ? view._size[0] - selected.x - selected.width + 2 : destination;
  // 通过 ZRender 实际命中测试，选区背景、移动区和端点均由生产配置决定。
  for (const [type, pointerX] of [['mousedown', x], ['mousemove', x + dx / 2], ['mousemove', x + dx], ['mouseup', x + dx]]) {
    zr.handler.dispatch(type, { zrX: pointerX, zrY: y, which: 1, preventDefault() {}, stopPropagation() {} });
  }
}

function dragChart(chart, slider) {
  if (slider) return dragSlider(chart, 0.5, -40);
  const event = { which: 1, preventDefault() {}, stopPropagation() {} };
  const mouse = (type, x) => chart.getZr().trigger(type, { offsetX: x, offsetY: 200, event });
  mouse('mousedown', 150);
  mouse('mousemove', 180);
  mouse('mousemove', 190);
  mouse('mouseup', 190);
}

async function probe(period, position) {
  const chart = realEcharts.init(null, null, { renderer: 'svg', ssr: true, width: 1100, height: 600 });
  try {
    const minutes = period.endsWith('m');
    const step = minutes ? parseInt(period, 10) * 60000 : (period === '1w' ? 7 : 1) * 86400000;
    const date = i => new Date(period === '1M' ? Date.UTC(1940, i, 1)
      : Date.UTC(2000, 0, 1) + i * step).toISOString().slice(0, minutes ? 16 : 10);
    const all = Array.from({ length: 900 }, (_, i) => ({
      date: date(i),
      open: 10, close: 11, low: 9, high: 12, volume: 80,
    }));
    const symbol = '300413.SZ', meta = { adjust: 'forward' };
    const initialCount = position === 'resetNearLeft' ? 245 : 300;
    const data = { symbol, period, meta, klines: all.slice(-initialCount), request_count: 900, has_more: true };
    Object.assign(STATE, { chart, symbol, period, adjust: 'forward', klineData: data,
      draw: { autoLines: position === 'future' || position === 'resetFuture' }, _periodSwitching: true, _initResized: true,
      _drawnKlineWindow: null, _fetchedKlineRenderKey: null, _klineZoomRevision: 0 });
    let deliver, requests = 0;
    globalThis.fetch = async url => {
      requests++;
      const q = new URL(url, 'http://localhost').searchParams;
      const end = all.findIndex(bar => bar.date === q.get('before'));
      const page = all.slice(Math.max(0, end - Number(q.get('limit'))), end);
      const fields = Object.keys(page[0]);
      await new Promise(resolve => { deliver = resolve; });
      return { ok: true, json: async () => ({ symbol, period, meta, fields,
        has_more: end > page.length, rows: page.map(b => fields.map(f => b[f])) }) };
    };
    if (position.startsWith('reset')) {
      // 让主页的真实 dblclick / dataZoom 监听器绑定到这个 SSR 图表。
      globalThis.echarts = { init: () => chart };
      initChart();
    }
    renderFetchedKline(data);
    if (position.startsWith('reset')) {
      const initial = viewport();
      const clickHandler = html.match(new RegExp('<button id="btn-' + period + '"[^>]*?onclick="([^"]+)"'))[1];
      const clickPeriod = () => vm.runInThisContext(clickHandler);
      const key = Object.keys(PERIOD_KEYS).find(key => PERIOD_KEYS[key] === period);
      const pressKey = tagName => onKeyDown({ key, target: { tagName }, preventDefault: noop });
      const zoomTo = (left, right) => chart.dispatchAction({ type: 'dataZoom', startValue: left, endValue: right });
      const overview = () => chart.getZr().trigger('dblclick', { offsetX: 200, offsetY: 150 });
      if (position === 'resetGesture') {
        const gestures = [];
        const settle = () => new Promise(resolve => setTimeout(resolve, 180));
        for (const slider of [false, true]) {
          zoomTo(80, 145);
          dragChart(chart, slider);  // 第二次 mousemove 可能还在 ECharts 节流队列里。
          pressKey('BODY');         // 松手后立即按快捷键，旧动作不能把已恢复的图拖回去。
          const immediate = viewport();
          await settle();
          const settled = viewport();
          dragChart(chart, slider);
          await settle();
          gestures.push({ immediate, settled, nextDrag: viewport() });
        }
        return { initial, gestures, requests };
      }
      if (position === 'resetNearLeft') {
        // 上一次缩放已排入 rAF，旧拖动还有延迟事件。复位后仍在左侧 8% 的分页阈值内。
        zoomTo(25, 65);
        dragChart(chart, false);
        clickPeriod();
        await new Promise(resolve => setTimeout(resolve, 180));
        return { initial, restored: viewport(), requests };
      }
      zoomTo(0, 39);
      await nextFrame();  // 左缘平移触发普通补页，返回前即点当前周期。
      const firstPage = STATE._historyPending.done;
      overview();         // 全景请求排队等普通页，复位后也不能再启动第二个请求。
      clickPeriod();
      const duringPage = viewport();
      deliver();
      await firstPage;
      await nextFrame();
      const afterPage = viewport();
      if (requests !== 1) throw new Error('恢复首屏后仍发起了排队的全景补页请求');
      zoomTo(120, 170);
      overview();
      const panoramaPage = STATE._historyPending.done;
      pressKey('BODY');
      deliver();
      await panoramaPage;
      await nextFrame();
      const afterPendingOverview = viewport();
      overview();         // 此时 900 根已全部加载，双击应正常展开全景。
      await nextFrame();
      const panorama = viewport();
      pressKey('INPUT');  // 输入框中的数字保留给输入。
      const afterInput = viewport();
      clickPeriod();
      await nextFrame();
      return { initial, duringPage, afterPage, afterPendingOverview, panorama, afterInput,
        restored: viewport(), loaded: data.klines.length, requests };
    }
    if (position === 'roundTrip') {
      const settle = () => new Promise(resolve => setTimeout(resolve,
        chart.getModel().getComponent('dataZoom', 1).get('throttle') + 30));
      const initial = viewport();
      dragSlider(chart, 0.5, 50, true);
      await settle();
      const resized = viewport(), trips = [];
      for (const fraction of [0.2, 0.8]) {
        dragSlider(chart, fraction, 'left');
        await settle();
        const atLeft = viewport();
        const pending = loadOlderKlines();
        deliver();
        await pending;
        const afterPage = viewport();
        dragSlider(chart, fraction, 'right');
        await settle();
        trips.push({ atLeft, afterPage, atRight: viewport() });
      }
      return { initial, resized, trips };
    }
    const pages = [];
    for (const pageSize of [300, 600]) {
      // 在左缘请求历史，响应在途时继续平移。两页分别使用主图和滑块。
      const span = period === '1m' ? 159 : 49;
      const pan = right => chart.dispatchAction({ type: 'dataZoom', dataZoomIndex: pages.length,
        startValue: right - span, endValue: right });
      pan(span + 15);
      const pending = loadOlderKlines();
      const right = { left: span + 5, nearTail: pageSize - 2, tail: pageSize - 1,
        future: pageSize + 2 }[position];
      const start = viewport();
      if (position === 'drag') dragChart(chart, pages.length > 0);
      else pan(right);
      const before = viewport();
      deliver();
      await pending;
      const after = viewport();
      // ECharts 的 inside dataZoom 会延迟派发最后一次 mousemove。
      // 等超过默认 100ms 节流时间，补页后的图也必须保持同样柱宽。
      if (position === 'drag') await new Promise(resolve => setTimeout(resolve, 180));
      pages.push({ loaded: chart.getModel().getComponent('xAxis').axis.scale.getOrdinalMeta().categories.length,
        animation: chart.getModel().get('animation'), start, before, after, settled: viewport() });
    }
    return pages;
  } finally {
    chart.dispose();
  }
}
(async () => {
  const results = {};
  for (const p of ['1d', '1w', '1M', '1m', '5m', '15m', '30m', '60m']) results[p] = await probe(p, 'left');
  for (const pos of ['nearTail', 'tail', 'future', 'drag']) results[pos] = await probe('1d', pos);
  const roundTrips = {};
  for (const period of ['1d', '1w', '1M', '5m']) roundTrips[period] = await probe(period, 'roundTrip');
  const resets = {};
  for (const period of ALL_PERIODS.filter(p => p !== 'intraday')) resets[period] = await probe(period, 'reset');
  resets.future = await probe('1d', 'resetFuture');
  const nearLeft = await probe('1m', 'resetNearLeft');
  const gestureReset = await probe('1d', 'resetGesture');
  console.log(JSON.stringify({ pages: results, roundTrips, resets, nearLeft, gestureReset }));
})().catch(error => { console.error(error.stack); process.exit(1); });
"""


class KlineHistoryZoomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = json.loads(run_node(PROBE, str(VISUAL / "static")))

    def test_older_pages_preserve_window_and_candle_width(self):
        for case, pages in self.results["pages"].items():
            for count, page in zip((600, 900), pages):
                with self.subTest(case=case, total=count):
                    self.assertEqual(page["loaded"], count)
                    self.assertFalse(page["animation"], "补页重绘时不应重复播放蜡烛入场动画")
                    before, after = page["before"], page["after"]
                    self.assertEqual(before["future"], 3 if case == "future" else 0)
                    self.assertGreater(before["width"], 0)
                    self.assertEqual(after, before)
                    if case == "drag":
                        self.assertNotEqual(before["first"], page["start"]["first"])
                        self.assertEqual(page["settled"], before)

    def test_drag_either_half_of_slider_to_history_and_back(self):
        for period, case in self.results["roundTrips"].items():
            with self.subTest(period=period):
                resized = case["resized"]
                self.assertLess(resized["slots"], case["initial"]["slots"], "拖动端点应当能主动缩放")
                self.assertGreater(resized["width"], case["initial"]["width"])
                last_left = resized["first"]
                for trip in case["trips"]:
                    left = trip["atLeft"]
                    self.assertLess(left["first"], last_left, "每次左拉都应能进入更早的历史")
                    self.assertEqual(left["slots"], resized["slots"])
                    self.assertEqual(left["width"], resized["width"])
                    self.assertEqual(trip["afterPage"], left)
                    self.assertEqual(trip["atRight"], resized)
                    last_left = left["first"]

    def test_reselect_period_restores_initial_window_without_fetching(self):
        for period, result in self.results["resets"].items():
            with self.subTest(period=period):
                initial = result["initial"]
                self.assertEqual(initial["candles"], 240 if period == "1m" else 80)
                self.assertEqual(initial["future"], 8 if period == "future" else 0)
                for checkpoint in ["duringPage", "afterPage", "afterPendingOverview", "restored"]:
                    self.assertEqual(result[checkpoint], initial, checkpoint)
                self.assertEqual(result["panorama"]["candles"], 900)
                self.assertEqual(result["afterInput"], result["panorama"])
                self.assertEqual(result["loaded"], 900)
                self.assertEqual(result["requests"], 2, "只应有普通补页和双击全景发起的两次请求")

    def test_reset_inside_left_threshold_does_not_trigger_pagination(self):
        result = self.results["nearLeft"]
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["restored"], result["initial"])

    def test_reset_supersedes_pending_drag_and_allows_next_drag(self):
        result = self.results["gestureReset"]
        initial = result["initial"]
        self.assertEqual(result["requests"], 0)
        for widget, drag in zip(("主图", "滑块"), result["gestures"]):
            with self.subTest(widget=widget):
                self.assertEqual(drag["immediate"], initial)
                self.assertEqual(drag["settled"], initial)
                self.assertLess(drag["nextDrag"]["last"], initial["last"])
                self.assertEqual(drag["nextDrag"]["width"], initial["width"])


if __name__ == "__main__":
    unittest.main()
