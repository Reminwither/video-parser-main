#!/usr/bin/env python3
"""
快手视频下载客户端
通过调用服务端API实现视频解析和下载
"""

import os
import time
import random
import string
import requests
from typing import Optional
from urllib.parse import urlparse, urlencode


class VigenereCipher:
    """Vigenère 加密类（与服务端保持一致）"""

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
                    result += '?'
            else:
                result += '?'
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
    """生成随机字符串"""
    characters = string.ascii_letters
    return ''.join(random.choice(characters) for _ in range(length))


class KuaishouVideoClient:
    """快手视频下载客户端"""

    def __init__(self, server_url: str = "http://127.0.0.1:7860"):
        """
        初始化客户端

        Args:
            server_url: 服务端地址，默认为本地 http://127.0.0.1:7860
        """
        self.server_url = server_url.rstrip('/')

    @staticmethod
    def _clean_kuaishou_url(url: str) -> str:
        """
        清理快手 URL，只保留必要的路径

        快手分享链接包含很多不必要的参数，只需要保留视频路径即可。
        """
        parsed = urlparse(url)

        # 只处理快手链接
        if 'kuaishou.com' not in parsed.netloc:
            return url

        # 重新构建 URL，只保留路径，不保留查询参数
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        return clean_url

    def _get_headers(self) -> dict:
        """生成带验证信息的请求头"""
        # 生成时间戳（毫秒）
        timestamp = str(int(time.time() * 1000))

        # 生成随机明文
        plain_text = generate_complex_text()

        # 加密明文
        cipher = VigenereCipher(timestamp)
        encrypted_text = cipher.vigenere_encrypt(plain_text)

        return {
            'Content-Type': 'application/json',
            'WX-OPEN-ID': 'kuaishou-client',
            'X-Timestamp': timestamp,
            'X-GCLT-Text': plain_text,      # 明文
            'X-EGCT-Text': encrypted_text,   # 加密后的文本
        }

    def parse_video(self, video_url: str) -> Optional[dict]:
        """
        解析视频链接，获取视频信息

        Args:
            video_url: 快手视频链接

        Returns:
            解析成功返回视频信息字典，失败返回 None
        """
        api_url = f"{self.server_url}/api/parse"

        # 清理 URL
        clean_url = self._clean_kuaishou_url(video_url)
        payload = {"text": clean_url}

        try:
            print(f"正在解析视频链接...")
            response = requests.post(api_url, json=payload, headers=self._get_headers(), timeout=60)
            result = response.json()

            if result.get('succ'):
                data = result.get('data', {})
                title = data.get('title') or ''
                print(f"解析成功!")
                print(f"  平台: {data.get('platform')}")
                print(f"  标题: {title[:50]}..." if len(title) > 50 else f"  标题: {title}")
                print(f"  视频ID: {data.get('video_id')}")
                print(f"  视频URL: {data.get('video_url')}")
                print(f"  封面URL: {data.get('cover_url')}")
                return data
            else:
                print(f"解析失败: {result.get('retdesc')}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"请求失败: {e}")
            return None

    def get_download_url(self, video_url: str, video_id: str) -> Optional[str]:
        """
        获取视频下载链接

        Args:
            video_url: 视频直链地址
            video_id: 视频ID

        Returns:
            下载链接或 None
        """
        api_url = f"{self.server_url}/api/download"
        payload = {
            "video_url": video_url,
            "video_id": video_id
        }

        try:
            print(f"正在获取下载链接...")
            response = requests.post(api_url, json=payload, headers=self._get_headers(), timeout=60)
            result = response.json()

            if result.get('succ'):
                download_url = result.get('data', {}).get('download_url')
                print(f"下载链接获取成功!")
                return download_url
            else:
                print(f"获取下载链接失败: {result.get('retdesc')}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"请求失败: {e}")
            return None

    def download_file(self, url: str, save_path: str, referer: str = None) -> bool:
        """下载文件到本地"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        }
        if referer:
            headers['Referer'] = referer

        try:
            print(f"正在下载: {os.path.basename(save_path)}")
            response = requests.get(url, headers=headers, stream=True, timeout=180)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress = (downloaded_size / total_size) * 100
                            print(f"\r下载进度: {progress:.1f}%", end='', flush=True)

            print(f"\n下载完成: {os.path.basename(save_path)} ({downloaded_size / 1024 / 1024:.1f} MB)")
            return True

        except requests.exceptions.RequestException as e:
            print(f"下载失败: {e}")
            return False

    def download_from_url(self, kuaishou_url: str, save_path: str = "./downloads") -> bool:
        """
        一键下载快手视频

        Args:
            kuaishou_url: 快手视频链接
            save_path: 保存路径，默认为 ./downloads

        Returns:
            下载成功返回 True，失败返回 False
        """
        # 确保保存目录存在
        os.makedirs(save_path, exist_ok=True)

        # 步骤1: 解析视频
        video_info = self.parse_video(kuaishou_url)
        if not video_info:
            return False

        video_url = video_info.get('video_url')
        video_id = video_info.get('video_id')
        title = video_info.get('title', 'kuaishou_video')
        cover_url = video_info.get('cover_url')

        if not video_url:
            print("解析结果缺少视频链接")
            return False

        # 清理文件名中的非法字符
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.', '，', '。', '！', '？')).strip()
        if not safe_title:
            safe_title = video_id or "kuaishou_video"
        safe_title = safe_title[:50]

        # 步骤2: 下载封面
        if cover_url:
            print(f"\n[1/2] 下载封面...")
            cover_path = os.path.join(save_path, f"{safe_title}_cover.jpg")
            self.download_file(cover_url, cover_path, referer='https://www.kuaishou.com/')

        # 步骤3: 尝试获取下载链接，如果失败则直接使用video_url
        print(f"\n[2/2] 下载视频...")
        download_url = video_url

        # 尝试通过download API获取链接（可选）
        if video_id:
            api_download_url = self.get_download_url(video_url, video_id)
            if api_download_url:
                download_url = api_download_url

        # 下载视频
        video_path = os.path.join(save_path, f"{safe_title}.mp4")
        return self.download_file(download_url, video_path, referer='https://www.kuaishou.com/')


def main():
    """主函数"""
    # 快手视频链接
    kuaishou_url = "https://www.kuaishou.com/short-video/3x8zha3ipq6bg8q?authorId=3xcyp2v85enrv7w&streamSource=find&area=homexxbrilliant"

    # 创建客户端实例
    client = KuaishouVideoClient(server_url="http://127.0.0.1:7860")

    print("=" * 60)
    print("快手视频下载客户端 (API版)")
    print("=" * 60)

    # 一键下载
    print(f"\n正在解析: {kuaishou_url[:60]}...")
    success = client.download_from_url(kuaishou_url, save_path="./downloads")

    if success:
        print("\n" + "=" * 60)
        print("视频下载成功!")
        print("=" * 60)

        # 显示下载目录内容
        print(f"\n下载目录内容:")
        for f in os.listdir("./downloads"):
            fpath = os.path.join("./downloads", f)
            if os.path.isfile(fpath):
                size = os.path.getsize(fpath) / 1024 / 1024
                print(f"  {f} ({size:.1f} MB)")
    else:
        print("\n" + "=" * 60)
        print("视频下载失败!")
        print("=" * 60)


if __name__ == "__main__":
    main()
