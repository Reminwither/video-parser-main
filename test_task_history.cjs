// Exercise the shipped polling script without a browser session or real requests.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { JSDOM } = require('jsdom');
const script = readFileSync(`${__dirname}/static/js/task-history.js`, 'utf8');
const fileName = 'vp-u1-测试_1727000000_123456abcdef.md';
const file = { name: fileName, label: '分析报告 · 测试' };
const task = (overrides = {}) => ({ id: 'a', action: 'analysis', platform: '抖音', outcome: 'running', started_at: '2026-10-02T01:00:00Z', percent: 45, message: '整理视频证据', files: [], ...overrides });
const reply = (tasks, user_id = 1) => ({ ok: true, status: 200, json: async () => ({ user_id, tasks }) });
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
function setup(t) {
  const dom = new JSDOM('<a id="vp-task-refresh" href="/account/files">刷新</a><p id="vp-task-status" role="status"></p><div id="vp-tasks" data-user-id="1">上次进度</div><div id="vp-account-files">本人文件</div>', { url: 'https://vidai.example/account/files', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, timers = new Map(), requests = [];
  let timerId = 0;
  w.setTimeout = (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; };
  w.clearTimeout = id => timers.delete(id);
  w.fetch = (url, options) => new Promise((resolve, reject) => {
    requests.push({ url, options, resolve, reject });
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  t.after(() => w.close());
  w.eval(script);
  return { w, requests, timers, el: id => w.document.getElementById(id), fire(ms) {
    const found = [...timers].find(([, timer]) => timer.ms === ms);
    assert.ok(found, `Expected a ${ms}ms timer`);
    timers.delete(found[0]); found[1].fn();
  } };
}

test('running tasks poll; completion provides an owned Unicode file and stops polling', async t => {
  const s = setup(t);
  assert.equal(s.requests[0].url, '/api/tasks');
  assert.equal(s.requests[0].options.credentials, 'same-origin');
  assert.equal(s.requests[0].options.cache, 'no-store');
  s.requests[0].resolve(reply([task()])); await flush();
  assert.equal(s.el('vp-tasks').querySelector('progress').value, 45);
  s.fire(5000);
  s.requests[1].resolve(reply([task({ outcome: 'success', message: '处理完成', files: [file] })])); await flush();
  assert.equal(s.el('vp-tasks').querySelector('progress'), null);
  assert.equal(s.el('vp-tasks').querySelector('a').getAttribute('href'), '/account/files/' + encodeURIComponent(fileName));
  assert.equal(s.timers.size, 0);
});

test('render escapes provider text, ignores unsafe filenames, and retains keyboard focus', async t => {
  const s = setup(t);
  const data = task({ outcome: 'success', message: '<img src=x onerror=bad()>', files: [file, { name: '../../secret', label: 'bad' }] });
  s.requests[0].resolve(reply([data])); await flush();
  assert.equal(s.el('vp-tasks').querySelector('img'), null);
  assert.match(s.el('vp-tasks').textContent, /<img/);
  assert.equal(s.el('vp-tasks').querySelectorAll('a').length, 1);
  s.el('vp-tasks').querySelector('a').focus();
  // A visible-page return triggers a refresh without moving download focus.
  s.w.document.dispatchEvent(new s.w.Event('visibilitychange'));
  s.requests[1].resolve(reply([data])); await flush();
  assert.equal(s.w.document.activeElement.dataset.taskFocus, 'a:' + fileName);
});

test('HTTP errors preserve the last state and retry without overlapping requests', async t => {
  const s = setup(t);
  s.el('vp-task-refresh').click();
  assert.equal(s.requests.length, 1);
  s.requests[0].resolve({ ok: false, status: 503 }); await flush();
  assert.equal(s.el('vp-tasks').textContent, '上次进度');
  assert.match(s.el('vp-task-status').textContent, /已保留/);
  s.fire(5000);
  s.requests[1].resolve(reply([])); await flush();
  assert.match(s.el('vp-tasks').textContent, /还没有/);
  assert.equal(s.timers.size, 0);
});

test('request timeout aborts, keeps previous progress, and permits retry', async t => {
  const s = setup(t);
  s.fire(12000); await flush();
  assert.equal(s.requests[0].options.signal.aborted, true);
  assert.equal(s.el('vp-tasks').textContent, '上次进度');
  s.el('vp-task-refresh').click();
  assert.equal(s.requests.length, 2);
  s.requests[1].resolve(reply([])); await flush();
});

test('expired login clears both private areas and stops polling', async t => {
  const s = setup(t);
  s.requests[0].resolve({ ok: false, status: 401 }); await flush();
  assert.equal(s.el('vp-tasks').textContent, '');
  assert.equal(s.el('vp-account-files').textContent, '');
  assert.match(s.el('vp-task-status').textContent, /登录已过期/);
  assert.equal(s.timers.size, 0);
  s.w.document.dispatchEvent(new s.w.Event('visibilitychange'));
  assert.equal(s.requests.length, 1);
});

test('account switch clears previous private files and never renders the other account', async t => {
  const s = setup(t);
  s.requests[0].resolve(reply([task({ message: 'Other account secret' })], 2)); await flush();
  assert.equal(s.el('vp-tasks').textContent, '');
  assert.equal(s.el('vp-account-files').textContent, '');
  assert.match(s.el('vp-task-status').textContent, /账号已切换/);
  assert.equal(s.timers.size, 0);
});

test('hidden pages pause future polling and resume when visible', async t => {
  const s = setup(t);
  s.requests[0].resolve(reply([task()])); await flush();
  Object.defineProperty(s.w.document, 'hidden', { configurable: true, value: true });
  s.w.document.dispatchEvent(new s.w.Event('visibilitychange'));
  assert.equal(s.timers.size, 0);
  Object.defineProperty(s.w.document, 'hidden', { configurable: true, value: false });
  s.w.document.dispatchEvent(new s.w.Event('visibilitychange'));
  assert.equal(s.requests.length, 2);
  s.requests[1].resolve(reply([])); await flush();
});

test('page departure invalidates late responses; cached-page restoration resumes safely', async t => {
  const s = setup(t);
  s.w.dispatchEvent(new s.w.Event('pagehide')); await flush();
  s.requests[0].resolve({ ok: false, status: 401 }); await flush();
  assert.equal(s.el('vp-account-files').textContent, '本人文件');
  const show = new s.w.Event('pageshow'); Object.defineProperty(show, 'persisted', { value: true });
  s.w.dispatchEvent(show); s.fire(5000);
  assert.equal(s.requests.length, 2);
  s.requests[1].resolve(reply([])); await flush();
});

test('malformed responses keep previous content and recover on the next fetch', async t => {
  const s = setup(t);
  s.requests[0].resolve(reply('broken')); await flush();
  assert.equal(s.el('vp-tasks').textContent, '上次进度');
  s.fire(5000);
  s.requests[1].resolve(reply([task({ outcome: 'success' })])); await flush();
  assert.match(s.el('vp-tasks').textContent, /48 小时/);
});
