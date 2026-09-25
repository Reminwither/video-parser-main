#!/usr/bin/env python3
"""
视频解析下载 Gradio 应用
支持抖音、哔哩哔哩、小红书、快手、好看视频的解析下载和在线播放
"""

import os
import html
import base64
import time
import random
import string
import threading
import requests
import subprocess
import gradio as gr
from typing import Optional, Tuple
from starlette.types import ASGIApp, Scope, Receive, Send
from urllib.parse import urlparse, parse_qs, urlencode
from openai import OpenAI
from dotenv import load_dotenv
from api import app as api_app, parse_video as api_parse_video, download_video as api_download_video, ParseRequest, DownloadRequest
from video_analysis import analyze_video_evidence_first

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
    url_lower = url.lower()
    if 'douyin.com' in url_lower or 'iesdouyin.com' in url_lower:
        return "抖音"
    elif 'bilibili.com' in url_lower or 'b23.tv' in url_lower:
        return "哔哩哔哩"
    elif 'xiaohongshu.com' in url_lower or 'xhslink.com' in url_lower:
        return "小红书"
    elif 'kuaishou.com' in url_lower:
        return "快手"
    elif 'haokan.baidu.com' in url_lower:
        return "好看视频"
    return "自动检测"


# ==================== API 调用 ====================

# Qwen3-VL API 配置（从环境变量读取）
# 优先从环境变量读取，避免被其他不相关的环境变量（如 API_SERVER_URL）干扰
QWEN_API_BASE_URL = os.getenv('QWEN_API_BASE_URL', 'https://api-inference.modelscope.cn/v1')
QWEN_API_KEY = os.getenv('QWEN_API_KEY', '')
QWEN_MODEL_ID = os.getenv('QWEN_MODEL_ID', 'Qwen/Qwen3-VL-8B-Instruct')

# 强制检查：如果 QWEN_MODEL_ID 看起来像一个 URL（通常是因为环境变量冲突），则重置为默认值
if QWEN_MODEL_ID.startswith('http'):
    print(f"警告: 检测到异常的模型 ID: {QWEN_MODEL_ID}，正在重置为默认值")
    QWEN_MODEL_ID = 'Qwen/Qwen3-VL-8B-Instruct'

MAX_ANALYSIS_FRAMES = int(os.getenv('MAX_ANALYSIS_FRAMES', '24'))

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
            response = requests.get(cover_url, headers=headers, timeout=30)
            response.raise_for_status()

            with open(cache_path, 'wb') as f:
                f.write(response.content)

            return cache_path
        except Exception as e:
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

        except Exception as e:
            print(f"解析出错: {e}")
            return False, {}, f"解析出错: {str(e)}"

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

        try:
            response = requests.get(url, headers=headers, stream=True, timeout=180)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if progress_callback and total_size > 0:
                            progress = downloaded_size / total_size
                            progress_callback(progress)

            size_mb = os.path.getsize(filepath) / 1024 / 1024
            return True, filepath, f"下载完成 ({size_mb:.1f} MB)"

        except Exception as e:
            if os.path.exists(filepath):
                os.remove(filepath)
            return False, "", f"下载失败: {str(e)}"

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
                return False, f"合并失败: {result.stderr}"
        except FileNotFoundError:
            return False, "未找到 ffmpeg，请确保已安装 ffmpeg 并添加到系统 PATH 中，或设置 FFMPEG_PATH 环境变量。"


# ==================== Gradio 界面函数 ====================

# 全局客户端实例
client = VideoClient()

# 存储当前解析的视频信息
current_video_info = {}


def parse_video(url: str, platform: str) -> Tuple[str, str, str, str, str]:
    """
    解析视频

    Returns:
        (status_message, info_bar_html, cover_update, video_url, guide_html)
    """
    import asyncio
    status, info, cover, video_url, guide = asyncio.run(_async_parse_video(url, platform))
    if cover:
        cover_out = gr.update(visible=True, value=cover)
    else:
        cover_out = gr.update(visible=False)
    return status, info, cover_out, video_url, guide

async def _async_parse_video(url: str, platform: str) -> Tuple[str, str, str, str, str]:
    global current_video_info

    if not url or not url.strip():
        return "请输入视频链接", _info_bar_html("请输入视频链接", "", ok=False), None, "", EMPTY_GUIDE_HTML

    url = url.strip()

    # 自动检测平台
    if platform == "自动检测":
        detected = detect_platform(url)
        if detected == "自动检测":
            detected = "未知平台"
        platform = detected

    success, data, message = await client.parse_video(url)

    if success:
        current_video_info = data
        current_video_info['original_url'] = url

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
            cover_local_path = client.download_cover(cover_url, video_id, referer)

        status = f"解析成功 - {platform_name}"
        return status, _info_bar_html(title, platform_name, ok=True), cover_local_path, video_url, ""
    else:
        current_video_info = {}
        return f"解析失败: {message}", _info_bar_html("解析失败：" + message, "", ok=False), None, "", EMPTY_GUIDE_HTML


def play_video(progress=gr.Progress()) -> Tuple[str, str]:
    """
    播放视频（先下载到本地缓存再播放）

    Returns:
        (video_update, status_message)
    """
    import asyncio
    video_path, status = asyncio.run(_async_play_video(progress))
    if video_path:
        return gr.update(visible=True, value=video_path), status
    return gr.update(visible=False), status

async def _async_play_video(progress) -> Tuple[str, str]:
    global current_video_info

    if not current_video_info:
        return None, "请先解析视频"

    video_url = current_video_info.get('video_url', '')
    audio_url = current_video_info.get('audio_url', '')
    video_id = current_video_info.get('video_id', 'video')
    platform = current_video_info.get('platform', '')
    original_url = current_video_info.get('original_url', '')

    # 获取视频下载地址
    download_url = await client.get_download_url(video_url, video_id, original_url)
    if download_url:
        video_url = download_url

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
    safe_id = "".join(c for c in video_id if c.isalnum())[:30] or "video"
    cache_path = os.path.join(client.cache_dir, f"{safe_id}_play.mp4")

    # 如果已经缓存过，直接返回
    if os.path.exists(cache_path):
        return cache_path, "加载完成（使用缓存）"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    }
    if referer:
        headers['Referer'] = referer

    # B站视频需要分别下载音视频再合并
    if platform == '哔哩哔哩' and audio_url:
        video_temp = os.path.join(client.cache_dir, f"{safe_id}_video.m4s")
        audio_temp = os.path.join(client.cache_dir, f"{safe_id}_audio.m4s")

        try:
            # 下载视频轨道
            progress(0, desc="下载视频轨道...")
            response = requests.get(video_url, headers=headers, stream=True, timeout=180)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(video_temp, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress(downloaded_size / total_size * 0.4, desc=f"下载视频轨道: {downloaded_size * 100 // total_size}%")

            # 下载音频轨道
            progress(0.4, desc="下载音频轨道...")
            response = requests.get(audio_url, headers=headers, stream=True, timeout=180)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(audio_temp, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress(0.4 + downloaded_size / total_size * 0.4, desc=f"下载音频轨道: {downloaded_size * 100 // total_size}%")

            # 合并音视频
            progress(0.8, desc="合并音视频...")
            success, msg = client.merge_video_audio(video_temp, audio_temp, cache_path)

            if success:
                progress(1.0, desc="加载完成")
                return cache_path, "加载完成"
            else:
                return None, f"合并失败: {msg}"

        except Exception as e:
            # 清理临时文件
            for temp_file in [video_temp, audio_temp, cache_path]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            return None, f"加载失败: {str(e)}"

    else:
        # 其他平台直接下载视频
        try:
            progress(0, desc="正在加载视频...")
            response = requests.get(video_url, headers=headers, stream=True, timeout=180)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(cache_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress(downloaded_size / total_size, desc=f"加载中: {downloaded_size * 100 // total_size}%")

            progress(1.0, desc="加载完成")
            return cache_path, "加载完成"

        except Exception as e:
            if os.path.exists(cache_path):
                os.remove(cache_path)
            return None, f"加载失败: {str(e)}"


def download_video(progress=gr.Progress()) -> Tuple[str, str]:
    """
    下载视频

    Returns:
        (file_update, status_message)
    """
    import asyncio
    filepath, status = asyncio.run(_async_download_video(progress))
    if filepath:
        return gr.update(visible=True, value=filepath), status
    return gr.update(visible=False), status

async def _async_download_video(progress) -> Tuple[str, str]:
    global current_video_info

    if not current_video_info:
        return None, "请先解析视频"

    video_url = current_video_info.get('video_url', '')
    audio_url = current_video_info.get('audio_url', '')
    video_id = current_video_info.get('video_id', 'video')
    title = current_video_info.get('title', 'video')
    platform = current_video_info.get('platform', '')
    original_url = current_video_info.get('original_url', '')

    # 获取视频下载地址
    download_url = await client.get_download_url(video_url, video_id, original_url)
    if download_url:
        video_url = download_url

    if not video_url:
        return None, "未找到视频链接"

    # 清理文件名
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).strip()
    if not safe_title:
        safe_title = video_id
    safe_title = safe_title[:50]

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

        video_temp = os.path.join(client.download_dir, f"{safe_title}_video.m4s")
        audio_temp = os.path.join(client.download_dir, f"{safe_title}_audio.m4s")
        output_path = os.path.join(client.download_dir, f"{safe_title}.mp4")

        # 下载视频轨道
        def video_progress(p):
            progress(p * 0.4, desc=f"下载视频轨道: {p*100:.0f}%")

        success, _, msg = client.download_file(video_url, f"{safe_title}_video.m4s",
                                                referer, video_progress)
        if not success:
            return None, f"视频轨道下载失败: {msg}"

        progress(0.4, desc="下载音频轨道...")

        # 下载音频轨道
        def audio_progress(p):
            progress(0.4 + p * 0.4, desc=f"下载音频轨道: {p*100:.0f}%")

        success, _, msg = client.download_file(audio_url, f"{safe_title}_audio.m4s",
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

        filename = f"{safe_title}.mp4"
        success, filepath, msg = client.download_file(video_url, filename,
                                                       referer, download_progress)

        if success:
            return filepath, msg
        else:
            return None, msg


def extract_video_content(multi_speaker: bool = False, progress=gr.Progress()) -> Tuple[str, str]:
    """
    按时间轴建立多模态证据后生成视频分析。

    Returns:
        (content_text, status_message)
    """
    global current_video_info

    if not current_video_info:
        return REPORT_PLACEHOLDER, "请先解析视频"

    video_id = current_video_info.get('video_id', 'video')

    # 检查是否有缓存的视频文件
    safe_id = "".join(c for c in video_id if c.isalnum())[:30] or "video"
    cache_path = os.path.join(client.cache_dir, f"{safe_id}_play.mp4")

    if not os.path.exists(cache_path):
        return REPORT_PLACEHOLDER, "请先点击「在线播放」加载视频后再提取内容"

    try:
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
            title=current_video_info.get('title'),
            video_info=current_video_info,
            max_frames=MAX_ANALYSIS_FRAMES,
            progress=report_progress,
            diarize=bool(multi_speaker),
        )
        diarize_note = "多人转写已开启" if multi_speaker else ""
        status = (
            f"证据优先分析完成：{len(evidence.frames)} 个时间点，"
            f"{len(evidence.subtitles)} 条字幕，ASR={evidence.transcript_status} {diarize_note}"
        )
        return result, status

    except Exception as e:
        import traceback
        traceback.print_exc()
        return "", f"提取失败: {str(e)}"


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
  <div class="vp-report-empty-head">分析报告将在这里生成</div>
  <div class="vp-report-empty-sub">先点击「在线播放」加载视频缓存，再点击「AI 时间轴证据分析」开始取证</div>
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
  <div class="vp-empty-head">等待视频解析</div>
  <div class="vp-empty-desc">在左侧粘贴视频链接并点击「解析视频」，即可在此生成封面与在线播放</div>
  <div class="vp-empty-flow">
    <span class="vp-flow-step"><i>1</i>粘贴链接</span>
    <span class="vp-flow-arrow">→</span>
    <span class="vp-flow-step"><i>2</i>解析视频</span>
    <span class="vp-flow-arrow">→</span>
    <span class="vp-flow-step"><i>3</i>AI 取证</span>
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
    vpApplyTheme(vpTheme() === 'dark' ? 'light' : 'dark');
  }
  function vpScrollTo(sel) {
    var el = document.querySelector(sel);
    if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  }
  function vpLogin() {
    alert('账号登录功能即将上线，敬请期待');
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
  window.addEventListener('load', vpSyncLabel);
  setTimeout(vpSyncLabel, 600);

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
    global current_video_info
    current_video_info = {}
    return ("", "自动检测", "", INFO_BAR_EMPTY,
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            REPORT_PLACEHOLDER, EMPTY_GUIDE_HTML)


# ==================== Gradio 界面 ====================

# 示例视频链接
EXAMPLE_URLS = {
    "抖音": "https://www.douyin.com/note/7580598241298069157",
    "哔哩哔哩": "https://www.bilibili.com/video/BV1TaqYBcEJc",
    "小红书": "https://www.xiaohongshu.com/explore/68ab2dd1000000001c0045d0?app_platform=android&ignoreEngage=true&app_version=9.13.1&share_from_user_hidden=true&xsec_source=app_share&type=video&xsec_token=CBLONm9tab3493BJUXCtvU8ScDzfYqa3cGwJstW8eSF3Y=&author_share=1&xhsshare=&shareRedId=OD04RDk1PUw2NzUyOTgwNjc7OThISj5O&apptime=1766754031&share_id=46cc1edc70704cd1ae467afb90d0e8e6&share_channel=wechat&wechatWid=94270060b457f72e6e3a2df13090e1d6&wechatOrigin=menu",
    "快手": "https://www.kuaishou.com/short-video/3x8zha3ipq6bg8q?authorId=3xcyp2v85enrv7w&streamSource=find&area=homexxbrilliant",
    "好看视频": "https://haokan.baidu.com/v?vid=13766973483433940333&tab=recommend",
}


def fill_example(platform: str):
    """填充示例链接"""
    url = EXAMPLE_URLS.get(platform, "")
    return url, platform


def check_ffmpeg() -> bool:
    """检查 ffmpeg 是否可用"""
    try:
        subprocess.run([os.getenv('FFMPEG_PATH', 'ffmpeg'), '-version'], capture_output=True)
        return True
    except FileNotFoundError:
        return False


# ==================== TikHub 风格主题（浅色默认，黑白灰克制视觉） ====================
# 视觉方向：对齐 tikhub.io —— 暖白底 #fafaf9、近黑文字 #1a1a1a、次级灰 #6b7280、
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
        "body_background_fill": "#fafaf9",
        "background_fill_primary": "#fafaf9",
        "background_fill_secondary": "#f4f4f5",
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
                  <a href="javascript:void(0)" onclick="vpScrollTo('.vp-ops')">视频解析</a>
                  <a href="javascript:void(0)" onclick="vpScrollTo('.vp-report')">AI 分析</a>
                  <a href="javascript:void(0)" onclick="vpScrollTo('.vp-help')">使用指南</a>
                </div>
                <div class="vp-nav-right">
                  <button class="vp-theme-round" id="vp-theme-btn" type="button" onclick="vpToggleTheme()" title="切换深色 / 浅色模式" aria-label="切换深色 / 浅色模式">
                    <svg class="vp-icon-sun" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
                    <svg class="vp-icon-moon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8z"/></svg>
                  </button>
                  <a class="vp-login-link" href="javascript:void(0)" onclick="vpLogin()">
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m10 17 5-5-5-5"/><path d="M15 12H3"/><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/></svg>
                    登录
                  </a>
                  <button class="vp-cta-pill" type="button" onclick="vpScrollTo('.vp-url')">开始使用</button>
                </div>
              </div>
            </div>
            """
        )

        # ===== Hero（首屏定位，对齐 TikHub：pill 徽章 + 大字标题 + 一句价值主张） =====
        gr.HTML(
            """
            <section class="vp-hero">
              <div class="vp-hero-eyebrow"><span class="vp-hero-dot"></span>视频智能分析平台</div>
              <h1 class="vp-hero-title">解析 · 下载 · <span class="vp-hero-accent">AI 取证</span></h1>
              <p class="vp-hero-sub">粘贴抖音、哔哩哔哩、小红书、快手、好看视频链接，在线播放、下载无水印原画，并按时间轴生成多模态证据报告。</p>
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
                gr.HTML('<div class="vp-section-label"><span class="vp-section-num">01</span>解析视频</div>')
                url_input = gr.Textbox(
                    label="视频链接",
                    placeholder="粘贴抖音 / B站 / 小红书 / 快手 / 好看视频分享链接…",
                    lines=3,
                    elem_classes=["vp-url"]
                )

                # 主 CTA：紧跟输入框，独立全宽，唯一视觉主导（黑底白字，克制的 TikHub 语言）
                parse_btn = gr.Button("解析视频", variant="primary", size="lg", elem_classes=["vp-cta"])

                # 信息条：状态点 + 平台徽章 + 标题
                title_bar = gr.HTML(INFO_BAR_EMPTY)

                gr.HTML('<div class="vp-divider"></div>')

                # 示例链接（轻量文字 chips）
                with gr.Row():
                    douyin_btn = gr.Button("抖音", size="sm", elem_classes=["example-btn"])
                    bilibili_btn = gr.Button("哔哩哔哩", size="sm", elem_classes=["example-btn"])
                    xiaohongshu_btn = gr.Button("小红书", size="sm", elem_classes=["example-btn"])
                    kuaishou_btn = gr.Button("快手", size="sm", elem_classes=["example-btn"])
                    haokan_btn = gr.Button("好看视频", size="sm", elem_classes=["example-btn"])

                # 平台选择与清空（次级）
                with gr.Row(equal_height=True):
                    platform_dropdown = gr.Dropdown(
                        label="来源平台",
                        choices=["自动检测", "抖音", "哔哩哔哩", "小红书", "快手", "好看视频"],
                        value="自动检测",
                        interactive=True,
                        elem_classes=["vp-platform-select"],
                        scale=1,
                    )
                    clear_btn = gr.Button("清空", variant="secondary", size="lg", scale=1, elem_classes=["vp-ghost"])

                gr.HTML('<div class="vp-divider"></div>')

                # 播放 / 下载（玻璃次级按钮）
                with gr.Row():
                    play_btn = gr.Button("在线播放", variant="secondary", elem_classes=["vp-secondary"])
                    download_btn = gr.Button("下载视频", variant="secondary", elem_classes=["vp-secondary"])

                # 视频内容提取（多人转写开关 + AI 分析按钮）
                with gr.Row(equal_height=True):
                    multi_speaker_chk = gr.Checkbox(
                        label="多人转写",
                        value=False,
                        interactive=speaker_avail,
                        info=speaker_info,
                        elem_classes=["vp-toggle"],
                    )
                    extract_btn = gr.Button("AI 时间轴证据分析", variant="primary", elem_classes=["vp-extract"])

                # 状态反馈（信息流底部）
                status_output = gr.Textbox(
                    label="状态",
                    lines=1,
                    max_lines=3,
                    interactive=False,
                    elem_classes=["status-box"]
                )

            # ---------- 右栏：主预览（唯一视觉锚点） ----------
            with gr.Column(scale=6, elem_classes=["vp-card", "vp-preview"]):
                gr.HTML('<div class="vp-section-label"><span class="vp-section-num">02</span>预览</div>')
                # 空态引导（解析前展示，解析成功后由事件输出空串隐藏）
                guide_html = gr.HTML(EMPTY_GUIDE_HTML)

                # 在线播放器（主导视觉；有内容才显示，避免首屏空白播放器）
                video_output = gr.Video(
                    label="在线播放",
                    height=440,
                    visible=False,
                    elem_classes=["vp-video"]
                )

                # 封面缩略 + 下载文件（预览下方信息带；有内容才显示）
                with gr.Row():
                    cover_output = gr.Image(
                        label="视频封面",
                        height=190,
                        visible=False,
                        elem_classes=["vp-cover"]
                    )
                    download_output = gr.File(
                        label="下载文件",
                        visible=False,
                    )

        # ---------- 全宽：AI 取证报告（Markdown Dashboard，章节 + 时间轴表格） ----------
        with gr.Column(elem_classes=["vp-card", "vp-card-wide"]):
            gr.HTML('<div class="vp-section-label"><span class="vp-section-num">03</span>AI 取证报告</div>')
            content_output = gr.Markdown(
                value=REPORT_PLACEHOLDER,
                sanitize_html=True,
                elem_classes=["vp-report"],
            )

        # 隐藏的视频 URL 存储
        video_url_state = gr.State("")

        # 示例链接按钮事件绑定
        douyin_btn.click(
            fn=lambda: fill_example("抖音"),
            inputs=[],
            outputs=[url_input, platform_dropdown]
        )
        bilibili_btn.click(
            fn=lambda: fill_example("哔哩哔哩"),
            inputs=[],
            outputs=[url_input, platform_dropdown]
        )
        xiaohongshu_btn.click(
            fn=lambda: fill_example("小红书"),
            inputs=[],
            outputs=[url_input, platform_dropdown]
        )
        kuaishou_btn.click(
            fn=lambda: fill_example("快手"),
            inputs=[],
            outputs=[url_input, platform_dropdown]
        )
        haokan_btn.click(
            fn=lambda: fill_example("好看视频"),
            inputs=[],
            outputs=[url_input, platform_dropdown]
        )

        # 事件绑定
        parse_btn.click(
            fn=parse_video,
            inputs=[url_input, platform_dropdown],
            outputs=[status_output, title_bar, cover_output, video_url_state, guide_html]
        )

        play_btn.click(
            fn=play_video,
            inputs=[],
            outputs=[video_output, status_output]
        )

        download_btn.click(
            fn=download_video,
            inputs=[],
            outputs=[download_output, status_output]
        )

        extract_btn.click(
            fn=extract_video_content,
            inputs=[multi_speaker_chk],
            outputs=[content_output, status_output]
        )

        clear_btn.click(
            fn=clear_all,
            inputs=[],
            outputs=[url_input, platform_dropdown, status_output, title_bar,
                    cover_output, video_output, download_output, content_output, guide_html]
        )

        # 使用指南（双列横向卡，打破同构小卡网格）
        gr.HTML(
            """
            <div class="vp-section-label"><span class="vp-section-num">04</span>使用指南</div>
            <div class="vp-help">
              <div class="vp-help-grid">
                <div class="vp-help-item">
                  <div class="vp-help-step">01</div>
                  <div class="vp-help-title">粘贴链接</div>
                  <div class="vp-help-desc">支持抖音 / 哔哩哔哩 / 小红书 / 快手 / 好看视频分享链接，自动识别平台</div>
                </div>
                <div class="vp-help-item">
                  <div class="vp-help-step">02</div>
                  <div class="vp-help-title">解析视频</div>
                  <div class="vp-help-desc">封面、时长与视频信息一屏展示，无需手动选择来源</div>
                </div>
                <div class="vp-help-item">
                  <div class="vp-help-step">03</div>
                  <div class="vp-help-title">播放 / 下载</div>
                  <div class="vp-help-desc">在线播放走本地缓存，下载输出无水印原画（B 站自动合并音视频）</div>
                </div>
                <div class="vp-help-item">
                  <div class="vp-help-step">04</div>
                  <div class="vp-help-title">AI 取证分析</div>
                  <div class="vp-help-desc">字幕 / 音频 / 画面逐段时间轴取证，支持多人转写输出分组稿</div>
                </div>
              </div>
              <div class="vp-help-notes">下载的视频保存在 downloads 目录 · AI 分析前需先播放视频加载缓存</div>
            </div>
            """
        )

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
    HEAD_CONTENT = '<link rel="stylesheet" href="/static/css/app.css?v=20260925c">\n' + THEME_SCRIPT
    try:
        combined_app = gr.mount_gradio_app(
            api_app, app, path="/",
            theme=_theme,
            head=HEAD_CONTENT,
        )
    except TypeError:
        app.theme = _theme
        combined_app = gr.mount_gradio_app(api_app, app, path="/")

    # 访问鉴权：若服务器 .env 配置了 APP_PASS，则启用 Basic Auth（账号密码从环境变量读）
    _app_user = os.getenv("APP_USER", "admin")
    _app_pass = os.getenv("APP_PASS", "")
    if os.getenv("REQUIRE_AUTH") == "1" and not _app_pass:
        raise RuntimeError("REQUIRE_AUTH=1 requires APP_PASS")

    # 缓存策略：页面/API no-store；前端资源 no-cache 回源校验（防"刷新就变"与损坏缓存）
    combined_app = NoCacheMiddleware(combined_app)

    # 资源缓存键升级：/v2/ 前缀绕开损坏的磁盘缓存条目（ERR_CACHE_READ_FAILURE 自愈）
    combined_app = VersionedAssetsMiddleware(combined_app)

    if _app_pass:
        combined_app = BasicAuthMiddleware(combined_app, _app_user, _app_pass)
        print("已启用 Basic Auth 访问鉴权")
    else:
        print("警告：未配置 APP_PASS，工作台将无访问鉴权，请尽快在 .env 配置")

    import uvicorn
    print("正在启动服务，请访问 http://localhost:7860")
    uvicorn.run(
        combined_app,
        host="0.0.0.0",
        port=7860,
    )
