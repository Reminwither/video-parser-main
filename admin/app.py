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
import json
import re
import threading
import shutil
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote
from werkzeug.middleware.proxy_fix import ProxyFix

from flask import (
    Flask, request, redirect, url_for,
    render_template, send_file, abort, session as flask_session, Response,
)

# ===================== 配置 =====================
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "/opt/video-parser/data/auth.db")
ASSETS_DIR = os.getenv("ASSETS_DIR", "/opt/video-parser/downloads")
CACHE_DIR = os.getenv("CACHE_DIR", "/opt/video-parser/cache")
LOGS_DIR = os.getenv("LOGS_DIR", "/opt/video-parser/logs")
ASR_CACHE_DIR = os.getenv("ASR_CACHE_DIR", "/opt/video-parser/cache/asr")
PORT = int(os.getenv("PORT", "7861"))
PBKDF2_ITERATIONS = 240_000
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".flv", ".m4v", ".ts"}
DOC_EXT = {".md", ".txt", ".json", ".srt", ".vtt", ".pdf", ".html"}
ASSET_CAP = 500                               # 资产列表单次扫描上限，防止目录过大卡死
ASR_CACHE_NAME = re.compile(r"^[0-9a-f]{64}\.json$")

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE, "templates"),
    static_folder=os.path.join(BASE, "static"),
    static_url_path="/static",
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.getenv("ADMIN_SECRET", secrets.token_urlsafe(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"},
    SESSION_COOKIE_PATH="/admin",
)

_LOGIN_LOCK = threading.Lock()
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def _login_limited(username: str, ip: str) -> bool:
    now = time.monotonic()
    with _LOGIN_LOCK:
        for key, window, limit in ((f"user:{username.lower()}", 900, 5), (f"ip:{ip}", 300, 10)):
            attempts = [when for when in _LOGIN_ATTEMPTS.get(key, []) if now - when < window]
            if attempts:
                _LOGIN_ATTEMPTS[key] = attempts
            else:
                _LOGIN_ATTEMPTS.pop(key, None)
            if len(attempts) >= limit:
                return True
    return False


def _record_login_failure(username: str, ip: str) -> None:
    now = time.monotonic()
    with _LOGIN_LOCK:
        if len(_LOGIN_ATTEMPTS) > 10000:
            _LOGIN_ATTEMPTS.clear()
        for key in (f"user:{username.lower()}", f"ip:{ip}"):
            _LOGIN_ATTEMPTS.setdefault(key, []).append(now)


def _clear_login_failures(username: str, ip: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(f"user:{username.lower()}", None)
        _LOGIN_ATTEMPTS.pop(f"ip:{ip}", None)


@app.after_request
def protect_admin_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:"
    )
    return response


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
    """Use the current database password and active admin role."""
    try:
        c = ro_conn()
        row = c.execute(
            "SELECT password_hash, is_admin, is_active FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        c.close()
        if not row or not row[1] or not row[2]:
            return False
        return verify_password(password, row[0])
    except Exception:
        return False


def active_admin(username: str) -> bool:
    try:
        c = ro_conn()
        row = c.execute(
            "SELECT is_admin, is_active FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        c.close()
        return bool(row and row[0] and row[1])
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
    # 导出与缓存分别统计，避免把大体积的视频缓存遗漏在容量看板之外。
    try:
        files = (f for f in Path(ASSETS_DIR).rglob("*") if f.is_file() and not f.is_symlink())
        stats["asset_count"] = stats["asset_size"] = 0
        for file in files:
            stats["asset_count"] += 1
            stats["asset_size"] += file.stat().st_size
        stats["asset_size_human"] = human_size(stats["asset_size"])
    except Exception:
        stats["asset_count"] = 0
        stats["asset_size"] = 0
        stats["asset_size_human"] = "—"
    try:
        files = (f for f in Path(CACHE_DIR).rglob("*") if f.is_file() and not f.is_symlink())
        stats["cache_count"] = stats["cache_size"] = 0
        for file in files:
            stats["cache_count"] += 1
            stats["cache_size"] += file.stat().st_size
        stats["cache_size_human"] = human_size(stats["cache_size"])
        disk = shutil.disk_usage(CACHE_DIR)
        stats["disk_free_human"] = human_size(disk.free)
        stats["disk_low"] = disk.free < 2 * 1024 * 1024 * 1024
    except Exception:
        stats["cache_count"] = stats["cache_size"] = 0
        stats["cache_size_human"] = stats["disk_free_human"] = "—"
        stats["disk_low"] = False
    return stats


def get_usage_stats():
    """Seven-day aggregate usage from the main app; no link or user data is stored."""
    data = {"ready": False, "parse": {}, "upload": {}, "analysis": {}, "transcribe": {}, "platforms": [], "days": [], "reasons": [], "success_rate": None}
    try:
        c = ro_conn()
        rows = c.execute(
            "SELECT day, action, platform, outcome, reason, SUM(count), SUM(duration_ms_total) "
            "FROM usage_metrics WHERE day >= date('now', '-6 days') "
            "GROUP BY day, action, platform, outcome, reason ORDER BY day",
        ).fetchall()
        c.close()
    except sqlite3.Error:
        return data

    data["ready"] = True
    platforms = {name: {"name": name, "total": 0, "success": 0, "failed": 0} for name in
                 ("抖音", "哔哩哔哩", "小红书", "视频号", "其他")}
    days = {}
    reasons = {}
    for day, action, platform, outcome, reason, count, duration_total in rows:
        if action not in ("parse", "upload", "analysis", "transcribe"):
            continue
        metric = data[action]
        metric[outcome] = metric.get(outcome, 0) + count
        metric["total"] = metric.get("total", 0) + count
        if outcome == "success":
            metric["success_duration_ms"] = metric.get("success_duration_ms", 0) + duration_total
        if reason:
            reasons[(action, reason)] = reasons.get((action, reason), 0) + count
        daily = days.setdefault(day, {"day": day, "parse": 0, "parse_success": 0, "upload": 0, "upload_success": 0, "analysis": 0, "analysis_success": 0, "transcribe": 0, "transcribe_success": 0})
        daily[action] += count
        if outcome == "success":
            daily[action + "_success"] += count
        if action == "parse":
            p = platforms.get(platform, platforms["其他"])
            p["total"] += count
            if outcome in ("success", "failed"):
                p[outcome] += count

    completed = data["parse"].get("success", 0) + data["parse"].get("failed", 0)
    if completed:
        data["success_rate"] = round(100 * data["parse"].get("success", 0) / completed, 1)
    for action in ("parse", "upload", "analysis", "transcribe"):
        m = data[action]
        n = m.get("success", 0)
        m["avg_success_seconds"] = round(m.get("success_duration_ms", 0) / n / 1000, 1) if n else None
    data["platforms"] = [p for p in platforms.values() if p["total"]]
    data["days"] = [days[day] for day in sorted(days, reverse=True)]
    reason_labels = {
        "upstream": "来源平台解析失败", "error": "服务执行异常",
        "missing_video": "未加载视频缓存", "disabled": "AI 功能未启用",
        "platform_mismatch": "平台选择与链接不一致",
        "no_audio": "视频没有音轨", "asr_empty": "语音识别无结果",
    }
    data["reasons"] = [
        {"action": {"parse": "解析", "upload": "上传", "analysis": "AI 分析", "transcribe": "语音转写"}[action], "reason": reason_labels.get(reason, reason), "count": count}
        for (action, reason), count in sorted(reasons.items(), key=lambda item: item[1], reverse=True)
    ]
    return data


def get_funnel_stats():
    """Seven-day distinct-user funnel; deleted and admin accounts are excluded."""
    data = {"ready": False, "registered": 0, "started": 0, "completed": 0,
            "returned": 0, "new_completed": 0, "started_completed": 0,
            "registration_rate": None, "completion_rate": None}
    try:
        c = ro_conn()
        since = "date('now', '-6 days')"
        data["registered"] = c.execute(
            f"SELECT COUNT(*) FROM users WHERE is_admin=0 AND substr(created_at,1,10)>={since}"
        ).fetchone()[0]
        data["started"] = c.execute(
            f"SELECT COUNT(DISTINCT a.user_id) FROM user_activity a JOIN users u ON u.id=a.user_id "
            f"WHERE u.is_admin=0 AND a.day>={since} AND a.action IN ('parse_success','upload_success')"
        ).fetchone()[0]
        data["completed"] = c.execute(
            f"SELECT COUNT(DISTINCT a.user_id) FROM user_activity a JOIN users u ON u.id=a.user_id "
            f"WHERE u.is_admin=0 AND a.day>={since} AND a.action='analysis_success'"
        ).fetchone()[0]
        data["started_completed"] = c.execute(
            f"SELECT COUNT(DISTINCT a.user_id) FROM user_activity a JOIN users u ON u.id=a.user_id "
            f"WHERE u.is_admin=0 AND a.day>={since} AND a.action='analysis_success' AND EXISTS ("
            f"SELECT 1 FROM user_activity b WHERE b.user_id=a.user_id AND b.day>={since} "
            f"AND b.action IN ('parse_success','upload_success'))"
        ).fetchone()[0]
        data["new_completed"] = c.execute(
            f"SELECT COUNT(DISTINCT u.id) FROM users u JOIN user_activity a ON a.user_id=u.id "
            f"WHERE u.is_admin=0 AND substr(u.created_at,1,10)>={since} "
            f"AND a.day>={since} AND a.action='analysis_success'"
        ).fetchone()[0]
        data["returned"] = c.execute(
            f"SELECT COUNT(*) FROM (SELECT a.user_id FROM user_activity a JOIN users u ON u.id=a.user_id "
            f"WHERE u.is_admin=0 AND a.day>={since} AND a.action IN "
            f"('parse_success','upload_success','transcribe_success','analysis_success') "
            f"GROUP BY a.user_id HAVING COUNT(DISTINCT a.day)>=2)"
        ).fetchone()[0]
        c.close()
    except sqlite3.Error:
        return data
    data["ready"] = True
    if data["registered"]:
        data["registration_rate"] = round(100 * data["new_completed"] / data["registered"], 1)
    if data["started"]:
        data["completion_rate"] = round(100 * data["started_completed"] / data["started"], 1)
    return data


def get_export_stats():
    """Server-completed full downloads, with exact-file usefulness matching."""
    data = {"ready": False, "report_users": 0, "asr_users": 0,
            "useful_users": 0, "report_files": 0, "asr_files": 0}
    try:
        with closing(ro_conn()) as c:
            rows = c.execute(
                "SELECT e.kind, COUNT(DISTINCT e.user_id), "
                "COUNT(DISTINCT CAST(e.user_id AS TEXT) || ':' || e.report_key) "
                "FROM artifact_exports e JOIN users u ON u.id=e.user_id "
                "WHERE u.is_admin=0 AND e.day>=date('now','-6 days') GROUP BY e.kind"
            ).fetchall()
            data["useful_users"] = c.execute(
                "SELECT COUNT(DISTINCT e.user_id) FROM artifact_exports e "
                "JOIN users u ON u.id=e.user_id JOIN report_feedback f "
                "ON f.user_id=e.user_id AND f.report_key=e.report_key "
                "WHERE u.is_admin=0 AND e.day>=date('now','-6 days') "
                "AND e.kind='report' AND f.rating=1"
            ).fetchone()[0]
        for kind, users, files in rows:
            data[f"{kind}_users"] = users
            data[f"{kind}_files"] = files
        data["ready"] = True
    except sqlite3.Error:
        pass
    return data


def get_feedback_stats():
    """Seven-day report ratings from active non-admin accounts."""
    data = {"ready": False, "positive": 0, "negative": 0, "total": 0,
            "positive_rate": None, "reasons": [], "reasons_ready": False}
    try:
        c = ro_conn()
        rows = c.execute(
            "SELECT f.rating, COUNT(*) FROM report_feedback f JOIN users u ON u.id=f.user_id "
            "WHERE u.is_admin=0 AND substr(f.updated_at,1,10)>=date('now','-6 days') "
            "GROUP BY f.rating"
        ).fetchall()
        # Admin may deploy before the main site's backward-compatible migration.
        if "reason" in {row[1] for row in c.execute("PRAGMA table_info(report_feedback)")}:
            reason_labels = {
                "asr": "语音转写不准确", "timeline": "时间点不准确",
                "evidence": "结论缺少视频证据", "actionable": "建议不够具体",
                "other": "其他问题", "": "未选原因",
            }
            reason_rows = c.execute(
                "SELECT f.reason, COUNT(*) FROM report_feedback f JOIN users u ON u.id=f.user_id "
                "WHERE u.is_admin=0 AND f.rating=-1 AND substr(f.updated_at,1,10)>=date('now','-6 days') "
                "GROUP BY f.reason ORDER BY COUNT(*) DESC, f.reason"
            ).fetchall()
            data["reasons"] = [{"label": reason_labels.get(reason, "其他问题"), "count": count}
                               for reason, count in reason_rows]
            data["reasons_ready"] = True
        c.close()
    except sqlite3.Error:
        return data
    for rating, count in rows:
        if rating == 1:
            data["positive"] = count
        elif rating == -1:
            data["negative"] = count
    data["total"] = data["positive"] + data["negative"]
    data["positive_rate"] = round(100 * data["positive"] / data["total"], 1) if data["total"] else None
    data["ready"] = True
    return data


def get_resource_stats():
    """Seven-day provider units and a clearly bounded paid-provider scenario."""
    data = {"ready": False, "model_calls": 0, "model_failed": 0,
            "unreported": 0, "input_tokens": 0, "output_tokens": 0,
            "asr_calls": 0, "asr_failed": 0, "audio_minutes": 0.0, "rows": [],
            "model_reference_cny": 0.0, "asr_reference_cny": 0.0,
            "unpriced_model_calls": 0, "unpriced_asr_calls": 0}
    try:
        c = ro_conn()
        rows = c.execute(
            "SELECT provider, operation, model, SUM(calls), SUM(failed_calls), "
            "SUM(missing_token_usage_calls), SUM(input_tokens), SUM(output_tokens), "
            "SUM(submitted_audio_ms) FROM resource_usage "
            "WHERE day >= date('now', '-6 days') GROUP BY provider, operation, model "
            "ORDER BY SUM(calls) DESC, provider, operation"
        ).fetchall()
        c.close()
    except sqlite3.Error:
        return data
    data["ready"] = True
    for provider, operation, model, calls, failed, unreported, input_tokens, output_tokens, audio_ms in rows:
        if provider == "model-api":
            data["model_calls"] += calls
            data["model_failed"] += failed
            data["unreported"] += unreported
            data["input_tokens"] += input_tokens
            data["output_tokens"] += output_tokens
            if model in {"Qwen/Qwen3.5-27B", "qwen3.5-27b"}:
                # Alibaba Cloud Model Studio, Beijing, qwen3.5-27b,
                # <=128K input tokens per request, 2026-09-28 list rates.
                data["model_reference_cny"] += (
                    input_tokens * 0.6 + output_tokens * 4.8
                ) / 1_000_000
            else:
                data["unpriced_model_calls"] += calls
        elif provider == "tencent-asr":
            data["asr_calls"] += calls
            data["asr_failed"] += failed
            data["audio_minutes"] += audio_ms / 60000
            if operation == "flash-transcribe" and model == "16k_zh":
                # Tencent ASR flash, first postpaid tier, before free packs.
                data["asr_reference_cny"] += audio_ms / 3_600_000 * 3.10
            else:
                data["unpriced_asr_calls"] += calls
        data["rows"].append({"provider": provider, "operation": operation,
                             "model": model, "calls": calls, "failed": failed,
                             "input_tokens": input_tokens, "output_tokens": output_tokens,
                             "audio_minutes": round(audio_ms / 60000, 2)})
    data["audio_minutes"] = round(data["audio_minutes"], 2)
    data["model_reference_cny"] = round(data["model_reference_cny"], 4)
    data["asr_reference_cny"] = round(data["asr_reference_cny"], 4)
    return data


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


def scan_asr_cache(limit=100):
    """Expose previous ASR results to admins without modifying the analysis cache."""
    root = Path(ASR_CACHE_DIR)
    if not root.is_dir():
        return []
    try:
        files = (p for p in root.iterdir() if p.is_file() and ASR_CACHE_NAME.fullmatch(p.name))
        recent = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return []
    out = []
    for path in recent:
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            out.append({
                "name": path.name, "short_name": path.stem[:12],
                "status": str(data.get("status") or "未知"),
                "segments": len(data.get("cues") or []),
                "mtime": _iso(path.stat().st_mtime),
            })
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return out


# ===================== 鉴权装饰 =====================
def require_admin(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*a, **k):
        if not flask_session.get("admin_user") or not active_admin(flask_session["admin_user"]):
            flask_session.clear()
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
        ip = request.remote_addr or "-"
        if _login_limited(u, ip):
            return render_template("login.html", error="尝试过于频繁，请稍后再试"), 429
        if auth_admin(u, p):
            _clear_login_failures(u, ip)
            flask_session["admin_user"] = u
            flask_session.permanent = True
            return redirect(url_for("dashboard"))
        _record_login_failure(u, ip)
        return render_template("login.html", error="用户名或密码不正确，或无管理员权限")
    return render_template("login.html", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    flask_session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_admin
def dashboard():
    return render_template("dashboard.html", s=get_stats(), m=get_usage_stats(), r=get_resource_stats(),
                           f=get_funnel_stats(), q=get_feedback_stats(), e=get_export_stats(),
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
    return render_template("assets.html", assets=data, asr_caches=scan_asr_cache(), query=q,
                           ext=ext, truncated=truncated,
                           user=flask_session.get("admin_user"))


@app.route("/asr-cache/<name>")
@require_admin
def asr_cache_file(name):
    if not ASR_CACHE_NAME.fullmatch(name):
        abort(404)
    root = Path(ASR_CACHE_DIR).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        abort(404)
    if path.stat().st_size > 1024 * 1024:
        abort(413)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        abort(404)
    if not isinstance(data, dict):
        abort(404)
    lines = ["ASR 机器转写（未人工校对）", f"状态：{data.get('status') or '未知'}", ""]
    for cue in data.get("cues") or []:
        if not isinstance(cue, dict):
            continue
        try:
            start, end = float(cue.get("start") or 0), float(cue.get("end") or 0)
        except (TypeError, ValueError):
            continue
        lines.append(f"[{start:.2f}–{end:.2f}] {str(cue.get('text') or '').strip()}")
    return Response("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


@app.route("/asset/<path:p>")
@require_admin
def asset_file(p):
    """只读发送下载目录内的文件（防目录穿越）。"""
    root = Path(ASSETS_DIR).resolve()
    target = (root / unquote(p)).resolve()
    if not target.is_relative_to(root):
        abort(403)
    if not target.is_file():
        abort(404)
    mt, _ = mimetypes.guess_type(str(target))
    # Only browser-native videos are safe to display inline. HTML/SVG and other
    # user-controlled assets must download so they cannot execute on this origin.
    return send_file(
        str(target),
        mimetype=mt or "application/octet-stream",
        as_attachment=target.suffix.lower() not in VIDEO_EXT,
        download_name=target.name,
    )


@app.route("/health")
def health():
    try:
        c = ro_conn()
        c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        c.close()
    except sqlite3.Error:
        return {"ok": False}, 503
    return {"ok": True}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
