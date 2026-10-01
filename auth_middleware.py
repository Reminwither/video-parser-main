#!/usr/bin/env python3
"""
会话鉴权中间件（ASGI 层，替代 BasicAuthMiddleware）

设计目标（访客可浏览 + 操作才登录）：
- 主页与整个 Gradio 应用/资源**匿名可访问**，访客能完整浏览工作台 UI；
- 真正消耗后端算力的「业务动作」由「前端弹登录框(modal) + 后端 fn 内 guard」双重拦截，
  因此中间件本身对主页/资源/WS 握手一律放行；
- /account* 需登录（整页跳 /login?next=）；
- /api/*（除 /api/auth/me）需登录（返回 401 JSON，前端 fetch 不跟随 HTML 重定向）；
- WebSocket（/queue/data）握手放行，但功能 fn 内部会校验会话（防匿名真的调用算力）；
- 豁免：/health（容器健康检查）、/login、/register、/logout、公共静态资源、/favicon.ico、/@vite/client。
"""

import ipaddress
import json
import os
import re
import tempfile
from urllib.parse import quote, unquote

from starlette.types import ASGIApp, Receive, Scope, Send

import auth_db

SESSION_COOKIE = auth_db.SESSION_COOKIE


def _get_cookie(scope: Scope, name: str) -> str:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                if "=" in part:
                    kk, vv = part.split("=", 1)
                    if kk.strip() == name:
                        return vv.strip()
    return ""


def get_client_ip(scope: Scope) -> str:
    # The public Caddy proxy overwrites X-Forwarded-For with the remote address.
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            candidate = v.decode("latin-1").split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                break
    client = scope.get("client") or ("", 0)
    return client[0] or "-"


def _requires_login_redirect(path: str) -> bool:
    """账户页：未登录整页跳登录。"""
    return path == "/account" or path.startswith("/account/")


def _requires_login_json(path: str) -> bool:
    """业务 API：未登录返回 401 JSON。
    /api/auth/* 是鉴权入口（me/登录），必须匿名可达；其余 /api/* 需登录。"""
    if path.startswith("/api/auth/"):
        return False
    if path.startswith("/api/"):
        return True
    if path == "/gradio_api/upload":
        return True
    if path.startswith("/static/videos/") or path.startswith("/gradio_api/file=") or path.startswith("/file="):
        return True
    return False


def _file_access_allowed(path: str, user_id: int) -> bool:
    """Enforce ownership of private outputs and raw Gradio uploads."""
    if not (path.startswith("/gradio_api/file=") or path.startswith("/file=")):
        return True
    file_path = os.path.realpath(unquote(path.split("file=", 1)[1]))
    name = os.path.basename(file_path)
    private = re.match(r"^vp-u([1-9][0-9]*)-", name)
    if private:
        return int(private.group(1)) == int(user_id)
    # Reports created before per-user names were introduced have no recoverable
    # ownership metadata, so revoke their old download links.
    if "/reports/" in file_path.replace("\\", "/"):
        return False
    upload_access = auth_db.uploaded_file_access(file_path, user_id)
    return upload_access is not False


def _upload_response_paths(body: bytes) -> list[str]:
    """Extract Gradio's server-side paths from its JSON upload response."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return []
    paths = []
    upload_root = os.path.realpath(os.getenv("GRADIO_TEMP_DIR") or os.path.join(tempfile.gettempdir(), "gradio"))

    def visit(value):
        if isinstance(value, str):
            resolved = os.path.realpath(value)
            try:
                if os.path.isfile(resolved) and os.path.commonpath((resolved, upload_root)) == upload_root:
                    paths.append(resolved)
            except ValueError:
                pass
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    visit(payload)
    return paths


def _export_filename(path: str, user_id: int) -> str | None:
    """Recognize the two download entry points, excluding previews and videos."""
    if path.startswith(("/gradio_api/file=", "/file=")):
        name = unquote(path.split("file=", 1)[1]).replace("\\", "/").rsplit("/", 1)[-1]
    elif path.startswith("/account/files/"):
        name = path.removeprefix("/account/files/")
    else:
        return None
    match = auth_db.REPORT_FILE_RE.fullmatch(name)
    return name if match and int(match.group(1)) == int(user_id) else None


class SessionAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("type") == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        token = _get_cookie(scope, SESSION_COOKIE)
        session = auth_db.get_session_user(token) if token else None

        if session:
            if scope.get("type") == "http" and not _file_access_allowed(path, session["id"]):
                await send({
                    "type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json; charset=utf-8")],
                })
                await send({"type": "http.response.body", "body": b'{"detail":"Forbidden"}'})
                return
            action = {
                "/api/parse": "parse",
                "/api/download": "download",
                "/gradio_api/upload": "upload_http",
            }.get(path)
            if scope.get("type") == "http" and scope.get("method") == "POST" and action:
                name = action
                limit, site_limit = auth_db.action_quota_limits(name)
                if not auth_db.consume_daily_quotas([
                    (f"user:{session['id']}", name, limit), ("site", name, site_limit),
                ]):
                    await send({
                        "type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json; charset=utf-8")],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": json.dumps({"retcode": 429, "retdesc": auth_db.quota_notice(session["id"], name), "succ": False}, ensure_ascii=False).encode("utf-8"),
                    })
                    return
            # 会话有效：把用户信息挂到 scope 供路由/ Gradio fn 读取
            scope = dict(scope)
            scope["state"] = dict(scope.get("state") or {})
            scope["state"]["user"] = session
            if scope.get("type") == "http" and scope.get("method") == "POST" and path == "/gradio_api/upload":
                chunks = []
                status = 0

                async def record_upload(message):
                    nonlocal status
                    if message["type"] == "http.response.start":
                        status = message["status"]
                    elif message["type"] == "http.response.body" and status in (200, 201):
                        chunks.append(message.get("body", b""))
                        if not message.get("more_body", False):
                            auth_db.register_uploaded_files(session["id"], _upload_response_paths(b"".join(chunks)))
                    await send(message)

                await self.app(scope, receive, record_upload)
            elif scope.get("type") == "http" and scope.get("method") == "GET" and (filename := _export_filename(path, session["id"])):
                status = 0
                finished = False

                async def track_export(message):
                    nonlocal status, finished
                    await send(message)
                    if message["type"] == "http.response.start":
                        status = message["status"]
                    elif message["type"] == "http.response.body" and not message.get("more_body", False):
                        finished = True

                await self.app(scope, receive, track_export)
                # Partial/range responses, HEAD probes and failed sends do not
                # prove full delivery. Telemetry never buffers the report body.
                if status == 200 and finished:
                    auth_db.record_artifact_export(session["id"], filename)
            else:
                await self.app(scope, receive, send)
            return

        # ---- 未登录分支 ----
        if _requires_login_redirect(path):
            next_url = quote(
                path + (("?" + scope.get("query_string", b"").decode("latin-1"))
                        if scope.get("query_string") else ""),
                safe="/?=&%",
            )
            await send({
                "type": "http.response.start",
                "status": 302,
                "headers": [
                    (b"location", f"/login?next={next_url}".encode("latin-1")),
                    (b"content-length", b"0"),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        if _requires_login_json(path):
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"retcode":401,"retdesc":"\\u672a\\u767b\\u5f55\\u6216\\u4f1a\\u8bdd\\u5df2\\u8fc7\\u671f","succ":false}',
            })
            return

        # 其余（主页 / Gradio 资源 / WS 握手）放行：访客可浏览工作台
        await self.app(scope, receive, send)
