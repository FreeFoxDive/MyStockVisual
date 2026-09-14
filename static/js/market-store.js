/* Shared reactive market state. Transport writes here; pages subscribe to fields. */
(function (global) {
  'use strict';
  if (!global.Vue || !global.Pinia) throw new Error('Vue and Pinia must load before market-store.js');

  const { createPinia, defineStore, setActivePinia } = global.Pinia;
  const pinia = createPinia();
  setActivePinia(pinia);

  function stamp(value) {
    if (value == null || value === '') return 0;
    const n = Number(value);
    if (Number.isFinite(n)) return n > 1e12 ? n : n * 1000;
    return Date.parse(value) || 0;
  }

  const useMarketStore = defineStore('market', {
    state: () => ({
      quotes: {}, depths: {}, infos: {},
      last: { quote: null, depth: null, info: null },
    }),
    actions: {
      accept(kind, symbol, value) {
        if (!symbol || !value) return false;
        const bucket = kind === 'quote' ? this.quotes : kind === 'depth' ? this.depths : this.infos;
        const old = bucket[symbol];
        const t = stamp(value.timestamp), ot = old ? stamp(old.timestamp) : 0;
        const rev = Number(value._revision) || 0, prev = old ? Number(old._revision) || 0 : 0;
        if (old && ((t && ot && t < ot) || (rev && prev && rev < prev) || (!rev && prev))) return false;
        bucket[symbol] = value;
        this.last = { kind, symbol, value };
        return true;
      },
      clearSymbol(symbol) {
        delete this.quotes[symbol];
        delete this.depths[symbol];
        delete this.infos[symbol];
      },
    },
  });

  const store = useMarketStore(pinia);
  global.VisualMarketStore = {
    store,
    watch(selector, callback) { return global.Vue.watch(selector, callback, { flush: 'sync' }); },
    stamp,
  };
})(typeof window !== 'undefined' ? window : globalThis);
