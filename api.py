#!/usr/bin/env python3
"""
视频解析下载 API 服务 (FastAPI 版本)
支持抖音、哔哩哔哩、小红书、快手、好看视频等平台
"""

import os
import time
import hashlib
import secrets
import shutil
from typing import Optional
from contextlib import asynccontextmanager

from requests.exceptions import RequestException, ConnectionError
from fastapi import FastAPI, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from configs.logging_config import logger
from configs.general_constants import (
    DOMAIN_TO_NAME, MINI_PROGRAM_LEGAL_DOMAIN,
    SAVE_VIDEO_PATH, DOMAIN, check_essential_dirs
)
from utils.web_fetcher import WebFetcher, UrlParser
from utils.vigenere_cipher import VigenereCipher
from utils.url_safety import open_public_stream
from src.downloader_factory import DownloaderFactory


# ==================== Pydantic 模型 ====================

class ParseRequest(BaseModel):
    """解析请求模型"""
    text: str = Field(max_length=8192)


class DownloadRequest(BaseModel):
    """下载请求模型"""
    video_url: str = Field(max_length=8192)
    video_id: str = Field(max_length=256)


class APIResponse(BaseModel):
    """统一响应模型"""
    retcode: int
    retdesc: str
    data: Optional[dict] = None
    ranking: Optional[dict] = None
    succ: bool


# ==================== 工具函数 ====================

def make_response(retcode: int, retdesc: str, data: dict = None,
                  ranking: dict = None, succ: bool = False) -> dict:
    """生成统一的响应格式"""
    return {
        'retcode': retcode,
        'retdesc': retdesc,
        'data': data,
        'ranking': ranking,
        'succ': succ
    }


def validate_timestamp(request_timestamp: int) -> bool:
    """验证时间戳是否在合理的时间窗口内"""
    current_timestamp = int(time.time() * 1000)
    time_window = 5 * 60 * 1000  # 5分钟的时间窗口
    return abs(current_timestamp - request_timestamp) <= time_window


def validate_request_headers(
    x_timestamp: str,
    x_gclt_text: str,
    x_egct_text: str,
    *args
) -> Optional[dict]:
    """
    验证请求头和参数

    Returns:
        None 如果验证通过，否则返回错误响应字典
    """
    # 检查是否有缺失的参数
    missing_params = [param for param in args if not param]
    if missing_params:
        logger.error(f'Missing parameters in request')
        return make_response(400, 'Missing parameters in request', None, None, False)

    if not x_timestamp:
        logger.error('Missing timestamp in request')
        return make_response(400, 'Missing timestamp in request', None, None, False)

    try:
        if not validate_timestamp(int(x_timestamp)):
            logger.error('Invalid timestamp')
            return make_response(400, 'Invalid timestamp', None, None, False)
    except ValueError:
        logger.error('Invalid timestamp format')
        return make_response(400, 'Invalid timestamp format', None, None, False)

    if not VigenereCipher(x_timestamp).verify_decryption(x_egct_text, x_gclt_text):
        logger.error('Decryption verification failed')
        return make_response(400, 'Decryption verification failed', None, None, False)

    return None


def _prune_expired_videos() -> int:
    """Optionally remove old server-cached videos; disabled unless configured."""
    try:
        retention_days = int(os.getenv("VIDEO_RETENTION_DAYS", "0"))
    except ValueError:
        logger.warning("Invalid VIDEO_RETENTION_DAYS; automatic cleanup is disabled")
        return 0
    if retention_days <= 0:
        return 0

    cutoff = time.time() - retention_days * 24 * 60 * 60
    removed = 0
    try:
        for name in os.listdir(SAVE_VIDEO_PATH):
            path = os.path.join(SAVE_VIDEO_PATH, name)
            if not name.lower().endswith(".mp4") or not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError as exc:
                logger.warning("Unable to prune cached video %s: %s", name, exc)
    except OSError as exc:
        logger.warning("Unable to inspect cached video directory: %s", exc)
    if removed:
        logger.info("Pruned %s cached video(s) older than %s days", removed, retention_days)
    return removed


def _download_public_video(url: str, destination: str, max_bytes: int) -> None:
    """Download with redirect validation, a byte cap, and atomic file replacement."""
    if shutil.disk_usage(os.path.dirname(destination)).free < max_bytes + 1024 * 1024 * 1024:
        raise OSError("服务器存储空间不足")
    response = None
    try:
        response = open_public_stream(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
                    "Referer": "https://www.pearvideo.com/" if "pearvideo.com" in url else "",
                },
                timeout=(10, 30),
            )

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise OverflowError("视频文件超过服务器允许的大小")

        temporary_path = destination + f".{secrets.token_hex(8)}.part"
        written = 0
        try:
            with open(temporary_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise OverflowError("视频文件超过服务器允许的大小")
                    output.write(chunk)
            os.replace(temporary_path, destination)
        except Exception:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
            raise
    finally:
        if response is not None:
            response.close()


# ==================== 应用生命周期 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("Starting Video Parser API Service (FastAPI)...")
    check_essential_dirs()
    if os.getenv("REQUIRE_AUTH", "1").strip().lower() not in {"0", "false", "no", "off"} or os.getenv("APP_PASS", ""):
        auth_db.init_db()
    _prune_expired_videos()
    yield
    # 关闭时执行
    logger.info("Shutting down Video Parser API Service...")


# ==================== 创建 FastAPI 应用 ====================

app = FastAPI(
    title="视频解析下载 API",
    description="支持抖音、哔哩哔哩、小红书、快手、好看视频等平台的视频解析下载服务",
    version="2.0.0",
    lifespan=lifespan
)

# CORS 中间件
_cors_origins = [origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Gradio 前端 dev 检测会请求 /@vite/client，非开发环境返回空 JS 避免 404 噪音
@app.api_route("/@vite/client", methods=["GET", "HEAD"])
async def vite_client_stub():
    from fastapi.responses import Response
    return Response(content="", media_type="application/javascript")


# ==================== 用户认证（数据库 + 会话） ====================
# 页面：/login /register /account；JSON：/api/auth/me /api/auth/change-password
# 中间件（auth_middleware.SessionAuthMiddleware）在 ASGI 层强制登录，
# 通过校验的请求会在 scope.state.user 带上用户信息。

import auth_db
import auth_pages
from auth_middleware import SessionAuthMiddleware, get_client_ip
from fastapi.responses import HTMLResponse, RedirectResponse

_api_auth_enabled = (
    os.getenv("REQUIRE_AUTH", "1").strip().lower() not in {"0", "false", "no", "off"}
    or bool(os.getenv("APP_PASS", ""))
)
if _api_auth_enabled:
    app.add_middleware(SessionAuthMiddleware)


def _allow_register() -> bool:
    return os.getenv("ALLOW_REGISTER", "0").strip().lower() in {"1", "true", "yes", "on"}


def _safe_next(next_url: str) -> str:
    """只允许站内相对路径，防开放重定向。"""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _current_user(request: Request) -> Optional[dict]:
    return getattr(request.scope.get("state", None), "user", None) or \
        (request.scope.get("state") or {}).get("user")


@app.get("/login")
async def login_page(request: Request, next: str = "/"):
    return HTMLResponse(auth_pages.login_page(_safe_next(next), allow_register=_allow_register()))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    next_url = _safe_next(str(form.get("next", "/")))
    ip = get_client_ip(request.scope)

    def fail(err: str, status: int = 200):
        return HTMLResponse(
            auth_pages.login_page(next_url, error=err, allow_register=_allow_register()),
            status_code=status,
        )

    if not username or not password:
        return fail("请输入用户名和密码")

    try:
        auth_db.check_login_allowed(username, ip)
    except auth_db.AuthError as e:
        return fail(str(e))

    user = auth_db.get_user_by_username(username)
    if not user or not user["is_active"] or not auth_db.verify_password(password, user["password_hash"]):
        auth_db.record_login_failure(username, ip)
        logger.warning(f"登录失败: {username} from {ip}")
        return fail("用户名或密码不正确")

    auth_db.record_login_success(username, ip)
    with auth_db._DB_LOCK:
        auth_db._db().execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (auth_db._now_iso(), user["id"]),
        )
        auth_db._db().commit()

    token = auth_db.create_session(
        user["id"], ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(value=token, **auth_db.session_cookie_kwargs())
    logger.info(f"登录成功: {username} from {ip}")
    return resp


@app.get("/register")
async def register_page(request: Request, next: str = "/"):
    if not _allow_register():
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(auth_pages.register_page(_safe_next(next)))


@app.post("/register")
async def register_submit(request: Request):
    if not _allow_register():
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    password2 = str(form.get("password2", ""))
    next_url = _safe_next(str(form.get("next", "/")))

    def fail(err: str):
        return HTMLResponse(auth_pages.register_page(next_url, error=err))

    if password != password2:
        return fail("两次输入的密码不一致")
    ip = get_client_ip(request.scope)
    if not auth_db.consume_daily_quota(f"ip:{ip}", "register", max(1, int(os.getenv("REGISTER_PER_IP_DAILY", "3")))):
        return fail("今日注册次数已达上限，请明天再试")
    try:
        user_id = auth_db.create_user(username, password)
    except auth_db.AuthError as e:
        return fail(str(e))

    logger.info(f"注册成功: {username}")
    token = auth_db.create_session(
        user_id, ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(value=token, **auth_db.session_cookie_kwargs())
    return resp


@app.get("/logout")
async def logout(request: Request):
    cookie = request.cookies.get(auth_db.SESSION_COOKIE, "")
    auth_db.delete_session(cookie)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth_db.SESSION_COOKIE, path="/")
    return resp


@app.get("/account")
async def account_page_view(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?next=/account", status_code=302)
    fresh = auth_db.get_user_by_id(user["id"]) or user
    with auth_db._DB_LOCK:
        n = auth_db._db().execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND expires_at > ?",
            (user["id"], auth_db._now()),
        ).fetchone()["n"]
    return HTMLResponse(auth_pages.account_page(fresh, sessions_active=n))


@app.post("/account/password")
async def change_password_submit(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?next=/account", status_code=302)
    form = await request.form()
    old_password = str(form.get("old_password", ""))
    new_password = str(form.get("new_password", ""))
    try:
        auth_db.change_password(user["id"], old_password, new_password)
    except auth_db.AuthError as e:
        fresh = auth_db.get_user_by_id(user["id"]) or user
        return HTMLResponse(auth_pages.account_page(fresh, error=str(e)))
    logger.info(f"密码已修改并吊销全部会话: {user['username']}")
    # change_password 已吊销全部会话，回登录页重新登录
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth_db.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"retcode": 401, "succ": False})
    return {
        "retcode": 0,
        "succ": True,
        "data": {
            "username": user["username"],
            "is_admin": bool(user.get("is_admin")),
        },
    }


@app.post("/api/auth/login")
async def api_login(request: Request):
    """JSON 登录（供前端登录弹窗 fetch 调用），成功后 Set-Cookie。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"retcode": 400, "succ": False, "retdesc": "请求格式错误"})
    username = str((data or {}).get("username", "")).strip()
    password = str((data or {}).get("password", ""))
    ip = get_client_ip(request.scope)

    if not username or not password:
        return JSONResponse(status_code=400,
                            content={"retcode": 400, "succ": False, "retdesc": "请输入用户名和密码"})

    try:
        auth_db.check_login_allowed(username, ip)
    except auth_db.AuthError as e:
        return JSONResponse(status_code=429,
                            content={"retcode": 429, "succ": False, "retdesc": str(e)})

    user = auth_db.get_user_by_username(username)
    if not user or not user["is_active"] or not auth_db.verify_password(password, user["password_hash"]):
        auth_db.record_login_failure(username, ip)
        logger.warning(f"JSON 登录失败: {username} from {ip}")
        return JSONResponse(status_code=401,
                            content={"retcode": 401, "succ": False, "retdesc": "用户名或密码不正确"})

    auth_db.record_login_success(username, ip)
    with auth_db._DB_LOCK:
        auth_db._db().execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (auth_db._now_iso(), user["id"]),
        )
        auth_db._db().commit()

    token = auth_db.create_session(
        user["id"], ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    resp = JSONResponse(content={
        "retcode": 0, "succ": True,
        "data": {"username": user["username"], "is_admin": bool(user.get("is_admin"))},
    })
    resp.set_cookie(value=token, **auth_db.session_cookie_kwargs())
    logger.info(f"JSON 登录成功: {username} from {ip}")
    return resp


# ==================== API 路由 ====================

@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "healthy", "timestamp": int(time.time() * 1000)}


@app.post("/api/parse")
async def parse_video(
    body: ParseRequest,
    x_timestamp: str = Header(default="", alias="X-Timestamp"),
    x_gclt_text: str = Header(default="", alias="X-GCLT-Text"),
    x_egct_text: str = Header(default="", alias="X-EGCT-Text"),
    wx_open_id: str = Header(default="Guest", alias="WX-OPEN-ID"),
):
    """
    解析视频链接

    从分享链接中提取视频信息，包括标题、封面、视频直链等
    """
    try:
        text = body.text

        # 验证请求
        validation_error = validate_request_headers(x_timestamp, x_gclt_text, x_egct_text, text)
        if validation_error:
            return JSONResponse(status_code=400, content=validation_error)

        # 提取URL
        extracted_url = UrlParser.get_url(text)
        logger.info(f'extracted_url: {extracted_url}')

        if not extracted_url:
            logger.error('No valid URL found in text')
            return JSONResponse(
                status_code=400,
                content=make_response(400, '未找到有效的视频链接', None, None, False)
            )

        # 获取重定向URL
        redirect_url = WebFetcher.fetch_redirect_url(extracted_url)
        logger.info(f'redirect_url: {redirect_url}')

        # 如果重定向失败，直接使用原始URL
        if not redirect_url:
            redirect_url = extracted_url
            logger.info(f'Using original URL: {redirect_url}')

        platform = DOMAIN_TO_NAME.get(UrlParser.get_domain(redirect_url))
        video_id = UrlParser.get_video_id(redirect_url)
        real_url = UrlParser.extract_video_address(redirect_url)
        logger.info(f'platform: {platform}, video_id: {video_id}, real_url: {real_url}')

        if not platform:
            logger.error(f'This link is not supported for extraction: {real_url}')
            return JSONResponse(
                status_code=400,
                content=make_response(400, '该链接尚未支持提取', None, None, False)
            )

        title = cover_url = video_url = None
        downloader = None

        # 小红书平台特殊处理（重试机制）
        if platform == '小红书':
            max_attempts = 2
            attempts = 0
            while attempts < max_attempts:
                downloader = DownloaderFactory.create_downloader(platform, real_url)
                title = downloader.get_title_content()
                video_url = downloader.get_real_video_url()
                cover_url = downloader.get_cover_photo_url()
                if video_url:
                    break
                attempts += 1
                logger.debug(f"Attempt {attempts} failed. Retrying...")
            if not video_url:
                logger.error("Failed to retrieve video URL after 2 attempts.")
        else:
            downloader = DownloaderFactory.create_downloader(platform, real_url)
            title = downloader.get_title_content()
            video_url = downloader.get_real_video_url()
            cover_url = downloader.get_cover_photo_url()

        # 转换为 HTTPS
        updated_video_url = UrlParser.convert_to_https(video_url)
        updated_cover_url = UrlParser.convert_to_https(cover_url)

        if not updated_video_url:
            logger.error(f'No playable resource returned for {platform}: {real_url}')
            return JSONResponse(
                status_code=502,
                content=make_response(502, '平台未返回可播放资源，请稍后重试', None, None, False)
            )

        if downloader and hasattr(downloader, 'get_metadata'):
            data_dict = downloader.get_metadata()
            # 统一保证主地址为 HTTPS；嵌套候选地址保留上游原始顺序。
            data_dict['video_id'] = str(data_dict.get('video_id') or video_id)
            data_dict['platform'] = data_dict.get('platform') or platform
            data_dict['video_url'] = UrlParser.convert_to_https(data_dict.get('video_url'))
            data_dict['cover_url'] = UrlParser.convert_to_https(data_dict.get('cover_url'))
            if data_dict.get('audio_url'):
                data_dict['audio_url'] = UrlParser.convert_to_https(data_dict['audio_url'])
        else:
            data_dict = {
                'video_id': video_id,
                'platform': platform,
                'title': title,
                'video_url': updated_video_url,
                'cover_url': updated_cover_url
            }

        # B站视频需要额外返回音频URL
        if platform == '哔哩哔哩' and downloader and hasattr(downloader, 'get_audio_url'):
            audio_url = downloader.get_audio_url()
            if audio_url:
                data_dict['audio_url'] = UrlParser.convert_to_https(audio_url)

        logger.debug(f'{wx_open_id} {platform} Parse Success')
        return JSONResponse(
            status_code=200,
            content=make_response(200, '成功', data_dict, None, True)
        )

    except Exception as e:
        logger.error(f"Parse error: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content=make_response(500, '平台暂时无法解析该链接，请检查链接是否有效或稍后重试', None, None, False)
        )


@app.post("/api/download")
async def download_video(
    request: Request,
    body: DownloadRequest,
    x_timestamp: str = Header(default="", alias="X-Timestamp"),
    x_gclt_text: str = Header(default="", alias="X-GCLT-Text"),
    x_egct_text: str = Header(default="", alias="X-EGCT-Text"),
    wx_open_id: str = Header(default="Guest", alias="WX-OPEN-ID"),
):
    """
    获取视频下载链接

    对于某些平台，需要服务器端下载并返回可访问的链接
    """
    try:
        request_video_url = body.video_url
        request_video_id = body.video_id

        # 验证请求
        validation_error = validate_request_headers(x_timestamp, x_gclt_text, x_egct_text, request_video_url)
        if validation_error:
            return JSONResponse(status_code=400, content=validation_error)

        # 判断视频链接的域名是否为小程序的合法域名
        domain = UrlParser.get_domain(request_video_url)
        logger.debug(f'Checking domain: {domain}')

        if domain in MINI_PROGRAM_LEGAL_DOMAIN:
            # 如果是，直接返回视频链接
            logger.debug(f'{wx_open_id} 直接返回视频链接')
            return JSONResponse(
                status_code=200,
                content=make_response(200, '成功', {'download_url': request_video_url}, None, True)
            )
        else:
            # 如果不是，服务器端下载视频并保存
            video_filename = hashlib.sha256(
                f"{request_video_id}:{request_video_url}".encode("utf-8")
            ).hexdigest()[:32] + ".mp4"
            video_path = os.path.join(SAVE_VIDEO_PATH, video_filename)

            # 获取当前域名，用于返回静态资源链接
            host = request.headers.get("host", "localhost:7860")
            protocol = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
            current_domain = DOMAIN or f"{protocol}://{host}"

            if not os.path.exists(video_path):
                logger.info(f"Starting server-side download for {request_video_url}")
                try:
                    max_video_mb = max(1, int(os.getenv("MAX_VIDEO_DOWNLOAD_MB", "500")))
                    _download_public_video(request_video_url, video_path, max_video_mb * 1024 * 1024)
                except OverflowError as e:
                    return JSONResponse(
                        status_code=413,
                        content=make_response(413, str(e), None, None, False),
                    )
                except ValueError as e:
                    return JSONResponse(
                        status_code=400,
                        content=make_response(400, str(e), None, None, False),
                    )
                except (RequestException, ConnectionError, OSError) as e:
                    logger.error(f'{wx_open_id} Failed to connect: {e}')
                    return JSONResponse(
                        status_code=200,
                        content=make_response(200, '服务器下载失败，返回原始链接', {'download_url': request_video_url}, None, True)
                    )

            # 返回视频的 URL
            download_url = f'{current_domain}/static/videos/{video_filename}'
            logger.debug(f'{wx_open_id} 返回视频地址: {download_url}')
            return JSONResponse(
                status_code=200,
                content=make_response(200, '成功', {'download_url': download_url}, None, True)
            )

    except Exception as e:
        logger.error(f"Download error: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content=make_response(500, '功能太火爆啦，请稍后再试', None, None, False)
        )


# ==================== 主程序入口 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=7860,
        reload=True,
        log_level="info"
    )
