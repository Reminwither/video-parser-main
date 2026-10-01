#!/usr/bin/env python3
"""
SQLite 用户与会话存储（标准库实现，零第三方依赖）

数据文件：data/auth.db（可用 AUTH_DB_PATH 覆盖）
  - 服务器部署时 data/ 目录经 Docker 卷挂载持久化（deploy.yml 已配置）

表结构：
  users           用户（PBKDF2-SHA256 口令哈希）
  sessions        服务端会话（Cookie 只存随机 token，库里存 sha256(token)）
  login_throttle  登录限流（按 IP 失败计数 + 账号锁定）
"""

import os
import re
import sqlite3
import secrets
import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

# ==================== 配置 ====================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(_BASE_DIR, "data", "auth.db"))

SESSION_TTL_SECONDS = int(os.getenv("AUTH_SESSION_TTL", str(7 * 24 * 3600)))  # 7 天
SESSION_COOKIE = "vp_session"

# 限流：单 IP 5 分钟窗口内最多 10 次失败；单账号连续失败 5 次锁定 15 分钟
THROTTLE_WINDOW = 300
THROTTLE_MAX_FAILS = 10
ACCOUNT_LOCK_FAILS = 5
ACCOUNT_LOCK_SECONDS = 15 * 60

PBKDF2_ITERATIONS = 240_000

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")

ACTION_QUOTAS = {
    "parse": ("视频解析", 30, 300), "upload": ("视频上传", 5, 30),
    "play": ("视频加载", 10, 50), "download": ("视频下载", 10, 50),
    "transcribe": ("语音转写", 5, 30), "analysis": ("AI 分析", 2, 10),
}

# SQLite 跨线程：每线程独立连接（threading.local 保存，避免
# "SQLite objects created in a thread" —— 曾把连接缓存在函数对象上导致
# 登录后的业务线程直接抛错）
_DB_LOCK = threading.Lock()
_DB_LOCAL = threading.local()
_LOG = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> int:
    return int(time.time())


# ==================== 连接 ====================

def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _db() -> sqlite3.Connection:
    """线程局部连接（uvicorn 默认单进程多线程）。
    连接必须存 threading.local，而不是函数属性——函数属性是全线程共享的，
    非创建线程使用会抛 'SQLite objects created in a thread' 错。"""
    conn = getattr(_DB_LOCAL, "conn", None)
    if conn is None:
        conn = _connect()
        _DB_LOCAL.conn = conn
    return conn


# ==================== 口令哈希 ====================

def valid_auth_payload(data: object) -> bool:
    """Check JSON credential types without changing values or account policy."""
    return (
        isinstance(data, dict)
        and all(isinstance(data.get(field), str) for field in ("username", "password"))
        and ("password2" not in data or isinstance(data["password2"], str))
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ==================== 建库与播种 ====================

def init_db() -> None:
    with _DB_LOCK:
        c = _db()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                ip         TEXT,
                user_agent TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expire ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS login_throttle (
                key         TEXT PRIMARY KEY,   -- 'ip:1.2.3.4' 或 'user:xxx'
                fail_count  INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER NOT NULL DEFAULT 0,
                window_start INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS daily_quota (
                day TEXT NOT NULL,
                subject TEXT NOT NULL,
                action TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (day, subject, action)
            );
            CREATE TABLE IF NOT EXISTS usage_metrics (
                day TEXT NOT NULL,
                action TEXT NOT NULL,
                platform TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                duration_ms_total INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, action, platform, outcome, reason)
            );
            CREATE TABLE IF NOT EXISTS resource_usage (
                day TEXT NOT NULL,
                provider TEXT NOT NULL,
                operation TEXT NOT NULL,
                model TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                failed_calls INTEGER NOT NULL DEFAULT 0,
                missing_token_usage_calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                submitted_audio_ms INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, provider, operation, model)
            );
            CREATE TABLE IF NOT EXISTS user_activity (
                day TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (day, user_id, action)
            );
            CREATE INDEX IF NOT EXISTS idx_user_activity_action_day ON user_activity(action, day);
            CREATE TABLE IF NOT EXISTS report_feedback (
                report_key TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, report_key)
            );
            CREATE INDEX IF NOT EXISTS idx_report_feedback_updated ON report_feedback(updated_at);
            CREATE TABLE IF NOT EXISTS uploaded_files (
                path TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (path, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_uploaded_files_created ON uploaded_files(created_at);
            """
        )
        c.commit()
    seed_admin_if_empty()


def seed_admin_if_empty() -> None:
    """空库时播种初始管理员：优先用 APP_PASS 环境变量保持既有凭据连续性，
    否则生成随机密码并打印一次到控制台。"""
    with _DB_LOCK:
        c = _db()
        n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n > 0:
            return
        username = os.getenv("APP_USER", "admin")
        password = os.getenv("APP_PASS", "")
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)
            generated = True
        c.execute(
            "INSERT INTO users (username, password_hash, is_admin, is_active, created_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (username, hash_password(password), _now_iso()),
        )
        c.commit()
    if generated:
        print("=" * 60)
        print(f"[auth] 初始管理员已创建  用户名: {username}")
        print(f"[auth] 初始密码(仅打印这一次，请立即保存): {password}")
        print("=" * 60)
    else:
        print(f"[auth] 初始管理员已创建（沿用 APP_PASS 环境变量）: {username}")


# ==================== 用户 ====================

class AuthError(Exception):
    """携带用户可读信息的鉴权错误。"""


def create_user(username: str, password: str, is_admin: bool = False) -> int:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("用户名需为 3-32 位字母、数字、下划线或连字符")
    if len(password) < 8:
        raise AuthError("密码至少 8 位")
    with _DB_LOCK:
        c = _db()
        if not is_admin:
            max_users = max(1, int(os.getenv("MAX_REGISTERED_USERS", "200")))
            if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= max_users:
                raise AuthError("注册名额已满，请稍后再试")
        try:
            cur = c.execute(
                "INSERT INTO users (username, password_hash, is_admin, is_active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (username, hash_password(password), 1 if is_admin else 0, _now_iso()),
            )
            c.commit()
        except sqlite3.IntegrityError:
            raise AuthError("用户名已存在")
        return cur.lastrowid


def consume_daily_quota(subject: str, action: str, limit: int) -> bool:
    return consume_daily_quotas([(subject, action, limit)])


def record_usage(action: str, platform: str, outcome: str, reason: str = "", duration_ms: int = 0) -> None:
    """Store bounded aggregate metrics without URLs, video content, IPs or user IDs."""
    allowed_actions = {"parse", "upload", "analysis", "transcribe"}
    allowed_platforms = {"抖音", "哔哩哔哩", "小红书", "视频号"}
    allowed_outcomes = {"success", "failed", "unsupported", "busy", "quota", "invalid"}
    allowed_reasons = {"", "upstream", "error", "missing_video", "disabled", "platform_mismatch", "no_audio", "asr_empty"}
    if action not in allowed_actions or outcome not in allowed_outcomes:
        return
    platform = platform if platform in allowed_platforms else "其他"
    reason = reason if reason in allowed_reasons else "error"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with _DB_LOCK:
            c = _db()
            c.execute(
                "INSERT INTO usage_metrics(day, action, platform, outcome, reason, count, duration_ms_total) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(day, action, platform, outcome, reason) DO UPDATE SET "
                "count = count + 1, duration_ms_total = duration_ms_total + excluded.duration_ms_total",
                (day, action, platform, outcome, reason, max(0, min(int(duration_ms), 3_600_000))),
            )
            c.commit()
    except sqlite3.Error:
        _LOG.exception("Unable to record usage metric")


def record_user_activity(user_id: int, action: str) -> None:
    """Record a successful product milestone without storing links or content."""
    if action not in {"parse_success", "upload_success", "transcribe_success", "analysis_success"}:
        return
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _DB_LOCK:
            c = _db()
            c.execute(
                "INSERT INTO user_activity(day, user_id, action, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(day, user_id, action) DO UPDATE SET count = count + 1",
                (day, int(user_id), action),
            )
            c.commit()
    except (sqlite3.Error, TypeError, ValueError):
        _LOG.exception("Unable to record user activity")


def get_report_feedback(user_id: int, filename: str) -> int | None:
    """Read a user's rating by a one-way key; filenames and content are not stored."""
    report_key = hashlib.sha256(filename.encode("utf-8")).hexdigest()
    try:
        with _DB_LOCK:
            row = _db().execute(
                "SELECT rating FROM report_feedback WHERE user_id=? AND report_key=?",
                (int(user_id), report_key),
            ).fetchone()
        return int(row["rating"]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        _LOG.exception("Unable to read report feedback")
        return None


def set_report_feedback(user_id: int, filename: str, rating: int) -> bool:
    """Store a rating after the caller validates report ownership."""
    if rating not in (-1, 1):
        return False
    report_key = hashlib.sha256(filename.encode("utf-8")).hexdigest()
    try:
        with _DB_LOCK:
            c = _db()
            c.execute(
                "INSERT INTO report_feedback(report_key,user_id,rating,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id,report_key) DO UPDATE SET "
                "rating=excluded.rating, updated_at=excluded.updated_at",
                (report_key, int(user_id), rating, _now_iso()),
            )
            c.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError):
        _LOG.exception("Unable to save report feedback")
        return False


def record_resource_usage(
    provider: str, operation: str, model: str, *, failed: bool = False,
    input_tokens: int | None = None, output_tokens: int | None = None,
    submitted_audio_ms: int = 0,
) -> None:
    """Count provider calls and reported units, without prompts or account data."""
    if provider not in {"model-api", "tencent-asr"} or not model or len(model) > 120:
        return
    if operation not in {
        "vision", "title-check", "quality-rewrite", "synthesis", "segment-summary",
        "visual-segment-summary", "audience-report", "audience-repair", "asr-clean",
        "flash-transcribe", "speaker-diarization",
    }:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    missing = provider == "model-api" and not failed and (input_tokens is None or output_tokens is None)
    try:
        with _DB_LOCK:
            c = _db()
            c.execute(
                "INSERT INTO resource_usage(day, provider, operation, model, calls, failed_calls, "
                "missing_token_usage_calls, input_tokens, output_tokens, submitted_audio_ms) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day, provider, operation, model) DO UPDATE SET "
                "calls = calls + 1, failed_calls = failed_calls + excluded.failed_calls, "
                "missing_token_usage_calls = missing_token_usage_calls + excluded.missing_token_usage_calls, "
                "input_tokens = input_tokens + excluded.input_tokens, "
                "output_tokens = output_tokens + excluded.output_tokens, "
                "submitted_audio_ms = submitted_audio_ms + excluded.submitted_audio_ms",
                (day, provider, operation, model, int(failed), int(missing),
                 max(0, int(input_tokens or 0)), max(0, int(output_tokens or 0)),
                 max(0, int(submitted_audio_ms))),
            )
            c.commit()
    except (sqlite3.Error, TypeError, ValueError):
        _LOG.exception("Unable to record resource usage")


def consume_daily_quotas(items: list[tuple[str, str, int]]) -> bool:
    """Atomically reserve several counters or none; prune old counters on use."""
    if any(limit <= 0 for _, _, limit in items):
        return False
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _DB_LOCK:
        c = _db()
        c.execute("DELETE FROM daily_quota WHERE day < date(?, '-8 days')", (day,))
        for subject, action, limit in items:
            row = c.execute(
                "SELECT count FROM daily_quota WHERE day = ? AND subject = ? AND action = ?",
                (day, subject, action),
            ).fetchone()
            if row and row["count"] >= limit:
                c.commit()
                return False
        for subject, action, _ in items:
            c.execute(
                "INSERT INTO daily_quota(day, subject, action, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(day, subject, action) DO UPDATE SET count = count + 1",
                (day, subject, action),
            )
        c.commit()
        return True


def action_quota_limits(action: str) -> tuple[int, int]:
    """Use the same configured caps for enforcement and the account display."""
    action = "upload" if action == "upload_http" else action
    _, user_default, site_default = ACTION_QUOTAS[action]
    return (max(0, int(os.getenv(f"DAILY_{action.upper()}_LIMIT", str(user_default)))),
            max(0, int(os.getenv(f"DAILY_SITE_{action.upper()}_LIMIT", str(site_default)))))


def user_daily_quota(user_id: int, now: datetime | None = None) -> dict:
    """Read only the user's counters; expose site availability without site totals."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    reset_local = reset.astimezone(timezone(timedelta(hours=8)))
    with _DB_LOCK:
        rows = _db().execute(
            "SELECT subject, action, count FROM daily_quota WHERE day = ? AND subject IN (?, 'site')",
            (day, f"user:{int(user_id)}"),
        ).fetchall()
    counts = {(row["subject"], row["action"]): row["count"] for row in rows}
    items = []
    for action, (label, _, _) in ACTION_QUOTAS.items():
        limit, site_limit = action_quota_limits(action)
        counters = ("upload", "upload_http") if action == "upload" else (action,)
        used = max(counts.get((f"user:{int(user_id)}", key), 0) for key in counters)
        site_available = all(counts.get(("site", key), 0) < site_limit for key in counters)
        item = {"action": action, "label": label, "limit": limit,
                "remaining": max(0, limit - used), "site_available": site_available}
        if action == "upload":
            item["stages"] = [
                {"action": key, "label": stage_label,
                 "remaining": max(0, limit - counts.get((f"user:{int(user_id)}", key), 0)),
                 "site_available": counts.get(("site", key), 0) < site_limit}
                for key, stage_label in (("upload_http", "上传新文件"), ("upload", "使用已上传视频"))
            ]
        items.append(item)
    return {"reset_at": reset.isoformat(), "reset_label": reset_local.strftime("北京时间 %m月%d日 08:00"), "items": items}


def quota_notice(user_id: int, action: str) -> str:
    snapshot = user_daily_quota(user_id)
    display_action = "upload" if action == "upload_http" else action
    item = next(row for row in snapshot["items"] if row["action"] == display_action)
    if display_action == "upload":
        # Receiving a new file and importing an already received file reserve
        # separate counters. Report the counter that actually rejected this call.
        item = next(stage for stage in item["stages"] if stage["action"] == action)
    if item["remaining"] == 0:
        message = f"今日{item['label']}额度已用完"
    elif not item["site_available"]:
        message = f"本站今日{item['label']}处理额度已满"
    else:
        return "处理额度暂不可用，请稍后重试；可在账户页查看剩余额度"
    return f"{message}；{snapshot['reset_label']}重置，可在账户页查看剩余额度"


def get_user_by_username(username: str):
    with _DB_LOCK:
        row = _db().execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int):
    with _DB_LOCK:
        row = _db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def change_password(user_id: int, old_password: str, new_password: str) -> None:
    user = get_user_by_id(user_id)
    if not user or not verify_password(old_password, user["password_hash"]):
        raise AuthError("当前密码不正确")
    if len(new_password or "") < 8:
        raise AuthError("新密码至少 8 位")
    with _DB_LOCK:
        _db().execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        # 改密后吊销该用户全部会话（除当前会话外全部失效）
        _db().execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        _db().commit()


def delete_user_account(user_id: int, password: str) -> None:
    """Delete a normal account and its sessions after password confirmation."""
    with _DB_LOCK:
        c = _db()
        user = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user or not verify_password(password or "", user["password_hash"]):
            raise AuthError("密码不正确")
        if user["is_admin"]:
            raise AuthError("管理员账号不能在此删除")
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        c.execute("DELETE FROM daily_quota WHERE subject = ?", (f"user:{user_id}",))
        c.commit()


def register_uploaded_files(user_id: int, paths: list[str]) -> None:
    """Record raw Gradio uploads so their file URLs remain private to uploaders."""
    if not paths:
        return
    with _DB_LOCK:
        c = _db()
        c.executemany(
            "INSERT OR IGNORE INTO uploaded_files(path, user_id, created_at) VALUES (?, ?, ?)",
            [(path, int(user_id), _now()) for path in set(paths)],
        )
        c.execute("DELETE FROM uploaded_files WHERE created_at < ?", (_now() - 3 * 86400,))
        c.commit()


def uploaded_file_access(path: str, user_id: int) -> bool | None:
    """True for an uploader, False for another user, None for an unknown file."""
    rows = _db().execute("SELECT user_id FROM uploaded_files WHERE path = ?", (path,)).fetchall()
    if not rows:
        return None
    return any(row["user_id"] == int(user_id) for row in rows)


# ==================== 限流 ====================

def _throttle_get(c, key: str):
    return c.execute("SELECT * FROM login_throttle WHERE key = ?", (key,)).fetchone()


def check_login_allowed(username: str, ip: str) -> None:
    now = _now()
    with _DB_LOCK:
        c = _db()
        for key in (f"user:{username.lower()}", f"ip:{ip}"):
            row = _throttle_get(c, key)
            if row and row["locked_until"] > now:
                wait = row["locked_until"] - now
                raise AuthError(f"尝试过于频繁，请 {max(wait // 60 + 1, 1)} 分钟后再试")


def record_login_failure(username: str, ip: str) -> None:
    now = _now()
    with _DB_LOCK:
        c = _db()
        for key, max_fails, lock in (
            (f"user:{username.lower()}", ACCOUNT_LOCK_FAILS, ACCOUNT_LOCK_SECONDS),
            (f"ip:{ip}", THROTTLE_MAX_FAILS, THROTTLE_WINDOW),
        ):
            row = _throttle_get(c, key)
            if not row or now - (row["window_start"] or 0) > THROTTLE_WINDOW:
                c.execute(
                    "INSERT INTO login_throttle (key, fail_count, locked_until, window_start) "
                    "VALUES (?, 1, 0, ?) ON CONFLICT(key) DO UPDATE SET "
                    "fail_count=1, locked_until=0, window_start=excluded.window_start",
                    (key, now),
                )
            else:
                fails = row["fail_count"] + 1
                locked_until = now + lock if fails >= max_fails else 0
                c.execute(
                    "UPDATE login_throttle SET fail_count = ?, locked_until = ? WHERE key = ?",
                    (fails, locked_until, key),
                )
        c.commit()


def record_login_success(username: str, ip: str) -> None:
    with _DB_LOCK:
        c = _db()
        c.execute("DELETE FROM login_throttle WHERE key IN (?, ?)",
                  (f"user:{username.lower()}", f"ip:{ip}"))
        c.commit()


# ==================== 会话 ====================

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, ip: str = "", user_agent: str = "") -> str:
    """签发新会话，返回原始 token（放入 Cookie；库里只存哈希）。"""
    token = secrets.token_urlsafe(32)
    now = _now()
    with _DB_LOCK:
        c = _db()
        # 顺手清理过期会话（低频全表小删除，SQLite 可忽略）
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        c.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_token_hash(token), user_id, now, now + SESSION_TTL_SECONDS, ip[:64], user_agent[:256]),
        )
        c.commit()
    return token


def get_session_user(token: str):
    """校验 token，返回 (user_dict, session_row)；无效/过期返回 None。
    滑动续期：剩余寿命不足一半时自动延长。"""
    if not token:
        return None
    now = _now()
    with _DB_LOCK:
        c = _db()
        row = c.execute(
            "SELECT s.*, u.id AS uid, u.username, u.is_admin, u.is_active, "
            "u.created_at AS user_created_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now or not row["is_active"]:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
            c.commit()
            return None
        if row["expires_at"] - now < SESSION_TTL_SECONDS // 2:
            c.execute(
                "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                (now + SESSION_TTL_SECONDS, row["token_hash"]),
            )
            c.commit()
    user = dict(row)
    user["id"] = user.pop("uid")  # 会话用户统一用 users.id
    return user


def delete_session(token: str) -> None:
    if not token:
        return
    with _DB_LOCK:
        c = _db()
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        c.commit()


# ==================== 会话 Cookie 属性 ====================

def session_cookie_kwargs() -> dict:
    secure = os.getenv("SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "max_age": SESSION_TTL_SECONDS,
        "secure": secure,
    }
