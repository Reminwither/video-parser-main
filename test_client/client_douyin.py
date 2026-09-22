#!/usr/bin/env python3
"""
抖音视频下载客户端示例
调用服务端API实现视频解析和下载
"""

import os
import time
import random
import string
import requests
from typing import Optional


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


class DouyinVideoClient:
    """抖音视频下载客户端"""

    def __init__(self, server_url: str = "http://127.0.0.1:7860"):
        """
        初始化客户端

        Args:
            server_url: 服务端地址，默认为本地 http://127.0.0.1:7860
        """
        self.server_url = server_url.rstrip('/')

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
            'WX-OPEN-ID': 'client-demo',
            'X-Timestamp': timestamp,
            'X-GCLT-Text': plain_text,      # 明文
            'X-EGCT-Text': encrypted_text,   # 加密后的文本
        }

    def parse_video(self, video_url: str) -> Optional[dict]:
        """
        解析视频链接，获取视频信息

        Args:
            video_url: 抖音视频链接

        Returns:
            解析成功返回视频信息字典，失败返回 None
        """
        api_url = f"{self.server_url}/api/parse"
        payload = {"text": video_url}

        try:
            print(f"正在解析视频链接: {video_url}")
            response = requests.post(api_url, json=payload, headers=self._get_headers(), timeout=30)
            result = response.json()

            if result.get('succ'):
                data = result.get('data', {})
                print(f"解析成功!")
                print(f"  平台: {data.get('platform')}")
                print(f"  标题: {data.get('title')}")
                print(f"  视频ID: {data.get('video_id')}")
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
            response = requests.post(api_url, json=payload, headers=self._get_headers(), timeout=30)
            result = response.json()

            if result.get('succ'):
                download_url = result.get('data', {}).get('download_url')
                print(f"下载链接: {download_url}")
                return download_url
            else:
                print(f"获取下载链接失败: {result.get('retdesc')}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"请求失败: {e}")
            return None

    def download_video(self, download_url: str, save_path: str = None, filename: str = None) -> bool:
        """
        下载视频到本地

        Args:
            download_url: 视频下载链接
            save_path: 保存路径，默认为当前目录
            filename: 文件名，默认为 video.mp4

        Returns:
            下载成功返回 True，失败返回 False
        """
        if save_path is None:
            save_path = os.getcwd()
        if filename is None:
            filename = "video.mp4"

        # 确保保存目录存在
        os.makedirs(save_path, exist_ok=True)
        filepath = os.path.join(save_path, filename)

        try:
            print(f"正在下载视频到: {filepath}")
            response = requests.get(download_url, stream=True, timeout=60)
            response.raise_for_status()

            # 获取文件大小
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress = (downloaded_size / total_size) * 100
                            print(f"\r下载进度: {progress:.1f}%", end='', flush=True)

            print(f"\n下载完成! 文件保存至: {filepath}")
            return True

        except requests.exceptions.RequestException as e:
            print(f"下载失败: {e}")
            return False

    def download_from_url(self, douyin_url: str, save_path: str = None) -> bool:
        """
        一键下载抖音视频

        Args:
            douyin_url: 抖音视频链接
            save_path: 保存路径，默认为当前目录

        Returns:
            下载成功返回 True，失败返回 False
        """
        # 步骤1: 解析视频
        video_info = self.parse_video(douyin_url)
        if not video_info:
            return False

        video_url = video_info.get('video_url')
        video_id = video_info.get('video_id')
        title = video_info.get('title', 'video')

        if not video_url or not video_id:
            print("解析结果缺少必要信息")
            return False

        # 步骤2: 获取下载链接
        download_url = self.get_download_url(video_url, video_id)
        if not download_url:
            # 如果获取下载链接失败，尝试直接使用 video_url
            print("尝试直接使用视频直链下载...")
            download_url = video_url

        # 步骤3: 下载视频
        # 清理文件名中的非法字符
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).strip()
        if not safe_title:
            safe_title = video_id
        filename = f"{safe_title[:50]}.mp4"

        return self.download_video(download_url, save_path, filename)


def main():
    """主函数 - 演示如何使用客户端"""

    # 抖音视频链接
    douyin_url = "https://www.douyin.com/note/7580598241298069157"
    #douyin_url = "https://www.douyin.com/jingxuan/animal?modal_id=7577705429694680320"

    # 创建客户端实例
    client = DouyinVideoClient(server_url="http://127.0.0.1:7860")

    print("=" * 60)
    print("抖音视频下载客户端")
    print("=" * 60)

    # 方式1: 一键下载
    print("\n[方式1] 一键下载视频:")
    print("-" * 40)
    success = client.download_from_url(douyin_url, save_path="./downloads")

    if success:
        print("\n视频下载成功!")
    else:
        print("\n视频下载失败!")

    # 方式2: 分步操作（如果你只需要获取视频信息而不下载）
    print("\n" + "=" * 60)
    print("[方式2] 分步操作示例:")
    print("-" * 40)

    # 仅解析视频信息
    video_info = client.parse_video(douyin_url)
    if video_info:
        print(f"\n视频信息:")
        print(f"  视频ID: {video_info.get('video_id')}")
        print(f"  平台: {video_info.get('platform')}")
        print(f"  标题: {video_info.get('title')}")
        print(f"  视频直链: {video_info.get('video_url')}")
        print(f"  封面图: {video_info.get('cover_url')}")


if __name__ == "__main__":
    main()
