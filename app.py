#!/usr/bin/env python3
"""
视频解析下载 Gradio 应用
支持抖音、哔哩哔哩、小红书、快手、好看视频的解析下载和在线播放
"""

import os
import time
import random
import string
import threading
import requests
import subprocess
import gradio as gr
from typing import Optional, Tuple
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
        (status_message, title, cover_image, video_url, platform_detected)
    """
    import asyncio
    return asyncio.run(_async_parse_video(url, platform))

async def _async_parse_video(url: str, platform: str) -> Tuple[str, str, str, str, str]:
    global current_video_info

    if not url or not url.strip():
        return "请输入视频链接", "", None, "", ""

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
        return status, title, cover_local_path, video_url, platform_name
    else:
        current_video_info = {}
        return f"解析失败: {message}", "", None, "", ""


def play_video(progress=gr.Progress()) -> Tuple[str, str]:
    """
    播放视频（先下载到本地缓存再播放）

    Returns:
        (video_path, status_message)
    """
    import asyncio
    return asyncio.run(_async_play_video(progress))

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
        (filepath, status_message)
    """
    import asyncio
    return asyncio.run(_async_download_video(progress))

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


def extract_video_content(progress=gr.Progress()) -> Tuple[str, str]:
    """
    按时间轴建立多模态证据后生成视频分析。

    Returns:
        (content_text, status_message)
    """
    global current_video_info

    if not current_video_info:
        return "", "请先解析视频"

    video_id = current_video_info.get('video_id', 'video')

    # 检查是否有缓存的视频文件
    safe_id = "".join(c for c in video_id if c.isalnum())[:30] or "video"
    cache_path = os.path.join(client.cache_dir, f"{safe_id}_play.mp4")

    if not os.path.exists(cache_path):
        return "", "请先点击「在线播放」加载视频后再提取内容"

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
        )
        status = (
            f"证据优先分析完成：{len(evidence.frames)} 个时间点，"
            f"{len(evidence.subtitles)} 条字幕，ASR={evidence.transcript_status}"
        )
        return result, status

    except Exception as e:
        import traceback
        traceback.print_exc()
        return "", f"提取失败: {str(e)}"


def clear_all():
    """清空所有内容"""
    global current_video_info
    current_video_info = {}
    return "", "自动检测", "", "", None, None, None, ""


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


# ==================== 深色沉浸主题 CSS ====================

DARK_CSS = """
:root {
  --body-background-fill: #0c0e13;
  --body-text-color: #e7ebf3;
  --body-text-color-subdued: #94a1b5;
  --block-background-fill: #141822;
  --block-background-fill-hover: #1a1f2b;
  --block-border-color: #242b38;
  --block-title-text-color: #f2f5fb;
  --block-label-text-color: #aab4c6;
  --block-label-background-fill: transparent;
  --input-background-fill: #0e1219;
  --input-border-color: #2a3340;
  --input-shadow-focus: 0 0 0 2px rgba(59,130,246,0.35);
  --input-focus-border-color: #3b82f6;
  --button-primary-background-fill: linear-gradient(135deg, #3b82f6 0%, #6366f1 100%);
  --button-primary-background-fill-hover: linear-gradient(135deg, #2f6fe0 0%, #5147e0 100%);
  --button-primary-text-color: #ffffff;
  --button-primary-border-color: transparent;
  --button-secondary-background-fill: #1c2330;
  --button-secondary-background-fill-hover: #232c3b;
  --button-secondary-text-color: #d7deea;
  --button-secondary-border-color: #2f3a4a;
  --button-secondary-border-color-hover: #3b4658;
  --panel-background-fill: #11151e;
  --border-color: #2a3340;
  --block-radius: 16px;
  --container-radius: 16px;
  --input-radius: 10px;
  --button-medium-radius: 10px;
  --button-large-radius: 12px;
  --button-small-radius: 999px;
  --block-shadow: 0 8px 30px rgba(0,0,0,0.35);
  --shadow-drop: 0 8px 30px rgba(0,0,0,0.35);
}

/* 全局背景：深空 + 双径向光晕，营造沉浸感 */
body, .gradio-container {
  background:
    radial-gradient(1200px 600px at 82% -12%, rgba(99,102,241,0.14), transparent 60%),
    radial-gradient(900px 500px at -5% 0%, rgba(59,130,246,0.12), transparent 55%),
    #0c0e13 !important;
}

/* ===== Hero ===== */
.vp-hero {
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 16px;
  padding: 22px 26px;
  border-radius: 18px;
  background: linear-gradient(135deg, rgba(59,130,246,0.16), rgba(99,102,241,0.10));
  border: 1px solid rgba(99,102,241,0.28);
  box-shadow: 0 10px 40px rgba(0,0,0,0.35);
}
.vp-hero-left { display: flex; align-items: center; gap: 16px; }
.vp-logo {
  width: 48px; height: 48px; border-radius: 14px;
  display: flex; align-items: center; justify-content: center;
  font-size: 22px; color: #fff;
  background: linear-gradient(135deg, #3b82f6, #6366f1);
  box-shadow: 0 6px 20px rgba(59,130,246,0.45);
}
.vp-title {
  margin: 0; font-size: 24px; font-weight: 800; letter-spacing: 0.5px;
  background: linear-gradient(90deg, #e7ebf3, #aeb9ff);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.vp-subtitle { margin: 4px 0 0; font-size: 13px; color: #9aa6bd; }
.vp-platforms { display: flex; gap: 8px; flex-wrap: wrap; }
.vp-platform {
  font-size: 12px; padding: 6px 12px; border-radius: 999px;
  background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10);
  color: #cdd6e6;
}

/* ===== 警告 ===== */
.vp-warn {
  padding: 14px 16px; border-radius: 12px; margin-bottom: 16px;
  background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.35);
  color: #f6c860; font-size: 13px; line-height: 1.6;
}

/* ===== 步骤指示器 ===== */
.vp-steps { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 4px 0 18px; }
.vp-step { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #c4cde0; }
.vp-step-num {
  width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 700; color: #fff;
  background: linear-gradient(135deg, #3b82f6, #6366f1);
}
.vp-step-arrow { color: #4a5468; font-size: 14px; }

/* ===== 卡片 ===== */
.vp-card {
  background: rgba(20,24,34,0.85) !important;
  border: 1px solid #242b38 !important;
  border-radius: 16px !important;
  padding: 18px !important;
  box-shadow: 0 8px 30px rgba(0,0,0,0.30);
}

/* ===== 区块标签 ===== */
.vp-section-label { font-size: 13px !important; color: #9aa6bd !important; margin-bottom: 6px !important; }

/* ===== 示例按钮（胶囊） ===== */
.example-btn {
  flex: 1 1 auto;
  border-radius: 999px !important;
  background: #1c2330 !important;
  border: 1px solid #2f3a4a !important;
  color: #d7deea !important;
}
.example-btn:hover { background: #232c3b !important; border-color: #3b4658 !important; }

/* ===== 平台下拉 ===== */
.vp-platform-select select { font-weight: 600; }

/* ===== 状态框（徽章化） ===== */
.status-box textarea {
  font-weight: 600 !important;
  color: #cfe3ff !important;
  background: rgba(59,130,246,0.08) !important;
}

/* ===== 只读信息框 ===== */
.vp-readonly input, .vp-readonly textarea { color: #cdd6e6 !important; }

/* ===== 封面 / 视频 ===== */
.vp-cover { border-radius: 12px; overflow: hidden; }
.vp-video { border-radius: 12px; overflow: hidden; background: #000; }

/* ===== 分割线 ===== */
.vp-divider { height: 1px; background: linear-gradient(90deg, transparent, #2a3340, transparent); margin: 14px 0; }

/* ===== AI 分析按钮 ===== */
.vp-extract { width: 100%; margin-top: 4px; }

/* ===== 报告框（等宽字体） ===== */
.vp-content textarea {
  font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; line-height: 1.7;
  background: #0e1219 !important;
}

/* ===== 说明区 ===== */
.vp-section-title { font-size: 16px; font-weight: 700; color: #e7ebf3; margin: 18px 0 10px; }
.vp-help {
  background: rgba(20,24,34,0.6) !important;
  border: 1px solid #242b38 !important;
  border-radius: 14px !important;
  padding: 16px !important;
}

/* ===== 页脚 ===== */
.vp-footer {
  display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  margin-top: 22px; padding-top: 16px; border-top: 1px solid #1c232f;
  font-size: 12px; color: #6b7689;
}

/* ===== 滚动条 ===== */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: #2a3340; border-radius: 8px; }
::-webkit-scrollbar-track { background: transparent; }

/* ===== 响应式 ===== */
@media (max-width: 720px) {
  .vp-hero { flex-direction: column; align-items: flex-start; }
  .vp-steps { gap: 6px; }
}
"""


def create_app():
    """创建 Gradio 应用"""
    ffmpeg_available = check_ffmpeg()

    with gr.Blocks(
        title="视频解析工作台",
    ) as app:

        # ===== 顶部 Hero =====
        gr.HTML(
            """
            <div class="vp-hero">
              <div class="vp-hero-left">
                <div class="vp-logo">▶</div>
                <div class="vp-hero-text">
                  <h1 class="vp-title">视频解析工作台</h1>
                  <p class="vp-subtitle">多平台解析 · 无水印下载 · AI 时间轴证据分析</p>
                </div>
              </div>
              <div class="vp-platforms">
                <span class="vp-platform">抖音</span>
                <span class="vp-platform">哔哩哔哩</span>
                <span class="vp-platform">小红书</span>
                <span class="vp-platform">快手</span>
                <span class="vp-platform">好看视频</span>
              </div>
            </div>
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

        # ===== 步骤指示器 =====
        gr.HTML(
            """
            <div class="vp-steps">
              <div class="vp-step"><span class="vp-step-num">1</span><span>粘贴链接</span></div>
              <div class="vp-step-arrow">→</div>
              <div class="vp-step"><span class="vp-step-num">2</span><span>解析视频</span></div>
              <div class="vp-step-arrow">→</div>
              <div class="vp-step"><span class="vp-step-num">3</span><span>播放 / 下载</span></div>
              <div class="vp-step-arrow">→</div>
              <div class="vp-step"><span class="vp-step-num">4</span><span>AI 分析</span></div>
            </div>
            """
        )

        with gr.Row(equal_height=False):
            # ---------- 左栏：输入 ----------
            with gr.Column(scale=2, elem_classes=["vp-card"]):
                url_input = gr.Textbox(
                    label="视频链接",
                    placeholder="粘贴抖音 / B站 / 小红书 / 快手 / 好看视频 分享链接…",
                    lines=3,
                    elem_classes=["vp-url"]
                )

                gr.Markdown("**一键示例**（点击自动填充）", elem_classes=["vp-section-label"])
                with gr.Row():
                    douyin_btn = gr.Button("抖音", size="sm", elem_classes=["example-btn"])
                    bilibili_btn = gr.Button("哔哩哔哩", size="sm", elem_classes=["example-btn"])
                    xiaohongshu_btn = gr.Button("小红书", size="sm", elem_classes=["example-btn"])
                with gr.Row():
                    kuaishou_btn = gr.Button("快手", size="sm", elem_classes=["example-btn"])
                    haokan_btn = gr.Button("好看视频", size="sm", elem_classes=["example-btn"])

                platform_dropdown = gr.Dropdown(
                    label="平台选择",
                    choices=["自动检测", "抖音", "哔哩哔哩", "小红书", "快手", "好看视频"],
                    value="自动检测",
                    interactive=True,
                    elem_classes=["vp-platform-select"]
                )

                with gr.Row():
                    parse_btn = gr.Button("解析视频", variant="primary", size="lg")
                    clear_btn = gr.Button("清空", variant="secondary", size="lg")

                # 状态显示
                status_output = gr.Textbox(
                    label="状态",
                    interactive=False,
                    elem_classes=["status-box"]
                )

                # 视频信息
                title_output = gr.Textbox(
                    label="视频标题",
                    interactive=False,
                    elem_classes=["vp-readonly"]
                )

                platform_output = gr.Textbox(
                    label="识别平台",
                    interactive=False,
                    elem_classes=["vp-readonly"]
                )

            # ---------- 右栏：输出 ----------
            with gr.Column(scale=3, elem_classes=["vp-card"]):
                cover_output = gr.Image(
                    label="视频封面",
                    height=300,
                    elem_classes=["vp-cover"]
                )

                # 操作按钮
                with gr.Row():
                    play_btn = gr.Button("在线播放", variant="primary")
                    download_btn = gr.Button("下载视频", variant="secondary")

                # 视频播放器
                video_output = gr.Video(
                    label="视频播放",
                    height=400,
                    elem_classes=["vp-video"]
                )

                # 下载文件
                download_output = gr.File(
                    label="下载文件",
                    visible=True
                )

                # 视频内容提取
                gr.HTML('<div class="vp-divider"></div>')
                extract_btn = gr.Button("AI 时间轴证据分析", variant="secondary", elem_classes=["vp-extract"])
                content_output = gr.Textbox(
                    label="视频证据与分析报告",
                    lines=10,
                    max_lines=20,
                    interactive=False,
                    placeholder="点击「AI 时间轴证据分析」，系统将先建立字幕/音频/画面的时间轴证据，再生成分析报告…",
                    elem_classes=["vp-content"]
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
            outputs=[status_output, title_output, cover_output, video_url_state, platform_output]
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
            inputs=[],
            outputs=[content_output, status_output]
        )

        clear_btn.click(
            fn=clear_all,
            inputs=[],
            outputs=[url_input, platform_dropdown, status_output, title_output,
                    cover_output, video_output, download_output, content_output]
        )

        # 使用说明（卡片化）
        gr.HTML('<div class="vp-section-title">使用说明</div>')
        gr.Markdown(
            """
            **① 粘贴链接**：将视频分享链接粘贴到输入框，或点击上方示例按钮快速填充
            **② 选择平台**：可自动检测或手动选择
            **③ 解析视频**：点击解析获取视频信息（标题、封面、直链）
            **④ 在线播放**：直接播放视频（B 站视频会先下载再合并播放）
            **⑤ 下载视频**：下载视频到本地 `downloads` 目录
            **⑥ AI 证据分析**：先点击在线播放加载视频，再按时间轴对齐字幕 / 可用转写 / 画面，生成可回查的分析报告

            **支持平台**：抖音 · 哔哩哔哩 · 小红书 · 快手 · 好看视频

            **注意事项**
            - 本服务已整合后端 API，单端口（7860）同时提供 Web 与接口
            - B 站视频为音视频分离，需要 ffmpeg 支持
            - 下载的视频保存在 `downloads` 目录，AI 分析需先播放视频（加载到本地缓存）
            """,
            elem_classes=["vp-help"]
        )

        # 页脚
        gr.HTML(
            """
            <div class="vp-footer">
              <span>视频解析工作台 · Video Parser v2.0</span>
              <span>FastAPI + Gradio + Qwen3-VL</span>
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

    # 深色沉浸主题
    app.theme = gr.themes.Base(
        primary_hue=gr.themes.colors.blue,
        secondary_hue=gr.themes.colors.cyan,
        neutral_hue=gr.themes.colors.slate,
        font=["PingFang SC", "Microsoft YaHei", "Inter", "system-ui", "sans-serif"],
    )

    app.css = DARK_CSS
    
    # 挂载 FastAPI 应用到 Gradio
    # 这使得 API 和 Gradio 可以共享同一个端口 (7860)
    combined_app = gr.mount_gradio_app(api_app, app, path="/")
    
    import uvicorn
    print("正在启动服务，请访问 http://localhost:7860")
    uvicorn.run(
        combined_app,
        host="0.0.0.0",
        port=7860,
    )
