"""主页横向滚轮平移保留柱宽，普通滚轮和捏合仍由 ECharts 处理。"""

import re
import unittest
from pathlib import Path

from js_test_util import run_node


STATIC = Path(__file__).resolve().parents[1] / "static"


class ChartWheelPanTest(unittest.TestCase):
    def test_wheel_gestures_on_category_axis(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        handler = re.search(r"^function bindChartWheelPan\(.*?^}", html, re.M | re.S)[0]
        run_node(handler + r"""
const assert = require('node:assert/strict');
const echarts = require(process.argv[1]);
const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 900, height: 400 });
const dates = Array.from({ length: 300 }, (_, i) => String(i));
const STATE = { period: '1d', _gridRects: [{ left: 50, right: 850, top: 20, bottom: 350 }] };
let wheel, locked = false, pages = 0;
const drawPanLocked = () => locked;
const loadOlderKlines = () => { pages++; };
const dom = {
  addEventListener(type, fn, options) {
    assert.equal(type, 'wheel');
    assert.deepEqual(options, { capture: true, passive: false });
    wheel = fn;
  },
  getBoundingClientRect: () => ({ left: 0, top: 40 }),
};
let clock = 0;
function scroll(change, elapsed = 16) {
  clock += elapsed;
  let consumed = 0;
  wheel({ timeStamp: clock, clientX: 100, clientY: 130, deltaX: 0, deltaY: 0, deltaMode: 0,
    preventDefault() { consumed++; }, stopPropagation() { consumed++; }, ...change });
  return consumed === 2;
}
const extent = () => chart.getModel().getComponent('xAxis').axis.scale.getExtent();
const width = () => chart.getModel().getSeriesByIndex(0).getData().getLayout('candleWidth');
// 放行的 DOM wheel 会继续走 ZRender 的 mousewheel，被 inside dataZoom 用来缩放。
function trackpad(deltaX, deltaY, elapsed = 16, modifiers = {}) {
  const captured = scroll({ deltaX, deltaY, ...modifiers }, elapsed);
  if (!captured) chart.getZr().trigger('mousewheel', {
    offsetX: 100, offsetY: 90, wheelDelta: -(deltaY || deltaX) / 40,
    event: { preventDefault() {}, stopPropagation() {}, ...modifiers },
  });
  return captured;
}
try {
  chart.setOption({
    animation: false, grid: {}, xAxis: { type: 'category', data: dates }, yAxis: {},
    dataZoom: [{ type: 'inside', xAxisIndex: [0], startValue: 100, endValue: 179, throttle: 0 }],
    series: [{ type: 'candlestick', data: dates.map(() => [10, 11, 9, 12]) }],
  });
  bindChartWheelPan(dom, chart);
  assert.ok(scroll({ deltaX: -80, deltaY: 2 }));
  assert.deepEqual(extent(), [92, 171]);
  assert.ok(scroll({ shiftKey: true, deltaY: 5, deltaMode: 1 }));
  assert.deepEqual(extent(), [100, 179]);
  for (let i = 0; i < 10; i++) scroll({ deltaX: -1 });
  assert.deepEqual(extent(), [99, 178]);
  for (const change of [{ deltaY: 50 }, { deltaX: 50, ctrlKey: true },
      { deltaX: 50, clientX: 20 }]) assert.equal(scroll(change, 500), false);
  locked = true;
  assert.ok(scroll({ deltaX: 80 }));
  assert.deepEqual(extent(), [99, 178]);
  locked = false;
  scroll({ deltaX: -5000 });
  assert.deepEqual(extent(), [0, 79]);
  scroll({ deltaX: -80 });
  assert.equal(pages, 1);
  STATE.period = 'intraday';
  scroll({ deltaX: -80 });
  assert.equal(pages, 1);
  scroll({ deltaX: 5000 });
  assert.deepEqual(extent(), [220, 299]);

  STATE.period = '1d';
  const initialWidth = width();
  // 连续触控板平移中，方向混杂或只剩下 1px 纵向惯性也不能触发缩放。
  for (const [dx, dy, ms] of [[-5000, -1, 500], [-1, -2, 16], [0, -1, 16],
      [5000, 2, 500], [1, 2, 16], [0, 1, 16]]) {
    trackpad(dx, dy, ms);
    const [start, end] = extent();
    assert.equal(end - start + 1, 80, `平移事件 (${dx}, ${dy}) 改变了可见柱数`);
    assert.equal(width(), initialWidth);
  }
  assert.deepEqual(extent(), [220, 299]);
  assert.equal(trackpad(-5000, 1, 500), true);
  assert.deepEqual(extent(), [0, 79]);
  assert.equal(trackpad(5000, 1, 500), true);
  assert.deepEqual(extent(), [220, 299]);
  assert.equal(width(), initialWidth);

  // 手指落下的首帧可能只有 1px 纵向位移，随后整段横滑仍按原柱宽向左移动。
  trackpad(0, -1, 500);
  assert.deepEqual(extent(), [220, 299]);
  trackpad(-120, -1);
  const [left, right] = extent();
  assert.ok(left < 220);
  assert.equal(right - left + 1, 80);
  trackpad(0, -1);
  assert.deepEqual(extent(), [left, right]);
  assert.equal(width(), initialWidth);

  // 慢速纯纵向滚轮以多个 1px 事件起步，累计足够位移后照常缩放。
  trackpad(0, -1, 500);
  for (let i = 0; i < 12 && width() === initialWidth; i++) trackpad(0, -1);
  assert.ok(width() > initialWidth);
  chart.dispatchAction({ type: 'dataZoom', startValue: 220, endValue: 299 });

  // 独立纵向滚轮仍能缩放，Ctrl 捏合即使在平移手势中出现也立即交给 ECharts。
  assert.equal(trackpad(0, -30, 500), false);
  const zoomedWidth = width();
  assert.ok(zoomedWidth > initialWidth);
  assert.equal(trackpad(-100, 1, 500), true);
  assert.equal(width(), zoomedWidth);
  assert.equal(trackpad(1, -30, 16, { ctrlKey: true }), false);
  assert.ok(width() > zoomedWidth);
} finally {
  chart.dispose();
}
""", str(STATIC / "vendor" / "echarts-5.5.0.min.js"))


if __name__ == "__main__":
    unittest.main()
