/* Drawing gestures own the native event before ECharts/zrender sees it. */
(function(root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.DrawInput = factory();
})(typeof self !== 'undefined' ? self : this, function() {
  'use strict';
  function bind(el, api) {
    let active = null, suppressUntil = 0;
    const listeners = [];
    const on = (type, fn) => {
      el.addEventListener(type, fn, {capture: true, passive: false});
      listeners.push([type, fn]);
    };
    const stop = e => { e.preventDefault(); e.stopImmediatePropagation(); };
    const eventAt = e => {
      const r = el.getBoundingClientRect();
      return {offsetX: (e.clientX - r.left) * el.clientWidth / r.width,
        offsetY: (e.clientY - r.top) * el.clientHeight / r.height, event: e};
    };
    const release = id => {
      try {
        if (el.hasPointerCapture(id)) el.releasePointerCapture(id);
      } catch (err) { /* pointer already gone */ }
    };
    function cancel(opts) {
      if (!active) return;
      const old = active;
      active = null;
      // 第二指要把后续 touch 交给图表做捏合，不能再挡 500ms。
      suppressUntil = opts && opts.handoff ? 0 : Date.now() + 500;
      api.cancel();
      release(old.id);
    }
    on('pointerdown', e => {
      if (active && e.pointerId !== active.id) {
        cancel({handoff: true}); // 取消未确认的落点，放行捏合。
        return;
      }
      if (!api.enabled() || e.button !== 0 || e.isPrimary === false) return;
      const ev = eventAt(e);
      if (!api.owns(ev)) return;
      try { el.setPointerCapture(e.pointerId); }
      catch (err) { return; } // InvalidPointerId: 不接管，留给图表。
      stop(e);
      active = {id: e.pointerId, key: api.key(), deferred: e.pointerType === 'touch' && api.creating()};
      suppressUntil = Date.now() + 500;
      if (active.deferred) api.move(ev);
      else api.down(ev);
    });
    on('pointermove', e => {
      if (!active) return;
      stop(e);
      if (e.pointerId !== active.id) return;
      if (!api.enabled() || api.key() !== active.key) { cancel(); return; }
      api.move(eventAt(e));
    });
    on('pointerup', e => {
      if (!active) return;
      stop(e);
      if (e.pointerId !== active.id) return;
      if (!api.enabled() || api.key() !== active.key) { cancel(); return; }
      const old = active;
      const ev = eventAt(e);
      api.move(ev);
      if (old.deferred) api.down(ev); // Touch can refine the point until release.
      active = null; // 先结束手势，up 里切工具时 cancel 才是空操作。
      suppressUntil = Date.now() + 500;
      release(old.id);
      api.up(ev, old.deferred);
    });
    on('pointercancel', e => { if (active && e.pointerId === active.id) { stop(e); cancel(); } });
    on('lostpointercapture', e => { if (active && e.pointerId === active.id) cancel(); });
    // zrender listens to mouse/touch as well as pointer events on some browsers.
    // Block their compatibility stream so one gesture cannot create two points.
    // dblclick 不是 pointer 的重复事件：折线结束、水平线改价都靠它，不能挡。
    ['mousedown', 'mousemove', 'mouseup', 'click', 'touchstart', 'touchmove', 'touchend', 'touchcancel'].forEach(type => {
      on(type, e => { if (active || Date.now() < suppressUntil) stop(e); });
    });
    return {cancel, dispose() { cancel(); listeners.forEach(([type, fn]) => el.removeEventListener(type, fn, true)); }};
  }
  return {bind};
});
