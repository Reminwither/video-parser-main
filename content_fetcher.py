#!/usr/bin/env python3
"""
通用链接内容抓取路由器

根据 URL 域名自动路由到不同处理器：
- 视频平台 (抖音/B站/小红书/快手/好看视频) -> 复用现有解析管线，下载视频到本地
- 微信公众号文章 (mp.weixin.qq.com) -> requests + BeautifulSoup 提取正文
- 通用网页 -> readability-lxml 提取正文
"""

import os
import time
import random
import subprocess
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import urlparse

from configs.logging_config import logger
from configs.general_constants import DOMAIN_TO_NAME, USER_AGENT_PC, USER_AGENT_M
from utils.web_fetcher import WebFetcher, UrlParser
from src.downloader_factory import DownloaderFactory

load_dotenv()

# 视频缓存目录（复用项目现有 cache 目录）
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


def fetch_content(url: str) -> dict | None:
    """
    主入口：根据 URL 类型路由到对应处理器。

    Returns:
        {"type": "video", "video_path": "...", "title": "...", "platform": "..."}
        {"type": "text", "title": "...", "content": "...", "url": "..."}
        None 如果无法处理
    """
    # 1) 先尝试视频平台路由（复用现有短链重定向逻辑）
    redirect_url = WebFetcher.fetch_redirect_url(url)
    if redirect_url:
        domain = UrlParser.get_domain(redirect_url)
        if DOMAIN_TO_NAME.get(domain):
            return _fetch_video(url, redirect_url)

    # 2) 非视频 URL，直接获取页面内容
    domain = UrlParser.get_domain(url)

    if 'mp.weixin.qq.com' in domain:
        return _fetch_wechat_article(url)

    return _fetch_general_webpage(url)


# ==================== 视频平台 ====================

def _fetch_video(original_url: str, redirect_url: str) -> dict | None:
    """
    解析视频链接并下载到本地文件。
    复用 api.py 的解析逻辑，但跳过 Vigenere 认证。
    """
    domain = UrlParser.get_domain(redirect_url)
    platform = DOMAIN_TO_NAME.get(domain)
    if not platform:
        return None

    real_url = UrlParser.extract_video_address(redirect_url)
    video_id = UrlParser.get_video_id(redirect_url)
    logger.info(f"[content_fetcher] video: platform={platform}, video_id={video_id}")

    try:
        # 小红书需要重试
        if platform == '小红书':
            max_attempts = 5
            for attempt in range(max_attempts):
                downloader = DownloaderFactory.create_downloader(platform, real_url)
                title = downloader.get_title_content()
                video_url = downloader.get_real_video_url()
                cover_url = downloader.get_cover_photo_url()
                if video_url:
                    break
                logger.debug(f"[content_fetcher] xhs attempt {attempt + 1} failed")
            else:
                logger.error("[content_fetcher] xhs: failed after 5 attempts")
                raise RuntimeError("小红书解析连续 5 次未返回视频地址")
        else:
            downloader = DownloaderFactory.create_downloader(platform, real_url)
            title = downloader.get_title_content()
            if (platform == '抖音' and
                    os.getenv('FEISHU_VIDEO_MODE', 'analysis').strip().lower() == 'transcript'):
                video_url = downloader.get_transcription_video_url()
            else:
                video_url = downloader.get_real_video_url()
            cover_url = downloader.get_cover_photo_url()

        metadata = downloader.get_metadata() if hasattr(downloader, "get_metadata") else {
            "video_id": video_id,
            "platform": platform,
            "title": title,
            "video_url": video_url,
            "cover_url": cover_url,
        }

        video_url = UrlParser.convert_to_https(video_url)
        audio_url = None
        if platform in {'哔哩哔哩', 'YouTube'} and hasattr(downloader, 'get_audio_url'):
            audio_url = UrlParser.convert_to_https(downloader.get_audio_url())

        if not video_url:
            logger.error(f"[content_fetcher] no video_url for {platform}")
            raise RuntimeError(f"{platform} 解析成功但未返回视频地址")

        # 长抖音视频的原声 MP3 有时就是完整音轨；抽样比对后可直接转写，省去视频下载。
        local_path = None
        if (platform == '抖音' and
                os.getenv('FEISHU_VIDEO_MODE', 'analysis').strip().lower() == 'transcript'):
            local_path = _try_verified_douyin_audio(downloader, video_url, video_id or 'unknown')

        # 没有匹配的独立音轨时下载完整视频
        if local_path:
            pass
        elif platform == 'YouTube' and hasattr(downloader, 'download_to'):
            safe_id = "".join(c for c in str(video_id or 'youtube') if c.isalnum())[:30] or "youtube"
            local_path = downloader.download_to(_CACHE_DIR, safe_id, int(time.time()))
        else:
            local_path = _download_video_file(
                video_url, audio_url, platform, original_url, video_id or 'unknown'
            )
        if not local_path:
            raise RuntimeError(f"{platform} 视频下载失败")

        return {
            "type": "video",
            "video_path": local_path,
            "title": title or '无标题',
            "platform": platform,
            "url": original_url,
            "metadata": metadata,
        }

    except Exception as e:
        logger.error(f"[content_fetcher] video fetch error: {e}")
        import traceback
        traceback.print_exc()
        raise RuntimeError(f"{platform} 内容获取失败: {e}") from e


def _try_verified_douyin_audio(downloader, video_url: str, video_id: str) -> str | None:
    """仅当独立音频与视频音轨吻合时使用它，避免转写成背景音乐。"""
    detail = (downloader.data or {}).get('aweme_detail') or {}
    video = detail.get('video') or {}
    music = detail.get('music') or {}
    music_url = downloader.get_audio_url()
    try:
        duration = float(video.get('duration') or 0) / 1000
        music_duration = float(music.get('duration') or 0)
    except (TypeError, ValueError):
        return None
    if not music_url or duration < 300 or abs(duration - music_duration) > 3:
        return None

    try:
        import numpy as np

        def sample_pcm(source: str, offset: float) -> bytes:
            result = subprocess.run(
                [os.getenv('FFMPEG_PATH', 'ffmpeg'), '-nostdin', '-v', 'error',
                 '-ss', f'{offset:.1f}', '-i', source, '-t', '6', '-vn',
                 '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1'],
                capture_output=True, timeout=20,
            )
            return result.stdout if result.returncode == 0 else b''

        def alignment(left: bytes, right: bytes) -> float:
            x = np.frombuffer(left, dtype=np.int16).astype(np.float32)
            y = np.frombuffer(right, dtype=np.int16).astype(np.float32)
            if len(x) < 16000 or len(y) < 16000:
                return 0.0
            x -= x.mean()
            y -= y.mean()
            denominator = float(np.linalg.norm(x) * np.linalg.norm(y))
            if denominator == 0:
                return 0.0
            fft_length = 1 << (len(x) + len(y) - 1).bit_length()
            cross = np.fft.irfft(
                np.fft.rfft(x, fft_length) * np.conj(np.fft.rfft(y, fft_length)),
                fft_length,
            )
            return float(np.max(np.abs(cross)) / denominator)

        for offset in (duration * 0.5,):
            if alignment(sample_pcm(video_url, offset), sample_pcm(music_url, offset)) < 0.75:
                logger.info('[content_fetcher] Douyin audio does not match video; using video')
                return None
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as exc:
        logger.warning(f'[content_fetcher] Douyin audio probe unavailable: {type(exc).__name__}')
        return None

    safe_id = ''.join(c for c in str(video_id) if c.isalnum())[:30] or 'video'
    audio_path = os.path.join(_CACHE_DIR, f'feishu_{safe_id}_{int(time.time())}.mp3')
    try:
        _download_file(music_url, {'Referer': 'https://www.douyin.com/'}, audio_path)
        probe = subprocess.run(
            [os.getenv('FFPROBE_PATH', 'ffprobe'), '-v', 'error',
             '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', audio_path],
            capture_output=True, text=True, timeout=10,
        )
        if probe.returncode != 0 or abs(float(probe.stdout.strip()) - duration) > 3:
            raise RuntimeError('独立音频时长与视频不一致')
        logger.info(f'[content_fetcher] verified Douyin audio downloaded: {os.path.getsize(audio_path) / 1048576:.1f} MB')
        return audio_path
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning(f'[content_fetcher] Douyin audio fallback: {type(exc).__name__}')
        if os.path.isfile(audio_path):
            os.remove(audio_path)
        return None


def _download_video_file(video_url: str, audio_url: str | None,
                          platform: str, original_url: str,
                          video_id: str) -> str | None:
    """下载视频（及 B 站音频）到本地缓存目录。"""
    headers = {
        'User-Agent': random.choice(USER_AGENT_PC)
    }
    # 设置 Referer
    if 'douyin.com' in original_url or 'iesdouyin.com' in original_url:
        headers['Referer'] = 'https://www.douyin.com/'
    elif 'bilibili.com' in original_url:
        headers['Referer'] = original_url
    elif 'xiaohongshu.com' in original_url:
        headers['Referer'] = 'https://www.xiaohongshu.com/'
    elif 'kuaishou.com' in original_url:
        headers['Referer'] = 'https://www.kuaishou.com/'

    safe_id = "".join(c for c in str(video_id) if c.isalnum())[:30] or "video"
    timestamp = int(time.time())
    video_path = os.path.join(_CACHE_DIR, f"feishu_{safe_id}_{timestamp}.mp4")

    try:
        if audio_url and platform == '哔哩哔哩':
            # B站：分别下载视频轨和音频轨，再合并
            video_temp = os.path.join(_CACHE_DIR, f"feishu_{safe_id}_{timestamp}_v.m4s")
            audio_temp = os.path.join(_CACHE_DIR, f"feishu_{safe_id}_{timestamp}_a.m4s")

            _download_file(video_url, headers, video_temp)
            _download_file(audio_url, headers, audio_temp)

            # 合并
            ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
            cmd = [ffmpeg_path, '-y', '-i', video_temp, '-i', audio_temp,
                   '-c:v', 'copy', '-c:a', 'aac', '-strict', 'experimental',
                   video_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            # 清理临时文件
            for f in [video_temp, audio_temp]:
                if os.path.exists(f):
                    os.remove(f)
            if result.returncode != 0:
                logger.error(f"[content_fetcher] ffmpeg merge failed: {result.stderr[:200]}")
                return None
        else:
            # 其他平台：直接下载
            _download_file(video_url, headers, video_path)

        size_mb = os.path.getsize(video_path) / 1024 / 1024
        logger.info(f"[content_fetcher] video downloaded: {video_path} ({size_mb:.1f} MB)")
        return video_path

    except Exception as e:
        logger.error(f"[content_fetcher] download error: {e}")
        if os.path.exists(video_path):
            os.remove(video_path)
        return None


def _download_file(url: str, headers: dict, filepath: str):
    """下载单个文件；对 CDN 断流做有限重试，完整后再原子替换目标文件。"""
    temp_path = filepath + ".part"
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, stream=True, timeout=(10, 60))
            response.raise_for_status()
            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
            os.replace(temp_path, filepath)
            return
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            logger.warning(f"[content_fetcher] download attempt {attempt + 1}/3 failed: {exc}")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"资源下载连续 3 次失败: {last_error}")


# ==================== 微信公众号文章 ====================

def _fetch_wechat_article(url: str) -> dict | None:
    """抓取微信公众号文章正文。"""
    headers = {
        'User-Agent': random.choice(USER_AGENT_M)  # 公众号用移动端 UA 效果更好
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = response.apparent_encoding or 'utf-8'
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'lxml')

        # 公众号标题
        title_tag = soup.find('h1', id='activity-name') or soup.find('h1', class_='rich_media_title')
        title = title_tag.get_text(strip=True) if title_tag else '无标题'

        # 公众号名称
        author_tag = soup.find('a', id='js_name') or soup.find('span', id='js_author_name')
        author = author_tag.get_text(strip=True) if author_tag else ''

        # 正文
        content_div = soup.find('div', id='js_content')
        if not content_div:
            # 降级：取整个 body
            content_div = soup.find('body') or soup

        # 清理：移除 script/style 标签
        for tag in content_div.find_all(['script', 'style', 'noscript']):
            tag.decompose()

        text = content_div.get_text(separator='\n', strip=True)
        # 合并多余空行
        text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())

        if not text or len(text) < 50:
            logger.warning(f"[content_fetcher] wechat article too short: {len(text)} chars")
            return None

        full_title = f"{title}"
        if author:
            full_title += f"（{author}）"

        logger.info(f"[content_fetcher] wechat article: title={title}, len={len(text)}")
        return {
            "type": "text",
            "title": full_title,
            "content": text,
            "url": url,
        }

    except Exception as e:
        logger.error(f"[content_fetcher] wechat fetch error: {e}")
        return None


# ==================== 通用网页 ====================

def _fetch_general_webpage(url: str) -> dict | None:
    """使用 readability-lxml 提取通用网页正文。"""
    headers = {
        'User-Agent': random.choice(USER_AGENT_PC)
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = response.apparent_encoding or 'utf-8'
        response.raise_for_status()

        html = response.text

        # 尝试使用 readability 提取正文
        try:
            from readability import Document
            doc = Document(html)
            title = doc.short_title() or '无标题'
            content_html = doc.summary()

            # 从 HTML 提取纯文本
            soup = BeautifulSoup(content_html, 'lxml')
            text = soup.get_text(separator='\n', strip=True)
            text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())
        except ImportError:
            # readability 未安装时降级为简单提取
            logger.warning("[content_fetcher] readability-lxml not installed, using fallback")
            soup = BeautifulSoup(html, 'lxml')
            title_tag = soup.find('title')
            title = title_tag.get_text(strip=True) if title_tag else '无标题'
            # 移除不需要的标签
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form']):
                tag.decompose()
            # 尝试取最长的文本块
            paragraphs = soup.find_all('p')
            if paragraphs:
                text = '\n'.join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
            else:
                text = soup.get_text(separator='\n', strip=True)

        if not text or len(text) < 50:
            logger.warning(f"[content_fetcher] webpage content too short: {len(text)} chars")
            # 降级：返回原始 HTML 的纯文本
            soup = BeautifulSoup(html, 'lxml')
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                tag.decompose()
            text = soup.get_text(separator='\n', strip=True)
            text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())
            if len(text) < 50:
                return None
            title = title + "（降级提取）"

        logger.info(f"[content_fetcher] webpage: title={title}, len={len(text)}")
        return {
            "type": "text",
            "title": title,
            "content": text,
            "url": url,
        }

    except Exception as e:
        logger.error(f"[content_fetcher] webpage fetch error: {e}")
        return None
