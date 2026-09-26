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

from urllib.parse import quote

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
    # 反代场景优先取 X-Forwarded-For 首段
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            return v.decode("latin-1").split(",")[0].strip()
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
    return False


class SessionAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        token = _get_cookie(scope, SESSION_COOKIE)
        session = auth_db.get_session_user(token) if token else None

        if session:
            # 会话有效：把用户信息挂到 scope 供路由/ Gradio fn 读取
            scope = dict(scope)
            scope["state"] = dict(scope.get("state") or {})
            scope["state"]["user"] = session
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
