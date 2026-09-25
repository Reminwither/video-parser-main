#!/usr/bin/env python3
"""
会话鉴权中间件（ASGI 层，替代 BasicAuthMiddleware）

- 基于 Cookie 的服务端会话（auth_db.py），未登录访问受保护页面 302 → /login?next=
- /api/* 未登录返回 401 JSON（前端 fetch 不跟随 HTML 重定向）
- WebSocket 握手无有效会话 → 1008 关闭
- 豁免：/health（容器健康检查）、/login、/register、/logout、
        /static/css|js|icons|fonts（公共静态资源）、/favicon.ico、/@vite/client
"""

from urllib.parse import quote

from starlette.types import ASGIApp, Receive, Scope, Send

import auth_db

# 这些前缀不要求登录
PUBLIC_PREFIXES = (
    "/health",
    "/login",
    "/register",
    "/logout",
    "/static/css/",
    "/static/js/",
    "/static/icons/",
    "/static/fonts/",
    "/favicon.ico",
    "/@vite/client",
)

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
    # 反代场景优先取 X-Forwarded-For 首段
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            return v.decode("latin-1").split(",")[0].strip()
    client = scope.get("client") or ("", 0)
    return client[0] or "-"


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PREFIXES)


class SessionAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_public(path):
            await self.app(scope, receive, send)
            return

        token = _get_cookie(scope, SESSION_COOKIE)
        session = auth_db.get_session_user(token)

        if session:
            # 会话有效：把用户信息挂到 scope 供路由读取
            scope = dict(scope)
            scope["state"] = dict(scope.get("state") or {})
            scope["state"]["user"] = session
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        # HTTP 未登录
        if path.startswith("/api/"):
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

        next_url = quote(path + (("?" + scope.get("query_string", b"").decode("latin-1")) if scope.get("query_string") else ""), safe="/?=&%")
        await send({
            "type": "http.response.start",
            "status": 302,
            "headers": [
                (b"location", f"/login?next={next_url}".encode("latin-1")),
                (b"content-length", b"0"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
