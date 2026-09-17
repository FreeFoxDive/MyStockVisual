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
const qs = (sym, price, rev, timestamp = rev) => ({ symbol: sym, last_price: price, _revision: rev, timestamp });
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
  // 超出 SSE 覆盖范围(前 50)的标的必须始终保留 HTTP 轮询回退。
  const wide = new LiveMarket({ onQuote: () => {} });
  const wideSyms = Array.from({ length: 60 }, (_, i) => '6000' + String(i).padStart(2, '0') + '.SH');
  wide.setSymbols(wideSyms);
  const wideStream = streams.at(-1);
  for (const r of requests.slice()) await finish(r, {}); // 清掉引导轮询, 解除 pending 节流
  const covered = {};
  for (const s of wideSyms.slice(0, 50)) covered[s] = qs(s, 10, 100);
  wideStream.emit(covered);
  const beforeWide = requests.length;
  now += 3000;
  wide.tick();
  const wideUrls = requests.slice(beforeWide).map(r => r.url);
  assert.ok(wideUrls.some(u => u.includes('600050.SH')), 'symbols beyond the SSE-covered 50 still poll');
  assert.ok(!wideUrls.some(u => u.includes('600000.SH')), 'healthy SSE-covered symbols stop polling');
  wide.dispose();
  // onQuote 抛异常不得毒化去重: 下一帧仍须重试回调 (否则页面必须刷新才恢复)。
  let boom = true, seen = 0;
  const throwing = new LiveMarket({ onQuote: (s, v) => { if (boom) throw new Error('render boom'); seen = v.last_price; } });
  throwing.setSymbols(['A']);
  assert.throws(() => throwing.accept('quote', 'A', q(10, 100)), /render boom/);
  boom = false;
  throwing.accept('quote', 'A', q(10, 100));
  assert.equal(seen, 10, 'callback retried after a throwing render');
  assert.equal(throwing.get('A').last_price, 10, 'value cached after successful callback');
  throwing.dispose();

  // ── SSE 建连失败必须指数退避 (现状固定 5s: 服务端挂掉就是 720 次/小时) ──
  const failHttp = async (r, status = 500) => {
    r.resolve({ ok: false, status });
    await new Promise(resolve => setImmediate(resolve));
  };
  const probes = () => requests.filter(r => r.url === '/api/stream/status');
  {
    const bt = new LiveMarket({ onQuote: () => {}, streamStatus: true });
    bt.setSymbols(['A']);
    await new Promise(resolve => setImmediate(resolve));
    const deltas = [];
    for (let i = 0; i < 8; i++) {
      now += 300000;                       // 越过任何退避窗口, 保证这次真的会重连
      const t0 = now;
      bt.tick();
      const probe = probes().at(-1);
      assert.ok(probe, `第 ${i} 次预检应该发出`);
      await failHttp(probe);
      deltas.push(bt.retryAt - t0);
    }
    assert.deepEqual(deltas, [5000, 10000, 20000, 40000, 80000, 120000, 120000, 120000],
      '退避 5s→10s→…→120s 封顶');
    // 预检成功只证明服务端在应答: 退避要等真的收到有效帧才复位
    const beforeOk = probes().length;
    now += 200000;                          // 越过 120s 封顶, 才可能真的重连
    bt.tick();
    assert.equal(probes().length, beforeOk + 1, '退避窗口过后才重连');
    const okProbe = probes().at(-1);
    okProbe.resolve({ ok: true, json: async () => ({ sse_allowed: true, retry_after: 5 }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(bt.es, '预检通过就建流');
    assert.ok(bt.connectFailures > 0, '预检成功不算"流好了", 退避先不清');
    bt.es.emit({ A: q(10, 100) });
    assert.equal(bt.connectFailures, 0, '收到有效帧才复位退避');
    bt.dispose();
  }

  // ── 服务端说"现在不能连"时按它给的秒数等, 不固定 30s 盲等 ──
  {
    requests.length = 0;
    const waited = new LiveMarket({ onQuote: () => {}, streamStatus: true });
    waited.setSymbols(['A']);
    await new Promise(resolve => setImmediate(resolve));
    const t0 = now;
    probes().at(-1).resolve({ ok: true, json: async () => ({ sse_allowed: false, retry_after: 600 }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(waited.retryAt - t0, 600000, '按服务端给的 600s 等');
    assert.equal(waited.connectFailures, 0, '"现在不能连"是正常答复, 不算失败');
    waited.dispose();
  }

  // ── HTTP 回退失败也要退避 (现状固定 2.5s, 故障期间一直打) ──
  {
    requests.length = 0;
    const pb = new LiveMarket({ onQuote: () => {}, pollMs: 2500 });
    pb.setSymbols(['A']);
    await new Promise(resolve => setImmediate(resolve));
    const quotes = () => requests.filter(r => r.url.includes('/api/quotes'));
    assert.equal(quotes().length, 1, '引导轮询');
    // 退避口径: 首次失败后仍按基础节奏重试一次, 之后每连败一次翻倍
    await failHttp(quotes().at(-1));
    let t = now;
    now = t + 2600; pb.tick();
    assert.equal(quotes().length, 2, '首次失败后仍按基础 2.5s 重试');
    await failHttp(quotes().at(-1));
    t = now;
    now = t + 2600; pb.tick();
    assert.equal(quotes().length, 2, '2.6s < 退避 5s: 不该再发');
    now = t + 5100; pb.tick();
    assert.equal(quotes().length, 3, '过了 5s 才发第三次');
    await failHttp(quotes().at(-1));
    t = now;
    now = t + 6000; pb.tick();
    assert.equal(quotes().length, 3, '6s < 退避 10s');
    now = t + 10100; pb.tick();
    assert.equal(quotes().length, 4, '过了 10s 才发第四次');
    await finish(quotes().at(-1), {});        // 成功 → 退避复位
    t = now;
    now = t + 2600; pb.tick();
    assert.equal(quotes().length, 5, '成功后退避复位, 回到 2.5s 节奏');
    pb.dispose();
  }

  // ── 半死流不能每 15s 重连一次 ──
  // 上游快照持续失败时服务端只发 ": tick" 注释帧, 客户端等不到有效帧 → 15s 判定
  // 陈旧并重连。这里钉住"每次重连都要退避", 否则加上退避也只是把探测换个地方打。
  {
    requests.length = 0;
    const half = new LiveMarket({ onQuote: () => {}, streamStatus: true });
    half.setSymbols(['A']);
    await new Promise(resolve => setImmediate(resolve));
    probes().at(-1).resolve({ ok: true, json: async () => ({ sse_allowed: true, retry_after: 5 }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(half.es, '首次建流成功');
    const deltas = [];
    for (let i = 0; i < 4; i++) {
      now += 16000;                          // 越过 staleMs: 一直收不到有效帧
      const t0 = now;
      half.tick();
      assert.equal(half.es, null, `第 ${i} 次应判为陈旧并断开`);
      deltas.push(half.retryAt - t0);
      // 让它真的重连, 好观察下一次的退避
      now += 200000;
      half.tick();
      const probe = probes().at(-1);
      probe.resolve({ ok: true, json: async () => ({ sse_allowed: true, retry_after: 5 }) });
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.deepEqual(deltas, [5000, 10000, 20000, 40000], '半死流重连也要指数退避');
    half.dispose();
  }

  // ── 相位帧: 交给市场时钟, 且"流活着"要有真凭据 ──
  {
    requests.length = 0;
    const markets = [], streamStates = [];
    const mk = new LiveMarket({
      onQuote: () => {},
      onMarket: p => markets.push(p),
      onStreamState: s => streamStates.push(s),
    });
    mk.setSymbols(['A']);
    const mkStream = streams.at(-1);
    assert.deepEqual(streamStates, [], 'OPEN 本身不算流活着');
    mkStream.emit({ session_phase: 'auction', next_live_in_sec: null }, 'market');
    // 帧对象来自 vm realm, 与宿主的对象原型不同, 逐字段比对而不是整体 deepEqual
    assert.equal(markets.length, 1, '相位帧要转交给市场时钟');
    assert.equal(markets[0].session_phase, 'auction');
    assert.equal(markets[0].next_live_in_sec, null);
    assert.deepEqual(streamStates, [true], '相位帧是流活着的凭据');
    mkStream.emit({ A: q(10, 100) });
    assert.deepEqual(streamStates, [true], '只在状态变化时通知');
    mkStream.onerror();
    assert.deepEqual(streamStates, [true, false], '断线立刻让调用方恢复探测兜底');
    mk.dispose();
  }

  // ── 建流门槛与数据门槛分离: 允许提前连流 (09:00-09:15), 但不拉数据 ──
  {
    requests.length = 0;
    const allowStream = [true];
    const pre = new LiveMarket({
      onQuote: () => {},
      active: () => false,                    // 还没到取数时段
      streamActive: () => allowStream[0],     // 但服务端允许建流
    });
    pre.setSymbols(['A']);
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(streams.at(-1), '允许建流就要连上: 开盘第一帧零握手延迟');
    assert.equal(requests.filter(r => r.url.includes('/api/quotes')).length, 0,
      '不活跃时不轮询 HTTP');
    streams.at(-1).emit({ A: q(10, 100) });
    assert.equal(pre.get('A').last_price, 10, '提前连上的流照常送帧');
    allowStream[0] = false;                   // 盘外: 流也不该维持
    pre.tick();
    assert.equal(pre.es, null, '不允许建流就停机');
    pre.dispose();
  }

  console.log('live-market boundary cases passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
