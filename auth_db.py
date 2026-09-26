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
import threading
import time
from datetime import datetime, timezone

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

# SQLite 跨线程：每线程独立连接（threading.local 保存，避免
# "SQLite objects created in a thread" —— 曾把连接缓存在函数对象上导致
# 登录后的业务线程直接抛错）
_DB_LOCK = threading.Lock()
_DB_LOCAL = threading.local()


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
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "max_age": SESSION_TTL_SECONDS,
        # 站点当前为 HTTP，不能加 Secure；上 HTTPS 反代后应加
    }
