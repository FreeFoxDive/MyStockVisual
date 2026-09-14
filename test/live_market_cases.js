// Execute the actual browser module with deferred HTTP responses and a fake clock.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let now = 100000, requests = [], streams = [], paints = [];
const context = {
  Date: class extends Date { static now() { return now; } },
  setInterval: () => 1, clearInterval() {}, setTimeout: () => 2, clearTimeout() {},
  AbortController,
  document: { hidden: false, addEventListener() {}, removeEventListener() {} },
  fetch(url, opts) { return new Promise(resolve => requests.push({ url, opts, resolve })); },
  EventSource: class {
    constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
    addEventListener(k, fn) { this.listeners[k] = fn; }
    close() { this.closed = true; }
    emit(q, kind = 'quote') {
      (kind === 'quote' ? this.onmessage : this.listeners[kind])({ data: JSON.stringify(q) });
    }
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const { LiveMarket, price } = context.VisualLive;
const q = (price, rev, timestamp = rev) => ({ symbol: 'A', last_price: price, _revision: rev, timestamp });
const finish = async (r, data) => {
  r.resolve({ ok: true, json: async () => data });
  await new Promise(resolve => setImmediate(resolve));
};
(async () => {
  for (const [v, expected] of [[1.230, '1.23'], [1.23456, '1.235'], [10, '10'], [0, '0'], [null, '—'], [NaN, '—'], [Infinity, '—']]) {
    assert.equal(price(v), expected);
  }
  const live = new LiveMarket({ onQuote: (s, v) => paints.push(v.last_price) });
  live.setSymbols(['A'], true);
  assert.equal(requests.length, 2, 'bootstrap quote and depth');
  const es = streams[0], bootstrap = requests[0];
  assert.equal(live.healthy('quote', 'A'), false, 'connection alone is not recovery');
  es.emit({ A: q(10, 100) });
  assert.equal(live.healthy('quote', 'A'), true);
  assert.equal(live.healthy('depth', 'A'), false, 'depth health is independent');
  await finish(bootstrap, { A: q(9, 99) });
  assert.equal(live.get('A').last_price, 10, 'in-flight bootstrap cannot override SSE');
  es.emit({ A: q(8, 101, 90) });
  assert.equal(live.get('A').last_price, 10, 'older exchange stamp rejected');
  es.emit({ A: q(10, 100) });
  assert.equal(paints.length, 1, 'duplicate frame does not recalculate');
  es.onerror();
  assert.equal(live.healthy('quote', 'A'), false);
  const fallback = requests.at(-1);
  await finish(fallback, { A: q(11, 110) });
  assert.equal(live.get('A').last_price, 11, 'disconnect fallback updates immediately');
  now += 6000; live.tick();
  const recovered = streams.at(-1), inFlight = requests.at(-1);
  recovered.emit({ A: q(12, 120) });
  await finish(inFlight, { A: q(99, 999) });
  assert.equal(live.get('A').last_price, 12, 'pre-recovery request invalid even with newer receipt');
  es.emit({ A: q(99, 999) });
  assert.equal(live.get('A').last_price, 12, 'closed connection callback ignored');
  recovered.emit({ A: null });
  const count = requests.length;
  live.tick();
  assert.equal(requests.length, count, 'healthy channel stops polling');
  now += 16000; live.tick();
  assert.ok(requests.length > count, 'silent stream falls back and reconnects');
  const oldRequest = requests.at(-1);
  live.setSymbols(['B']); live.setSymbols(['A']);
  await finish(oldRequest, { A: q(77, 777) });
  assert.notEqual(live.get('A')?.last_price, 77, 'A to B to A rejects old request generation');
  const current = streams.at(-1);
  current.emit({ A: q(13, 130) });
  assert.equal(live.get('A').last_price, 13);
  live.accept('quote', 'A', q(9, 90));
  assert.equal(live.get('A').last_price, 13, 'slow embedded kline quote cannot roll back live data');
  live.dispose();
  current.emit({ A: q(88, 888) });
  assert.equal(live.get('A').last_price, 13, 'disposed stream cannot repaint');
  const resources = new LiveMarket({ onBars: () => paints.push('bars') });
  resources.setSymbols(['A'], true, { period: '1d', count: 1006, adjust: 'none' });
  const resourceStream = streams.at(-1);
  const barRequest = requests.at(-1);
  resourceStream.emit({ A: q(14, 140) });
  assert.equal(resources.healthy('bars', 'A'), false);
  resourceStream.emit({ A: { symbol: 'A', _revision: 140, bid_prices: [14], ask_prices: [15] } }, 'depth');
  assert.equal(resources.healthy('depth', 'A'), true);
  assert.equal(resources.healthy('bars', 'A'), false, 'depth cannot suppress indicator fallback');
  const bars = { symbol: 'A', period: '1d', _revision: 140, meta: { adjust: 'none' }, bars: [{ date: '2026-09-14', close: 14 }] };
  resourceStream.emit({ A: bars }, 'bars');
  await finish(barRequest, { ...bars, _revision: 150, bars: [{ date: '2026-09-14', close: 99 }] });
  assert.equal(resources.get('A', 'bars').bars[0].close, 14, 'late indicator HTTP cannot corrupt recovered SSE');
  resourceStream.emit({ A: { ...bars, _revision: 160, meta: { adjust: 'forward' } } }, 'bars');
  assert.equal(resources.get('A', 'bars')._revision, 140, 'adjustment scope is checked');
  const pendingCount = requests.length;
  now += 3000; resources.tick();
  assert.equal(requests.length, pendingCount, 'all recovered resources stop polling');
  context.document.hidden = true; resources.visibility();
  resourceStream.emit({ A: q(99, 999) });
  assert.equal(resources.get('A').last_price, 14, 'hidden page callbacks are invalidated');
  context.document.hidden = false; resources.visibility();
  assert.ok(streams.at(-1) !== resourceStream, 'visible page resubscribes');
  resources.dispose();
  console.log('live-market boundary cases passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
