/* One acceptance path for SSE, fallback HTTP and embedded initial quotes. */
(function (global) {
  'use strict';
  function price(v) {
    return v == null || v === '' || !Number.isFinite(Number(v)) ? '—' : String(Number(Number(v).toFixed(3)));
  }
  function stamp(v) {
    if (v == null || v === '') return 0;
    const n = Number(v);
    if (Number.isFinite(n)) return n > 1e12 ? n : n * 1000;
    return Date.parse(v) || 0;
  }
  class LiveMarket {
    constructor(options = {}) {
      this.options = options;
      this.symbols = [];
      this.values = new Map();
      this.health = new Map();
      this.serial = new Map();
      this.pending = new Map();
      this.lastPoll = new Map();
      this.epoch = 0;
      this.retryAt = 0;
      this.es = null;
      this.timer = setInterval(() => this.tick(), 250);
      this.visibility = () => {
        if (document.hidden) this.suspend();
        else { this.restart(); this.tick(); }
      };
      if (typeof document !== 'undefined') document.addEventListener('visibilitychange', this.visibility);
      this.pagehide = () => this.suspend();
      this.pageshow = () => { this.restart(); this.tick(); };
      if (global.addEventListener) {
        global.addEventListener('pagehide', this.pagehide);
        global.addEventListener('pageshow', this.pageshow);
      }
    }
    key(kind, symbol) { return kind + ':' + symbol; }
    get(symbol, kind = 'quote') { return this.values.get(this.key(kind, symbol)); }
    setSymbols(symbols, depth = false, tail = null) {
      const next = [...new Set(symbols.filter(Boolean))].sort();
      const tag = next.join(',') + ':' + !!depth + ':' + JSON.stringify(tail);
      if (tag === this.tag) return;
      this.tag = tag;
      this.symbols = next;
      this.depth = !!depth;
      if (JSON.stringify(this.tail) !== JSON.stringify(tail)) {
        for (const key of this.values.keys()) if (key.startsWith('bars:')) this.values.delete(key);
      }
      this.tail = tail;
      for (const key of this.values.keys()) {
        if (!next.includes(key.slice(key.indexOf(':') + 1))) this.values.delete(key);
      }
      this.restart();
      this.tick();
    }
    suspend() {
      this.epoch++;
      if (this.es) this.es.close();
      this.es = null;
      for (const p of this.pending.values()) p.abort.abort();
      this.pending.clear();
      this.health.clear();
    }
    restart() { this.suspend(); this.retryAt = 0; this.lastPoll.clear(); }
    dispose() {
      this.suspend(); clearInterval(this.timer);
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', this.visibility);
      if (global.removeEventListener) {
        global.removeEventListener('pagehide', this.pagehide);
        global.removeEventListener('pageshow', this.pageshow);
      }
    }
    valid(kind, q) {
      return q && (kind === 'quote' ? Number.isFinite(q.last_price) && q.last_price > 0
        : kind === 'bars' ? Array.isArray(q.bars) && q.bars.length > 0
        : Array.isArray(q.bid_prices) && Array.isArray(q.ask_prices));
    }
    accept(kind, symbol, q, source = 'embedded') {
      if (!this.symbols.includes(symbol) || !this.valid(kind, q) || (q.symbol && q.symbol !== symbol)) return false;
      if (kind === 'bars' && (!this.tail || q.period !== this.tail.period
          || !q.meta || q.meta.adjust !== this.tail.adjust)) return false;
      const key = this.key(kind, symbol), old = this.values.get(key);
      const t = stamp(q.timestamp), ot = old ? stamp(old.timestamp) : 0;
      const rev = Number(q._revision) || 0, prev = old ? Number(old._revision) || 0 : 0;
      // A newer receipt must never make an older exchange snapshot authoritative.
      if (old && ((t && ot && t < ot) || (rev && prev && rev < prev) || (!rev && prev))) return false;
      if (source === 'sse') {
        this.serial.set(key, (this.serial.get(key) || 0) + 1);
        // Replayed cached frames are not evidence of a working upstream forever.
        const h = this.health.get(key);
        if (!h || h.revision !== rev || h.timestamp !== t) {
          this.health.set(key, { at: Date.now(), revision: rev, timestamp: t });
        }
      }
      if (old && JSON.stringify(old) === JSON.stringify(q)) return true;
      this.values.set(key, q);
      const callback = kind === 'quote' ? this.options.onQuote : kind === 'bars' ? this.options.onBars : this.options.onDepth;
      if (callback) callback(symbol, q);
      return true;
    }
    healthy(kind, symbol) {
      const h = this.health.get(this.key(kind, symbol));
      return !!h && Date.now() - h.at < (this.options.staleMs || 15000);
    }
    connect() {
      if (!global.EventSource || this.es || Date.now() < this.retryAt || !this.symbols.length) return;
      const epoch = this.epoch;
      this.retryAt = Date.now() + 5000;
      // Server permits 50 symbols per stream. Remaining symbols retain HTTP fallback.
      const url = '/api/stream/quotes?symbols=' + encodeURIComponent(this.symbols.slice(0, 50).join(','))
        + (this.depth ? '&depth=1' : '')
        + (this.tail ? '&tail=' + encodeURIComponent(this.tail.period) + '&count=' + this.tail.count
          + '&adjust=' + encodeURIComponent(this.tail.adjust) : '');
      try {
        const es = new global.EventSource(url);
        this.es = es;
        const receive = (kind, ev) => {
          if (this.es !== es || this.epoch !== epoch) return;
          try {
            const rows = JSON.parse(ev.data);
            for (const [s, q] of Object.entries(rows)) this.accept(kind, s, q, 'sse');
          } catch (_) { /* malformed frames cannot stop fallback */ }
        };
        es.onmessage = ev => receive('quote', ev);
        es.addEventListener('depth', ev => receive('depth', ev));
        es.addEventListener('bars', ev => receive('bars', ev));
        // OPEN alone is not recovery. Only valid frames establish channel health.
        es.onerror = () => {
          if (this.es !== es || this.epoch !== epoch) return;
          this.health.clear();
          this.lastPoll.clear();
          es.close(); this.es = null;
          this.retryAt = Date.now() + 5000;
          this.tick();
        };
        this.connectedAt = Date.now();
      } catch (_) { this.es = null; }
    }
    async poll(kind, symbols, force = false) {
      if (!symbols.length) return;
      const id = kind + ':' + symbols.join(',');
      if (this.pending.has(id)) return;
      if (!force && Date.now() - (this.lastPoll.get(id) || 0) < (this.options.pollMs || 2500)) return;
      this.lastPoll.set(id, Date.now());
      const epoch = this.epoch, versions = new Map(symbols.map(s => [s, this.serial.get(this.key(kind, s)) || 0]));
      const abort = new AbortController(), pending = { abort };
      this.pending.set(id, pending);
      const timeout = setTimeout(() => abort.abort(), 10000);
      try {
        const url = kind === 'quote' ? '/api/quotes?symbols=' + encodeURIComponent(symbols.join(','))
          : kind === 'bars' ? '/api/kline/tail?symbol=' + encodeURIComponent(symbols[0])
            + '&period=' + encodeURIComponent(this.tail.period) + '&count=' + this.tail.count
            + '&adjust=' + encodeURIComponent(this.tail.adjust) + '&n=2'
          : '/api/depth?symbol=' + encodeURIComponent(symbols[0]);
        const response = await fetch(url, { signal: abort.signal });
        if (!response.ok) return;
        const body = await response.json();
        if (abort.signal.aborted || epoch !== this.epoch) return;
        const rows = kind === 'quote' ? body : { [symbols[0]]: kind === 'bars' ? body : body.depth };
        for (const s of symbols) {
          // Even when abort loses a race, a recovered SSE wins over its old HTTP request.
          if ((this.serial.get(this.key(kind, s)) || 0) !== versions.get(s)) continue;
          if (this.healthy(kind, s)) continue;
          this.accept(kind, s, rows[s], 'poll');
        }
      } catch (_) { /* keep last good values */ }
      finally {
        clearTimeout(timeout);
        if (this.pending.get(id) === pending) this.pending.delete(id);
      }
    }
    refresh() { this.lastPoll.clear(); this.tick(); }
    tick() {
      if (!this.symbols.length || (typeof document !== 'undefined' && document.hidden)) return;
      if (this.options.active && !this.options.active()) { if (this.es) this.suspend(); return; }
      if (this.es && Date.now() - this.connectedAt > (this.options.staleMs || 15000)
          && !this.symbols.slice(0, 50).some(s => this.healthy('quote', s))) {
        this.es.close(); this.es = null; this.health.clear(); this.retryAt = 0;
      }
      this.connect();
      // Stable batches keep at most one HTTP request in flight per resource.
      for (let i = 0; i < this.symbols.length; i += 50) {
        const batch = this.symbols.slice(i, i + 50);
        if (batch.some(s => !this.healthy('quote', s))) this.poll('quote', batch);
      }
      if (this.depth) for (const s of this.symbols) {
        if (!this.healthy('depth', s)) this.poll('depth', [s]);
      }
      if (this.tail && !this.healthy('bars', this.symbols[0])) this.poll('bars', [this.symbols[0]]);
    }
  }
  global.VisualLive = { LiveMarket, price };
})(typeof window !== 'undefined' ? window : globalThis);
