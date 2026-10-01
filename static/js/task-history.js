(function () {
  'use strict';
  var root = document.getElementById('vp-tasks');
  if (!root) return;
  var status = document.getElementById('vp-task-status');
  var refresh = document.getElementById('vp-task-refresh');
  var files = document.getElementById('vp-account-files');
  var timer, busy = false, generation = 0, stopped = false, activeController;
  function node(tag, text) { var n = document.createElement(tag); if (text) n.textContent = text; return n; }
  function clearPrivate() { root.replaceChildren(); if (files) files.replaceChildren(); }
  function render(tasks) {
    var focusId = document.activeElement && document.activeElement.dataset.taskFocus;
    var frag = document.createDocumentFragment();
    if (!tasks.length) frag.append(node('p', '还没有进入处理阶段的任务。排队或加载视频时，请保持工作台打开。'));
    tasks.forEach(function (task) {
      var card = node('article'); card.className = 'task-card';
      card.style.cssText = 'padding:14px;margin:12px 0;border:1px solid var(--border);border-radius:12px';
      card.append(node('strong', (task.action === 'analysis' ? 'AI 分析' : '语音转写') + ' · ' + task.platform));
      var when = node('p', task.started_at.replace('T', ' ').replace('Z', ' UTC'));
      when.style.cssText = 'font-size:12px;color:var(--text-sub);margin:7px 0'; card.append(when);
      card.append(node('p', task.message));
      if (task.outcome === 'running') {
        var progress = node('progress'); progress.max = 100; progress.value = Math.max(0, Math.min(99, Number(task.percent) || 0));
        progress.setAttribute('aria-label', '阶段进度'); progress.style.cssText = 'width:100%;margin-top:10px'; card.append(progress);
      }
      var links = node('p'); links.style.cssText = 'font-size:13px;margin-top:10px';
      (task.files || []).forEach(function (file) {
        if (!/^vp-u[1-9][0-9]*-[\p{L}\p{N}_]{1,30}_\d+_[0-9a-f]{12}(?:_asr\.(?:txt|srt)|\.md)$/u.test(file.name)) return;
        if (links.childNodes.length) links.append(document.createTextNode(' · '));
        var a = node('a', '下载' + file.label.split(' · ')[0]);
        a.href = '/account/files/' + encodeURIComponent(file.name); a.dataset.taskFocus = task.id + ':' + file.name; links.append(a);
      });
      if (task.outcome === 'success' && !links.childNodes.length) links.textContent = '此处暂未找到对应文件，请查看下方列表。生成文件超过 48 小时会自动清理。';
      card.append(links); frag.append(card);
    });
    root.replaceChildren(frag);
    if (focusId) root.querySelectorAll('[data-task-focus]').forEach(function (el) { if (el.dataset.taskFocus === focusId) el.focus({preventScroll: true}); });
  }
  function schedule(active) { clearTimeout(timer); if (active && !stopped && !document.hidden) timer = setTimeout(load, 5000); }
  function load() {
    if (stopped || document.hidden) return;
    if (busy) { schedule(true); return; }
    busy = true; var serial = ++generation;
    var controller = new AbortController(); activeController = controller;
    var deadline = setTimeout(function () { controller.abort(); }, 12000);
    status.textContent = '正在刷新进度…';
    fetch('/api/tasks', {credentials: 'same-origin', cache: 'no-store', signal: controller.signal}).then(function (response) {
      if (serial !== generation || stopped) return null;
      if (response.status === 401) { clearPrivate(); stopped = true; status.textContent = '登录已过期，请重新登录后刷新页面。'; return null; }
      if (!response.ok) throw new Error('request failed');
      return response.json();
    }).then(function (data) {
      if (!data || serial !== generation) return;
      if (String(data.user_id) !== root.dataset.userId) { clearPrivate(); stopped = true; status.textContent = '账号已切换，请刷新页面。'; return; }
      if (!Array.isArray(data.tasks)) throw new Error('invalid response');
      render(data.tasks); status.textContent = '进度已更新。任务处理期间每 5 秒自动刷新，切到其他页面时暂停。';
      schedule(data.tasks.some(function (task) { return task.outcome === 'running'; }));
    }).catch(function () { if (serial === generation) { status.textContent = '暂时无法刷新，已保留上次进度。请稍后重试。'; schedule(true); } })
      .finally(function () { clearTimeout(deadline); busy = false; if (activeController === controller) activeController = null; });
  }
  refresh.addEventListener('click', function (event) { if (!stopped) { event.preventDefault(); clearTimeout(timer); load(); } });
  document.addEventListener('visibilitychange', function () { clearTimeout(timer); if (!document.hidden) load(); });
  window.addEventListener('pagehide', function () { stopped = true; ++generation; clearTimeout(timer); if (activeController) activeController.abort(); });
  window.addEventListener('pageshow', function (event) { if (event.persisted) { stopped = false; schedule(true); } });
  load();
})();
