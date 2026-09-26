#!/usr/bin/env python3
"""
视频解析工作台 · 管理后台（独立站点）

仅做「只读」聚合展示，绝不写主站数据库：
  - 运营看板：用户总数 / 管理员数 / 活跃会话 / 资产总数与体积 / 最近登录与活跃会话
  - 每个账号使用情况：注册时间、最近登录、当前活跃会话数、是否管理员
  - 资产记录库：扫描 downloads/ 下的视频与报告文件，支持检索与下载

数据来源（服务器上由视频解析容器只读挂载）：
  AUTH_DB_PATH  默认 /opt/video-parser/data/auth.db
  ASSETS_DIR    默认 /opt/video-parser/downloads
  LOGS_DIR      默认 /opt/video-parser/logs

鉴权：复用在 auth_db 中注册的 admin 账号（PBKDF2 校验），独立会话 Cookie，
      仅 is_admin=1 的用户可进入。
"""
import os
import sqlite3
import hashlib
import hmac
import time
import secrets
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote

from flask import (
    Flask, request, redirect, url_for,
    render_template, send_file, abort, session as flask_session,
)

# ===================== 配置 =====================
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "/opt/video-parser/data/auth.db")
ASSETS_DIR = os.getenv("ASSETS_DIR", "/opt/video-parser/downloads")
LOGS_DIR = os.getenv("LOGS_DIR", "/opt/video-parser/logs")
ADMIN_USER = os.getenv("APP_USER", "admin")
APP_PASS = os.getenv("APP_PASS", "")          # 若设置，则强制主管理员必须用此密码
PORT = int(os.getenv("PORT", "7861"))
PBKDF2_ITERATIONS = 240_000
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".flv", ".m4v", ".ts"}
DOC_EXT = {".md", ".txt", ".json", ".srt", ".vtt", ".pdf", ".html"}
ASSET_CAP = 500                               # 资产列表单次扫描上限，防止目录过大卡死

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE, "templates"),
    static_folder=os.path.join(BASE, "static"),
    static_url_path="/static",
)
app.secret_key = os.getenv("ADMIN_SECRET", secrets.token_urlsafe(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_PATH="/",
)


# ===================== 数据访问（只读） =====================
def ro_conn():
    # WAL 库可被另一进程只读打开，读取已提交数据
    return sqlite3.connect(f"file:{AUTH_DB_PATH}?mode=ro", uri=True)


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def auth_admin(username: str, password: str) -> bool:
    """校验管理员账号；优先用 APP_PASS 强制主管理员密码。"""
    if APP_PASS and username == ADMIN_USER:
        return password == APP_PASS
    try:
        c = ro_conn()
        row = c.execute(
            "SELECT password_hash, is_admin FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        c.close()
        if not row or not row[1]:
            return False
        return verify_password(password, row[0])
    except Exception:
        return False


def _iso(ts):
    if not ts:
        return "—"
    try:
        # 可能是 ISO 字符串或 unix 秒
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def get_stats():
    now = int(time.time())
    stats = {}
    try:
        c = ro_conn()
        stats["total_users"] = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        stats["admin_count"] = c.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
        stats["active_sessions"] = c.execute(
            "SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (now,)).fetchone()[0]
        stats["throttled"] = c.execute(
            "SELECT COUNT(*) FROM login_throttle WHERE locked_until > ?", (now,)).fetchone()[0]
        # 最近活跃会话
        stats["recent_sessions"] = [
            {
                "username": r[0], "ip": r[1] or "—",
                "created": _iso(r[2]), "expires": _iso(r[3]),
                "ua": (r[4] or "")[:80],
            }
            for r in c.execute(
                "SELECT u.username, s.ip, s.created_at, s.expires_at, s.user_agent "
                "FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.expires_at > ? ORDER BY s.created_at DESC LIMIT 15", (now,)
            ).fetchall()
        ]
        # 最近登录用户
        stats["recent_users"] = [
            {"username": r[0], "is_admin": r[1], "created": _iso(r[2]),
             "last_login": _iso(r[3])}
            for r in c.execute(
                "SELECT username, is_admin, created_at, last_login_at FROM users "
                "ORDER BY last_login_at IS NULL, last_login_at DESC LIMIT 8")
            .fetchall()
        ]
        c.close()
    except Exception as e:
        stats["error"] = str(e)
    # 资产统计
    try:
        files = list(Path(ASSETS_DIR).rglob("*"))
        files = [f for f in files if f.is_file()]
        stats["asset_count"] = len(files)
        stats["asset_size"] = sum(f.stat().st_size for f in files)
        stats["asset_size_human"] = human_size(stats["asset_size"])
    except Exception:
        stats["asset_count"] = 0
        stats["asset_size"] = 0
    return stats


def get_users():
    now = int(time.time())
    try:
        c = ro_conn()
        rows = c.execute(
            "SELECT id, username, is_admin, created_at, last_login_at FROM users "
            "ORDER BY is_admin DESC, last_login_at IS NULL, last_login_at DESC"
        ).fetchall()
        users = []
        for r in rows:
            uid, uname, is_adm, created, last = r
            active = c.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=? AND expires_at>?",
                (uid, now)).fetchone()[0]
            users.append({
                "id": uid, "username": uname, "is_admin": is_adm,
                "created": _iso(created), "last_login": _iso(last),
                "active_sessions": active,
            })
        c.close()
        return users
    except Exception as e:
        return [{"error": str(e)}]


def scan_assets(query="", ext=None, limit=ASSET_CAP):
    out = []
    root = Path(ASSETS_DIR)
    if not root.exists():
        return out
    q = (query or "").strip().lower()
    count = 0
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(root))
        if q and q not in rel.lower():
            continue
        if ext and f.suffix.lower() != ext.lower():
            continue
        count += 1
        if count > limit:
            out.append({"_truncated": True})
            break
        try:
            sz = f.stat().st_size
            mt = _iso(f.stat().st_mtime)
        except Exception:
            sz, mt = 0, "—"
        out.append({
            "name": f.name, "rel": rel, "size": sz, "mtime": mt,
            "ext": f.suffix.lower(),
            "kind": "video" if f.suffix.lower() in VIDEO_EXT else
                    ("doc" if f.suffix.lower() in DOC_EXT else "file"),
        })
    # 按修改时间倒序
    out.sort(key=lambda x: x.get("mtime", ""), reverse=True)
    return out


# ===================== 鉴权装饰 =====================
def require_admin(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*a, **k):
        if not flask_session.get("admin_user"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


# ===================== 工具：字节可读化 =====================
def human_size(n):
    n = int(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} PB"


app.jinja_env.filters["human"] = human_size


# ===================== 路由 =====================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if auth_admin(u, p):
            flask_session["admin_user"] = u
            flask_session.permanent = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="用户名或密码不正确，或无管理员权限")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    flask_session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_admin
def dashboard():
    return render_template("dashboard.html", s=get_stats(),
                           user=flask_session.get("admin_user"))


@app.route("/users")
@require_admin
def users():
    return render_template("users.html", users=get_users(),
                           user=flask_session.get("admin_user"))


@app.route("/assets")
@require_admin
def assets():
    q = request.args.get("q", "")
    ext = request.args.get("ext", "")
    data = scan_assets(q, ext or None)
    truncated = any(d.get("_truncated") for d in data)
    data = [d for d in data if not d.get("_truncated")]
    return render_template("assets.html", assets=data, query=q,
                           ext=ext, truncated=truncated,
                           user=flask_session.get("admin_user"))


@app.route("/asset/<path:p>")
@require_admin
def asset_file(p):
    """只读发送下载目录内的文件（防目录穿越）。"""
    root = Path(ASSETS_DIR).resolve()
    target = (root / unquote(p)).resolve()
    if not str(target).startswith(str(root)):
        abort(403)
    if not target.is_file():
        abort(404)
    mt, _ = mimetypes.guess_type(str(target))
    return send_file(str(target), mimetype=mt, as_attachment=False)


@app.route("/health")
def health():
    return {"ok": True}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
