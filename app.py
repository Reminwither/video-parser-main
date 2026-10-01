#!/usr/bin/env python3
"""
视频解析下载 Gradio 应用
核心平台：抖音、B站、小红书、微信视频号
"""

import os
import html
import base64
import hashlib
import logging
import shutil
import secrets
import time
import random
import string
import threading
import requests
import re
import subprocess
import tempfile
from pathlib import Path
import gradio as gr
from typing import Optional, Tuple
from starlette.types import ASGIApp, Scope, Receive, Send
from urllib.parse import urlparse, parse_qs, urlencode
from openai import OpenAI
from dotenv import load_dotenv
from api import app as api_app, parse_video as api_parse_video, download_video as api_download_video, ParseRequest, DownloadRequest
import auth_db
import auth_pages
from auth_middleware import SessionAuthMiddleware
from utils.url_safety import fetch_public_response, open_public_stream
from video_analysis import analyze_video_evidence_first, format_asr_srt, format_diarized_transcript, inspect_video, transcribe_audio

# 加载环境变量
load_dotenv()


# ==================== 认证相关 ====================

class VigenereCipher:
    """用于 API 认证的加密类"""

    def __init__(self, timestamp):
        self.key = self.timestamp_to_letters(timestamp)

    @staticmethod
    def timestamp_to_letters(timestamp):
        digits_to_letters = 'abcdefghijklmnopqrstuvwxyz'
        result = ''
        for char in timestamp:
            if char.isdigit():
                index = int(char)
                if 0 <= index < len(digits_to_letters):
                    result += digits_to_letters[index]
                else:
                    result += 'a'
            else:
                result += 'a'
        return result

    def vigenere_encrypt(self, text):
        encrypted = []
        key_index = 0
        for char in text:
            if char.isalpha():
                shift = 65 if char.isupper() else 97
                key_char = self.key[key_index % len(self.key)].lower()
                key_shift = ord(key_char) - 97
                encrypted_char = chr((ord(char) - shift + key_shift) % 26 + shift)
                encrypted.append(encrypted_char)
                key_index += 1
            else:
                encrypted.append(char)
        return ''.join(encrypted)


def generate_complex_text(length=32):
    """生成随机字符串用于认证"""
    characters = string.ascii_letters
    return ''.join(random.choice(characters) for _ in range(length))


def get_auth_headers(client_id: str = "gradio-client"):
    """生成带有认证信息的请求头"""
    timestamp = str(int(time.time() * 1000))
    original_text = generate_complex_text()
    cipher = VigenereCipher(timestamp)
    encrypted_text = cipher.vigenere_encrypt(original_text)

    return {
        'X-Timestamp': timestamp,
        'X-GCLT-Text': original_text,
        'X-EGCT-Text': encrypted_text,
        'WX-OPEN-ID': client_id,
        'Content-Type': 'application/json'
    }


# ==================== 会话校验（供功能按钮后端兜底） ====================

def _vp_session_from_request(request):
    """从 gr.Request 取 vp_session cookie 并校验，返回 user dict 或 None。

    匿名用户直接访问 Gradio predict 接口在中间件层是放行的，
    因此真正的访问边界放在每个功能函数里：未登录返回 None，
    函数据此返回「请先登录」提示而不执行实际工作。
    """
    if request is None:
        return None
    starlette_req = getattr(request, "request", None)
    if starlette_req is None:
        return None
    token = starlette_req.cookies.get("vp_session", "")
    if not token:
        return None
    return auth_db.get_session_user(token)


# Gradio 按钮前置 JS：匿名用户记录「待续动作」并弹登录框；已登录则透传输入值。
# 登录成功后 vpSubmitLogin 读取 window.__vp_pending 自动续点原按钮——
# 用户先粘贴链接再点「解析视频」→ 弹窗登录 → 自动继续解析，链接不丢、不用重点。
# 不依赖 return false 取消语义（各版本行为不一），未登录时仍返回原输入，
# 由后端 _vp_session_from_request 兜底拦截，避免输入数量不匹配。
def _gate_js(action: str) -> str:
    return """
(...args) => {
  if (!window.__vp_logged_in) {
    window.__vp_pending = '__ACTION__';
    if (window.vpOpenLoginModal) window.vpOpenLoginModal();
  }
  return args;
}
""".replace("__ACTION__", action)


GATE_JS = _gate_js("parse")


# ==================== URL 处理 ====================

def clean_url(url: str) -> str:
    """清理 URL，只保留必要的参数"""
    parsed = urlparse(url)

    # 小红书链接处理
    if 'xiaohongshu.com' in parsed.netloc:
        query_params = parse_qs(parsed.query)
        essential_params = {}
        if 'xsec_token' in query_params:
            essential_params['xsec_token'] = query_params['xsec_token'][0]
        if 'xsec_source' in query_params:
            essential_params['xsec_source'] = query_params['xsec_source'][0]
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if essential_params:
            clean_url += '?' + urlencode(essential_params)
        return clean_url

    # 快手链接处理
    if 'kuaishou.com' in parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    return url


def detect_platform(url: str) -> str:
    """根据 URL 自动检测平台"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (host == "weixin.qq.com" and parsed.path.startswith("/sph/")) or host == "channels.weixin.qq.com":
        return "视频号"
    if host in {"douyin.com", "www.douyin.com", "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}:
        return "抖音"
    elif host == "b23.tv" or host == "bilibili.com" or host.endswith(".bilibili.com"):
        return "哔哩哔哩"
    elif host == "xhslink.com" or host.endswith(".xhslink.com") or host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
        return "小红书"
    return "自动检测"


# ==================== API 调用 ====================

# Qwen3-VL API 配置（从环境变量读取）
# 优先从环境变量读取，避免被其他不相关的环境变量（如 API_SERVER_URL）干扰
QWEN_API_BASE_URL = os.getenv('QWEN_API_BASE_URL', 'https://api-inference.modelscope.cn/v1')
QWEN_API_KEY = os.getenv('QWEN_API_KEY', '')
QWEN_MODEL_ID = os.getenv('QWEN_MODEL_ID', 'Qwen/Qwen3.5-27B')
AI_ANALYSIS_ENABLED = bool(QWEN_API_KEY) and os.getenv('AI_ANALYSIS_ENABLED', '1').strip().lower() not in {'0', 'false', 'no', 'off'}

# 强制检查：如果 QWEN_MODEL_ID 看起来像一个 URL（通常是因为环境变量冲突），则重置为默认值
if QWEN_MODEL_ID.startswith('http'):
    print(f"警告: 检测到异常的模型 ID: {QWEN_MODEL_ID}，正在重置为默认值")
    QWEN_MODEL_ID = 'Qwen/Qwen3.5-27B'

MAX_ANALYSIS_FRAMES = int(os.getenv('MAX_ANALYSIS_FRAMES', '24'))


def max_video_download_bytes() -> int:
    try:
        limit_mb = int(os.getenv("MAX_VIDEO_DOWNLOAD_MB", "500"))
    except ValueError:
        limit_mb = 500
    return max(1, limit_mb) * 1024 * 1024


def _media_transfer_message(error: Exception, action: str) -> str:
    if isinstance(error, ValueError) and str(error).startswith("视频超过单文件下载上限"):
        return f"视频超过 {max_video_download_bytes() // (1024 * 1024)} MB 处理上限，请选择较短视频"
    return f"{action}失败，请检查来源链接是否仍有效或稍后重试"


def max_upload_bytes() -> int:
    """Keep user uploads below the existing media cap and a 200 MiB single-file ceiling."""
    return min(max_video_download_bytes(), 200 * 1024 * 1024)

class VideoClient:
    """视频解析下载客户端"""

    def __init__(self, server_url: str = None):
        # 默认使用容器内网地址，环境变量可覆盖
        if server_url is None:
            # 默认使用 7860 端口（合并服务模式）
            server_url = os.getenv('API_SERVER_URL', 'http://127.0.0.1:7860')
        self.server_url = server_url.rstrip('/')
        self.download_dir = os.path.join(os.path.dirname(__file__), "downloads")
        self.cache_dir = os.path.join(os.path.dirname(__file__), "cache")
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    def download_cover(self, cover_url: str, video_id: str, referer: str = None) -> Optional[str]:
        """
        下载封面图片到本地缓存

        Returns:
            本地文件路径或 None
        """
        if not cover_url:
            return None

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
        }
        if referer:
            headers['Referer'] = referer

        # 生成缓存文件名
        safe_id = "".join(c for c in video_id if c.isalnum())[:30] or "cover"
        cache_path = os.path.join(self.cache_dir, f"{safe_id}_cover.jpg")

        # 如果已经缓存过，直接返回
        if os.path.exists(cache_path):
            return cache_path

        try:
            response = fetch_public_response(cover_url, headers=headers, timeout=30, max_bytes=8 * 1024 * 1024)
            response.raise_for_status()

            temporary_path = cache_path + f".{secrets.token_hex(6)}.part"
            with open(temporary_path, 'wb') as f:
                f.write(response.content)
            os.replace(temporary_path, cache_path)
            response.close()
            return cache_path
        except Exception as e:
            if 'temporary_path' in locals() and os.path.exists(temporary_path):
                os.remove(temporary_path)
            print(f"下载封面失败: {e}")
            return None

    async def parse_video(self, video_url: str) -> Tuple[bool, dict, str]:
        """
        解析视频链接

        Returns:
            (success, data, message)
        """
        clean_video_url = clean_url(video_url)
        payload = ParseRequest(text=clean_video_url)
        auth_headers = get_auth_headers()

        try:
            # 直接调用 api.py 中的函数，模拟 FastAPI 的请求环境
            from fastapi import Request
            from starlette.datastructures import Headers
            
            # 由于 api_parse_video 主要是逻辑处理，我们直接传入参数
            response = await api_parse_video(
                body=payload,
                x_timestamp=auth_headers['X-Timestamp'],
                x_gclt_text=auth_headers['X-GCLT-Text'],
                x_egct_text=auth_headers['X-EGCT-Text'],
                wx_open_id=auth_headers['WX-OPEN-ID']
            )
            
            # response 是 JSONResponse 对象
            import json
            result = json.loads(response.body.decode())

            if result.get('succ') or result.get('retcode') == 200:
                data = result.get('data', {})
                return True, data, "解析成功"
            else:
                return False, {}, result.get('retdesc', '解析失败')

        except Exception:
            logging.exception("Video client parse failed")
            return False, {}, "解析暂时失败，请检查链接是否有效或稍后重试"

    async def get_download_url(self, video_url: str, video_id: str, original_url: str = "") -> Optional[str]:
        """获取下载链接"""
        payload = DownloadRequest(
            video_url=video_url,
            video_id=video_id
        )
        auth_headers = get_auth_headers()

        try:
            # 创建一个模拟的 Request 对象，用于 api_download_video 获取 host
            from fastapi import Request
            from starlette.datastructures import Headers
            
            # 模拟 request 对象
            # 如果是单端口模式，我们需要获取当前请求的 host
            # 在 Gradio 内部调用时，我们假定是 localhost:7860
            scope = {
                "type": "http",
                "headers": [
                    (b"host", b"localhost:7860"),
                    (b"x-forwarded-proto", b"http")
                ]
            }
            mock_request = Request(scope=scope)

            response = await api_download_video(
                request=mock_request,
                body=payload,
                x_timestamp=auth_headers['X-Timestamp'],
                x_gclt_text=auth_headers['X-GCLT-Text'],
                x_egct_text=auth_headers['X-EGCT-Text'],
                wx_open_id=auth_headers['WX-OPEN-ID']
            )
            
            import json
            result = json.loads(response.body.decode())

            if result.get('succ') or result.get('retcode') == 200:
                return result.get('data', {}).get('download_url')
            return None
        except Exception as e:
            print(f"获取下载链接出错: {e}")
            return None

    def download_file(self, url: str, filename: str, referer: str = None,
                      progress_callback=None) -> Tuple[bool, str, str]:
        """
        下载文件到本地

        Returns:
            (success, filepath, message)
        """
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        if referer:
            headers['Referer'] = referer

        filepath = os.path.join(self.download_dir, filename)

        response = None
        try:
            response = open_public_stream(url, headers=headers, timeout=180)

            total_size = int(response.headers.get('content-length', 0))
            if total_size > max_video_download_bytes():
                raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
            downloaded_size = 0

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if downloaded_size > max_video_download_bytes():
                            raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
                        if progress_callback and total_size > 0:
                            progress = downloaded_size / total_size
                            progress_callback(progress)

            response.close()
            size_mb = os.path.getsize(filepath) / 1024 / 1024
            return True, filepath, f"下载完成 ({size_mb:.1f} MB)"

        except Exception as error:
            if response is not None:
                response.close()
            if os.path.exists(filepath):
                os.remove(filepath)
            logging.exception("Video file download failed")
            return False, "", _media_transfer_message(error, "下载")

    def merge_video_audio(self, video_path: str, audio_path: str,
                          output_path: str) -> Tuple[bool, str]:
        """使用 ffmpeg 合并视频和音频"""
        try:
            cmd = [
                os.getenv('FFMPEG_PATH', 'ffmpeg'), '-y',
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-strict', 'experimental',
                output_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                # 删除临时文件
                os.remove(video_path)
                os.remove(audio_path)
                return True, "合并成功"
            else:
                logging.error("FFmpeg media merge failed: %s", result.stderr[-2000:])
                return False, "音视频合并失败，请稍后重试"
        except FileNotFoundError:
            logging.exception("FFmpeg is unavailable")
            return False, "音视频处理服务暂时不可用，请稍后重试"


# ==================== Gradio 界面函数 ====================

# 全局客户端实例
client = VideoClient()


def _video_cache_key(video_info: dict) -> str:
    supplied = video_info.get("_cache_key")
    if isinstance(supplied, str) and re.fullmatch(r"vp-u[1-9][0-9]*-[0-9a-f]{32}", supplied):
        return supplied
    identity = "|".join(str(video_info.get(key, "")) for key in ("platform", "video_id", "original_url"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _owns_uploaded_video(user: dict, video_info: dict) -> bool:
    key = video_info.get("_cache_key")
    if isinstance(key, str) and key.startswith("vp-u"):
        return bool(re.fullmatch(rf"vp-u{int(user['id'])}-[0-9a-f]{{32}}", key))
    return not video_info.get("_uploaded")


_PARSE_SLOTS = threading.BoundedSemaphore(max(1, int(os.getenv("MAX_CONCURRENT_PARSE", "3"))))
_TRANSFER_SLOTS = threading.BoundedSemaphore(max(1, int(os.getenv("MAX_CONCURRENT_TRANSFER", "2"))))
_ANALYSIS_SLOTS = threading.BoundedSemaphore(max(1, int(os.getenv("MAX_CONCURRENT_ANALYSIS", "1"))))
_TRANSCRIBE_SLOTS = threading.BoundedSemaphore(max(1, int(os.getenv("MAX_CONCURRENT_TRANSCRIBE", "1"))))


def _allow_user_action(user: dict, action: str, default_limit: int) -> bool:
    limit = max(0, int(os.getenv(f"DAILY_{action.upper()}_LIMIT", str(default_limit))))
    site_default = {"parse": 300, "upload": 30, "play": 50, "download": 50, "analysis": 10, "transcribe": 30}[action]
    site_limit = max(0, int(os.getenv(f"DAILY_SITE_{action.upper()}_LIMIT", str(site_default))))
    return auth_db.consume_daily_quotas([
        (f"user:{user['id']}", action, limit),
        ("site", action, site_limit),
    ])


def _media_disk_available() -> bool:
    return shutil.disk_usage(client.cache_dir).free >= 2 * 1024 * 1024 * 1024


def parse_video(url: str, platform: str, request: gr.Request = None) -> tuple:
    """
    解析视频

    Returns:
        (status_message, info_bar_html, cover_update, video_url, guide_html, session_video_info)
    """
    user = _vp_session_from_request(request)
    if not user:
        return "🔒 请先登录后再使用此功能 / Please sign in first", _info_bar_html("请先登录 / Please sign in", "", ok=False), gr.update(visible=False), "", EMPTY_GUIDE_HTML, {}
    started = time.monotonic()
    metric_platform = detect_platform(url) if url else platform
    platform_issue = _platform_issue(url, platform)
    if platform_issue:
        auth_db.record_usage("parse", metric_platform, "unsupported", "platform_mismatch" if "不一致" in platform_issue[1] else "")
        message, title, badge = platform_issue
        return message, _info_bar_html(title, badge, ok=False), gr.update(visible=False), "", EMPTY_GUIDE_HTML, {}
    if not _PARSE_SLOTS.acquire(blocking=False):
        auth_db.record_usage("parse", metric_platform, "busy")
        return "当前解析任务较多，请稍后重试", _info_bar_html("当前解析任务较多", "", ok=False), gr.update(visible=False), "", EMPTY_GUIDE_HTML, {}
    try:
        if not _allow_user_action(user, "parse", 30):
            auth_db.record_usage("parse", metric_platform, "quota")
            return "今日解析次数已用完，请明天再试", _info_bar_html("今日解析次数已用完", "", ok=False), gr.update(visible=False), "", EMPTY_GUIDE_HTML, {}
        import asyncio
        status, info, cover, video_url, guide, video_info = asyncio.run(_async_parse_video(url, platform, user["id"]))
        if video_info:
            auth_db.record_user_activity(user["id"], "parse_success")
        auth_db.record_usage(
            "parse", video_info.get("platform", metric_platform) if video_info else metric_platform,
            "success" if video_info else "failed", "" if video_info else "upstream",
            int((time.monotonic() - started) * 1000),
        )
    except Exception:
        logging.exception("Video parse failed")
        auth_db.record_usage("parse", metric_platform, "failed", "error", int((time.monotonic() - started) * 1000))
        return "解析服务暂时不可用，请稍后重试", _info_bar_html("解析服务暂时不可用", "", ok=False), gr.update(visible=False), "", EMPTY_GUIDE_HTML, {}
    finally:
        _PARSE_SLOTS.release()
    if cover:
        cover_out = gr.update(visible=True, value=cover)
    else:
        cover_out = gr.update(visible=False)
    return status, info, cover_out, video_url, guide, video_info


def parse_video_for_ui(url: str, platform: str, request: gr.Request = None) -> tuple:
    """Clear prior media and exports whenever a new link is submitted."""
    result = parse_video(url, platform, request)
    return (*result, gr.update(value=None, visible=False), gr.update(value=None, visible=False),
            REPORT_PLACEHOLDER, gr.update(value=None, visible=False),
            gr.update(value=None, visible=False), gr.update(value=None, visible=False),
            gr.update(value=None))


def upload_owned_video(file_path: str | None, platform: str, request: gr.Request = None) -> tuple:
    """Import an owned MP4 for the same playback, ASR and analysis workflow."""
    empty = (gr.update(value=None, visible=False), "", EMPTY_GUIDE_HTML, {},
             gr.update(value=None, visible=False), gr.update(value=None, visible=False),
             REPORT_PLACEHOLDER, gr.update(value=None, visible=False),
             gr.update(value=None, visible=False), gr.update(value=None, visible=False))

    def rejected(message: str, outcome: str = "invalid", reason: str = "") -> tuple:
        auth_db.record_usage("upload", platform, outcome, reason)
        return (message, _info_bar_html(message, "", ok=False), *empty)

    user = _vp_session_from_request(request)
    if not user:
        return ("🔒 请先登录后再上传", _info_bar_html("请先登录", "", ok=False), *empty)
    if not file_path:
        return rejected("请先选择 MP4 视频文件")
    try:
        from gradio.utils import get_upload_folder
        source = Path(file_path).resolve(strict=True)
        upload_root = Path(get_upload_folder()).resolve(strict=True)
        if os.path.commonpath([source, upload_root]) != str(upload_root) or source.suffix.lower() != ".mp4":
            return rejected("只接受通过页面上传的 MP4 视频")
        file_size = source.stat().st_size
        if not 0 < file_size <= max_upload_bytes():
            return rejected(f"视频大小需在 1 字节至 {max_upload_bytes() // (1024 * 1024)} MB 之间")
        if shutil.disk_usage(client.cache_dir).free < 2 * 1024 * 1024 * 1024 + file_size:
            return rejected("服务器存储空间不足，请稍后重试", "failed", "error")
        media = inspect_video(str(source))
        max_duration = max(1, int(os.getenv("MAX_UPLOAD_DURATION_SECONDS", "1800")))
        if media.width <= 0 or media.height <= 0 or not 0 < media.duration <= max_duration:
            return rejected(f"请上传时长不超过 {max_duration // 60} 分钟的有效 MP4 视频")
    except (OSError, ValueError, RuntimeError):
        logging.exception("Uploaded video validation failed")
        return rejected("视频文件无法读取，请检查格式后重试")
    if not _TRANSFER_SLOTS.acquire(blocking=False):
        return rejected("当前视频加载任务较多，请稍后重试", "busy")
    try:
        if not _allow_user_action(user, "upload", 5):
            return rejected("今日视频上传次数已用完，请明天再试", "quota")
        cache_key = f"vp-u{int(user['id'])}-{secrets.token_hex(16)}"
        cache_path = Path(client.cache_dir) / f"{cache_key}_play.mp4"
        temporary_path = cache_path.with_suffix(".part")
        try:
            shutil.copyfile(source, temporary_path)
            if temporary_path.stat().st_size != file_size:
                raise OSError("upload copy size mismatch")
            os.replace(temporary_path, cache_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
            logging.exception("Uploaded video cache copy failed")
            return rejected("视频保存失败，请稍后重试", "failed", "error")
    finally:
        _TRANSFER_SLOTS.release()
    selected = platform if platform in ("抖音", "哔哩哔哩", "小红书", "视频号") else "自有视频"
    video_info = {"platform": selected, "title": source.stem[:80], "video_id": cache_key,
                  "_cache_key": cache_key, "_uploaded": True, "duration": media.duration}
    auth_db.record_user_activity(user["id"], "upload_success")
    auth_db.record_usage("upload", selected, "success")
    return (f"上传完成 - {selected}", _info_bar_html(source.stem[:80], selected, ok=True),
            gr.update(value=None, visible=False), "", "", video_info,
            gr.update(value=str(cache_path), visible=True), gr.update(value=str(cache_path), visible=True),
            REPORT_PLACEHOLDER, gr.update(value=None, visible=False),
            gr.update(value=None, visible=False), gr.update(value=None, visible=False))

async def _async_parse_video(url: str, platform: str, owner_id: int) -> tuple:

    if not url or not url.strip():
        return "请输入视频链接", _info_bar_html("请输入视频链接", "", ok=False), None, "", EMPTY_GUIDE_HTML, {}

    url = url.strip()
    platform_issue = _platform_issue(url, platform)
    if platform_issue:
        message, title, badge = platform_issue
        return message, _info_bar_html(title, badge, ok=False), None, "", EMPTY_GUIDE_HTML, {}

    detected = detect_platform(url)
    if platform == "自动检测":
        platform = detected if detected != "自动检测" else "未知平台"

    success, data, message = await client.parse_video(url)

    if success:
        video_info = dict(data)
        video_info['original_url'] = url
        video_info['_cache_key'] = f"vp-u{int(owner_id)}-{secrets.token_hex(16)}"

        title = data.get('title', '无标题')
        cover_url = data.get('cover_url', '')
        video_url = data.get('video_url', '')
        video_id = data.get('video_id', 'unknown')
        platform_name = data.get('platform', platform)

        # 获取 referer 用于下载封面
        referer = None
        if 'douyin.com' in url:
            referer = 'https://www.douyin.com/'
        elif 'bilibili.com' in url:
            referer = url
        elif 'xiaohongshu.com' in url:
            referer = 'https://www.xiaohongshu.com/'
        elif 'kuaishou.com' in url:
            referer = 'https://www.kuaishou.com/'
        elif 'haokan.baidu.com' in url:
            referer = 'https://haokan.baidu.com/'

        # 下载封面到本地（避免 Gradio 直接访问外部 URL 失败）
        cover_local_path = None
        if cover_url:
            cover_local_path = client.download_cover(cover_url, _video_cache_key(video_info), referer)

        status = f"解析成功 - {platform_name}"
        return status, _info_bar_html(title, platform_name, ok=True), cover_local_path, video_url, "", video_info
    else:
        return f"解析失败: {message}", _info_bar_html("解析失败：" + message, "", ok=False), None, "", EMPTY_GUIDE_HTML, {}


def _platform_issue(url: str, platform: str) -> Optional[tuple[str, str, str]]:
    """Reject mismatched choices and video-number links before charging parse quota."""
    detected = detect_platform(url) if url else "自动检测"
    if platform != "自动检测" and detected != "自动检测" and detected != platform:
        return (
            f"链接识别为{detected}，但当前选择的是{platform}；请切换平台或使用自动检测",
            "平台选择与链接不一致",
            "",
        )
    selected = detected if platform == "自动检测" else platform
    if selected == "视频号":
        return (
            "已识别为微信视频号链接，但暂不能直接解析。可在下方上传本人有权使用的 MP4 视频。",
            "视频号解析通道尚未接入",
            "视频号",
        )
    return None


def play_video(video_info: dict, progress=gr.Progress(), request: gr.Request = None) -> Tuple[str, str]:
    """
    播放视频（先下载到本地缓存再播放）

    Returns:
        (video_update, status_message)
    """
    user = _vp_session_from_request(request)
    if not user:
        return gr.update(visible=False), "🔒 请先登录后再使用此功能 / Please sign in first"
    if not video_info:
        return gr.update(visible=False), "请先解析视频"
    if not _owns_uploaded_video(user, video_info):
        return gr.update(visible=False), "无权访问该上传视频"
    if not _media_disk_available():
        return gr.update(visible=False), "服务器存储空间不足，请稍后重试"
    if not _TRANSFER_SLOTS.acquire(blocking=False):
        return gr.update(visible=False), "当前下载任务较多，请稍后重试"
    try:
        if not _allow_user_action(user, "play", 10):
            return gr.update(visible=False), "今日播放加载次数已用完，请明天再试"
        import asyncio
        video_path, status = asyncio.run(_async_play_video(progress, video_info))
    finally:
        _TRANSFER_SLOTS.release()
    if video_path:
        return gr.update(visible=True, value=video_path), status
    return gr.update(visible=False), status

async def _async_play_video(progress, video_info: dict) -> Tuple[str, str]:
    if not video_info:
        return None, "请先解析视频"

    video_url = video_info.get('video_url', '')
    audio_url = video_info.get('audio_url', '')
    video_id = video_info.get('video_id', 'video')
    platform = video_info.get('platform', '')
    original_url = video_info.get('original_url', '')

    cache_path = os.path.join(client.cache_dir, f"{_video_cache_key(video_info)}_play.mp4")
    if video_info.get("_uploaded"):
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
            return cache_path, "加载完成（使用已上传视频）"
        return None, "上传的视频已过期，请重新上传"
    if not video_url:
        return None, "未找到视频链接"

    # 获取 referer
    referer = None
    if 'douyin.com' in original_url:
        referer = 'https://www.douyin.com/'
    elif 'bilibili.com' in original_url:
        referer = original_url
    elif 'xiaohongshu.com' in original_url:
        referer = 'https://www.xiaohongshu.com/'
    elif 'kuaishou.com' in original_url:
        referer = 'https://www.kuaishou.com/'
    elif 'haokan.baidu.com' in original_url:
        referer = 'https://haokan.baidu.com/'

    # 生成缓存文件名
    cache_key = _video_cache_key(video_info)
    cache_path = os.path.join(client.cache_dir, f"{cache_key}_play.mp4")

    # 如果已经缓存过，直接返回
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path, "加载完成（使用缓存）"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    }
    if referer:
        headers['Referer'] = referer

    # B站视频需要分别下载音视频再合并
    if platform == '哔哩哔哩' and audio_url:
        nonce = secrets.token_hex(6)
        video_temp = os.path.join(client.cache_dir, f"{cache_key}_{nonce}_video.m4s")
        audio_temp = os.path.join(client.cache_dir, f"{cache_key}_{nonce}_audio.m4s")
        merged_temp = os.path.join(client.cache_dir, f"{cache_key}_{nonce}_merged.mp4")

        response = None
        try:
            # 下载视频轨道
            progress(0, desc="下载视频轨道...")
            response = open_public_stream(video_url, headers=headers, timeout=180)
            total_size = int(response.headers.get('content-length', 0))
            if total_size > max_video_download_bytes():
                raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
            downloaded_size = 0

            with open(video_temp, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if downloaded_size > max_video_download_bytes():
                            raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
                        if total_size > 0:
                            progress(downloaded_size / total_size * 0.4, desc=f"下载视频轨道: {downloaded_size * 100 // total_size}%")

            # 下载音频轨道
            response.close()
            progress(0.4, desc="下载音频轨道...")
            response = open_public_stream(audio_url, headers=headers, timeout=180)
            total_size = int(response.headers.get('content-length', 0))
            if total_size > max_video_download_bytes():
                raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
            downloaded_size = 0

            with open(audio_temp, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if downloaded_size > max_video_download_bytes():
                            raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
                        if total_size > 0:
                            progress(0.4 + downloaded_size / total_size * 0.4, desc=f"下载音频轨道: {downloaded_size * 100 // total_size}%")

            # 合并音视频
            response.close()
            progress(0.8, desc="合并音视频...")
            success, msg = client.merge_video_audio(video_temp, audio_temp, merged_temp)

            if success:
                os.replace(merged_temp, cache_path)
                progress(1.0, desc="加载完成")
                return cache_path, "加载完成"
            else:
                return None, f"合并失败: {msg}"

        except Exception as error:
            if response is not None:
                response.close()
            # 清理临时文件
            for temp_file in [video_temp, audio_temp, merged_temp]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            logging.exception("Video and audio track loading failed")
            return None, _media_transfer_message(error, "视频加载")

    else:
        # 其他平台直接下载视频
        response = None
        temporary_path = cache_path + f".{secrets.token_hex(6)}.part"
        try:
            progress(0, desc="正在加载视频...")
            response = open_public_stream(video_url, headers=headers, timeout=180)

            total_size = int(response.headers.get('content-length', 0))
            if total_size > max_video_download_bytes():
                raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
            downloaded_size = 0

            with open(temporary_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if downloaded_size > max_video_download_bytes():
                            raise ValueError("视频超过单文件下载上限，请调整 MAX_VIDEO_DOWNLOAD_MB")
                        if total_size > 0:
                            progress(downloaded_size / total_size, desc=f"加载中: {downloaded_size * 100 // total_size}%")

            response.close()
            os.replace(temporary_path, cache_path)
            progress(1.0, desc="加载完成")
            return cache_path, "加载完成"

        except Exception as error:
            if response is not None:
                response.close()
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
            logging.exception("Video loading failed")
            return None, _media_transfer_message(error, "视频加载")


def download_video(video_info: dict, progress=gr.Progress(), request: gr.Request = None) -> Tuple[str, str]:
    """
    下载视频

    Returns:
        (file_update, status_message)
    """
    user = _vp_session_from_request(request)
    if not user:
        return gr.update(visible=False), "🔒 请先登录后再使用此功能 / Please sign in first"
    if not video_info:
        return gr.update(visible=False), "请先解析视频"
    if not _owns_uploaded_video(user, video_info):
        return gr.update(visible=False), "无权访问该上传视频"
    if not _media_disk_available():
        return gr.update(visible=False), "服务器存储空间不足，请稍后重试"
    if not _TRANSFER_SLOTS.acquire(blocking=False):
        return gr.update(visible=False), "当前下载任务较多，请稍后重试"
    try:
        if not _allow_user_action(user, "download", 10):
            return gr.update(visible=False), "今日下载次数已用完，请明天再试"
        import asyncio
        filepath, status = asyncio.run(_async_download_video(progress, video_info))
    finally:
        _TRANSFER_SLOTS.release()
    if filepath:
        return gr.update(visible=True, value=filepath), status
    return gr.update(visible=False), status

async def _async_download_video(progress, video_info: dict) -> Tuple[str, str]:
    if not video_info:
        return None, "请先解析视频"

    video_url = video_info.get('video_url', '')
    audio_url = video_info.get('audio_url', '')
    video_id = video_info.get('video_id', 'video')
    title = video_info.get('title', 'video')
    platform = video_info.get('platform', '')
    original_url = video_info.get('original_url', '')

    if video_info.get("_uploaded"):
        cache_path = os.path.join(client.cache_dir, f"{_video_cache_key(video_info)}_play.mp4")
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
            return cache_path, "已提供上传的视频原文件"
        return None, "上传的视频已过期，请重新上传"
    if not video_url:
        return None, "未找到视频链接"

    # 清理文件名
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).strip()
    if not safe_title:
        safe_title = video_id
    safe_title = safe_title[:35]
    unique_name = f"{safe_title}_{secrets.token_hex(6)}"

    # 获取 referer
    referer = None
    if 'douyin.com' in original_url:
        referer = 'https://www.douyin.com/'
    elif 'bilibili.com' in original_url:
        referer = original_url
    elif 'xiaohongshu.com' in original_url:
        referer = 'https://www.xiaohongshu.com/'
    elif 'kuaishou.com' in original_url:
        referer = 'https://www.kuaishou.com/'

    # B站视频需要分别下载音视频再合并
    if platform == '哔哩哔哩' and audio_url:
        progress(0, desc="下载视频轨道...")

        video_temp = os.path.join(client.download_dir, f"{unique_name}_video.m4s")
        audio_temp = os.path.join(client.download_dir, f"{unique_name}_audio.m4s")
        output_path = os.path.join(client.download_dir, f"{unique_name}.mp4")

        # 下载视频轨道
        def video_progress(p):
            progress(p * 0.4, desc=f"下载视频轨道: {p*100:.0f}%")

        success, _, msg = client.download_file(video_url, f"{unique_name}_video.m4s",
                                                referer, video_progress)
        if not success:
            return None, f"视频轨道下载失败: {msg}"

        progress(0.4, desc="下载音频轨道...")

        # 下载音频轨道
        def audio_progress(p):
            progress(0.4 + p * 0.4, desc=f"下载音频轨道: {p*100:.0f}%")

        success, _, msg = client.download_file(audio_url, f"{unique_name}_audio.m4s",
                                                referer, audio_progress)
        if not success:
            return None, f"音频轨道下载失败: {msg}"

        progress(0.8, desc="合并音视频...")

        # 合并音视频
        success, msg = client.merge_video_audio(video_temp, audio_temp, output_path)
        if not success:
            return None, msg

        progress(1.0, desc="完成")
        return output_path, f"下载完成: {os.path.basename(output_path)}"

    else:
        # 其他平台直接下载
        def download_progress(p):
            progress(p, desc=f"下载中: {p*100:.0f}%")

        filename = f"{unique_name}.mp4"
        success, filepath, msg = client.download_file(video_url, filename,
                                                       referer, download_progress)

        if success:
            return filepath, msg
        else:
            return None, msg


def _analysis_result(content: str, status: str, report_path: str | None = None,
                     asr_text_path: str | None = None, asr_srt_path: str | None = None) -> tuple:
    return (
        content, status,
        gr.update(value=report_path, visible=bool(report_path)),
        gr.update(value=asr_text_path, visible=bool(asr_text_path)),
        gr.update(value=asr_srt_path, visible=bool(asr_srt_path)),
    )


def _asr_status_label(status: str) -> str:
    return {
        "timestamped": "转写完成（带时间戳）",
        "chunk_timestamped": "转写完成（分块时间戳）",
        "partial": "部分转写成功",
        "coarse": "转写完成（无句级时间戳）",
        "no_audio": "视频没有音轨",
        "not_configured": "语音识别未配置",
        "asr_failed": "语音识别失败",
        "extract_failed": "音轨提取失败",
        "empty": "未识别到语音",
    }.get(status, status or "未知")


def _load_media_for_processing(user: dict, video_info: dict, progress) -> tuple[str | None, str, str]:
    """Return a cached local file, loading it once under the shared transfer quota."""
    if not _owns_uploaded_video(user, video_info):
        return None, "无权访问该上传视频", "forbidden"
    cache_path = os.path.join(client.cache_dir, f"{_video_cache_key(video_info)}_play.mp4")
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path, "", ""
    if not _media_disk_available():
        return None, "服务器存储空间不足，请稍后重试", "failed"
    if not _TRANSFER_SLOTS.acquire(blocking=False):
        return None, "当前视频加载任务较多，请稍后重试", "busy"
    try:
        if not _allow_user_action(user, "play", 10):
            return None, "今日视频加载次数已用完，请明天再试", "quota"
        import asyncio
        loaded_path, load_status = asyncio.run(_async_play_video(progress, video_info))
    finally:
        _TRANSFER_SLOTS.release()
    if not loaded_path:
        return None, f"视频加载失败：{load_status}", "failed"
    return loaded_path, "", ""


def _save_asr_exports(owner_id: int, video_id: str, cues, status: str, readable: str, warnings=()) -> tuple[str, str]:
    reports_dir = os.path.join(client.download_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    safe_id = "".join(c for c in str(video_id) if c.isalnum())[:30] or "video"
    export_base = f"vp-u{int(owner_id)}-{safe_id}_{int(time.time())}_{secrets.token_hex(6)}"
    text_path = os.path.join(reports_dir, export_base + "_asr.txt")
    srt_path = os.path.join(reports_dir, export_base + "_asr.srt")
    warning_note = ("识别提示：" + "；".join(str(item) for item in warnings if item) + "\n") if warnings else ""
    plain_text = (
        "ASR 机器转写（未人工校对）\n"
        f"识别状态：{_asr_status_label(status)}\n"
        f"{warning_note}"
        "整理稿可能调整断句和标点；原始时间戳与识别文本请以 SRT 为准。\n\n"
        f"{readable.strip()}\n"
    )
    try:
        with open(text_path, "w", encoding="utf-8", newline="\n") as text_file:
            text_file.write(plain_text)
        with open(srt_path, "w", encoding="utf-8", newline="\n") as srt_file:
            srt_file.write(format_asr_srt(cues))
    except OSError:
        logging.exception("Unable to save ASR exports")
        for path in (text_path, srt_path):
            if os.path.exists(path):
                os.remove(path)
        raise
    return text_path, srt_path


def transcribe_video_only(video_info: dict | None = None, progress=gr.Progress(), request: gr.Request = None) -> tuple:
    """Make a transcript without paying for or waiting on video analysis."""
    user = _vp_session_from_request(request)
    if not user:
        return _analysis_result(REPORT_PLACEHOLDER, "🔒 请先登录后再使用此功能 / Please sign in first")
    if not video_info:
        return _analysis_result(REPORT_PLACEHOLDER, "请先解析视频")
    platform = video_info.get("platform", "其他")
    if not os.getenv("ASR_MODEL_ID", "").strip():
        auth_db.record_usage("transcribe", platform, "unsupported", "disabled")
        return _analysis_result(REPORT_PLACEHOLDER, "语音识别服务暂未配置")
    if not _TRANSCRIBE_SLOTS.acquire(blocking=False):
        auth_db.record_usage("transcribe", platform, "busy")
        return _analysis_result(REPORT_PLACEHOLDER, "当前转写任务较多，请稍后重试")
    started = time.monotonic()
    try:
        media_path, load_error, load_outcome = _load_media_for_processing(user, video_info, progress)
        if not media_path:
            auth_db.record_usage("transcribe", platform, load_outcome, "upstream" if load_outcome == "failed" else "")
            return _analysis_result(REPORT_PLACEHOLDER, load_error)
        media = inspect_video(media_path)
        if not media.audio_tracks:
            auth_db.record_usage("transcribe", platform, "invalid", "no_audio", int((time.monotonic() - started) * 1000))
            return _analysis_result(REPORT_PLACEHOLDER, "视频没有音轨，无法进行语音转写")
        if not _allow_user_action(user, "transcribe", 5):
            auth_db.record_usage("transcribe", platform, "quota")
            return _analysis_result(REPORT_PLACEHOLDER, "今日独立转写次数已用完，请明天再试")
        with tempfile.TemporaryDirectory(prefix="vp_asr_") as temp_dir:
            cues, transcript_status, warning = transcribe_audio(
                media_path, media, temp_dir,
                progress=lambda value, message: progress(value, desc=message),
            )
        if not cues:
            auth_db.record_usage("transcribe", platform, "failed", "asr_empty", int((time.monotonic() - started) * 1000))
            detail = f"：{warning}" if warning else ""
            return _analysis_result(REPORT_PLACEHOLDER, f"{_asr_status_label(transcript_status)}{detail}")
        readable = format_diarized_transcript(cues)
        text_path, srt_path = _save_asr_exports(
            user["id"], video_info.get("video_id", "video"), cues, transcript_status, readable, [warning] if warning else []
        )
        auth_db.record_user_activity(user["id"], "transcribe_success")
        auth_db.record_usage("transcribe", platform, "success", duration_ms=int((time.monotonic() - started) * 1000))
        preview = html.escape(readable[:12000])
        if len(readable) > 12000:
            preview += "\n……（下载 TXT 查看全文）"
        content = f"### ASR 机器转写（未人工校对）\n\n<pre>{preview}</pre>"
        return _analysis_result(content, f"{_asr_status_label(transcript_status)}；共 {len(cues)} 条。{warning or ''}", None, text_path, srt_path)
    except Exception:
        logging.exception("Standalone transcription failed")
        auth_db.record_usage("transcribe", platform, "failed", "error", int((time.monotonic() - started) * 1000))
        return _analysis_result(REPORT_PLACEHOLDER, "转写服务暂时不可用，请稍后重试")
    finally:
        _TRANSCRIBE_SLOTS.release()


def extract_video_content(multi_speaker: bool = False, video_info: dict | None = None, progress=gr.Progress(), request: gr.Request = None) -> tuple:
    """
    按时间轴建立多模态证据后生成视频分析。

    Returns: report, status, report download, readable ASR download, raw SRT download.
    """
    user = _vp_session_from_request(request)
    if not user:
        return _analysis_result("🔒 请先登录后再使用此功能 / Please sign in first", "🔒 请先登录后再使用此功能 / Please sign in first")
    if not video_info:
        return _analysis_result(REPORT_PLACEHOLDER, "请先解析视频")
    metric_platform = video_info.get("platform", "其他")
    if not AI_ANALYSIS_ENABLED:
        auth_db.record_usage("analysis", metric_platform, "unsupported", "disabled")
        return _analysis_result(REPORT_PLACEHOLDER, "AI 分析暂未启用：模型服务账号尚未完成配置")

    video_id = video_info.get('video_id', 'video')

    if not _ANALYSIS_SLOTS.acquire(blocking=False):
        auth_db.record_usage("analysis", metric_platform, "busy")
        return _analysis_result(REPORT_PLACEHOLDER, "当前有分析任务正在运行，请稍后重试")

    started = time.monotonic()
    try:
        cache_path, load_error, load_outcome = _load_media_for_processing(user, video_info, progress)
        if not cache_path:
            auth_db.record_usage("analysis", metric_platform, load_outcome, "upstream" if load_outcome == "failed" else "")
            return _analysis_result(REPORT_PLACEHOLDER, load_error)

        if not _allow_user_action(user, "analysis", 2):
            auth_db.record_usage("analysis", metric_platform, "quota")
            return _analysis_result(REPORT_PLACEHOLDER, "今日 AI 分析次数已用完，请明天再试")
        qwen_client = OpenAI(
            base_url=QWEN_API_BASE_URL,
            api_key=QWEN_API_KEY,
            timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "180")),
            max_retries=1,
        )

        def report_progress(value: float, message: str) -> None:
            progress(value, desc=message)

        result, evidence = analyze_video_evidence_first(
            cache_path,
            qwen_client,
            QWEN_MODEL_ID,
            title=video_info.get('title'),
            video_info=video_info,
            max_frames=MAX_ANALYSIS_FRAMES,
            progress=report_progress,
            diarize=bool(multi_speaker),
        )
        diarize_note = "多人转写已开启" if multi_speaker else ""
        status = (
            f"证据优先分析完成：{len(evidence.frames)} 个时间点，"
            f"{len(evidence.subtitles)} 条字幕，ASR：{_asr_status_label(evidence.transcript_status)} {diarize_note}"
        )
        reports_dir = os.path.join(client.download_dir, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        safe_id = "".join(c for c in str(video_id) if c.isalnum())[:30] or "video"
        export_base = f"vp-u{int(user['id'])}-{safe_id}_{int(time.time())}_{secrets.token_hex(6)}"
        report_path = os.path.join(reports_dir, export_base + ".md")
        if evidence.raw_transcript:
            transcript_appendix = (
                "\n\n---\n\n## 附录：ASR 转写整理稿（机器生成，未人工校对）\n\n"
                + (evidence.cleaned_transcript or "\n".join(cue.text for cue in evidence.raw_transcript)).strip()
                + "\n"
            )
        else:
            transcript_appendix = (
                "\n\n---\n\n## ASR 转写状态\n\n"
                f"本次未生成语音转写：{_asr_status_label(evidence.transcript_status)}。\n"
            )
        with open(report_path, "w", encoding="utf-8", newline="\n") as report_file:
            report_file.write(result + transcript_appendix)
        asr_text_path = asr_srt_path = None
        if evidence.raw_transcript:
            raw_lines = "\n".join(
                f"[{cue.start:.2f}–{cue.end:.2f}] {cue.text}" for cue in evidence.raw_transcript
            )
            readable = (evidence.cleaned_transcript or raw_lines).strip()
            asr_warnings = [str(warning) for warning in getattr(evidence, "warnings", [])
                            if "ASR" in str(warning) or "转写" in str(warning)]
            try:
                asr_text_path, asr_srt_path = _save_asr_exports(
                    user["id"], video_id, evidence.raw_transcript, evidence.transcript_status, readable, asr_warnings
                )
            except OSError:
                status += "；ASR 转写文件保存失败"
        auth_db.record_user_activity(user["id"], "analysis_success")
        auth_db.record_usage("analysis", metric_platform, "success", duration_ms=int((time.monotonic() - started) * 1000))
        return _analysis_result(result, status, report_path, asr_text_path, asr_srt_path)

    except Exception:
        logging.exception("Video analysis failed")
        auth_db.record_usage("analysis", metric_platform, "failed", "error", int((time.monotonic() - started) * 1000))
        return _analysis_result(REPORT_PLACEHOLDER, "分析暂时失败，请稍后重试；持续出现时请联系站点管理员")
    finally:
        _ANALYSIS_SLOTS.release()


# 信息条初始占位（空状态引导）
INFO_BAR_EMPTY = (
    '<div class="vp-info-bar"><span class="vp-info-dot"></span>'
    '<span class="vp-info-text">视频信息将显示在此</span></div>'
)

# 解析 CTA 按钮图标（白色 spark，渐变紫底上双主题通用）
ICON_SPARK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icons", "spark.svg")

# 报告区初始占位（Markdown 渲染为空态引导，分析完成后由结果替换）
REPORT_PLACEHOLDER = """
<div class="vp-report-empty">
  <div class="vp-report-empty-icon">
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M4 6h16M4 12h16M4 18h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
    </svg>
  </div>
  <div class="vp-report-empty-head">分析与转写结果将在这里生成</div>
  <div class="vp-report-empty-sub">先解析视频，再选择「仅语音转写」或「AI 时间轴证据分析」；视频会自动加载</div>
</div>
"""

# 右栏空态引导（解析前展示，解析成功后由事件输出空串隐藏）
EMPTY_GUIDE_HTML = """
<div class="vp-empty-state">
  <div class="vp-empty-mark">
    <svg viewBox="0 0 24 24" width="30" height="30" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="2.5" y="5" width="19" height="14" rx="3.5" stroke="currentColor" stroke-width="1.5"/>
      <path d="M10 9.2v5.6l5-2.8-5-2.8z" fill="currentColor"/>
    </svg>
  </div>
  <div class="vp-empty-head" data-i18n="empty_head">等待视频解析</div>
  <div class="vp-empty-desc" data-i18n="empty_desc">在左侧粘贴视频链接并点击「解析视频」，即可在此生成封面与在线播放</div>
  <div class="vp-empty-flow">
    <span class="vp-flow-step"><i>1</i><span data-i18n="empty_s1">粘贴链接</span></span>
    <span class="vp-flow-arrow">→</span>
    <span class="vp-flow-step"><i>2</i><span data-i18n="empty_s2">解析视频</span></span>
    <span class="vp-flow-arrow">→</span>
    <span class="vp-flow-step"><i>3</i><span data-i18n="empty_s3">AI 取证</span></span>
  </div>
</div>
"""

# 注入 <head> 的主题脚本：head 解析期间同步应用主题（默认浅色），避免主题闪屏；
# Gradio 的 gr.HTML 组件不执行内联 script，因此整段必须经 head 注入。
# 主题偏好用 vp-theme-v2：旧系统的 vp-theme=light 是一次性作废的遗留值
# （旧奶油编辑风时代写入），不再信任，保证新浅色系为默认。
THEME_SCRIPT = """
<script>
(function () {
  function vpTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }
  function vpApplyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('vp-theme-v2', t); } catch (e) {}
    var lbl = document.querySelector('.vp-theme-label');
    if (lbl) { lbl.textContent = (t === 'dark') ? '深色' : '浅色'; }
  }
  function vpToggleTheme() {
    // 切换时给 <html> 挂临时动画类（CSS: html.vp-anim * 统一过渡），
    // 400ms 后移除，避免常驻全局 transition 拖慢 hover 等微交互。
    var root = document.documentElement;
    root.classList.add('vp-anim');
    vpApplyTheme(vpTheme() === 'dark' ? 'light' : 'dark');
    clearTimeout(vpToggleTheme._t);
    vpToggleTheme._t = setTimeout(function () { root.classList.remove('vp-anim'); }, 420);
  }
  function vpScrollTo(sel) {
    var el = document.querySelector(sel);
    if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  }
  function vpLogin() {
    location.href = '/login';
  }
  // ---- 多语言（简体中文 / English）----
  // 文案由 data-i18n / data-i18n-ph 标记 + Gradio 组件 elem_id 决定，
  // 切换时统一重渲染；偏好存 localStorage['vp-lang']，默认 zh。
  var VP_I18N = {
    zh: {
      nav_parse: "视频解析", nav_ai: "AI 分析", nav_help: "使用指南",
      nav_login: "登录", nav_start: "开始使用", nav_logout: "退出",
      hero_eyebrow: "视频智能分析平台",
      hero_title_a: "解析 · 下载 · ", hero_title_b: "AI 取证",
      hero_sub: "粘贴公开视频链接或上传有权使用的 MP4，随后播放、下载、转写或生成时间轴证据报告。链接可用性取决于来源平台的访问限制。",
      hero_demo: "先看一份报告示例 →",
      sec_parse: "解析视频", sec_preview: "预览", sec_report: "分析与转写结果", sec_help: "使用指南",
      empty_head: "等待视频解析",
      empty_desc: "在左侧解析视频链接或上传自有 MP4，即可在此预览",
      empty_s1: "粘贴链接", empty_s2: "解析视频", empty_s3: "AI 取证",
      help_t1: "粘贴链接或上传", help_d1: "抖音、B站可粘贴分享链接；小红书请复制 App 内完整链接，无法访问时可上传自有 MP4；视频号需上传自有 MP4。",
      help_t2: "解析视频", help_d2: "封面、时长与视频信息一屏展示，无需手动选择来源",
      help_t3: "播放 / 下载", help_d3: "在线播放使用临时缓存；可下载平台提供的媒体流（B 站自动合并音视频）",
      help_t4: "转写 / AI 分析", help_d4: "可单独导出语音转写 TXT / SRT，或按时间轴分析字幕、音频和画面",
      help_notes: "生成的视频、转写与报告将在约 48 小时后清理 · 处理时自动加载视频",
      warn_ffmpeg: "⚠️ 警告：未在系统中检测到 ffmpeg。B 站视频合并与 AI 内容提取可能无法运行。请安装 ffmpeg 并加入系统 PATH，或设置 FFMPEG_PATH 环境变量。",
      lang_btn: "EN",
      vp_in_url: "视频链接", vp_btn_parse: "解析视频", vp_dd_platform: "来源平台",
      vp_btn_clear: "清空", vp_btn_play: "在线播放", vp_btn_download: "下载视频",
      vp_upload_input: "上传自有 MP4（视频号素材可用）", vp_btn_upload: "使用上传的视频",
      vp_chk_speaker: "多人转写（AI 分析）", vp_btn_extract: "AI 时间轴证据分析",
      vp_btn_transcribe: "仅语音转写 · 导出 TXT / SRT",
      vp_out_status: "状态", vp_vid: "在线播放", vp_img_cover: "视频封面", vp_file: "下载文件"
    },
    en: {
      nav_parse: "Parse", nav_ai: "AI Analysis", nav_help: "Guide",
      nav_login: "Sign in", nav_start: "Get started", nav_logout: "Sign out",
      hero_eyebrow: "Video Intelligence Platform",
      hero_title_a: "Parse · Download · ", hero_title_b: "AI Evidence",
      hero_sub: "Paste a public video link or upload an MP4 you have rights to use, then play, download, transcribe or analyze it. Link availability depends on the source platform.",
      hero_demo: "See a sample report →",
      sec_parse: "Parse Video", sec_preview: "Preview", sec_report: "Analysis & Transcript", sec_help: "Guide",
      empty_head: "Awaiting video",
      empty_desc: "Parse a link or upload your MP4 on the left to preview it here.",
      empty_s1: "Paste link", empty_s2: "Parse", empty_s3: "AI Evidence",
      help_t1: "Paste or upload", help_d1: "Paste Douyin or Bilibili links. For Xiaohongshu, use the full in-app share link or upload your own MP4 if access fails. Upload your own MP4 for WeChat Channels.",
      help_t2: "Parse", help_d2: "Cover, duration and video info shown on one screen, no manual source selection",
      help_t3: "Play / Download", help_d3: "Playback uses temporary cache; downloads use the source media stream (Bilibili audio and video are merged)",
      help_t4: "Transcript / AI", help_d4: "Export a standalone TXT / SRT transcript or analyze subtitle, audio and frame evidence on a timeline",
      help_notes: "Generated media, transcripts and reports are removed after about 48 hours · Video loads automatically",
      warn_ffmpeg: "⚠️ Warning: ffmpeg not found on this system. Bilibili merging and AI extraction may fail. Install ffmpeg and add it to PATH, or set FFMPEG_PATH.",
      lang_btn: "中文",
      vp_in_url: "Video URL", vp_btn_parse: "Parse Video", vp_dd_platform: "Source Platform",
      vp_btn_clear: "Clear", vp_btn_play: "Play Online", vp_btn_download: "Download Video",
      vp_upload_input: "Upload your MP4 (for inaccessible links)", vp_btn_upload: "Use uploaded video",
      vp_chk_speaker: "Multi-speaker (AI analysis)", vp_btn_extract: "AI Timeline Evidence",
      vp_btn_transcribe: "Transcribe only · Export TXT / SRT",
      vp_out_status: "Status", vp_vid: "Online Playback", vp_img_cover: "Cover", vp_file: "Download File"
    }
  };
  // Gradio 组件：elem_id -> 文本节点选择器（button 直接取内部 button，其余取 label）
  var VP_COMP_SEL = {
    vp_in_url: "label", vp_btn_parse: "button", vp_dd_platform: "label",
    vp_btn_clear: "button", vp_btn_play: "button", vp_btn_download: "button",
    vp_upload_input: "label", vp_btn_upload: "button",
    vp_chk_speaker: "label", vp_btn_extract: "button", vp_btn_transcribe: "button", vp_out_status: "label",
    vp_vid: "label", vp_img_cover: "label", vp_file: "label"
  };
  function vpCurLang() { return window.__vp_lang || 'zh'; }
  document.addEventListener('click', function (event) {
    if (!window.__vp_logged_in && event.target.closest && event.target.closest('#vp_upload_input')) {
      event.preventDefault();
      event.stopPropagation();
      if (window.vpOpenLoginModal) window.vpOpenLoginModal();
    }
  }, true);
  function vpApplyLang(l) {
    try { localStorage.setItem('vp-lang', l); } catch (e) {}
    window.__vp_lang = l;
    var d = VP_I18N[l] || VP_I18N.zh;
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var k = el.getAttribute('data-i18n');
      if (d[k] != null) el.textContent = d[k];
    });
    document.querySelectorAll('[data-i18n-ph]').forEach(function (el) {
      var k = el.getAttribute('data-i18n-ph');
      if (d[k] != null) el.setAttribute('placeholder', d[k]);
    });
    Object.keys(VP_COMP_SEL).forEach(function (id) {
      var sel = VP_COMP_SEL[id];
      var root = document.getElementById(id);
      if (!root) return;
      var t = d[id];
      if (t == null) return;
      var target;
      if (root.tagName === 'BUTTON') {
        // Gradio 6 把 elem_id 直接放在 <button> 自身上
        target = root;
      } else if (sel === 'button') {
        target = root.querySelector('button');
      } else {
        target = root.querySelector(sel) || root.querySelector('label') || root.querySelector('[class*="label"]');
      }
      if (target) {
        // 【关键修复】Gradio 6 的 label 是容器：
        //   <label><span data-testid="block-info">文本</span><div><textarea/input></div></label>
        // 整体 textContent 会把内部输入控件一起抹掉（曾导致 URL 输入框/状态框
        // 消失、无法粘贴链接）。只替换 block-info / 首个 span 的文本；
        // 仅当目标不含任何子元素时才整体替换。
        var innerCtl = target.querySelector('textarea,input,select');
        var infoSpan = target.querySelector('span[data-testid="block-info"]') || target.querySelector('span');
        if (infoSpan) {
          infoSpan.textContent = t;
        } else if (!innerCtl && target.children.length === 0) {
          target.textContent = t;
        }
      }
    });
    var lb = document.getElementById('vp-lang-btn');
    if (lb) lb.textContent = d.lang_btn;
    if (window.vpSetAuthMode) window.vpSetAuthMode(window.__vp_auth_mode || 'login');
  }
  function vpToggleLang() {
    vpApplyLang(vpCurLang() === 'zh' ? 'en' : 'zh');
  }
  // 会话用户态：登录后把「登录」链接替换为「用户名 · 退出」
  function vpInitAuthUI() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/auth/me', true);
    xhr.onload = function () {
      if (xhr.status !== 200) return;
      try {
        var d = JSON.parse(xhr.responseText);
        var name = d && d.data && d.data.username;
        if (!name) return;
        window.__vp_logged_in = true;
        var link = document.querySelector('.vp-login-link');
        if (link) {
          link.removeAttribute('onclick');
          link.setAttribute('href', '/account');
          link.innerHTML = '<span class="vp-user-dot"></span>' + name.replace(/[<>&]/g, '');
        }
        var cta = document.querySelector('.vp-nav-right');
        if (cta && !document.getElementById('vp-logout-link')) {
          var out = document.createElement('a');
          out.id = 'vp-logout-link';
          out.className = 'vp-login-link';
          out.href = '/logout';
          out.textContent = (vpCurLang() === 'zh') ? '退出' : 'Logout';
          cta.insertBefore(out, cta.querySelector('.vp-cta-pill'));
        }
      } catch (e) {}
    };
    xhr.send();
  }
  function vpSyncLabel() {
    var lbl = document.querySelector('.vp-theme-label');
    if (lbl) { lbl.textContent = (vpTheme() === 'dark') ? '深色' : '浅色'; }
  }
  var saved = null;
  try { saved = localStorage.getItem('vp-theme-v2'); } catch (e) {}
  vpApplyTheme(saved === 'dark' ? 'dark' : 'light');
  window.vpToggleTheme = vpToggleTheme;
  window.vpLogin = vpLogin;
  window.vpScrollTo = vpScrollTo;
  window.vpToggleLang = vpToggleLang;
  window.vpApplyLang = vpApplyLang;
  window.__vp_logged_in = false;
  window.vpInitAuthUI = vpInitAuthUI;
  window.addEventListener('load', vpSyncLabel);
  window.addEventListener('load', vpInitAuthUI);
  setTimeout(vpSyncLabel, 600);
  setTimeout(vpInitAuthUI, 400);

  // TikHub 式导航：透明起始，滚动 >8px 后加毛玻璃底 + 细边框（CSS .vp-nav-scrolled）
  var _nav = null;
  function vpOnScroll() {
    if (!_nav) { _nav = document.getElementById('vp-nav'); }
    if (_nav) {
      if (window.scrollY > 8) { _nav.classList.add('vp-nav-scrolled'); }
      else { _nav.classList.remove('vp-nav-scrolled'); }
    }
  }
  window.addEventListener('scroll', vpOnScroll, { passive: true });
  vpOnScroll();

  // 语言初始化：读偏好并应用（chrome 同步），重试覆盖 Gradio 异步挂载的组件标签
  var _lang = 'zh';
  try { _lang = localStorage.getItem('vp-lang') || 'zh'; } catch (e) {}
  window.__vp_lang = _lang;
  vpApplyLang(_lang);
  setTimeout(function () { vpApplyLang(window.__vp_lang); }, 600);
  setTimeout(function () { vpApplyLang(window.__vp_lang); }, 1500);
  setTimeout(function () { vpApplyLang(window.__vp_lang); }, 2500);
})();
</script>
"""


def _info_bar_html(title: str, platform: str, ok: bool) -> str:
    """构建编辑风信息条：状态点 + 平台徽章 + 标题（超出截断）。"""
    badge = f'<span class="vp-badge">{html.escape(platform)}</span>' if platform else ""
    cls = "vp-info-ok" if ok else "vp-info-err"
    return (
        f'<div class="vp-info-bar {cls}">'
        f'<span class="vp-info-dot"></span>{badge}'
        f'<span class="vp-info-text">{html.escape(title)}</span>'
        f'</div>'
    )


def speaker_backend_status() -> Tuple[bool, str]:
    """检测说话人分离后端是否可用，返回 (可用, 面向用户的提示文案)。
    内部配置细节不暴露给终端用户，仅给中性提示。"""
    backend = os.getenv("ASR_SPEAKER_BACKEND", "").strip()
    if not backend:
        return False, "多人转写暂不可用（需管理员开启后端）"
    if backend == "tencent":
        missing = []
        if not os.getenv("TENCENT_ASR_SECRET_ID", "").strip():
            missing.append("TENCENT_ASR_SECRET_ID")
        if not os.getenv("TENCENT_ASR_SECRET_KEY", "").strip():
            missing.append("TENCENT_ASR_SECRET_KEY")
        if not (os.getenv("DOMAIN", "").strip() or os.getenv("PUBLIC_BASE_URL", "").strip()):
            missing.append("DOMAIN")
        if missing:
            print(f"[config] 说话人分离后端缺配置: {'、'.join(missing)}")
            return False, "多人转写暂不可用（后端未就绪）"
    return True, f"已启用 {backend} 说话人分离后端"


def clear_all():
    """清空所有内容"""
    return ("", "自动检测", "", INFO_BAR_EMPTY,
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            REPORT_PLACEHOLDER, gr.update(value=None, visible=False),
            gr.update(value=None, visible=False), gr.update(value=None, visible=False),
            EMPTY_GUIDE_HTML, {}, gr.update(value=None))


# ==================== Gradio 界面 ====================

def select_platform(platform: str):
    """切换平台选择，同时保留用户已粘贴的链接。"""
    return platform


def check_ffmpeg() -> bool:
    """检查 ffmpeg 是否可用"""
    try:
        subprocess.run([os.getenv('FFMPEG_PATH', 'ffmpeg'), '-version'], capture_output=True)
        return True
    except FileNotFoundError:
        return False


# ==================== TikHub 风格主题（浅色默认，黑白灰克制视觉） ====================
# 视觉方向：对齐 tikhub.io —— 白底 #ffffff、近黑文字 #1a1a1a、次级灰 #6b7280、
# 黑色主 CTA、无渐变光晕、靠排版留白取胜。全套样式在 static/css/app.css
# （经 <head> <link> 注入，改 CSS 无需重启）；此处 theme.set 仅让组件内联值同步浅色。

def build_glass_theme():
    """构建 TikHub 风格浅色主题（默认浅色，黑主色 + Geist 字体，克制黑白灰）。
    Gradio 6 将主题计算值内联到组件，此处把组件默认值定为浅色系，
    深色模式由 app.css 的 html[data-theme="dark"] 覆盖反转。"""
    theme = gr.themes.Base(
        primary_hue=gr.themes.colors.zinc,
        secondary_hue=gr.themes.colors.zinc,
        neutral_hue=gr.themes.colors.zinc,
        font=["Geist Sans", "Inter", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
              "system-ui", "-apple-system", "sans-serif"],
        font_mono=["Geist Mono", "ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
    )
    _vars = {
        "body_background_fill": "#ffffff",
        "background_fill_primary": "#ffffff",
        "background_fill_secondary": "#ffffff",
        "block_background_fill": "#ffffff",
        "block_border_color": "#e9e9ec",
        "block_title_text_color": "#1a1a1a",
        "block_info_text_color": "#6b7280",
        "block_label_text_color": "#3f3f46",
        "body_text_color": "#1a1a1a",
        "body_text_color_subdued": "#6b7280",
        "input_background_fill": "#ffffff",
        "input_background_fill_focus": "#ffffff",
        "input_border_color": "#d4d4d8",
        "input_border_color_focus": "#1a1a1a",
        "input_placeholder_color": "#9ca3af",
        "input_shadow_focus": "0 0 0 3px rgba(26, 26, 26, 0.08)",
        "button_primary_background_fill": "#1a1a1a",
        "button_primary_background_fill_hover": "#2d2d30",
        "button_primary_text_color": "#ffffff",
        "button_primary_border_color": "#1a1a1a",
        "button_secondary_background_fill": "#ffffff",
        "button_secondary_background_fill_hover": "#f4f4f5",
        "button_secondary_text_color": "#1a1a1a",
        "button_secondary_border_color": "#d4d4d8",
        "button_secondary_border_color_hover": "#1a1a1a",
        "border_color_primary": "#e9e9ec",
        "panel_background_fill": "#ffffff",
        "panel_border_color": "#e9e9ec",
        "code_background_fill": "#f4f4f5",
        "link_text_color": "#1a1a1a",
        "link_text_color_hover": "#000000",
        "link_text_color_active": "#1a1a1a",
        "loader_color": "#1a1a1a",
        "slider_color": "#1a1a1a",
        "stat_background_fill": "#ffffff",
        "checkbox_background_color": "#ffffff",
        "checkbox_background_color_selected": "#1a1a1a",
        "checkbox_border_color": "#c4c4cc",
        "checkbox_border_color_selected": "#1a1a1a",
        "checkbox_check": "#ffffff",
        "checkbox_label_text_color": "#3f3f46",
        "error_background_fill": "#fdf2f2",
        "error_border_color": "#f5c6c6",
        "error_text_color": "#b91c1c",
    }
    for k, v in _vars.items():
        theme.set(**{k: v})
    return theme


def create_app():
    """创建 Gradio 应用"""
    ffmpeg_available = check_ffmpeg()

    with gr.Blocks(
        title="VidAI · 视频智能分析平台",
        delete_cache=(3600, 48 * 3600),
    ) as app:

        # ===== 顶部导航（照抄 TikHub：吸顶透明起始，滚动后毛玻璃 + 细边框过渡出现） =====
        gr.HTML(
            """
            <div class="vp-nav" id="vp-nav">
              <div class="vp-nav-inner">
                <div class="vp-nav-left">
                  <a class="vp-brand" href="javascript:void(0)" aria-label="VidAI 首页">
                    <span class="vp-nav-logo">
                      <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M8 5.5v13l11-6.5-11-6.5z" fill="#ffffff"/>
                      </svg>
                    </span>
                    <span class="vp-brand-name">VidAI</span>
                  </a>
                </div>
                <div class="vp-nav-links">
                  <a href="javascript:void(0)" onclick="vpScrollTo('.vp-ops')"><span data-i18n="nav_parse">视频解析</span></a>
                  <a href="javascript:void(0)" onclick="vpScrollTo('.vp-report')"><span data-i18n="nav_ai">AI 分析</span></a>
                  <a href="javascript:void(0)" onclick="vpScrollTo('.vp-help')"><span data-i18n="nav_help">使用指南</span></a>
                </div>
                <div class="vp-nav-right">
                  <button class="vp-theme-round" id="vp-theme-btn" type="button" onclick="vpToggleTheme()" title="切换深色 / 浅色模式" aria-label="切换深色 / 浅色模式">
                    <svg class="vp-icon-sun" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
                    <svg class="vp-icon-moon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8z"/></svg>
                  </button>
                  <button class="vp-lang-round" id="vp-lang-btn" type="button" onclick="vpToggleLang()" title="切换语言 / Switch language" aria-label="切换语言">EN</button>
                  <a class="vp-login-link" id="vp-login-link" href="/login" onclick="if(window.vpOpenLoginModal){vpOpenLoginModal();return false;}">
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m10 17 5-5-5-5"/><path d="M15 12H3"/><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/></svg>
                    <span data-i18n="nav_login">登录</span>
                  </a>
                  <button class="vp-cta-pill" type="button" onclick="vpScrollTo('.vp-url')" data-i18n="nav_start">开始使用</button>
                </div>
              </div>
            </div>
            """
        )

        # ===== 登录/注册弹窗：共用可访问的表单，保留页面内状态 =====
        gr.HTML(auth_pages.workspace_auth_modal(
            os.getenv('ALLOW_REGISTER', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
        ))

        # ===== Hero（首屏定位，对齐 TikHub：pill 徽章 + 大字标题 + 一句价值主张） =====
        gr.HTML(
            """
            <section class="vp-hero">
              <div class="vp-hero-eyebrow"><span class="vp-hero-dot"></span><span data-i18n="hero_eyebrow">视频智能分析平台</span></div>
              <h1 class="vp-hero-title"><span data-i18n="hero_title_a">解析 · 下载 · </span><span class="vp-hero-accent" data-i18n="hero_title_b">AI 取证</span></h1>
              <p class="vp-hero-sub" data-i18n="hero_sub">粘贴公开视频链接，解析可访问的媒体资源，播放或下载，并按时间轴生成多模态证据报告。不同平台的可用性取决于其访问限制。</p>
              <a class="vp-demo-link" href="/sample-report" data-i18n="hero_demo">先看一份报告示例 →</a>
            </section>
            """
        )

        # ===== ffmpeg 警告 =====
        if not ffmpeg_available:
            gr.HTML(
                """
                <div class="vp-warn">
                  <strong>⚠️ 警告:</strong> 未在系统中检测到 <strong>ffmpeg</strong>。
                  B 站视频合并与 AI 内容提取可能无法运行。请安装 ffmpeg 并加入系统 PATH，或设置 FFMPEG_PATH 环境变量。
                </div>
                """
            )

        # ===== 步骤指示器（编辑风极简：删除） =====

        # 说话人分离后端可用性（加载时检测，不可用则禁用开关）
        speaker_avail, speaker_info = speaker_backend_status()

        with gr.Row(elem_classes=["vp-main"]):
            # ---------- 左栏：操作台（紧凑，主 CTA 独立全宽） ----------
            with gr.Column(scale=4, elem_classes=["vp-card", "vp-ops"]):
                gr.HTML('<div class="vp-section-label"><span class="vp-section-num">01</span><span data-i18n="sec_parse">解析视频</span></div>')
                url_input = gr.Textbox(
                    label="视频链接",
                    placeholder="粘贴抖音、B站或小红书分享链接…",
                    lines=3,
                    elem_id="vp_in_url",
                    elem_classes=["vp-url"]
                )

                # 主 CTA：紧跟输入框，独立全宽，唯一视觉主导（黑底白字，克制的 TikHub 语言）
                parse_btn = gr.Button("解析视频", variant="primary", size="lg", elem_classes=["vp-cta"], elem_id="vp_btn_parse")

                # 信息条：状态点 + 平台徽章 + 标题
                title_bar = gr.HTML(INFO_BAR_EMPTY)

                gr.HTML('<div class="vp-divider"></div>')

                # 四个平台入口：点击只选择来源平台，不会覆盖用户已粘贴的链接。
                with gr.Row():
                    douyin_btn = gr.Button("抖音", size="sm", elem_classes=["example-btn"])
                    bilibili_btn = gr.Button("B站", size="sm", elem_classes=["example-btn"])
                    xiaohongshu_btn = gr.Button("小红书", size="sm", elem_classes=["example-btn"])
                    wechat_channels_btn = gr.Button("视频号", size="sm", elem_classes=["example-btn"])

                # 平台选择与清空（次级）
                with gr.Row(equal_height=True):
                    platform_dropdown = gr.Dropdown(
                        label="来源平台",
                        choices=["自动检测", "抖音", "哔哩哔哩", "小红书", "视频号"],
                        value="自动检测",
                        interactive=True,
                        elem_id="vp_dd_platform",
                        elem_classes=["vp-platform-select"],
                        scale=1,
                    )
                    clear_btn = gr.Button("清空", variant="secondary", size="lg", scale=1, elem_classes=["vp-ghost"], elem_id="vp_btn_clear")

                upload_input = gr.File(
                    label="上传自有 MP4（链接不可用时使用）", file_types=[".mp4"], type="filepath",
                    elem_id="vp_upload_input",
                )
                upload_btn = gr.Button("使用上传的视频", variant="secondary", elem_id="vp_btn_upload")
                upload_minutes = max(1, int(os.getenv("MAX_UPLOAD_DURATION_SECONDS", "1800"))) / 60
                gr.Markdown(
                    f"小红书链接若无法访问、或使用视频号素材，可先从本人有权使用的素材导出 MP4 再上传"
                    f"（最多 {max_upload_bytes() // (1024 * 1024)} MB、{upload_minutes:g} 分钟）。"
                )

                gr.HTML('<div class="vp-divider"></div>')

                # 播放 / 下载（玻璃次级按钮）
                with gr.Row():
                    play_btn = gr.Button("在线播放", variant="secondary", elem_classes=["vp-secondary"], elem_id="vp_btn_play")
                    download_btn = gr.Button("下载视频", variant="secondary", elem_classes=["vp-secondary"], elem_id="vp_btn_download")

                # 独立语音转写与证据分析
                with gr.Row(equal_height=True):
                    multi_speaker_chk = gr.Checkbox(
                        label="多人转写（AI 分析）",
                        value=False,
                        interactive=speaker_avail,
                        info=speaker_info,
                        elem_id="vp_chk_speaker",
                        elem_classes=["vp-toggle"],
                    )
                    extract_btn = gr.Button("AI 时间轴证据分析", variant="primary", interactive=AI_ANALYSIS_ENABLED,
                                            elem_classes=["vp-extract"], elem_id="vp_btn_extract")
                transcribe_btn = gr.Button("仅语音转写 · 导出 TXT / SRT", variant="secondary",
                                           interactive=bool(os.getenv("ASR_MODEL_ID", "").strip()),
                                           elem_id="vp_btn_transcribe")
                asr_limit = float(os.getenv("ASR_MAX_DURATION_SECONDS", "0") or 0)
                asr_limit_note = f"最多处理前 {asr_limit / 60:g} 分钟" if asr_limit > 0 else "处理完整音轨"
                gr.Markdown(f"语音转写会自动加载视频；当前{asr_limit_note}。结果由机器识别，请人工核对。")
                gr.Markdown("AI 分析暂未启用：模型服务账号尚未完成绑定。视频解析与下载可正常使用。",
                            visible=not AI_ANALYSIS_ENABLED)

                # 状态反馈（信息流底部）
                status_output = gr.Textbox(
                    label="状态",
                    lines=1,
                    max_lines=3,
                    interactive=False,
                    elem_id="vp_out_status",
                    elem_classes=["status-box"]
                )

            # ---------- 右栏：主预览（唯一视觉锚点） ----------
            with gr.Column(scale=6, elem_classes=["vp-card", "vp-preview"]):
                gr.HTML('<div class="vp-section-label"><span class="vp-section-num">02</span><span data-i18n="sec_preview">预览</span></div>')
                # 空态引导（解析前展示，解析成功后由事件输出空串隐藏）
                guide_html = gr.HTML(EMPTY_GUIDE_HTML)

                # 在线播放器（主导视觉；有内容才显示，避免首屏空白播放器）
                video_output = gr.Video(
                    label="在线播放",
                    height=440,
                    visible=False,
                    elem_id="vp_vid",
                    elem_classes=["vp-video"]
                )

                # 封面缩略 + 下载文件（预览下方信息带；有内容才显示）
                with gr.Row():
                    cover_output = gr.Image(
                        label="视频封面",
                        height=190,
                        visible=False,
                        elem_id="vp_img_cover",
                        elem_classes=["vp-cover"]
                    )
                    download_output = gr.File(
                        label="下载文件", elem_id="vp_file",
                        visible=False,
                    )

        # ---------- 全宽：AI 取证报告（Markdown Dashboard，章节 + 时间轴表格） ----------
        with gr.Column(elem_classes=["vp-card", "vp-card-wide"]):
            gr.HTML('<div class="vp-section-label"><span class="vp-section-num">03</span><span data-i18n="sec_report">分析与转写结果</span></div>')
            content_output = gr.Markdown(
                value=REPORT_PLACEHOLDER,
                sanitize_html=True,
                elem_classes=["vp-report"],
            )
            report_file_output = gr.File(
                label="下载分析报告（Markdown）",
                visible=False,
                elem_id="vp_report_download",
            )
            gr.HTML('<div class="vp-report-history"><a href="/account/files">查看并评价我的分析文件 →</a></div>')
            with gr.Row():
                asr_text_output = gr.File(label="下载 ASR 整理稿（TXT）", visible=False)
                asr_srt_output = gr.File(label="下载 ASR 原始字幕（SRT）", visible=False)

        # 解析结果保存在每个 Gradio 会话内，避免不同用户覆盖彼此的视频。
        video_url_state = gr.State("")
        video_info_state = gr.State({})

        # 平台 chips 只切换来源选择，不替换输入框里的链接。
        for button, platform_name in (
            (douyin_btn, "抖音"),
            (bilibili_btn, "哔哩哔哩"),
            (xiaohongshu_btn, "小红书"),
            (wechat_channels_btn, "视频号"),
        ):
            button.click(
                fn=lambda value=platform_name: select_platform(value),
                inputs=[],
                outputs=[platform_dropdown],
            )

        # 事件绑定
        parse_btn.click(
            fn=parse_video_for_ui,
            js=GATE_JS,
            inputs=[url_input, platform_dropdown],
            outputs=[status_output, title_bar, cover_output, video_url_state, guide_html, video_info_state,
                     video_output, download_output, content_output, report_file_output, asr_text_output, asr_srt_output,
                     upload_input]
        )

        upload_btn.click(
            fn=upload_owned_video,
            js=_gate_js("upload"),
            inputs=[upload_input, platform_dropdown],
            outputs=[status_output, title_bar, cover_output, video_url_state, guide_html, video_info_state,
                     video_output, download_output, content_output, report_file_output, asr_text_output, asr_srt_output],
            concurrency_limit=2, concurrency_id="media_transfer",
        )

        play_btn.click(
            fn=play_video,
            js=_gate_js("play"),
            inputs=[video_info_state],
            outputs=[video_output, status_output],
            concurrency_limit=2, concurrency_id="media_transfer",
        )

        download_btn.click(
            fn=download_video,
            js=_gate_js("download"),
            inputs=[video_info_state],
            outputs=[download_output, status_output],
            concurrency_limit=2, concurrency_id="media_transfer",
        )

        extract_btn.click(
            fn=extract_video_content,
            js=_gate_js("extract"),
            inputs=[multi_speaker_chk, video_info_state],
            outputs=[content_output, status_output, report_file_output, asr_text_output, asr_srt_output],
            concurrency_limit=1, concurrency_id="media_analysis",
        )

        transcribe_btn.click(
            fn=transcribe_video_only,
            js=_gate_js("transcribe"),
            inputs=[video_info_state],
            outputs=[content_output, status_output, report_file_output, asr_text_output, asr_srt_output],
            concurrency_limit=1, concurrency_id="media_analysis",
        )

        clear_btn.click(
            fn=clear_all,
            inputs=[],
            outputs=[url_input, platform_dropdown, status_output, title_bar,
                    cover_output, video_output, download_output, content_output,
                    report_file_output, asr_text_output, asr_srt_output,
                    guide_html, video_info_state, upload_input]
        )

        # 使用指南（双列横向卡，打破同构小卡网格）
        gr.HTML(
            """
            <div class="vp-section-label"><span class="vp-section-num">04</span><span data-i18n="sec_help">使用指南</span></div>
            <div class="vp-help">
              <div class="vp-help-grid">
                <div class="vp-help-item">
                  <div class="vp-help-step">01</div>
                  <div class="vp-help-title" data-i18n="help_t1">粘贴链接或上传</div>
                  <div class="vp-help-desc" data-i18n="help_d1">抖音、B站可粘贴分享链接；小红书请复制 App 内完整链接，无法访问时可上传自有 MP4；视频号需上传自有 MP4。</div>
                </div>
                <div class="vp-help-item">
                  <div class="vp-help-step">02</div>
                  <div class="vp-help-title" data-i18n="help_t2">解析视频</div>
                  <div class="vp-help-desc" data-i18n="help_d2">封面、时长与视频信息一屏展示，无需手动选择来源</div>
                </div>
                <div class="vp-help-item">
                  <div class="vp-help-step">03</div>
                  <div class="vp-help-title" data-i18n="help_t3">播放 / 下载</div>
                  <div class="vp-help-desc" data-i18n="help_d3">在线播放使用临时缓存；可下载平台提供的媒体流（B 站自动合并音视频）</div>
                </div>
                <div class="vp-help-item">
                  <div class="vp-help-step">04</div>
                  <div class="vp-help-title" data-i18n="help_t4">转写 / AI 分析</div>
                  <div class="vp-help-desc" data-i18n="help_d4">可单独导出语音转写 TXT / SRT，或按时间轴分析字幕、音频和画面</div>
                </div>
              </div>
              <div class="vp-help-notes" data-i18n="help_notes">生成的视频、转写与报告将在约 48 小时后清理 · 处理时自动加载视频</div>
              <div class="vp-help-notes"><a href="/data-policy">使用与数据说明 / Data &amp; usage</a></div>
            </div>
            """
        )

    app.queue(max_size=12)
    return app


def install_ffmpeg():
    """在 Linux 环境下尝试自动安装 ffmpeg"""
    if os.name == 'nt':  # Windows 不支持自动安装
        return

    try:
        # 检查是否已安装
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        print("检测到 ffmpeg 已安装")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("未检测到 ffmpeg，尝试自动安装...")
        try:
            # 尝试使用 apt-get 安装 (适用于 Debian/Ubuntu)
            # 先尝试不用 sudo
            process = subprocess.run(['apt-get', 'update'], capture_output=True)
            if process.returncode != 0:
                # 如果失败，尝试 sudo
                print("尝试使用 sudo apt-get update...")
                subprocess.run(['sudo', 'apt-get', 'update'], check=True)
                subprocess.run(['sudo', 'apt-get', 'install', '-y', 'ffmpeg'], check=True)
            else:
                print("使用 apt-get install ffmpeg...")
                subprocess.run(['apt-get', 'install', '-y', 'ffmpeg'], check=True)
            print("ffmpeg 安装成功")
        except Exception as e:
            print(f"自动安装 ffmpeg 失败: {e}")
            print("请手动安装 ffmpeg 或在部署平台配置 packages.txt")


# ==================== 访问鉴权中间件（Basic Auth）====================
# 仓库为公开仓库，密码不写死在代码里，从环境变量 APP_USER / APP_PASS 读取
# 在 ASGI 层包裹整个应用：未携带正确凭证一律 401（HTTP）或关闭连接（WebSocket）
# 例外：/health 放行，否则容器 HEALTHCHECK 会判定 unhealthy
class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, username: str, password: str):
        self.app = app
        self.expected = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            # 容器健康检查放行，否则会被判定 unhealthy
            if path == "/health":
                await self.app(scope, receive, send)
                return
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode("latin-1")
            if auth == self.expected:
                await self.app(scope, receive, send)
                return
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"www-authenticate", b'Basic realm="video-parser"'),
                ],
            })
            await send({"type": "http.response.body", "body": b"401 Unauthorized"})
            return
        await self.app(scope, receive, send)


class VersionedAssetsMiddleware:
    """资源缓存键升级：把 HTML 里的 /assets/、/theme.css、/static/js/ 引用改写为 /v2/ 前缀，
    并将 /v2/* 请求还原回原路径交给内部路由。
    目的：让浏览器以全新 URL 重新拉取资源，绕开早期 no-store 阶段写坏的磁盘缓存条目
    （Chrome ERR_CACHE_READ_FAILURE / Failed to fetch dynamically imported module）。
    动态 import 的 chunk 相对 index.js 解析，因此随前缀一起换键，整棵资源树都会干净重拉。
    """

    def __init__(self, app, prefix="/v2"):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(self.prefix + "/"):
            scope = dict(scope)
            scope["path"] = path[len(self.prefix):]

        is_html = False

        async def send_wrapper(message):
            nonlocal is_html
            if message["type"] == "http.response.start":
                is_html = any(
                    k.lower() == b"content-type" and b"text/html" in v
                    for k, v in message.get("headers", [])
                )
                if is_html:
                    # body 会被改写，去掉 content-length 让服务端按 chunked 重算
                    message = dict(message)
                    message["headers"] = [
                        (k, v) for (k, v) in message.get("headers", [])
                        if k.lower() != b"content-length"
                    ]
            elif message["type"] == "http.response.body" and is_html and "body" in message:
                body = message["body"].replace(
                    b"/assets/", (self.prefix + "/assets/").encode()
                ).replace(
                    b"/theme.css", (self.prefix + "/theme.css").encode()
                ).replace(
                    b"/static/js/", (self.prefix + "/static/js/").encode()
                )
                message = dict(message)
                message["body"] = body
            await send(message)

        await self.app(scope, receive, send_wrapper)


class NoCacheMiddleware:
    """缓存策略：
    - 页面与 API：no-store，杜绝浏览器/预览器缓存旧版页面导致"刷新就变"
    - 前端资源（/assets、/theme.css、/static）：no-cache + must-revalidate，
      强制每次加载先回源校验——既自愈早期 no-store 造成的损坏缓存条目
      （Chrome ERR_CACHE_READ_FAILURE / 动态模块加载失败），也保证部署后不残留旧资源
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                path = scope.get("path", "")
                ctype = ""
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        ctype = v.decode("latin-1", "ignore")
                if "text/html" in ctype or ctype.startswith("application/json"):
                    policy = b"no-store, no-cache, must-revalidate, max-age=0"
                elif path.startswith("/assets/") or path.startswith("/theme.css") or path.startswith("/static/"):
                    policy = b"no-cache, must-revalidate"
                else:
                    await send(message)
                    return
                headers = [
                    (k, v) for (k, v) in message.get("headers", [])
                    if k.lower() not in (b"cache-control", b"pragma", b"expires")
                ]
                headers.append((b"cache-control", policy))
                headers.append((b"pragma", b"no-cache"))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ==================== 主程序 ====================

if __name__ == "__main__":
    # 尝试安装依赖
    install_ffmpeg()
    
    # 启动飞书机器人长连接（后台守护线程）
    try:
        from feishu_bot import start_feishu_bot
        bot_thread = threading.Thread(target=start_feishu_bot, daemon=True)
        bot_thread.start()
        print("飞书机器人已启动（后台线程）")
    except ImportError:
        print("lark-oapi 未安装，飞书机器人未启动（pip install lark-oapi 后可用）")
    except Exception as e:
        print(f"飞书机器人启动失败: {e}")
    
    app = create_app()

    # 深色玻璃工作台主题（Gradio 6 需 set 变量使组件内联计算值同步为深色）
    _theme = build_glass_theme()

    # 挂载 FastAPI 应用到 Gradio，共享端口 (7860)
    # 样式经 <link> 注入 <head>（static/css/app.css，改 CSS 无需重启服务），
    # 主题脚本内联注入，head 解析期间同步应用主题，杜绝闪屏与布局抖动。
    # ?v= 版本号防缓存：CSS 迭代后强制浏览器拉新（否则旧样式会残留在用户端）
    HEAD_CONTENT = ('<link rel="stylesheet" href="/static/css/app.css?v=20261001a">\n' + THEME_SCRIPT
                    + '<script src="/static/js/auth-modal.js?v=20261001a" defer></script>')
    try:
        combined_app = gr.mount_gradio_app(
            api_app, app, path="/",
            theme=_theme,
            head=HEAD_CONTENT,
            max_file_size=max_upload_bytes(),
        )
    except TypeError:
        app.theme = _theme
        combined_app = gr.mount_gradio_app(api_app, app, path="/")

    # 访问鉴权：SQLite 数据库 + 会话 Cookie（auth_db / auth_middleware）。
    # 默认启用会话鉴权；首次启动空库时以 APP_USER / APP_PASS 播种初始管理员，
    # 之后凭据以数据库为准（可在 /register 注册、/account 改密）。
    # 兼容旧部署：REQUIRE_AUTH 未配置但设置了 APP_PASS 时也启用。
    _auth_on = os.getenv("REQUIRE_AUTH", "1").strip().lower() not in {"0", "false", "no", "off"} or bool(os.getenv("APP_PASS", ""))
    if _auth_on:
        auth_db.init_db()

    # 缓存策略：页面/API no-store；前端资源 no-cache 回源校验（防"刷新就变"与损坏缓存）
    combined_app = NoCacheMiddleware(combined_app)

    # 资源缓存键升级：/v2/ 前缀绕开损坏的磁盘缓存条目（ERR_CACHE_READ_FAILURE 自愈）
    combined_app = VersionedAssetsMiddleware(combined_app)

    if _auth_on:
        # 会话鉴权放最外层：未登录一律挡在缓存/改写逻辑之前
        combined_app = SessionAuthMiddleware(combined_app)
        print("已启用会话登录鉴权（SQLite）")
    else:
        print("警告：未启用访问鉴权（REQUIRE_AUTH=1 或 APP_PASS 均未配置）")

    import uvicorn
    print("正在启动服务，请访问 http://localhost:7860")
    uvicorn.run(
        combined_app,
        host="0.0.0.0",
        port=7860,
    )
