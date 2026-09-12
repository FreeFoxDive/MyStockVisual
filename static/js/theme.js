/**
 * Shared light/dark theme for Visual pages.
 * Storage key: visual-theme JSON { theme: 'light'|'dark', auto: boolean }
 * Legacy key: trades-theme plain 'light'|'dark' (read/write for compat)
 */
(function (global) {
  'use strict';

  var KEY = 'visual-theme';
  var LEGACY_KEY = 'trades-theme';

  function load() {
    var theme = null;
    var auto = false;
    try {
      var shared = localStorage.getItem(KEY);
      if (shared) {
        var st = JSON.parse(shared);
        if (st && (st.theme === 'dark' || st.theme === 'light')) {
          theme = st.theme;
          auto = st.auto === true;
        }
      }
    } catch (e) { /* ignore */ }
    if (!theme) {
      try {
        var legacy = localStorage.getItem(LEGACY_KEY);
        if (legacy === 'dark' || legacy === 'light') theme = legacy;
      } catch (e2) { /* ignore */ }
    }
    return { theme: theme || 'light', auto: auto };
  }

  function save(theme, auto) {
    try {
      localStorage.setItem(KEY, JSON.stringify({ theme: theme, auto: !!auto }));
      localStorage.setItem(LEGACY_KEY, theme);
    } catch (e) { /* ignore */ }
  }

  function current() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function apply(theme, opts) {
    opts = opts || {};
    theme = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', theme);
    if (opts.buttonId) syncButton(opts.buttonId, opts.auto);
    if (opts.onChange) opts.onChange(theme);
    return theme;
  }

  var ICON_SUN = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  var ICON_MOON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function syncButton(buttonId, auto) {
    var btn = typeof buttonId === 'string' ? document.getElementById(buttonId) : buttonId;
    if (!btn) return;
    var dark = current() === 'dark';
    // 图标展示"点击后将切换到"的主题 (与旧 emoji 语义一致): 暗色显示太阳, 亮色显示月亮
    btn.innerHTML = (dark ? ICON_SUN : ICON_MOON)
      + (auto ? '<span class="theme-auto">auto</span>' : '');
    btn.title = auto ? '自动模式(点击锁定)' : '切换主题';
  }

  function toggle(opts) {
    opts = opts || {};
    var next = current() === 'dark' ? 'light' : 'dark';
    apply(next, opts);
    save(next, false);
    return next;
  }

  /** Simple pages: load stored theme, wire toggle on button. */
  function init(opts) {
    opts = opts || {};
    var stored = load();
    apply(stored.theme, { buttonId: opts.buttonId, auto: false });
    return stored;
  }

  global.VisualTheme = {
    KEY: KEY,
    LEGACY_KEY: LEGACY_KEY,
    load: load,
    save: save,
    current: current,
    apply: apply,
    syncButton: syncButton,
    toggle: toggle,
    init: init,
  };

  // Global helpers used by inline onclick="toggleTheme()"
  global.toggleTheme = function () {
    var btn = document.getElementById('btn-theme')
      || document.getElementById('theme-btn')
      || document.querySelector('#toolbar button[title*="主题"]');
    return VisualTheme.toggle({ buttonId: btn });
  };
})(typeof window !== 'undefined' ? window : this);
