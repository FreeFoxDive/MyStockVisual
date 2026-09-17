// 市场时段时钟的边界用例: 纯函数 + 定时器/请求行为。
//
// 关注点全部来自真实故障:
//   - 开盘后最多 ~60s 不刷新 (靠 60s ping 撞开盘时刻)
//   - 标签页隐藏/机器休眠后不恢复
//   - 盘外无脑轮询 (每天上万次探测)
//   - 服务端不可达时的探测风暴
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let clockNow = 0;
let seq = 0;
let timers = new Map();   // id -> {fn, ms}
let requests = [];        // {url, resolve, reject}
let listeners = {};       // window 事件
let docListeners = {};    // document 事件
let hidden = false;

function reset() {
  clockNow = 1e12;
  seq = 0;
  timers = new Map();
  requests = [];
  listeners = {};
  docListeners = {};
  hidden = false;
}

function dueTimers() {
  return [...timers.entries()].sort((a, b) => a[1].ms - b[1].ms);
}
/** 触发最早到期的定时器(不推进时钟), 返回它的间隔 ms */
function fireEarliest() {
  const next = dueTimers()[0];
  if (!next) return null;
  const [id, t] = next;
  timers.delete(id);
  t.fn();
  return t.ms;
}
function pendingCount() { return timers.size; }

const context = {
  Date: class extends Date { static now() { return clockNow; } },
  setTimeout(fn, ms) { const id = ++seq; timers.set(id, { fn, ms }); return id; },
  clearTimeout(id) { timers.delete(id); },
  addEventListener(k, fn) { listeners[k] = fn; },
  removeEventListener(k) { delete listeners[k]; },
  document: {
    get hidden() { return hidden; },
    addEventListener(k, fn) { docListeners[k] = fn; },
    removeEventListener(k) { delete docListeners[k]; },
  },
  fetch(url) {
    return new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const MC = context.VisualMarketClock;

const flush = () => new Promise(r => setImmediate(r));

/** 应答第 n 个挂起请求 */
async function answer(index, data, ok = true) {
  const r = requests[index];
  assert.ok(r, 'request #' + index + ' should be pending');
  if (ok) r.resolve({ ok: true, json: async () => data });
  else r.resolve({ ok: false, status: 500 });
  await flush();
}
async function fail(index) {
  requests[index].reject(new Error('network down'));
  await flush();
}

function status(over) {
  return Object.assign({
    ok: true,
    time: '2026-09-17 08:50:00',
    server_ms: 1789608191000,
    is_trading_day: true,
    in_session: false,
    session_phase: 'pre',
    is_auction: false,
    quote_live: false,
    next_open_at: '2026-09-17 09:30:00',
    next_open_in_sec: 2400.0,
    next_live_at: '2026-09-17 09:15:00',
    next_live_in_sec: 1500.0,
    next_stream_at: '2026-09-17 09:00:00',
    next_stream_in_sec: 600.0,
    calendar_source: 'xshg',
  }, over);
}

function newClock(over) {
  return new MC.MarketClock(Object.assign({ onChange() {} }, over));
}

(async () => {
  // ── 纯函数 ──────────────────────────────────────────────
  assert.equal(MC.countdownText(409), '06:49');
  assert.equal(MC.countdownText(5025), '1:23:45');
  assert.equal(MC.countdownText(0), '00:00');
  assert.equal(MC.countdownText(59), '00:59');
  assert.equal(MC.countdownText(3600), '1:00:00');
  assert.equal(MC.countdownText(NaN), '00:00');
  assert.equal(MC.countdownText(null), '00:00');
  assert.equal(MC.countdownText(-5), '00:00');

  // 安全网是固定值: 由 next_live_in_sec 推出来的心跳永远比唤醒更早, 会让精确
  // 唤醒变成死代码 —— 这条断言把那个关系钉住。
  assert.equal(MC.safetyMs(), 600000);

  // 唤醒: 相对秒 + 余量, 钳位; 载荷不给数时不下发唤醒
  assert.equal(MC.wakeMs(409), 410500);
  assert.equal(MC.wakeMs(300), 301500);
  assert.equal(MC.wakeMs(0), 60000, '不给正数就退回下界, 不紧循环');
  assert.equal(MC.wakeMs(171000), 600000, '钳上界: 不会比安全网更晚');
  assert.equal(MC.wakeMs(null), null);

  // 浏览器时钟兜底与后端 is_live 同口径: 09:15 起算, 含午休排除
  const at = (y, mo, d, h, mi) => new Date(y, mo - 1, d, h, mi).getTime();
  assert.equal(MC.localSessionFallback(at(2026, 9, 17, 9, 14)), false);
  assert.equal(MC.localSessionFallback(at(2026, 9, 17, 9, 15)), true, '集合竞价算活跃');
  assert.equal(MC.localSessionFallback(at(2026, 9, 17, 11, 30)), true);
  assert.equal(MC.localSessionFallback(at(2026, 9, 17, 11, 31)), false);
  assert.equal(MC.localSessionFallback(at(2026, 9, 17, 13, 0)), true);
  assert.equal(MC.localSessionFallback(at(2026, 9, 17, 15, 0)), true);
  assert.equal(MC.localSessionFallback(at(2026, 9, 17, 15, 1)), false);
  assert.equal(MC.localSessionFallback(at(2026, 9, 19, 10, 0)), false, '周六');

  // ── 侧栏「下一开盘」行 (不重复相位名, 「状态」行已经写了相位) ──
  const st = (over) => Object.assign({ today: '2026-09-17', phase: 'pre' }, over);
  assert.equal(MC.openHintText(st({ phase: 'trading' }), 10), '—',
    '盘中没有"下一次开盘"可言, 给占位符而不是空行');
  assert.equal(MC.openHintText(null, 10), '—');
  assert.equal(
    MC.openHintText(st({ phase: 'auction', nextOpenAt: '2026-09-17 09:30:00', nextOpenInSec: 252 }), 252),
    '09:30 余 04:12',
    '竞价期必须给到 09:30 的倒计时 —— 这正是"09:25 页面不动却没有任何提示"的场景');
  assert.equal(
    MC.openHintText(st({ phase: 'break', nextOpenAt: '2026-09-17 13:00:00', nextOpenInSec: 5025 }), 5025),
    '13:00 余 1:23:45', '「余」点明后半段是剩余时长, 不会被读成第二个钟点');
  assert.equal(
    MC.openHintText(st({ phase: 'pre', nextLiveAt: '2026-09-17 09:15:00', nextLiveInSec: 409 }), 409),
    '09:15 余 06:49');
  assert.equal(
    MC.openHintText(st({ phase: 'closed', nextLiveAt: '2026-09-18 09:15:00', nextLiveInSec: 64800 }), 64800),
    '09-18 09:15',
    '跨日只给 月-日 时:分, 不再叠"下一交易日"前缀 (那是撑爆值区的主因)');
  assert.equal(
    MC.openHintText(st({ phase: 'non_trading', nextLiveAt: '2026-09-21 09:15:00', nextLiveInSec: 171000 }), 171000),
    '09-21 09:15');
  assert.equal(
    MC.openHintText(st({ phase: 'auction', nextOpenAt: null }), 60), '—',
    '载荷缺时刻宁可给占位符, 也不能拼出半截文案');
  // 不知道"今天"就只报日期 (把当天说成隔天更误导), 此时也不叠倒计时
  assert.equal(
    MC.openHintText({ phase: 'break', nextOpenAt: '2026-09-17 13:00:00', nextOpenInSec: 60 }, 60),
    '09-17 13:00');
  // 自相矛盾的载荷 (break 却配明天的开盘) 不叠倒计时: 跨日的相位本来就没有倒计时,
  // 这条守卫让宽度上限成为结构性保证, 而不是靠服务端载荷自洽
  assert.equal(
    MC.openHintText(st({ phase: 'break', nextOpenAt: '2026-09-18 13:00:00', nextOpenInSec: 60 }), 60),
    '09-18 13:00');

  // 悬停说明: 值区放不下的那部分语义都在这里
  assert.equal(MC.openHintDetail(st({ phase: 'trading' })), null, '盘中这行是 —, 没有说明可给');
  assert.equal(MC.openHintDetail(null), null);
  assert.equal(MC.openHintDetail(st({ phase: 'break', nextOpenAt: '2026-09-17 13:00:00' })),
    '09-17 13:00 连续竞价开盘');
  assert.equal(MC.openHintDetail(st({ phase: 'auction', nextOpenAt: '2026-09-17 09:30:00' })),
    '09-17 09:30 连续竞价开盘');
  assert.equal(MC.openHintDetail(st({ phase: 'pre', nextLiveAt: '2026-09-17 09:15:00' })),
    '09-17 09:15 集合竞价开始');
  assert.equal(MC.openHintDetail(st({ phase: 'closed', nextLiveAt: '2026-09-18 09:15:00' })),
    '下一交易日 09-18 09:15 集合竞价开始');
  assert.equal(MC.openHintDetail(st({ phase: 'non_trading', nextLiveAt: '2026-09-21 09:15:00' })),
    '下一交易日 09-21 09:15 集合竞价开始');
  assert.equal(MC.openHintDetail(st({ phase: 'break', nextOpenAt: null })), null);

  // ── 宽度预算: 值区只有 129px ──
  // 面板 204 − 左右 padding 16 − 右边框 1 − 标签列 58 = 129px (手机 150px 面板 → 75px)。
  // 关键: CJK 在 monospace 里约 1em, 不是 0.6em —— 旧文案 9 个汉字实际约 202px, 按
  // 0.6em 估只有 158px, 所以溢出一直没被拦住。这里换真实口径兜住。
  {
    const VALUE_BOX_PX = 129;
    const textW = (s) => [...s].reduce(
      (w, ch) => w + (/[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]/.test(ch) ? 12 : 7.2), 0);
    const CASES = {
      trading: null,
      auction: ['nextOpenAt', '2026-09-17 09:30:00', 252],
      break: ['nextOpenAt', '2026-09-17 13:00:00', 5025],
      pre: ['nextLiveAt', '2026-09-17 09:15:00', 409],
      closed: ['nextLiveAt', '2026-09-18 09:15:00', 64800],
      non_trading: ['nextLiveAt', '2026-09-21 09:15:00', 171000],
    };
    const over = [];
    let worst = 0, worstText = '';
    for (const [phase, spec] of Object.entries(CASES)) {
      const stamp = spec ? { [spec[0]]: spec[1] } : {};
      for (const today of ['2026-09-17', null]) {
        for (const sec of spec ? [spec[2], null] : [null]) {
          const text = MC.openHintText(Object.assign({ today: today, phase: phase }, stamp), sec);
          const px = textW(text);
          if (px > worst) { worst = px; worstText = text; }
          if (px > VALUE_BOX_PX) {
            over.push(`${phase}/today=${today}/sec=${sec} → "${text}" ${Math.round(px)}px`);
          }
        }
      }
    }
    assert.deepEqual(over, [], `值超宽 (值区 ${VALUE_BOX_PX}px)`);
    assert.ok(worst <= VALUE_BOX_PX * 0.9,
      `最坏文案 "${worstText}" 应留 ≥10% 余量 (现 ${Math.round(worst)}px / ${VALUE_BOX_PX}px)`);
  }

  // 倒计时盯哪个时刻: 竞价/午休盯连续竞价开盘, 盘前盯集合竞价开始, 收盘不给
  assert.equal(MC.countdownTargetSec(st({ phase: 'auction', nextOpenInSec: 252, nextLiveInSec: null })), 252);
  assert.equal(MC.countdownTargetSec(st({ phase: 'break', nextOpenInSec: 5025 })), 5025);
  assert.equal(MC.countdownTargetSec(st({ phase: 'pre', nextLiveInSec: 409 })), 409);
  assert.equal(MC.countdownTargetSec(st({ phase: 'closed', nextLiveInSec: 64800 })), null);
  assert.equal(MC.countdownTargetSec(st({ phase: 'non_trading', nextLiveInSec: 171000 })), null);
  assert.equal(MC.countdownTargetSec(st({ phase: 'trading' })), null);
  assert.equal(MC.countdownTargetSec(null), null);
  // 竞价期没有"下一次开始取数", 但文案照样要带倒计时 (盯的是开盘)
  assert.equal(
    MC.openHintText(st({ phase: 'auction', nextOpenAt: '2026-09-17 09:30:00', nextOpenInSec: 60, nextLiveInSec: null }), 60),
    '09:30 余 01:00');

  // ── 冷启动: 一次 ping, 随后按自适应间隔排心跳 ────────────
  reset();
  let changes = [];
  const c1 = newClock({ onChange: (s, kind) => changes.push(kind) });
  c1.start();
  assert.equal(requests.length, 1, '冷启动只发一次 ping');
  assert.equal(requests[0].url, '/api/ping');
  await answer(0, status());
  assert.equal(c1.state.phase, 'pre');
  assert.equal(c1.live(), false);
  assert.deepEqual(changes, ['change']);
  // next_live_in_sec=1500 → 唤醒被钳到 600s, 与安全网同档 → 只挂安全网
  assert.equal(pendingCount(), 1);
  assert.equal(dueTimers()[0][1].ms, 600000, '距开盘还远: 只靠安全网, 不空转');

  // 临近开盘: 唤醒(301.5s) 早于安全网(600s) → 两个都挂, 唤醒先到
  requests.length = 0;
  clockNow += 600000;
  fireEarliest();
  await answer(0, status({ next_live_in_sec: 300 }));
  assert.deepEqual(
    dueTimers().map(([, t]) => t.ms).sort((a, b) => a - b), [301500, 600000]);

  // ── 精确唤醒: 到点主动同步一次, 且不受去抖影响 ──────────
  reset();
  const c2 = newClock({ onChange() {} });
  c2.start();
  await answer(0, status({ next_live_in_sec: 120 }));
  requests.length = 0;
  assert.deepEqual(
    dueTimers().map(([, t]) => t.ms).sort((a, b) => a - b), [121500, 600000]);
  clockNow += 121500;
  fireEarliest();
  assert.equal(requests.length, 1, '唤醒到点必须发出去');
  assert.equal(requests[0].url, '/api/ping');
  await answer(0, status({
    quote_live: true, session_phase: 'auction', in_session: false,
    next_live_in_sec: null, next_live_at: null,
  }));
  assert.equal(c2.live(), true, '服务端说活跃就以它为准 (集合竞价)');

  // ── 连接期零探测 ────────────────────────────────────────
  reset();
  const c3 = newClock({ onChange() {} });
  c3.start();
  await answer(0, status({ quote_live: true, session_phase: 'trading', in_session: true, next_live_in_sec: null }));
  c3.setStreamConnected(true);
  requests.length = 0;
  assert.equal(pendingCount(), 1, '连接期只留一个休眠哨兵定时器');
  clockNow += 600000;
  fireEarliest();
  await flush();
  assert.equal(requests.length, 0, 'SSE 连着时心跳不发任何请求');
  c3.setStreamConnected(false);

  // ── 休眠/节流: 间隙检测立刻补同步 ────────────────────────
  reset();
  const kinds4 = [];
  const c4 = newClock({ onChange: (s, kind) => kinds4.push(kind) });
  c4.start();
  await answer(0, status({ quote_live: true, session_phase: 'trading', in_session: true, next_live_in_sec: null }));
  kinds4.length = 0;
  requests.length = 0;
  clockNow += 60_000 * 45;          // 机器睡了 45 分钟, 哨兵才轮到执行
  fireEarliest();
  await flush();
  assert.equal(requests.length, 1, '丢过时间要立刻补一次权威状态');
  assert.ok(kinds4.includes('resume'), '要通知调用方这是一次恢复');
  await answer(0, status({ quote_live: true, session_phase: 'trading', in_session: true, next_live_in_sec: null }));

  // ── 恢复事件: 隐藏/可见、断网恢复、bfcache ───────────────
  reset();
  const c5 = newClock({ onChange() {} });
  c5.start();
  await answer(0, status());
  requests.length = 0;
  hidden = true;
  docListeners.visibilitychange();
  assert.equal(pendingCount(), 0, '隐藏期间不留定时器 (零探测)');
  assert.equal(requests.length, 0);
  hidden = false;
  docListeners.visibilitychange();
  assert.equal(requests.length, 1, '回到前台立即补一次');
  await answer(0, status());
  requests.length = 0;
  listeners.online();
  assert.equal(requests.length, 1, '断网恢复立即补一次');
  await answer(0, status());
  requests.length = 0;
  listeners.pageshow({ persisted: false });
  assert.equal(requests.length, 0, '普通 pageshow 不重复同步');
  listeners.pageshow({ persisted: true });
  assert.equal(requests.length, 1, 'bfcache 恢复要补');
  await answer(0, status());

  // ── 服务端不可达: 状态不清空 + 固定下界重试 ──────────────
  reset();
  let unreachable = 0;
  const c6 = newClock({ onChange: (s, kind) => { if (kind === 'unreachable') unreachable++; } });
  c6.start();
  await answer(0, status({ session_phase: 'trading', quote_live: true, in_session: true, next_live_in_sec: null }));
  requests.length = 0;
  for (let i = 0; i < 3; i++) {
    clockNow += 60000;
    fireEarliest();
    await fail(i);
  }
  assert.equal(unreachable, 1, '连续失败到阈值只提示一次');
  assert.equal(c6.state.phase, 'trading', '失败不清空已有相位');
  assert.equal(c6.live(), true, '一次抖动不该让页面判成休市');
  assert.equal(dueTimers()[0][1].ms, 60000, '失败期间用下界重试');
  assert.equal(c6.unreachable, true);
  clockNow += 60000;
  fireEarliest();
  await answer(3, status({ session_phase: 'trading', quote_live: true, in_session: true, next_live_in_sec: null }));
  assert.equal(c6.unreachable, false, '恢复后复位');
  assert.equal(c6.failures, 0);

  // ── SSE 相位帧: 与 ping 同源, 直接驱动状态 ───────────────
  reset();
  changes = [];
  const c7 = newClock({ onChange: (s, kind) => changes.push(s.phase + ':' + kind) });
  c7.start();
  await answer(0, status());
  changes.length = 0;
  c7.applyMarketFrame({
    session_phase: 'auction', time: '2026-09-17 09:15:00', quote_live: true, in_session: false,
    is_trading_day: true, next_open_at: '2026-09-17 09:30:00', next_open_in_sec: 900,
    next_live_at: null, next_live_in_sec: null, calendar_source: 'xshg',
  });
  assert.deepEqual(changes, ['auction:change']);
  assert.equal(c7.live(), true);
  assert.equal(c7.state.today, '2026-09-17');
  assert.equal(c7.state.nextOpenInSec, 900,
    '相位帧本身就要带开盘倒计时 (不必再等一次 ping 才显示)');
  assert.equal(
    MC.openHintText(c7.state, 900), '09:30 余 15:00');
  changes.length = 0;
  c7.applyMarketFrame({
    session_phase: 'auction', time: '2026-09-17 09:16:00', quote_live: true, in_session: false,
    is_trading_day: true, next_open_at: '2026-09-17 09:30:00', next_open_in_sec: 840,
    next_live_at: null, next_live_in_sec: null, calendar_source: 'xshg',
  });
  assert.deepEqual(changes, [], '相位没变不重复回调');
  // 兼容别名: 老帧可能用 phase
  c7.applyMarketFrame({ phase: 'trading', time: '2026-09-17 09:30:00', quote_live: true, is_trading_day: true });
  assert.deepEqual(changes, ['trading:change']);
  assert.equal(c7.live(), true);

  // ── 日历降级要能看出来 ──────────────────────────────────
  reset();
  const c8 = newClock({ onChange() {} });
  c8.start();
  await answer(0, status({ calendar_source: 'weekday' }));
  assert.equal(c8.degraded(), true, '缺日历包时前端要能提示');

  // ── 从未同步成功时用浏览器时钟兜底 ──────────────────────
  reset();
  clockNow = at(2026, 9, 17, 9, 20);
  const c9 = newClock({ onChange() {} });
  assert.equal(c9.live(), true, '09:20 集合竞价: 兜底也应判活跃');
  clockNow = at(2026, 9, 17, 12, 0);
  assert.equal(c9.live(), false, '午休不算活跃');

  // ── dispose 后不再发请求, 且 dispose→start 必须能真的重启 ──
  reset();
  const c10 = newClock({ onChange() {} });
  c10.start();
  await answer(0, status());
  c10.dispose();
  assert.equal(pendingCount(), 0);
  requests.length = 0;
  await c10.sync('after-dispose', true);
  assert.equal(requests.length, 0);
  c10.start();                       // 重启: 不能因为 _started 仍是 true 就静默失效
  assert.equal(requests.length, 1, 'dispose 之后再 start 必须重新同步');
  await answer(0, status());
  assert.ok(pendingCount() > 0, '重启后定时器要重新排上');
  c10.dispose();

  // ── onChange 抛异常不能拖垮时钟 (也不该全静默) ──
  reset();
  const warned = [];
  const savedWarn = context.console;
  context.console = { warn: (...a) => warned.push(a.join(' ')) };
  const c11 = newClock({ onChange: () => { throw new Error('render boom'); } });
  c11.start();                          // start() 会触发一次 change
  await answer(0, status());
  assert.equal(c11.state.phase, 'pre', '回调抛异常后状态仍然落盘');
  assert.equal(c11.failures, 0, '回调异常不算同步失败');
  assert.ok(pendingCount() > 0, '定时器照常排上');
  assert.equal(warned.length, 1, '要留下一条 warn, 否则"数据不动"无从查起');
  assert.ok(warned[0].includes('render boom'));
  context.console = savedWarn;
  c11.dispose();

  console.log('market_clock_cases: ok');
})().catch(e => { console.error(e); process.exit(1); });
