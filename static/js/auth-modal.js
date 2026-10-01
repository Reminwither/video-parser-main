(function () {
  'use strict';
  var busy = false;
  var generation = 0;
  var returnFocus = null;
  var previousOverflow = '';
  var errorKey = '';
  var serverError = '';
  var copy = {
    zh: {
      title: '登录工作台', registerTitle: '创建账号', sub: '视频解析 · AI 分析 · 多模态取证',
      username: '用户名', password: '密码', confirm: '确认密码', close: '关闭',
      login: '登录', register: '创建账号', loading: '登录中…', registering: '注册中…',
      noAccount: '没有账号？', signUp: '立即注册', hasAccount: '已有账号？', signIn: '直接登录',
      hint: '用户名需为 3–32 位字母、数字、下划线或连字符；密码至少 8 位。',
      missing: '请输入用户名和密码', mismatch: '两次输入的密码不一致',
      usernameRule: '用户名需为 3–32 位字母、数字、下划线或连字符', passwordRule: '密码至少 8 位',
      network: '网络连接失败，请重试', timeout: '请求超时，请重试', registerTimeout: '请求超时；若账号已创建，请先尝试用此账号登录',
      failed: '操作失败，请稍后重试', badCredentials: '用户名或密码不正确',
      closed: '当前暂未开放注册', registerLimit: '注册次数已达上限，每日北京时间 08:00 重置',
      taken: '用户名已存在', full: '注册名额已满，请稍后再试', throttle: '尝试过于频繁，请 {minutes} 分钟后再试'
    },
    en: {
      title: 'Sign in to workspace', registerTitle: 'Create an account', sub: 'Video parsing · AI analysis · multimodal evidence',
      username: 'Username', password: 'Password', confirm: 'Confirm password', close: 'Close',
      login: 'Sign in', register: 'Create account', loading: 'Signing in…', registering: 'Creating account…',
      noAccount: 'No account? ', signUp: 'Sign up', hasAccount: 'Already have an account? ', signIn: 'Sign in',
      hint: 'Use 3–32 letters, digits, underscores or hyphens for your username. Passwords need at least 8 characters.',
      missing: 'Enter your username and password', mismatch: 'Passwords do not match',
      usernameRule: 'Use 3–32 letters, digits, underscores or hyphens for your username', passwordRule: 'Use a password with at least 8 characters',
      network: 'Connection failed. Please try again.', timeout: 'Request timed out. Please try again.',
      registerTimeout: 'Request timed out. Your account may have been created; try signing in first.',
      failed: 'Unable to complete this request. Please try again later.',
      badCredentials: 'Incorrect username or password', closed: 'Registration is currently closed',
      registerLimit: 'Daily registration limit reached. Resets at 08:00 China Standard Time (UTC+8).',
      taken: 'This username is already taken', full: 'Registration is full. Please try again later.',
      throttle: 'Too many attempts. Please try again in {minutes} minutes.'
    }
  };
  function texts() { return copy[window.__vp_lang] || copy.zh; }
  function el(id) { return document.getElementById(id); }
  function isOpen() { var m = el('vp-login-modal'); return m && m.getAttribute('aria-hidden') === 'false'; }
  function errorText() {
    var d = texts();
    if (errorKey) return d[errorKey];
    if (!serverError) return '';
    var known = { '请输入用户名和密码': 'missing', '两次输入的密码不一致': 'mismatch',
      '密码至少 8 位': 'passwordRule', '用户名需为 3-32 位字母、数字、下划线或连字符': 'usernameRule',
      '用户名或密码不正确': 'badCredentials', '当前暂未开放注册': 'closed',
      '今日注册次数已达上限，请明天再试': 'registerLimit', '注册次数已达上限，每日北京时间 08:00 重置': 'registerLimit', '用户名已存在': 'taken',
      '注册名额已满，请稍后再试': 'full' };
    var minutes = serverError.match(/^尝试过于频繁，请 (\d+) 分钟后再试$/);
    if (minutes) return d.throttle.replace('{minutes}', minutes[1]);
    return known[serverError] ? d[known[serverError]] : (window.__vp_lang === 'en' ? d.failed : serverError);
  }
  function renderError() {
    var msg = el('vp-modal-msg');
    if (msg) { msg.textContent = errorText(); msg.className = 'vp-modal-msg' + (msg.textContent ? ' err' : ''); }
  }
  function clearError() {
    errorKey = serverError = '';
    ['vp-modal-user', 'vp-modal-pass', 'vp-modal-pass2'].forEach(function (id) { var p = el(id); if (p) p.removeAttribute('aria-invalid'); });
    renderError();
  }
  function invalid(key, field) {
    errorKey = key; serverError = ''; renderError();
    var p = el(field); if (p) { p.setAttribute('aria-invalid', 'true'); p.focus(); }
  }
  function renderBusy() {
    var registering = window.__vp_auth_mode === 'register';
    var d = texts();
    var card = document.querySelector('#vp-login-modal .vp-modal-card');
    if (card) card.setAttribute('aria-busy', String(busy));
    ['vp-modal-user', 'vp-modal-pass', 'vp-modal-submit'].forEach(function (id) { var p = el(id); if (p) p.disabled = busy; });
    var p2 = el('vp-modal-pass2'); if (p2) p2.disabled = busy || !registering;
    var btn = el('vp-modal-submit'); if (btn) btn.textContent = busy ? (registering ? d.registering : d.loading) : (registering ? d.register : d.login);
    var toggle = el('vp-modal-toggle');
    if (toggle) { toggle.setAttribute('aria-disabled', String(busy)); toggle.tabIndex = busy ? -1 : 0; }
  }
  function controls(m) {
    return Array.from(m.querySelectorAll('button, input, a[href]')).filter(function (p) {
      return !p.disabled && p.tabIndex >= 0 && !p.closest('[hidden]');
    });
  }
  window.__vp_auth_mode = 'login';
  window.vpBindLoginModal = function () {
    var m = el('vp-login-modal');
    if (!m || m.getAttribute('data-vp-bound') === '1') return m;
    window.__vp_register_enabled = m.getAttribute('data-register-enabled') === 'true';
    el('vp-modal-close').onclick = window.vpCloseLoginModal;
    m.onclick = function (e) { if (e.target === m) window.vpCloseLoginModal(); };
    el('vp-modal-form').onsubmit = function (e) { e.preventDefault(); window.vpSubmitLogin(); };
    el('vp-modal-toggle').onclick = function (e) { e.preventDefault(); if (!busy) window.vpSetAuthMode(window.__vp_auth_mode === 'register' ? 'login' : 'register'); };
    ['vp-modal-user', 'vp-modal-pass', 'vp-modal-pass2'].forEach(function (id) { el(id).addEventListener('input', clearError); });
    m.setAttribute('data-vp-bound', '1');
    window.vpSetAuthMode('login');
    return m;
  };
  window.vpSetAuthMode = function (mode) {
    var m = el('vp-login-modal'); if (!m) return;
    mode = mode === 'register' ? 'register' : 'login';
    if ((mode === 'register' && !window.__vp_register_enabled) || (busy && mode !== window.__vp_auth_mode)) return;
    var changed = mode !== window.__vp_auth_mode;
    window.__vp_auth_mode = mode;
    var registering = mode === 'register';
    var d = texts();
    el('vp-modal-form').action = registering ? '/register' : '/login';
    el('vp-modal-title').textContent = registering ? d.registerTitle : d.title;
    el('vp-modal-sub').textContent = d.sub;
    el('vp-modal-close').setAttribute('aria-label', d.close);
    [['vp-modal-user', 'username'], ['vp-modal-pass', 'password'], ['vp-modal-pass2', 'confirm']].forEach(function (item) {
      el(item[0]).placeholder = d[item[1]];
      m.querySelector('label[for="' + item[0] + '"]').textContent = d[item[1]];
    });
    el('vp-modal-pass').autocomplete = registering ? 'new-password' : 'current-password';
    el('vp-modal-confirm').hidden = !registering;
    el('vp-modal-pass2').required = registering;
    el('vp-modal-register-hint').hidden = !registering;
    el('vp-modal-register-hint').textContent = d.hint;
    ['vp-modal-user', 'vp-modal-pass', 'vp-modal-pass2'].forEach(function (id) {
      if (registering) el(id).setAttribute('aria-describedby', 'vp-modal-register-hint');
      else el(id).removeAttribute('aria-describedby');
    });
    el('vp-modal-foot-pre').textContent = registering ? d.hasAccount : d.noAccount;
    el('vp-modal-toggle').textContent = registering ? d.signIn : d.signUp;
    el('vp-modal-toggle').href = registering ? '/login' : '/register';
    el('vp-modal-toggle').hidden = !window.__vp_register_enabled;
    el('vp-modal-foot-pre').hidden = !window.__vp_register_enabled;
    if (changed) { el('vp-modal-pass2').value = ''; clearError(); }
    renderBusy(); renderError();
  };
  window.vpOpenLoginModal = function () {
    var m = window.vpBindLoginModal(); if (!m) { location.href = '/login'; return; }
    if (!isOpen()) {
      returnFocus = document.activeElement;
      previousOverflow = document.body.style.overflow;
      generation += 1;
      window.vpSetAuthMode('login');
      m.style.display = 'flex'; m.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    }
    (busy ? el('vp-modal-close') : el('vp-modal-user')).focus();
  };
  window.vpCloseLoginModal = function () {
    var m = el('vp-login-modal'); if (!m || !isOpen()) return;
    generation += 1;
    m.style.display = 'none'; m.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = previousOverflow;
    el('vp-modal-pass').value = ''; el('vp-modal-pass2').value = '';
    clearError(); window.__vp_pending = null;
    if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') returnFocus.focus();
    returnFocus = null;
  };
  document.addEventListener('keydown', function (e) {
    if (!isOpen()) return;
    if (e.key === 'Escape') { e.preventDefault(); window.vpCloseLoginModal(); return; }
    if (e.key !== 'Tab') return;
    var m = el('vp-login-modal'); var items = controls(m);
    var first = items[0]; var last = items[items.length - 1];
    if (!first) return;
    if (!m.contains(document.activeElement) || (e.shiftKey && document.activeElement === first) || (!e.shiftKey && document.activeElement === last)) {
      e.preventDefault(); (e.shiftKey ? last : first).focus();
    }
  });
  document.addEventListener('focusin', function (e) {
    var m = el('vp-login-modal');
    if (isOpen() && !m.contains(e.target)) (busy ? el('vp-modal-close') : el('vp-modal-user')).focus();
  });
  window.vpSubmitLogin = function () {
    if (busy || !isOpen()) return;
    var u = el('vp-modal-user').value.trim(); var p = el('vp-modal-pass').value;
    var p2 = el('vp-modal-pass2').value; var registering = window.__vp_auth_mode === 'register';
    clearError();
    if (!u || !p) { invalid('missing', !u ? 'vp-modal-user' : 'vp-modal-pass'); return; }
    if (registering && !/^[A-Za-z0-9_-]{3,32}$/.test(u)) { invalid('usernameRule', 'vp-modal-user'); return; }
    if (registering && p.length < 8) { invalid('passwordRule', 'vp-modal-pass'); return; }
    if (registering && p !== p2) { invalid('mismatch', 'vp-modal-pass2'); return; }
    busy = true; renderBusy(); var submittedGeneration = generation;
    var controller = new AbortController();
    var deadline = setTimeout(function () { controller.abort(); }, 20000);
    return Promise.resolve().then(function () {
      return fetch(registering ? '/api/auth/register' : '/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', signal: controller.signal,
        body: JSON.stringify({ username: u, password: p, password2: p2 })
      });
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok && d.succ, d: d }; });
    }).then(function (o) {
      if (o.ok) {
        window.__vp_logged_in = true;
        var link = el('vp-login-link');
        if (link && o.d.data && o.d.data.username) {
          link.removeAttribute('onclick'); link.href = '/account';
          var dot = document.createElement('span'); dot.className = 'vp-user-dot';
          link.replaceChildren(dot, document.createTextNode(String(o.d.data.username)));
        }
        if (window.vpInitAuthUI) window.vpInitAuthUI();
        if (submittedGeneration !== generation) return;
        var pending = window.__vp_pending;
        window.vpCloseLoginModal();
        if (pending) setTimeout(function () {
          var button = el('vp_btn_' + pending); if (button && window.__vp_logged_in) button.click();
        }, 350);
      } else if (submittedGeneration === generation) {
        serverError = String(o.d.retdesc || ''); errorKey = serverError ? '' : 'failed'; renderError();
      }
    }).catch(function (error) {
      if (submittedGeneration === generation) {
        errorKey = error.name === 'AbortError' ? (registering ? 'registerTimeout' : 'timeout') : 'network';
        serverError = ''; renderError();
      }
    }).finally(function () { clearTimeout(deadline); busy = false; renderBusy(); });
  };
  window.addEventListener('load', window.vpBindLoginModal);
  setTimeout(window.vpBindLoginModal, 800);
  setTimeout(window.vpBindLoginModal, 2500);
})();
