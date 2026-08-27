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

  function syncButton(buttonId, auto) {
    var btn = typeof buttonId === 'string' ? document.getElementById(buttonId) : buttonId;
    if (!btn) return;
    var dark = current() === 'dark';
    if (auto) {
      btn.textContent = dark ? '🌙 auto' : '☀️ auto';
      btn.title = '自动模式(点击锁定)';
    } else {
      btn.textContent = dark ? '☀️' : '🌙';
      btn.title = '切换主题';
    }
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
