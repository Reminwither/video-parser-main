#!/usr/bin/env python3
"""
登录 / 注册 / 账户页面的 HTML 渲染（自包含，无模板引擎依赖）

视觉与工作台的 TikHub 风格设计令牌一致：
  浅色 #ffffff 底 / 深色 #17171d 底，indigo 强调色，圆角卡片。
深浅色跟随站点主题键 localStorage['vp-theme-v2']，与主站互通。
"""

import html as _html

# 公共样式与主题脚本（页面间共享）
_COMMON_STYLE = """
:root { color-scheme: light; }
html[data-theme="dark"] { color-scheme: dark; }
* { margin: 0; padding: 0; box-sizing: border-box; }
html[data-theme="light"], html[data-theme="light"] body { --bg: #ffffff; --surface: #ffffff;
  --border: #e4e4e7; --border-strong: #d4d4d8; --text: #1a1a1a; --text-sub: #6b7280;
  --accent: #6366f1; --accent-hover: #4f46e5; --accent-light: #eef2ff; --err: #dc2626; --ok: #16a34a; }
html[data-theme="dark"], html[data-theme="dark"] body { --bg: #17171d; --surface: #17171d;
  --border: #26262e; --border-strong: #3f3f46; --text: #f2f2f0; --text-sub: #9ca3af;
  --accent: #818cf8; --accent-hover: #a5b4fc; --accent-light: #1e1b4b; --err: #f87171; --ok: #4ade80; }
body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  display: flex; align-items: center; justify-content: center; padding: 24px;
  transition: background .2s ease, color .2s ease; }
.card { width: 100%; max-width: 400px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; padding: 36px 32px; box-shadow: 0 8px 24px rgba(0,0,0,.06); }
.brand { display: flex; align-items: center; justify-content: center; gap: 10px;
  margin-bottom: 28px; text-decoration: none; color: var(--text); font-weight: 700; font-size: 20px; }
.brand-logo { width: 34px; height: 34px; border-radius: 9px; background: var(--accent);
  color: #fff; display: flex; align-items: center; justify-content: center; font-size: 15px; font-weight: 800; }
h1 { font-size: 20px; text-align: center; margin-bottom: 6px; letter-spacing: -0.02em; }
.sub { text-align: center; color: var(--text-sub); font-size: 13px; margin-bottom: 24px; }
label { display: block; font-size: 13px; color: var(--text-sub); margin: 14px 0 6px; }
input { width: 100%; padding: 10px 12px; border: 1px solid var(--border-strong);
  border-radius: 10px; background: var(--bg); color: var(--text); font-size: 14px; outline: none;
  transition: border-color .15s ease, box-shadow .15s ease; }
input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.btn { width: 100%; margin-top: 22px; padding: 11px 0; border: none; border-radius: 10px;
  background: var(--accent); color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
  transition: background .15s ease; }
.btn:hover { background: var(--accent-hover); }
.btn:disabled { opacity: .6; cursor: not-allowed; }
.msg { display: none; margin-top: 14px; padding: 10px 12px; border-radius: 10px;
  font-size: 13px; line-height: 1.5; }
.msg.err { display: block; background: color-mix(in srgb, var(--err) 10%, transparent); color: var(--err); }
.msg.ok { display: block; background: color-mix(in srgb, var(--ok) 12%, transparent); color: var(--ok); }
.foot { text-align: center; margin-top: 18px; font-size: 13px; color: var(--text-sub); }
.foot a { color: var(--accent); text-decoration: none; font-weight: 600; }
.foot a:hover { text-decoration: underline; }
.ghost { position: fixed; top: 16px; right: 16px; width: 34px; height: 34px; border-radius: 999px;
  border: 1px solid var(--border-strong); background: transparent; color: var(--text);
  cursor: pointer; font-size: 15px; display: flex; align-items: center; justify-content: center; }
.vp-lang-btn { right: 56px; font-size: 12px; font-weight: 600; min-width: 34px; }
.info-rows { margin: 18px 0; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
.info-rows div { display: flex; justify-content: space-between; padding: 10px 14px;
  font-size: 13px; border-bottom: 1px solid var(--border); }
.info-rows div:last-child { border-bottom: none; }
.info-rows span:first-child { color: var(--text-sub); }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
  background: var(--accent-light); color: var(--accent); font-weight: 600; }
"""

# 主题脚本：跟随主站主题键，无记录时跟系统；?theme= 可显式指定（便于分享/测试）
_THEME_SCRIPT = """
<script>
(function(){
  var t = null;
  try { t = new URLSearchParams(location.search).get("theme"); } catch(e) {}
  if (t !== "dark" && t !== "light") {
    try { t = localStorage.getItem("vp-theme-v2"); } catch(e) { t = null; }
  }
  if (t !== "dark" && t !== "light") {
    t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", t);
})();
function vpToggleTheme(){
  var d = document.documentElement;
  var t = d.getAttribute("data-theme") === "dark" ? "light" : "dark";
  d.setAttribute("data-theme", t);
  try { localStorage.setItem("vp-theme-v2", t); } catch(e) {}
}
function vpShowMsg(id, text){
  var el = document.getElementById(id);
  el.textContent = text; el.className = "msg err";
}
</script>
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)} · VidAI 视频解析工作台</title>
<style>{_COMMON_STYLE}</style>
{_THEME_SCRIPT}
</head>
<body>
<button class="ghost" type="button" onclick="vpToggleTheme()" title="切换深色 / 浅色" aria-label="切换主题">◐</button>
{body}
</body>
</html>"""


def _brand() -> str:
    return """
<a class="brand" href="/"><span class="brand-logo">VA</span>VidAI</a>
"""


def _hidden_next(next_url: str) -> str:
    safe = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/"
    return f'<input type="hidden" name="next" value="{_html.escape(safe, quote=True)}">'


def login_page(next_url: str = "/", error: str = "", allow_register: bool = True) -> str:
    err_html = f'<div class="msg err">{_html.escape(error)}</div>' if error else ""
    reg_line = '<div class="foot">没有账号？<a href="/register">立即注册</a></div>' if allow_register else ""
    body = f"""
<div class="card">
  {_brand()}
  <h1>登录工作台</h1>
  <p class="sub">视频解析 · AI 分析 · 多模态取证</p>
  <form method="post" action="/login">
    {_hidden_next(next_url)}
    <label for="username">用户名</label>
    <input id="username" name="username" type="text" autocomplete="username" required autofocus>
    <label for="password">密码</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button class="btn" type="submit">登 录</button>
    {err_html}
  </form>
  {reg_line}
</div>
"""
    return _page("登录", body)


def register_page(next_url: str = "/", error: str = "") -> str:
    err_html = f'<div class="msg err">{_html.escape(error)}</div>' if error else ""
    body = f"""
<div class="card">
  {_brand()}
  <h1>注册账号</h1>
  <p class="sub">用户名 3-32 位字母/数字/下划线/连字符，密码至少 8 位</p>
  <form method="post" action="/register">
    {_hidden_next(next_url)}
    <label for="username">用户名</label>
    <input id="username" name="username" type="text" autocomplete="username" required autofocus>
    <label for="password">密码</label>
    <input id="password" name="password" type="password" autocomplete="new-password" required minlength="8">
    <label for="password2">确认密码</label>
    <input id="password2" name="password2" type="password" autocomplete="new-password" required minlength="8">
    <button class="btn" type="submit">注 册</button>
    {err_html}
  </form>
  <div class="foot">注册即表示你会遵守<a href="/data-policy">使用与数据说明</a>；请只处理有权使用的视频。</div>
  <div class="foot">已有账号？<a href="/login">直接登录</a></div>
</div>
"""
    return _page("注册", body)


def account_page(user: dict, sessions_active: int = 0, error: str = "", ok: str = "") -> str:
    err_html = f'<div class="msg err">{_html.escape(error)}</div>' if error else ""
    ok_html = f'<div class="msg ok">{_html.escape(ok)}</div>' if ok else ""
    admin_badge = ' <span class="badge" data-i18n="admin">管理员</span>' if user.get("is_admin") else ""
    delete_form = "" if user.get("is_admin") else """
  <form method="post" action="/account/delete" onsubmit="return confirm('确定永久删除此账号吗？')">
    <label for="delete_password">删除账号</label>
    <input id="delete_password" name="password" type="password" autocomplete="current-password" placeholder="输入当前密码" required>
    <input name="confirm" type="text" placeholder="输入‘删除’确认" required>
    <button class="btn" type="submit" style="background:#b91c1c">永久删除账号</button>
  </form>
"""
    body = f"""
<div class="card">
  {_brand()}
  <h1>{_html.escape(user["username"])}{admin_badge}</h1>
  <p class="sub">账户信息与安全设置</p>
  <div class="info-rows">
    <div><span>注册时间</span><span>{_html.escape(str(user.get("created_at") or "-"))}</span></div>
    <div><span>最近登录</span><span>{_html.escape(str(user.get("last_login_at") or "-"))}</span></div>
    <div><span>活跃会话</span><span>{sessions_active} 个</span></div>
  </div>
  <form method="post" action="/account/password">
    <label for="old_password">当前密码</label>
    <input id="old_password" name="old_password" type="password" autocomplete="current-password" required>
    <label for="new_password">新密码（至少 8 位）</label>
    <input id="new_password" name="new_password" type="password" autocomplete="new-password" required minlength="8">
    <button class="btn" type="submit">修改密码</button>
    {err_html}{ok_html}
  </form>
  {delete_form}
  <div class="foot"><a href="/" data-i18n="back">← 返回工作台</a> · <a href="/data-policy">使用与数据说明</a> · <a href="/logout" data-i18n="logout">退出登录</a></div>
</div>
"""
    return _page("账户", body)


def data_policy_page() -> str:
    body = """
<div class="card" style="max-width:680px;line-height:1.7">
  <a class="brand" href="/"><span class="brand-logo">VA</span>VidAI</a>
  <h1>使用与数据说明</h1>
  <p class="sub">公开试用服务 · 更新于 2026 年 9 月 28 日</p>
  <p>请仅提交你有权访问和使用的公开视频链接。来源平台可能限制解析或下载；可用性、画质和结果准确性无法保证。AI 报告仅供参考，请核对原视频。</p>
  <p style="margin-top:14px">本服务保存账号名、加密后的密码、登录会话、访问 IP、浏览器信息和每日操作计数。为完成解析与分析，服务器会暂存视频、封面及报告；AI 分析所需内容会发送给已配置的模型服务商。</p>
  <p style="margin-top:14px">生成文件在超过 48 小时后的每日清理时删除。会话通常有效 7 天；账号数据保留到你在<a href="/account">账户页</a>删除账号。删除后在线账号与会话立即清除，数据库备份最多保留最近 14 份，过期后轮替删除。</p>
  <p style="margin-top:14px">不要提交私人、机密或无权处理的内容。请遵守来源平台的规则以及适用法律。使用本服务即表示你了解以上处理方式。</p>
  <div class="foot"><a href="/">← 返回工作台</a></div>
</div>
"""
    return _page("使用与数据说明", body)
