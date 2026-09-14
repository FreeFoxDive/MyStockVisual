const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const root = process.argv[2];
const context = { window: null, globalThis: null, console };
context.window = context;
context.globalThis = context;
vm.createContext(context);
for (const name of ['vue-3.5.13.global.prod.js', 'vue-demi-0.14.10.iife.js', 'pinia-2.2.6.iife.prod.js']) {
  vm.runInContext(fs.readFileSync(`${root}/static/vendor/${name}`, 'utf8'), context);
}
vm.runInContext(fs.readFileSync(`${root}/static/js/market-store.js`, 'utf8'), context);

const store = context.VisualMarketStore.store;
const quote = (price, revision, timestamp = revision) => ({ symbol: 'A', last_price: price, _revision: revision, timestamp });
const observed = [];
const stop = context.VisualMarketStore.watch(() => store.last, change => observed.push(change && change.kind));

assert.equal(store.accept('quote', 'A', quote(10, 100)), true);
assert.equal(store.accept('quote', 'A', quote(9, 99)), false, 'older poll cannot replace SSE');
assert.equal(store.quotes.A.last_price, 10);
assert.equal(store.accept('quote', 'A', quote(11, 101)), true);
assert.equal(store.quotes.A.last_price, 11);
assert.equal(store.accept('depth', 'A', { symbol: 'A', bid_prices: [10], ask_prices: [11], _revision: 100, timestamp: 100 }), true);
assert.equal(store.accept('depth', 'A', { symbol: 'A', bid_prices: [9], ask_prices: [10], _revision: 99, timestamp: 99 }), false);
assert.equal(store.depths.A.bid_prices[0], 10, 'depth has its own monotonic ordering');
assert.equal(store.accept('info', 'A', { symbol: 'A', industry: '银行', _revision: 1 }), true,
  'a quote revision must not suppress independent basic-info data');
assert.equal(store.infos.A.industry, '银行');
assert.deepEqual(observed, ['quote', 'quote', 'depth', 'info'], 'only accepted updates notify reactive consumers');
stop();
store.clearSymbol('A');
assert.equal(store.quotes.A, undefined);
assert.equal(store.depths.A, undefined);
console.log('market-store boundaries passed');
