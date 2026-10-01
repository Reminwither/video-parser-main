#!/usr/bin/env python3
"""
登录 / 注册 / 账户页面的 HTML 渲染（自包含，无模板引擎依赖）

视觉与工作台的 TikHub 风格设计令牌一致：
  浅色 #ffffff 底 / 深色 #17171d 底，indigo 强调色，圆角卡片。
深浅色跟随站点主题键 localStorage['vp-theme-v2']，与主站互通。
"""

import html as _html


def workspace_auth_modal(register_enabled: bool) -> str:
    """Accessible form used by the workspace's sign-in/sign-up dialog."""
    return f"""
    <div id="vp-login-modal" class="vp-modal-mask" aria-hidden="true" data-register-enabled="{str(register_enabled).lower()}">
      <div class="vp-modal-card" role="dialog" aria-modal="true" aria-labelledby="vp-modal-title" aria-describedby="vp-modal-sub" tabindex="-1">
        <button id="vp-modal-close" class="vp-modal-x" type="button" aria-label="关闭">&times;</button>
        <div class="vp-modal-brand"><span class="vp-modal-logo">VA</span>VidAI</div>
        <h2 id="vp-modal-title" class="vp-modal-title">登录工作台</h2>
        <p id="vp-modal-sub" class="vp-modal-sub">视频解析 · AI 分析 · 多模态取证</p>
        <form id="vp-modal-form" method="post" action="/login" novalidate>
          <label for="vp-modal-user" class="vp-modal-label">用户名</label>
          <input id="vp-modal-user" name="username" class="vp-modal-input" type="text" placeholder="用户名" autocomplete="username" autocapitalize="none" spellcheck="false" required />
          <label for="vp-modal-pass" class="vp-modal-label">密码</label>
          <input id="vp-modal-pass" name="password" class="vp-modal-input" type="password" placeholder="密码" autocomplete="current-password" required />
          <div id="vp-modal-confirm" hidden>
            <label for="vp-modal-pass2" class="vp-modal-label">确认密码</label>
            <input id="vp-modal-pass2" name="password2" class="vp-modal-input" type="password" placeholder="确认密码" autocomplete="new-password" disabled />
          </div>
          <p id="vp-modal-register-hint" class="vp-modal-hint" hidden>用户名需为 3–32 位字母、数字、下划线或连字符；密码至少 8 位。</p>
          <button id="vp-modal-submit" class="vp-modal-btn" type="submit">登录</button>
          <div id="vp-modal-msg" class="vp-modal-msg" role="alert" aria-atomic="true"></div>
        </form>
        <div class="vp-modal-foot"><span id="vp-modal-foot-pre">没有账号？</span><a id="vp-modal-toggle" href="/register">立即注册</a></div>
      </div>
    </div>
    """

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


def account_page(user: dict, sessions_active: int = 0, error: str = "", ok: str = "", quotas: dict | None = None) -> str:
    err_html = f'<div class="msg err">{_html.escape(error)}</div>' if error else ""
    ok_html = f'<div class="msg ok">{_html.escape(ok)}</div>' if ok else ""
    admin_badge = ' <span class="badge" data-i18n="admin">管理员</span>' if user.get("is_admin") else ""
    quota_html = ""
    if quotas:
        rows = ""
        for item in quotas["items"]:
            label = "上传并使用新视频" if item["action"] == "upload" else item["label"]
            details = ""
            for stage in item.get("stages", []):
                details += (
                    '<small style="display:block;color:var(--text-sub);line-height:1.6">'
                    f"{_html.escape(stage['label'])}：剩余 {int(stage['remaining'])} / {int(item['limit'])} 次"
                    + (" · 本站今日额度已满" if not stage["site_available"] else "") + "</small>"
                )
            rows += (
                f"<div><span>{_html.escape(label)}</span><span>剩余 {int(item['remaining'])} / {int(item['limit'])} 次"
                + (" · 本站今日额度已满" if not item["site_available"] else "") + details + "</span></div>"
            )
        quota_html = f"""
  <section style="margin:20px 0" aria-labelledby="quota-title">
    <h2 id="quota-title" style="font-size:16px;margin-bottom:10px">当前试用额度</h2>
    <div class="info-rows" style="font-size:13px">{rows}</div>
    <p style="font-size:12px;color:var(--text-sub);line-height:1.6;margin-top:10px">{_html.escape(quotas['reset_label'])}重置。上传并使用新视频需要两个步骤均有剩余次数；已上传的视频可按“使用已上传视频”的剩余次数继续使用。任务受理后失败也可能计次；剩余次数还受全站额度与并发限制。重复读取这些信息不会扣除额度。</p>
  </section>"""
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
  <div class="foot"><a href="/account/files">最近的分析报告与转写文件 →</a></div>
  {quota_html}
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


def account_files_page(user: dict, files: list[dict], csrf_token: str, error: str = "", ok: str = "") -> str:
    err_html = f'<div class="msg err">{_html.escape(error)}</div>' if error else ""
    ok_html = f'<div class="msg ok">{_html.escape(ok)}</div>' if ok else ""
    if files:
        rows = []
        for item in files:
            name = _html.escape(item["name"], quote=True)
            label = _html.escape(item["label"])
            size = _html.escape(item["size"])
            age = _html.escape(item["modified"])
            preview = (f'<a href="/account/files/{name}/preview">在线查看</a> · '
                       if item["name"].endswith((".md", ".txt")) else "")
            rows.append(f"""
  <div class="info-rows" style="margin:12px 0;padding:12px;border:1px solid #263247;border-radius:12px">
    <div><span>{label}</span><span>{size} · {age}</span></div>
    <div style="flex-wrap:wrap;gap:8px"><span>{preview}<a href="/account/files/{name}">下载文件</a></span>
      <form method="post" action="/account/files/delete" style="display:inline;margin-left:12px" onsubmit="return confirm('确定删除这个文件吗？')">
        <input type="hidden" name="csrf_token" value="{_html.escape(csrf_token, quote=True)}">
        <input type="hidden" name="name" value="{name}">
        <button type="submit" style="background:none;border:0;color:#f87171;cursor:pointer">删除</button>
      </form></div>
  </div>""")
        file_rows = "".join(rows)
    else:
        file_rows = '<p class="sub">这里还没有报告或转写文件。生成后会在此显示，文件超过 48 小时会自动清理。</p>'
    body = f"""
<div class="card" style="max-width:760px">
  {_brand()}
  <h1>我的分析文件</h1>
  <p class="sub">仅显示当前账号生成的文件，最多展示最近 100 个。文件在超过 48 小时后的每日清理时删除。</p>
  {err_html}{ok_html}{file_rows}
  <div class="foot"><a href="/account">← 账户设置</a> · <a href="/">返回工作台</a></div>
</div>
"""
    return _page("我的分析文件", body)


def account_file_preview_page(name: str, label: str, content: str, truncated: bool,
                              csrf_token: str = "", feedback_rating: int | None = None) -> str:
    safe_name = _html.escape(name, quote=True)
    safe_label = _html.escape(label)
    safe_content = _html.escape(content)
    note = '<p class="sub">文件较长，网页仅显示前 256 KB。下载后可查看完整内容。</p>' if truncated else ""
    feedback = ""
    if name.endswith(".md") and csrf_token:
        rating_text = {1: "已评价：有帮助", -1: "已评价：需改进"}.get(feedback_rating, "")
        feedback = f"""
  <div style="margin-top:22px;padding-top:18px;border-top:1px solid var(--border)">
    <p style="font-size:14px;font-weight:600">这份报告有帮助吗？</p>
    <p style="font-size:12px;color:var(--text-sub);margin:7px 0">只记录选择，不收集视频内容或评论。可以随时修改评价。</p>
    <form method="post" action="/account/files/feedback" style="display:flex;flex-wrap:wrap;gap:10px;margin-top:12px">
      <input type="hidden" name="csrf_token" value="{_html.escape(csrf_token, quote=True)}">
      <input type="hidden" name="name" value="{safe_name}">
      <button type="submit" name="rating" value="1" style="padding:9px 14px;border:1px solid var(--border-strong);border-radius:9px;background:var(--accent-light);color:var(--accent);cursor:pointer">有帮助</button>
      <button type="submit" name="rating" value="-1" style="padding:9px 14px;border:1px solid var(--border-strong);border-radius:9px;background:var(--surface);color:var(--text);cursor:pointer">需改进</button>
    </form>
    <p style="font-size:12px;color:var(--text-sub);margin-top:10px">{rating_text}</p>
  </div>"""
    body = f"""
<div class="card" style="max-width:960px">
  {_brand()}
  <h1>{safe_label}</h1>
  {note}
  <div style="display:flex;justify-content:center;gap:18px;margin:18px 0;font-size:14px">
    <a href="/account/files/{safe_name}">下载完整文件</a>
    <a href="/account/files">返回我的分析文件</a>
  </div>
  <pre style="white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;font-size:14px;max-height:70vh;overflow:auto;padding:18px;border:1px solid var(--border);border-radius:12px">{safe_content}</pre>
  {feedback}
</div>
"""
    return _page(safe_label, body)


def sample_report_page() -> str:
    """Public, clearly fictional example of the evidence-first report format."""
    body = """
<style>
body { align-items: flex-start; }
.sample { max-width: 900px; margin: 20px auto; line-height: 1.7; }
.sample h2 { font-size: 17px; margin: 26px 0 10px; }
.sample p { margin: 10px 0; font-size: 14px; }
.sample table { width: 100%; border-collapse: collapse; min-width: 590px; font-size: 13px; }
.sample th, .sample td { border: 1px solid var(--border); padding: 10px; text-align: left; vertical-align: top; }
.sample th { background: var(--accent-light); }
.sample .sample-table { overflow-x: auto; }
.sample .tag { display: inline-block; padding: 3px 9px; border-radius: 999px; background: var(--accent-light); color: var(--accent); font-size: 12px; font-weight: 700; }
.sample .foot a { margin: 0 7px; }
</style>
<article class="card sample">
  <a class="brand" href="/"><span class="brand-logo">VA</span>VidAI</a>
  <h1>一份视频证据报告长什么样</h1>
  <p class="sub">以下是虚构的 32 秒“桌面收纳盒介绍”演示。时间点、台词和结论仅用于说明报告结构，并非真实平台视频或效果承诺。</p>
  <span class="tag">演示报告 · 非真实素材</span>

  <h2>时间轴证据</h2>
  <div class="sample-table"><table>
    <thead><tr><th>时间</th><th>可核查的画面与口播</th><th>可据此作出的解释</th></tr></thead>
    <tbody>
      <tr><td>00:00–00:08</td><td>镜头展示凌乱桌面；口播：“三步整理桌面”。</td><td>以常见问题开场，让观众迅速理解用途。</td></tr>
      <tr><td>00:09–00:20</td><td>手部演示收纳盒的三格隔板；画面依次放入笔、线材和便签。</td><td>用操作过程解释结构，而非只展示成品。</td></tr>
      <tr><td>00:21–00:32</td><td>同角度呈现整理前后桌面；字幕写“整理后”。</td><td>前后对照帮助观众判断收纳效果。</td></tr>
    </tbody>
  </table></div>

  <h2>核心总结</h2>
  <p><strong>视频明确表达：</strong>这款收纳盒分为三格，演示了三类物品的摆放方式。</p>
  <p><strong>合理释义：</strong>视频按“问题 → 操作 → 结果”组织，信息较易跟随。</p>
  <p><strong>分析推论：</strong>这种结构可能适合演示型产品视频；仅凭视频本身，无法判断销量或传播效果。</p>

  <h2>可复用的创作提示</h2>
  <p>先让观众看见问题，再展示关键操作，最后用同角度画面呈现结果。真实报告会尽量标注支撑每条判断的时间点，并明确区分画面、字幕、机器转写和推论。</p>
  <p class="sub" style="text-align:left;margin-top:18px">实际报告取决于视频能否访问、画面与音轨质量及模型服务状态。缺少可识别语音时会标明转写状态；请依据原视频复核重要结论。</p>
  <div class="foot"><a href="/">分析自己的视频 →</a><a href="/data-policy">使用与数据说明</a></div>
</article>
"""
    return _page("报告示例", body)


def data_policy_page() -> str:
    body = """
<div class="card" style="max-width:680px;line-height:1.7">
  <a class="brand" href="/"><span class="brand-logo">VA</span>VidAI</a>
  <h1>使用与数据说明</h1>
  <p class="sub">公开试用服务 · 更新于 2026 年 9 月 29 日</p>
  <p>请仅提交你有权访问和使用的公开视频链接，或上传你有权处理的 MP4 视频。视频号链接暂不能直接解析，可上传自有素材。来源平台可能限制解析或下载；可用性、画质和结果准确性无法保证。AI 报告和语音转写仅供参考，请核对原视频。</p>
  <p style="margin-top:14px">本服务保存账号名、加密后的密码、登录会话、访问 IP、浏览器信息和每日操作计数。还会按账号记录每天是否完成解析、上传、转写和分析，用于在后台汇总功能完成率；若你主动评价报告，会保存“有帮助/需改进”的选择及报告文件名的不可逆摘要，不保存文字评论。这些统计记录不包含视频链接或报告内容，删除账号时一并删除。为完成解析与分析，服务器会暂存视频、封面、ASR 转写稿及报告；AI 分析所需内容会发送给已配置的模型服务商。启用语音识别时，提取的音轨会发送给腾讯云语音识别服务进行转写。</p>
  <p style="margin-top:14px">生成文件在超过 48 小时后的每日清理时删除。会话通常有效 7 天；账号数据保留到你在<a href="/account">账户页</a>删除账号。删除后在线账号与会话立即清除，已生成文件按上述周期清理。本机数据库备份保留最近 14 份，腾讯云 COS 异地备份保留 30 天，过期后轮替删除；已写入备份的账号数据也会在相应备份过期后消失。</p>
  <p style="margin-top:14px">不要提交私人、机密或无权处理的内容。请遵守来源平台的规则以及适用法律。使用本服务即表示你了解以上处理方式。</p>
  <div class="foot"><a href="/">← 返回工作台</a></div>
</div>
"""
    return _page("使用与数据说明", body)
