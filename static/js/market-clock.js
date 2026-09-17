/* 市场时段时钟: 什么时候该拉行情、下一次开盘是什么时候、丢过时间怎么补回来。

   设计要点 (每一条都对应一个真实故障):
   - 相位变更由服务端经 SSE 的 event: market 推送, 连上之后**一个探测请求都不发**;
     轮询只在冷启动/兜底/回到前台时发生, 所以不会"每 60s 问一次"。
   - 唤醒用服务端给的**相对秒数**(next_live_in_sec), 不跟本地时钟比绝对时刻,
     用户机器时钟不准也能精确命中开盘。
   - 标签页隐藏时主动停机 (没人在看), 回到前台立即补一次 —— 及时性与探测频率
     因此不冲突。
   - 计时器在后台被节流、或机器休眠, 会让心跳的间隔异常变大; 检测到这种"丢过
     时间"立即补同步, 不等下一个周期。
*/
(function (global) {
  'use strict';

  // 安全网: 无论什么情况, 最长这么久没核对过服务端状态就核对一次。
  // 它与"精确唤醒"是**互补**关系, 谁先到谁触发 —— 唤醒负责准点, 它负责兜住
  // "提示算错"(日历降级/服务端刚重启)的情况, 所以必须是固定值, 不能由
  // next_live_in_sec 推出来: 那样它永远比唤醒更早, 唤醒就成了死代码。
  var SAFETY_MS = 600e3;
  var WAKE_MIN_MS = 60e3;
  var WAKE_MAX_MS = 600e3;        // 唤醒钳上界 = 安全网, 保证不会比安全网更晚
  // 唤醒余量: 宁可晚一点点一次到位, 也不要早到再补一次探测 (每次探测都是成本)
  var WAKE_AFTER_MS = 1500;
  var SYNC_MIN_GAP_MS = 2000;     // 非强制同步的去抖窗口 (避免恢复事件叠发)
  var GAP_FACTOR = 3;             // 实际间隔超过预期这么多 → 判定丢过时间
  var FAILED_RETRY_MS = 60e3;     // 失败时固定用下界重试 (失败恰恰需要活性)
  var FAIL_NOTICE = 3;            // 连续失败次数达此值 → 通知调用方"不可达"

  function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }

  function num(v) {
    if (v == null || v === '') return null;
    var n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  /** 浏览器时钟兜底 (仅在从未拿到服务端状态时使用)。
   *  周末 + 09:15-11:30 / 13:00-15:00 —— 与后端 is_live 同口径, 集合竞价也算。 */
  function localSessionFallback(nowMs) {
    var d = new Date(nowMs == null ? Date.now() : nowMs);
    var wd = d.getDay();
    if (wd === 0 || wd === 6) return false;
    var t = d.getHours() * 60 + d.getMinutes();
    return (t >= 9 * 60 + 15 && t <= 11 * 60 + 30) || (t >= 13 * 60 && t <= 15 * 60);
  }

  /** 倒计时文本: 409 → "06:49", 5025 → "1:23:45"。 */
  function countdownText(sec) {
    var s = Math.max(0, Math.round(num(sec) || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), q = s % 60;
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return h > 0 ? h + ':' + pad(m) + ':' + pad(q) : pad(m) + ':' + pad(q);
  }

  /** 安全网间隔: 固定值, 与唤醒互补 (见 SAFETY_MS 的说明)。 */
  function safetyMs() { return SAFETY_MS; }

  /** 精确唤醒延时(ms): 服务端给的相对秒数 + 余量, 钳位。
   *  不给数就退回 null (只有安全网); 给 0/负数退回下界, 不"立即重试" ——
   *  服务端自相矛盾时紧循环比晚一分钟更糟。 */
  function wakeMs(nextLiveInSec) {
    var sec = num(nextLiveInSec);
    if (sec == null) return null;
    if (sec <= 1) return WAKE_MIN_MS;
    return clamp(Math.round(sec * 1000) + WAKE_AFTER_MS, WAKE_MIN_MS, WAKE_MAX_MS);
  }

  /** 时刻片段: 当日只给 HH:MM, 跨日给 MM-DD HH:MM。
   *  不加「下一交易日」「开盘」「集合竞价」这类字样 —— 紧邻的「状态」行已经写了相位,
   *  重复一遍就把值区撑爆 (CJK 在 monospace 里是 1em, 4 个汉字 = 48px, 而值区只有
   *  129px)。被省掉的语义在 openHintDetail() 里, 由行的 title 承载。 */
  function _clockAt(stamp, today) {
    if (typeof stamp !== 'string' || stamp.length < 16) return null;
    var hm = stamp.slice(11, 16);
    if (!/^\d{2}:\d{2}$/.test(hm)) return null;
    return (today && stamp.slice(0, 10) === today) ? hm : stamp.slice(5, 10) + ' ' + hm;
  }

  /** 这一行盯的是哪个时刻: 竞价/午休盯连续竞价开盘, 盘前/收盘盯集合竞价开始。 */
  function _hintStamp(state) {
    return (state.phase === 'auction' || state.phase === 'break')
      ? state.nextOpenAt : state.nextLiveAt;
  }

  /** 倒计时盯的是哪个时刻 —— 唯一口径, 决定文案里带不带倒计时、前端减哪个数。
   *  竞价/午休盯"连续竞价开盘"(09:30 / 13:00), 盘前盯"集合竞价开始"(09:15),
   *  收盘/休市的下一站动辄十几小时, 不给倒计时 (那是噪声, 不是信息)。 */
  function countdownTargetSec(state) {
    if (!state || !state.phase) return null;
    if (state.phase === 'auction' || state.phase === 'break') return num(state.nextOpenInSec);
    if (state.phase === 'pre') return num(state.nextLiveInSec);
    return null;
  }

  /** 侧栏「下一开盘」行的值: `时刻 [余 倒计时]`; 盘中给占位符。
   *  最长 "13:00 余 1:23:45" ≈ 113px, 值区 129px (test 里有宽度断言兜着)。 */
  function openHintText(state, remainingSec) {
    if (!state || !state.phase || state.phase === 'trading') return '—';
    var stamp = _hintStamp(state);
    var text = _clockAt(stamp, state.today);
    if (!text) return '—';
    // 「余」点明后半段是剩余时长, 不然两个 时:分:秒 并列会被读成两个钟点。
    // 只有同日才叠: 跨日的相位本来就没有倒计时, 这条同时挡住自相矛盾的载荷
    // (phase=break 却配明天的 next_open_at → 叠起来必然超宽)。
    var sameDay = !!state.today && stamp.slice(0, 10) === state.today;
    if (sameDay && countdownTargetSec(state) != null && num(remainingSec) != null) {
      text += ' 余 ' + countdownText(remainingSec);
    }
    return text;
  }

  /** 行的悬停说明 —— 值区放不下的那部分语义: 是连续竞价开盘还是集合竞价开始、
   *  以及跨到哪一天。与值共用 _hintStamp, 只有装饰不同, 不会各自漂移。 */
  function openHintDetail(state) {
    if (!state || !state.phase || state.phase === 'trading') return null;
    var stamp = _hintStamp(state);
    var text = _clockAt(stamp, null);        // 一律带日期, 免得"今天/隔天"含混
    if (!text) return null;
    var what = (state.phase === 'auction' || state.phase === 'break')
      ? '连续竞价开盘' : '集合竞价开始';
    var nextDay = !!state.today && stamp.slice(0, 10) !== state.today;
    return (nextDay ? '下一交易日 ' : '') + text + ' ' + what;
  }

  function MarketClock(options) {
    options = options || {};
    this.options = options;
    this.now = options.now || function () { return Date.now(); };
    this.timers = options.timers || {
      setTimeout: function (fn, ms) { return global.setTimeout(fn, ms); },
      clearTimeout: function (id) { return global.clearTimeout(id); },
    };
    this.doc = options.doc !== undefined
      ? options.doc
      : (typeof document !== 'undefined' ? document : null);
    this.state = {
      phase: null, live: null, inSession: null, isTradingDay: null,
      nextLiveAt: null, nextLiveInSec: null, nextOpenAt: null, nextOpenInSec: null,
      streamAllowed: null, calendar: null, today: null, at: null, source: null,
    };
    this.failures = 0;
    this.unreachable = false;
    this._started = false;
    this._disposed = false;
    this._syncing = false;
    this._syncAgain = false;
    this._streamOpen = false;
    this._lastSyncAt = 0;
    this._lastBeatAt = 0;
    this._beatMs = 0;
    this._beatTimer = null;
    this._wakeTimer = null;
  }

  MarketClock.prototype._fetch = function (url, init) {
    if (this.options.fetchImpl) return this.options.fetchImpl(url, init);
    return global.fetch(url, init);
  };

  MarketClock.prototype._isHidden = function () {
    if (typeof this.options.isHidden === 'function') return !!this.options.isHidden();
    return !!(this.doc && this.doc.hidden);
  };

  MarketClock.prototype._emit = function (kind, detail) {
    if (typeof this.options.onChange !== 'function') return;
    try {
      this.options.onChange(this.state, kind, detail);
    } catch (e) {
      // 回调出错不能拖垮时钟 (否则一次渲染异常就再也不刷新了), 但也不能全静默:
      // 页面里的刷新动作挂掉会表现为"数据不动", 没有这行就完全无从查起。
      if (typeof console !== 'undefined' && console.warn) {
        console.warn('[market-clock] onChange(' + kind + ') 抛异常:', e);
      }
    }
  };

  MarketClock.prototype.start = function () {
    if (this._started) return this;
    this._started = true;
    this._disposed = false;
    this._bind();
    this.sync('start', true);
    return this;
  };

  MarketClock.prototype._bind = function () {
    var self = this;
    if (this.doc && this.doc.addEventListener) {
      this._onVis = function () {
        if (self._isHidden()) { self._clearTimers(); return; }
        // 回到前台立即补: 隐藏期间一次探测都没发, 这里必须拿权威状态
        self.sync('visible', true);
      };
      this.doc.addEventListener('visibilitychange', this._onVis);
    }
    if (global.addEventListener) {
      this._onOnline = function () { self.sync('online', true); };
      this._onPageShow = function (ev) {
        // 普通首次加载 start() 已经同步过; 只有 bfcache 恢复才需要补
        if (ev && ev.persisted) self.sync('pageshow', true);
      };
      global.addEventListener('online', this._onOnline);
      global.addEventListener('pageshow', this._onPageShow);
    }
  };

  /** 把服务端状态并入当前状态。返回相位/活跃性是否发生了真变化。 */
  MarketClock.prototype._apply = function (d, source) {
    if (!d || typeof d !== 'object') return false;
    var prev = this.state;
    var live = typeof d.quote_live === 'boolean' ? d.quote_live
      : (typeof d.in_session === 'boolean' ? d.in_session : prev.live);
    var next = {
      // session_phase 是规范字段名 (ping/kline meta/SSE 帧一致); phase 是兼容别名
      phase: typeof d.session_phase === 'string' ? d.session_phase
        : (typeof d.phase === 'string' ? d.phase : prev.phase),
      live: live,
      inSession: typeof d.in_session === 'boolean' ? d.in_session : prev.inSession,
      isTradingDay: typeof d.is_trading_day === 'boolean' ? d.is_trading_day : prev.isTradingDay,
      nextLiveAt: typeof d.next_live_at === 'string' ? d.next_live_at : null,
      nextLiveInSec: num(d.next_live_in_sec),
      nextOpenAt: typeof d.next_open_at === 'string' ? d.next_open_at : null,
      nextOpenInSec: num(d.next_open_in_sec),
      streamAllowed: typeof d.stream_allowed === 'boolean' ? d.stream_allowed : prev.streamAllowed,
      calendar: typeof d.calendar_source === 'string' ? d.calendar_source : prev.calendar,
      today: typeof d.time === 'string' ? d.time.slice(0, 10) : prev.today,
      at: this.now(),          // 同步时刻: 倒计时以它为基准按本地已过时间递减
      source: source || null,
    };
    var changed = next.phase !== prev.phase || next.live !== prev.live
      || next.isTradingDay !== prev.isTradingDay || next.calendar !== prev.calendar;
    this.state = next;
    this._markSeen();
    this._arm();
    if (changed) this._emit('change');
    return changed;
  };

  /** SSE 的 event: market 帧 (与 /api/ping 同源, 字段名一致)。 */
  MarketClock.prototype.applyMarketFrame = function (payload) {
    this._streamOpen = true;
    return this._apply(payload, 'sse');
  };

  /** LiveMarket 回报行情流连通性: 连上时相位由推送给出, 心跳不再发请求。 */
  MarketClock.prototype.setStreamConnected = function (open) {
    if (!!open === this._streamOpen) return;
    this._streamOpen = !!open;
    this._markSeen();
    this._arm();
  };

  MarketClock.prototype._markSeen = function () {
    this._lastBeatAt = this.now();
  };

  /** 距上次心跳过了多久; 明显超过预期说明后台被节流或机器休眠过。 */
  MarketClock.prototype.gapMs = function () {
    return this.now() - (this._lastBeatAt || this.now());
  };

  MarketClock.prototype.sync = function (reason, force) {
    var self = this;
    if (this._disposed) return Promise.resolve(null);
    var now = this.now();
    if (!force && this._lastSyncAt && now - this._lastSyncAt < SYNC_MIN_GAP_MS) {
      // 心跳撞上刚完成的唤醒/恢复: 让已经拿到的新状态生效, 不重复发
      this._arm();
      return Promise.resolve(this.state);
    }
    if (this._syncing) { this._syncAgain = true; return Promise.resolve(this.state); }
    this._syncing = true;
    this._lastSyncAt = now;
    return this._fetch('/api/ping', { cache: 'no-store' }).then(function (r) {
      if (!r || !r.ok) throw new Error('ping ' + (r && r.status));
      return r.json();
    }).then(function (d) {
      self.failures = 0;
      if (self.unreachable) { self.unreachable = false; self._emit('reachable'); }
      self._apply(d, 'ping');
    }).catch(function () {
      // 失败**不清空**已有状态: 一次网络抖动不该让页面判成休市
      self.failures += 1;
      if (self.failures >= FAIL_NOTICE && !self.unreachable) {
        self.unreachable = true;
        self._emit('unreachable');
      }
    }).then(function () {
      self._syncing = false;
      self._arm();
      if (self._syncAgain) { self._syncAgain = false; self.sync('coalesced', true); }
      return self.state;
    });
  };

  MarketClock.prototype._clearTimers = function () {
    if (this._beatTimer != null) { this.timers.clearTimeout(this._beatTimer); this._beatTimer = null; }
    if (this._wakeTimer != null) { this.timers.clearTimeout(this._wakeTimer); this._wakeTimer = null; }
  };

  /** 排下一次安全网 + 精确唤醒 (谁先到谁触发)。隐藏时一个定时器都不留。
   *  "SSE 连着就不发探测"不在这里判 —— 定时器是休眠哨兵, 必须有; 抑制发生在
   *  _beat() 里 (见那里的 _streamOpen 判断)。 */
  MarketClock.prototype._arm = function () {
    this._clearTimers();
    if (this._disposed || !this._started) return;
    if (this._isHidden()) return;
    var self = this;
    // 失败期间固定用下界重试: 服务端抖动时恰恰需要活性, 不该放宽
    var ms = this.failures > 0 ? FAILED_RETRY_MS : safetyMs();
    this._beatMs = ms;
    this._beatTimer = this.timers.setTimeout(function () { self._beat(); }, ms);
    // 精确唤醒: 只在比安全网更早时挂 (晚于它就没有意义, 白多一个定时器)
    var wake = this.state.live ? null : wakeMs(this.state.nextLiveInSec);
    if (wake != null && wake < ms) {
      this._wakeTimer = this.timers.setTimeout(function () { self.sync('wake', true); }, wake);
    }
  };

  MarketClock.prototype._beat = function () {
    var gap = this.gapMs();
    var expected = Math.max(this._beatMs || 0, 1000);
    this._lastBeatAt = this.now();
    this._arm();
    if (gap > expected * GAP_FACTOR) {
      // 后台被节流或机器刚睡醒: 现在的时间点与上次已经脱节, 立即要一次权威状态
      this.sync('resume', true);
      this._emit('resume', gap);
      return;
    }
    if (this._streamOpen && this.live()) return;  // 连接期内不发探测
    this.sync('heartbeat');
  };

  /** 该不该自动刷新行情。服务端状态优先, 从未同步成功才退回浏览器时钟。 */
  MarketClock.prototype.live = function () {
    if (this.state.live === null || this.state.live === undefined) {
      return localSessionFallback(this.now());
    }
    return !!this.state.live && this.state.isTradingDay !== false;
  };

  /** 该不该维持行情流: 服务端允许提前建连 (交易日 09:00 起, 只保活不取数),
   *  比 live() 宽 —— 提前连上换来开盘第一帧零握手延迟。
   *  从未同步成功时的兜底用 localSessionFallback (09:15 起): 保守一档 —— 丢掉
   *  那 15 分钟预热, 但绝不会在盘外去连流。 */
  MarketClock.prototype.streamAllowed = function () {
    if (this.state.streamAllowed === null || this.state.streamAllowed === undefined) {
      return localSessionFallback(this.now());
    }
    return !!this.state.streamAllowed;
  };

  /** 交易日历降级 (缺 pandas_market_calendars → 周一~周五, 节假日会误判)。 */
  MarketClock.prototype.degraded = function () {
    return this.state.calendar === 'weekday';
  };

  MarketClock.prototype.dispose = function () {
    this._disposed = true;
    // _started 也要清掉: 否则 dispose() 之后再 start() 会被开头那行 return 挡住,
    // 而 _disposed 已是 true → sync() 全部空转, 时钟静默死掉且不报错。
    this._started = false;
    this._clearTimers();
    if (this.doc && this.doc.removeEventListener && this._onVis) {
      this.doc.removeEventListener('visibilitychange', this._onVis);
    }
    if (global.removeEventListener) {
      if (this._onOnline) global.removeEventListener('online', this._onOnline);
      if (this._onPageShow) global.removeEventListener('pageshow', this._onPageShow);
    }
  };

  global.VisualMarketClock = {
    MarketClock: MarketClock,
    localSessionFallback: localSessionFallback,
    countdownText: countdownText,
    safetyMs: safetyMs,
    wakeMs: wakeMs,
    countdownTargetSec: countdownTargetSec,
    openHintText: openHintText,
    openHintDetail: openHintDetail,
  };
})(typeof window !== 'undefined' ? window : globalThis);
