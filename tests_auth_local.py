#!/usr/bin/env python3
"""本地登录模块全链路自测（对 localhost:7860，假账号，测试库）。"""
import requests

BASE = "http://localhost:7860"
s = requests.Session()
s.trust_env = False  # 忽略系统代理
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("  | " + detail if detail else ""))


# T1 未登录访问 / -> 302 登录页
r = s.get(BASE + "/", allow_redirects=False)
check("T1 未登录 / -> 302 登录页", r.status_code == 302 and "/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")

# T2 登录页 200
r = s.get(BASE + "/login")
check("T2 /login 200", r.status_code == 200)

# T3 未登录 API -> 401 JSON
r = s.get(BASE + "/api/auth/me")
check("T3 未登录 /api/auth/me -> 401", r.status_code == 401, str(r.json())[:40])

# T4 播种管理员登录
r = s.post(BASE + "/login", data={"username": "testadmin", "password": "testpass123", "next": "/"},
           allow_redirects=False)
has_cookie = "vp_session" in s.cookies
check("T4 管理员登录 303+Cookie", r.status_code == 303 and has_cookie, f"{r.status_code}")

# T5 会话访问首页
r = s.get(BASE + "/")
check("T5 带 Cookie 访问 / -> 200", r.status_code == 200)

# T6 me 接口
r = s.get(BASE + "/api/auth/me")
d = r.json().get("data", {})
check("T6 /api/auth/me 返回用户名", d.get("username") == "testadmin" and d.get("is_admin") is True,
      str(d))

# T7 /account 页面
r = s.get(BASE + "/account")
check("T7 /account 含用户名", r.status_code == 200 and "testadmin" in r.text)

# T8 注册新用户（自动登录）
r = s.post(BASE + "/register",
           data={"username": "newuser01", "password": "abcdef12345", "password2": "abcdef12345", "next": "/"},
           allow_redirects=False)
r2 = s.get(BASE + "/api/auth/me")
check("T8 注册->自动登录", r.status_code == 303 and r2.json().get("data", {}).get("username") == "newuser01",
      f"{r.status_code}")

# T9 重名注册报错
s2 = requests.Session(); s2.trust_env = False
r = s2.post(BASE + "/register",
            data={"username": "newuser01", "password": "abcdef12345", "password2": "abcdef12345"})
check("T9 重名注册 -> 错误提示", "用户名已存在" in r.text)

# T10 弱密码拒绝
r = s2.post(BASE + "/register",
            data={"username": "weakpw", "password": "short", "password2": "short"})
check("T10 弱密码被拒", "至少 8 位" in r.text)

# T11 错误旧密码改密失败
r = s.post(BASE + "/account/password", data={"old_password": "wrong-pass-123", "new_password": "zyxwv98765"})
check("T11 错误旧密码 -> 报错", "当前密码不正确" in r.text)

# T12 正确改密 -> 303 且旧会话吊销
r = s.post(BASE + "/account/password", data={"old_password": "abcdef12345", "new_password": "zyxwv98765"},
           allow_redirects=False)
r2 = s.get(BASE + "/api/auth/me")
check("T12 改密成功+旧会话吊销", r.status_code == 303 and r2.status_code == 401, f"{r.status_code}/{r2.status_code}")

# T13 新密码可登录
r = s.post(BASE + "/login", data={"username": "newuser01", "password": "zyxwv98765"}, allow_redirects=False)
check("T13 新密码登录成功", r.status_code == 303)

# T14 错误密码登录报错
s3 = requests.Session(); s3.trust_env = False
r = s3.post(BASE + "/login", data={"username": "newuser01", "password": "totally-wrong-1"})
check("T14 错误密码 -> 用户名或密码不正确", "用户名或密码不正确" in r.text)

# T15 登出
r = s.get(BASE + "/logout", allow_redirects=False)
r2 = s.get(BASE + "/api/auth/me")
check("T15 登出后会话失效", r.status_code == 302 and r2.status_code == 401)

# T16 账号连续错 5 次 -> 锁定提示
s4 = requests.Session(); s4.trust_env = False
msgs = []
for i in range(6):
    r = s4.post(BASE + "/login", data={"username": "lockme", "password": f"badpass-{i}"})
    msgs.append(r.text)
check("T16 账号锁定提示出现", any("频繁" in m for m in msgs))

# T17 公共资源豁免
for p in ("/health", "/static/css/app.css?v=20260925d", "/login", "/register"):
    r = requests.get(BASE + p, allow_redirects=False); r.trust_env = False
    check(f"T17 豁免 {p.split('?')[0]}", r.status_code == 200, str(r.status_code))

# T18 播种管理员仍可用原密码登录
s5 = requests.Session(); s5.trust_env = False
r = s5.post(BASE + "/login", data={"username": "testadmin", "password": "testpass123"}, allow_redirects=False)
check("T18 管理员原凭据可登录", r.status_code == 303)

fails = [n for n, ok, _ in results if not ok]
print(f"\n=== {len(results) - len(fails)}/{len(results)} PASS ===")
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
