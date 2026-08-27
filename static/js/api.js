/**
 * Shared fetch helper: same-origin JSON + CSRF double-submit header.
 */
(function (global) {
  'use strict';

  function getCookie(name) {
    try {
      var parts = (document.cookie || '').split(';');
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i].trim();
        if (p.indexOf(name + '=') === 0) {
          return decodeURIComponent(p.substring(name.length + 1));
        }
      }
    } catch (e) { /* ignore */ }
    return '';
  }

  /**
   * @param {string} path
   * @param {RequestInit} [options]
   * @param {{ onUnauthorized?: function }} [hooks]
   */
  async function api(path, options, hooks) {
    options = options || {};
    hooks = hooks || {};
    var method = (options.method || 'GET').toUpperCase();
    var headers = Object.assign(
      { 'Content-Type': 'application/json' },
      options.headers || {}
    );
    if (method !== 'GET' && method !== 'HEAD') {
      var csrf = getCookie('csrf_token');
      if (csrf) headers['X-CSRF-Token'] = csrf;
    }
    var opts = Object.assign({ credentials: 'same-origin' }, options, { headers: headers });
    var res = await fetch(path, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { /* empty */ }
    if (res.status === 401 && typeof hooks.onUnauthorized === 'function') {
      hooks.onUnauthorized();
    }
    if (!res.ok) {
      throw new Error((data && data.error) || ('请求失败 (' + res.status + ')'));
    }
    return data;
  }

  global.VisualApi = { getCookie: getCookie, api: api };
})(typeof window !== 'undefined' ? window : this);
