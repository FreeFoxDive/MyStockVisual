/* One acceptance path for SSE, fallback HTTP and embedded initial quotes. */
(function (global) {
  'use strict';
  // 失败退避: 服务端挂掉时固定 5s/2.5s 重试等于每次故障打 720~1440 次/小时。
  var CONNECT_BASE_MS = 5000;
  var CONNECT_CAP_MS = 120000;
  // 服务端"下次可以连"的提示值得照办 (它就是下一次开盘时刻), 但要有上界:
  // 万一算错/日历降级, 最多一小时后自己回来复探, 不会整段卡住。
  var CONNECT_HINT_CAP_MS = 3600000;
  var POLL_CAP_MS = 30000;
  function backoffMs(failures, base, cap) {
    if (!failures) return base;
    return Math.min(base * Math.pow(2, failures - 1), cap);
  }
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
      this.sseConnecting = false;
      this.sseProbe = null;
      this.connectFailures = 0;
      this.pollFailures = new Map();
      this.streamAlive = false;
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
      this.sseConnecting = false;
      if (this.sseProbe) { this.sseProbe.abort(); this.sseProbe = null; }
      for (const p of this.pending.values()) p.abort.abort();
      this.pending.clear();
      this.health.clear();
      this._noteAlive(false);
    }
    restart() { this.suspend(); this.retryAt = 0; this.lastPoll.clear(); this.connectFailures = 0; this.pollFailures.clear(); }
    dispose() {
      this.suspend(); clearInterval(this.timer);
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', this.visibility);
      if (global.removeEventListener) {
        global.removeEventListener('pagehide', this.pagehide);
        global.removeEventListener('pageshow', this.pageshow);
      }
    }
    /** 流"活着"的判据是收到过有效帧, 不是 OPEN —— 让调用方 (市场时钟) 据此
     *  停掉自己的兜底探测; 反之流一断就立刻恢复探测。 */
    _noteAlive(open) {
      if (open) this.connectFailures = 0;
      if (!!open === this.streamAlive) return;
      this.streamAlive = !!open;
      if (this.options.onStreamState) {
        try { this.options.onStreamState(this.streamAlive); } catch (_) { /* 回调不得影响传输 */ }
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
      const callback = kind === 'quote' ? this.options.onQuote : kind === 'bars' ? this.options.onBars : this.options.onDepth;
      // 先回调再落值: 回调抛异常时不缓存该帧, 下一帧仍会重试回调, 避免一次渲染
      // 异常把该标的的后续更新永久去重掉 (页面必须刷新才恢复)。
      if (callback) callback(symbol, q);
      this.values.set(key, q);
      return true;
    }
    healthy(kind, symbol) {
      const h = this.health.get(this.key(kind, symbol));
      return !!h && Date.now() - h.at < (this.options.staleMs || 15000);
    }
    connect() {
      if (!global.EventSource || this.es || this.sseConnecting || Date.now() < this.retryAt || !this.symbols.length) return;
      const epoch = this.epoch;
      // 首次仍等 5s (与历史一致); 反复失败则指数退避到 2 分钟, 不再每次故障打满
      this.retryAt = Date.now() + backoffMs(this.connectFailures, CONNECT_BASE_MS, CONNECT_CAP_MS);
      // Server permits 50 symbols per stream. Remaining symbols retain HTTP fallback.
      const url = '/api/stream/quotes?symbols=' + encodeURIComponent(this.symbols.slice(0, 50).join(','))
        + (this.depth ? '&depth=1' : '')
        + (this.tail ? '&tail=' + encodeURIComponent(this.tail.period) + '&count=' + this.tail.count
          + '&adjust=' + encodeURIComponent(this.tail.adjust) : '');
      // 生产页面开启 status 预检；保留无预检模式供嵌入调用和旧客户端兼容。
      if (this.options.streamStatus !== true) {
        this._openSSE(url, epoch);
        return;
      }
      this.sseConnecting = true;
      const probeAbort = new AbortController();
      this.sseProbe = probeAbort;
      fetch('/api/stream/status', { signal: probeAbort.signal, cache: 'no-store' }).then(r => {
        if (!r.ok) throw new Error('stream status ' + r.status);
        return r.json();
      }).then(status => {
        if (this.epoch !== epoch) return;
        this.sseConnecting = false; this.sseProbe = null;
        if (!status.sse_allowed) {
          // 服务端给出"下次可以连"的秒数 (就是下一次开盘时刻): 照它等, 不再固定
          // 30s 盲等; 钳一个上界防异常值, 下界用基础值防打转。
          const hinted = Math.max(CONNECT_BASE_MS, Number(status.retry_after || 30) * 1000);
          this.retryAt = Date.now() + Math.min(hinted, CONNECT_HINT_CAP_MS);
          return;
        }
        // 这里**不**清 connectFailures: 预检成功只证明服务端在应答, 不证明数据在流
        // (上游坏了服务端仍会 200 + 只发注释帧)。退避只在收到有效帧时复位 (见
        // _noteAlive), 否则半死流会每 ~15s 重连一次而退避永远不生效。
        if (this.es) return;
        this._openSSE(url, epoch);
      }).catch(() => {
        if (this.epoch !== epoch) return;
        this.sseConnecting = false; this.sseProbe = null;
        this.connectFailures += 1;
        this.retryAt = Date.now() + backoffMs(this.connectFailures, CONNECT_BASE_MS, CONNECT_CAP_MS);
        this.tick();
      });
    }
    _openSSE(url, epoch) {
        const es = new global.EventSource(url);
        this.es = es;
        this.sseConnecting = false; this.sseProbe = null;
        const receive = (kind, ev) => {
          if (this.es !== es || this.epoch !== epoch) return;
          try {
            const rows = JSON.parse(ev.data);
            let accepted = false;
            for (const [s, q] of Object.entries(rows)) if (this.accept(kind, s, q, 'sse')) accepted = true;
            // 有效帧才算流活着 (OPEN 不算): 收到帧就恢复调用方的探测兜底预算
            if (accepted) this._noteAlive(true);
          } catch (_) { /* malformed frames cannot stop fallback */ }
        };
        es.onmessage = ev => receive('quote', ev);
        es.addEventListener('depth', ev => receive('depth', ev));
        es.addEventListener('bars', ev => receive('bars', ev));
        // 市场相位帧: 服务端在开盘/午休/收盘切换时推一次, 前端据此切自动刷新,
        // 不必再定时轮询 /api/ping 去"发现"已经开盘了。
        es.addEventListener('market', ev => {
          if (this.es !== es || this.epoch !== epoch) return;
          try {
            const payload = JSON.parse(ev.data);
            this._noteAlive(true);
            if (this.options.onMarket) this.options.onMarket(payload);
          } catch (_) { /* 同上: 坏帧不影响回退 */ }
        });
        // OPEN alone is not recovery. Only valid frames establish channel health.
        es.onerror = () => {
          if (this.es !== es || this.epoch !== epoch) return;
          this.health.clear();
          this.lastPoll.clear();
          es.close(); this.es = null;
          this.sseConnecting = false;
          this.connectFailures += 1;
          this.retryAt = Date.now() + backoffMs(this.connectFailures, CONNECT_BASE_MS, CONNECT_CAP_MS);
          this._noteAlive(false);
          this.tick();
        };
        this.connectedAt = Date.now();
    }
    async poll(kind, symbols, force = false) {
      if (!symbols.length) return;
      const id = kind + ':' + symbols.join(',');
      if (this.pending.has(id)) return;
      const base = this.options.pollMs || 2500;
      const failures = this.pollFailures.get(id) || 0;
      if (!force && Date.now() - (this.lastPoll.get(id) || 0) < backoffMs(failures, base, POLL_CAP_MS)) return;
      this.lastPoll.set(id, Date.now());
      const epoch = this.epoch, versions = new Map(symbols.map(s => [s, this.serial.get(this.key(kind, s)) || 0]));
      const abort = new AbortController(), pending = { abort };
      this.pending.set(id, pending);
      const timeout = setTimeout(() => abort.abort(), 10000);
      const failed = () => this.pollFailures.set(id, (this.pollFailures.get(id) || 0) + 1);
      try {
        const url = kind === 'quote' ? '/api/quotes?symbols=' + encodeURIComponent(symbols.join(','))
          : kind === 'bars' ? '/api/kline/tail?symbol=' + encodeURIComponent(symbols[0])
            + '&period=' + encodeURIComponent(this.tail.period) + '&count=' + this.tail.count
            + '&adjust=' + encodeURIComponent(this.tail.adjust) + '&n=2'
          : '/api/depth?symbol=' + encodeURIComponent(symbols[0]);
        const response = await fetch(url, { signal: abort.signal });
        // 服务端 5xx/429 也是故障: 记一次退避, 免得故障期间按 2.5s 一直打
        if (!response.ok) { failed(); return; }
        const body = await response.json();
        if (abort.signal.aborted || epoch !== this.epoch) return;
        this.pollFailures.delete(id);
        const rows = kind === 'quote' ? body : { [symbols[0]]: kind === 'bars' ? body : body.depth };
        for (const s of symbols) {
          // Even when abort loses a race, a recovered SSE wins over its old HTTP request.
          if ((this.serial.get(this.key(kind, s)) || 0) !== versions.get(s)) continue;
          if (this.healthy(kind, s)) continue;
          this.accept(kind, s, rows[s], 'poll');
        }
      } catch (_) {
        // 主动 abort (切标的/隐藏) 不算故障, 只有超时/网络错误才退避
        if (!abort.signal.aborted) failed();
        /* keep last good values */
      }
      finally {
        clearTimeout(timeout);
        if (this.pending.get(id) === pending) this.pending.delete(id);
      }
    }
    refresh() { this.lastPoll.clear(); this.tick(); }
    tick() {
      if (!this.symbols.length || (typeof document !== 'undefined' && document.hidden)) return;
      const active = !this.options.active || this.options.active();
      // 行情流与数据轮询的门槛不同: 服务端允许提前建流 (交易日 09:00 起, 只发
      // 保活帧不取数), 提前连上换来开盘第一帧零握手延迟; 而 HTTP 轮询仍只在
      // 真正活跃时才做。
      const streamOk = this.options.streamActive ? this.options.streamActive() : active;
      // 门槛刚放开 (通常是"到点了该连了") 就清掉之前拿到的等待提示, 立刻重试 ——
      // 否则一个早先的 retry_after 会把开盘那一刻的首次连接推后。
      if (streamOk && !this._lastStreamOk) this.retryAt = 0;
      this._lastStreamOk = streamOk;
      // 流门槛比数据门槛宽 (服务端允许 09:00 起建流, 但 09:15 起才有数据),
      // 所以只判流: 不允许流就停机; 允许流但不允许数据就只维持连接不轮询。
      if (!streamOk) { if (this.es) this.suspend(); return; }
      if (this.es && Date.now() - this.connectedAt > (this.options.staleMs || 15000)
          && !this.symbols.slice(0, 50).some(s => this.healthy('quote', s))) {
        this.es.close(); this.es = null; this.health.clear(); this.retryAt = 0;
        // 半死流也算一次建连失败: 上游快照持续失败时服务端只发 : tick 注释帧,
        // 客户端永远等不到有效帧 → 这个分支每 ~15s 触发一次。不退避就是每 15s
        // 重连一次, 比固定间隔的探测更糟。
        this.connectFailures += 1;
        this._noteAlive(false);
      }
      this.connect();
      if (!active) return;
      // Stable batches keep at most one HTTP request in flight per resource.
      // Per-batch health check already skips symbols SSE keeps fresh; batches
      // beyond the SSE-covered first 50 always retain HTTP fallback.
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
