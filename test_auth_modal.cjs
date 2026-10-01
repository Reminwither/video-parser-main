// Behavioral checks run the shipped dialog HTML/JS without contacting production.
// npm ci --ignore-scripts && npm run test:auth
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const script = readFileSync(`${__dirname}/static/js/auth-modal.js`, 'utf8');
const modal = execFileSync(process.platform === 'win32' ? 'python' : 'python3', [
  '-c', 'import auth_pages; print(auth_pages.workspace_auth_modal(True))'
], { cwd: __dirname, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
const theme = execFileSync(process.platform === 'win32' ? 'python' : 'python3', [
  '-c', "import ast; from pathlib import Path; t=ast.parse(Path('app.py').read_text(encoding='utf-8')); print(next(ast.literal_eval(n.value) for n in t.body if isinstance(n,ast.Assign) and any(isinstance(a,ast.Name) and a.id=='THEME_SCRIPT' for a in n.targets)))"
], { cwd: __dirname, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } }).replace(/^\s*<script>\s*|\s*<\/script>\s*$/g, '');

function setup(t, { language = 'zh', enabled = true, fetch, lateMount = false } = {}) {
  const html = enabled ? modal : modal.replace('data-register-enabled="true"', 'data-register-enabled="false"');
  const dom = new JSDOM(`<button id="opener">Sign in</button><a id="vp-login-link" href="/login">Sign in</a><button id="vp_btn_parse">Parse</button>${lateMount ? '' : html}`, {
    url: 'https://vidai.example/', runScripts: 'outside-only'
  });
  const w = dom.window;
  w.__vp_lang = language;
  w.fetch = fetch || (() => { throw new Error('Unexpected network request'); });
  w.vpInitAuthUI = () => {};
  w.eval(script);
  if (lateMount) w.document.body.insertAdjacentHTML('beforeend', html);
  w.vpBindLoginModal();
  t.after(() => w.close());
  const el = id => w.document.getElementById(id);
  el('opener').focus();
  w.vpOpenLoginModal();
  return { w, el };
}
function fill(el, { username = 'tester', password = 'test-password', confirm = password } = {}) {
  el('vp-modal-user').value = username;
  el('vp-modal-pass').value = password;
  el('vp-modal-pass2').value = confirm;
}
function press(w, key, shiftKey = false) {
  w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true }));
}
function reply(succ, retdesc = '') { return { ok: succ, json: async () => ({ succ, retdesc, data: succ ? { username: 'tester' } : undefined }) }; }

test('focus stays inside a labeled dialog; Escape restores focus, scroll and clears passwords', t => {
  const { w, el } = setup(t, { lateMount: true });
  assert.equal(el('vp-login-modal').getAttribute('aria-hidden'), 'false');
  const card = w.document.querySelector('[role="dialog"]');
  assert.equal(card.getAttribute('aria-modal'), 'true');
  assert.equal(el('vp-modal-form').method, 'post');
  assert.ok(el(card.getAttribute('aria-labelledby')).textContent);
  for (const id of ['vp-modal-user', 'vp-modal-pass', 'vp-modal-pass2']) assert.ok(w.document.querySelector(`label[for="${id}"]`));
  assert.equal(w.document.activeElement, el('vp-modal-user'));
  el('vp-modal-toggle').focus(); press(w, 'Tab');
  assert.equal(w.document.activeElement, el('vp-modal-close'));
  press(w, 'Tab', true);
  assert.equal(w.document.activeElement, el('vp-modal-toggle'));
  el('opener').focus();
  assert.equal(w.document.activeElement, el('vp-modal-user'));
  fill(el); w.__vp_pending = 'parse';
  assert.equal(w.document.body.style.overflow, 'hidden');
  press(w, 'Escape');
  assert.equal(el('vp-login-modal').getAttribute('aria-hidden'), 'true');
  assert.equal(w.document.activeElement, el('opener'));
  assert.equal(w.document.body.style.overflow, '');
  assert.equal(el('vp-modal-pass').value, '');
  assert.equal(el('vp-modal-pass2').value, '');
  assert.equal(w.__vp_pending, null);
});

test('sign-up stays on the page and supports language changes and closed registration', t => {
  const { w, el } = setup(t);
  const before = w.location.href;
  el('vp-modal-toggle').click();
  assert.equal(w.location.href, before);
  assert.equal(w.__vp_auth_mode, 'register');
  assert.equal(el('vp-modal-form').action, 'https://vidai.example/register');
  assert.equal(el('vp-modal-confirm').hidden, false);
  assert.equal(el('vp-modal-pass2').disabled, false);
  assert.equal(el('vp-modal-pass').autocomplete, 'new-password');
  w.__vp_lang = 'en'; w.vpSetAuthMode('register');
  assert.equal(el('vp-modal-title').textContent, 'Create an account');
  assert.equal(el('vp-modal-pass2').placeholder, 'Confirm password');
  el('vp-modal-toggle').click();
  assert.equal(el('vp-modal-confirm').hidden, true);
  assert.equal(el('vp-modal-pass2').disabled, true);
  const closed = setup(t, { enabled: false });
  closed.w.vpSetAuthMode('register');
  assert.equal(closed.w.__vp_auth_mode, 'login');
  assert.equal(closed.el('vp-modal-toggle').hidden, true);
});

test('invalid registration is explained before a request and editing clears field errors', t => {
  const { w, el } = setup(t, { language: 'en' });
  w.vpSetAuthMode('register');
  fill(el, { username: 'x' }); w.vpSubmitLogin();
  assert.equal(el('vp-modal-user').getAttribute('aria-invalid'), 'true');
  fill(el, { password: 'short' }); w.vpSubmitLogin();
  assert.equal(w.document.activeElement, el('vp-modal-pass'));
  fill(el, { confirm: 'different-password' }); w.vpSubmitLogin();
  assert.equal(el('vp-modal-msg').textContent, 'Passwords do not match');
  assert.equal(w.document.activeElement, el('vp-modal-pass2'));
  el('vp-modal-pass2').dispatchEvent(new w.Event('input', { bubbles: true }));
  assert.equal(el('vp-modal-msg').textContent, '');
  assert.equal(el('vp-modal-pass2').hasAttribute('aria-invalid'), false);
});

test('duplicate submissions and mode changes are blocked while the request is pending', async t => {
  let finish, calls = 0, sent;
  const { w, el } = setup(t, { fetch: (url, options) => {
    calls++; sent = { url, options }; return new Promise(resolve => { finish = resolve; });
  } });
  fill(el);
  const request = w.vpSubmitLogin();
  w.vpSubmitLogin();
  w.vpSetAuthMode('register');
  await Promise.resolve();
  assert.equal(calls, 1);
  assert.equal(w.__vp_auth_mode, 'login');
  assert.equal(el('vp-modal-submit').disabled, true);
  assert.equal(sent.url, '/api/auth/login');
  assert.equal(sent.options.credentials, 'same-origin');
  assert.equal(JSON.parse(sent.options.body).username, 'tester');
  finish(reply(false, '用户名或密码不正确')); await request;
  assert.equal(el('vp-modal-submit').disabled, false);
  assert.equal(el('vp-modal-msg').textContent, '用户名或密码不正确');
});

test('server errors and network failures are readable in English and allow a retry', async t => {
  const { w, el } = setup(t, { language: 'en', fetch: async () => reply(false, '尝试过于频繁，请 4 分钟后再试') });
  fill(el); await w.vpSubmitLogin();
  assert.match(el('vp-modal-msg').textContent, /4 minutes/);
  w.fetch = async () => { throw new Error('offline'); };
  await w.vpSubmitLogin();
  assert.equal(el('vp-modal-msg').textContent, 'Connection failed. Please try again.');
  assert.equal(el('vp-modal-submit').disabled, false);
});

test('closing a pending request never resumes an old action or closes a newly opened dialog', async t => {
  let finish, resumed = 0, synced = 0;
  const { w, el } = setup(t, { fetch: () => new Promise(resolve => { finish = resolve; }) });
  w.vpInitAuthUI = () => { synced++; };
  el('vp_btn_parse').onclick = () => { resumed++; };
  fill(el); w.__vp_pending = 'parse';
  const request = w.vpSubmitLogin(); await Promise.resolve();
  w.vpCloseLoginModal(); w.vpOpenLoginModal();
  finish(reply(true)); await request;
  await new Promise(resolve => setTimeout(resolve, 380));
  assert.equal(resumed, 0);
  assert.equal(synced, 1);
  assert.equal(el('vp-login-modal').getAttribute('aria-hidden'), 'false');
  assert.equal(el('vp-modal-submit').disabled, false);
});

test('a successful sign-up resumes the requested action once after the modal closes', async t => {
  let resumed = 0, destination;
  const { w, el } = setup(t, { fetch: async url => { destination = url; return reply(true); } });
  w.vpSetAuthMode('register'); fill(el); w.__vp_pending = 'parse';
  el('vp_btn_parse').onclick = () => { resumed++; };
  await w.vpSubmitLogin();
  await new Promise(resolve => setTimeout(resolve, 380));
  assert.equal(destination, '/api/auth/register');
  assert.equal(w.__vp_logged_in, true);
  assert.equal(el('vp-login-link').href, 'https://vidai.example/account');
  assert.equal(el('vp-login-link').textContent, 'tester');
  assert.equal(resumed, 1);
  assert.equal(el('vp-login-modal').getAttribute('aria-hidden'), 'true');
  assert.equal(el('vp-modal-pass').value, '');
});

test('a timed-out sign-up stops waiting and explains how to recover an account', async t => {
  let deadline;
  const { w, el } = setup(t, { language: 'en', fetch: (_, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
  }) });
  const original = w.setTimeout.bind(w);
  w.setTimeout = (fn, ms) => { if (ms === 20000) { deadline = fn; return 123456; } return original(fn, ms); };
  w.vpSetAuthMode('register'); fill(el);
  const request = w.vpSubmitLogin(); await Promise.resolve();
  deadline(); await request;
  assert.match(el('vp-modal-msg').textContent, /try signing in first/);
  assert.equal(el('vp-modal-submit').disabled, false);
});

test('the shipped theme and deferred dialog cooperate after asynchronous HTML mounting', async t => {
  const dom = new JSDOM('<body></body>', { url: 'https://vidai.example/', runScripts: 'outside-only' });
  const w = dom.window;
  t.after(() => w.close());
  w.XMLHttpRequest = class { open() {} send() {} };
  w.eval(theme);
  w.eval(script);
  w.document.body.insertAdjacentHTML('beforeend', modal);
  w.vpBindLoginModal();
  w.vpApplyLang('en');
  w.vpOpenLoginModal();
  assert.equal(w.document.getElementById('vp-modal-title').textContent, 'Sign in to workspace');
  w.vpSetAuthMode('register');
  w.vpToggleLang();
  assert.equal(w.document.getElementById('vp-modal-title').textContent, '创建账号');
  assert.equal(w.document.getElementById('vp-modal-confirm').hidden, false);
  w.document.getElementById('vp-modal-form').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(w.document.getElementById('vp-modal-msg').textContent, '请输入用户名和密码');
});
