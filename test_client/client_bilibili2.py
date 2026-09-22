#!/usr/bin/env python3
"""
B站视频下载客户端
通过调用 app.py 的 API 接口实现视频解析和下载
"""
import os
import time
import random
import string
import subprocess
import requests


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


def get_auth_headers():
    """生成带有认证信息的请求头"""
    timestamp = str(int(time.time() * 1000))
    original_text = generate_complex_text()
    cipher = VigenereCipher(timestamp)
    encrypted_text = cipher.vigenere_encrypt(original_text)

    return {
        'X-Timestamp': timestamp,
        'X-GCLT-Text': original_text,
        'X-EGCT-Text': encrypted_text,
        'WX-OPEN-ID': 'Client-Bilibili',
        'Content-Type': 'application/json'
    }


def download_file(url: str, save_path: str, referer: str = None) -> bool:
    """下载文件到本地，带进度显示"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    }
    if referer:
        headers['Referer'] = referer

    try:
        print(f"正在下载: {os.path.basename(save_path)}")
        response = requests.get(url, headers=headers, stream=True, timeout=120)
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


def merge_video_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    """使用ffmpeg合并视频和音频"""
    try:
        print(f"正在合并音视频...")
        cmd = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-i', audio_path,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-strict', 'experimental',
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"合并完成: {os.path.basename(output_path)}")
            # 删除临时文件
            os.remove(video_path)
            os.remove(audio_path)
            return True
        else:
            print(f"合并失败: {result.stderr}")
            return False
    except FileNotFoundError:
        print("警告: 未安装ffmpeg，无法合并音视频")
        print("请安装ffmpeg: sudo apt install ffmpeg 或 brew install ffmpeg")
        return False


def parse_and_download_bilibili(video_url: str):
    """调用 app.py 的 API 接口解析并下载B站视频"""
    api_base = "http://localhost:7860/api"
    save_dir = "downloads"

    print("=" * 60)
    print("B站视频下载客户端 (API版)")
    print("=" * 60)

    # 1. 调用 /api/parse 接口解析视频信息
    print(f"\n正在解析视频链接: {video_url}")
    parse_api = f"{api_base}/parse"
    headers = get_auth_headers()
    payload = {"text": video_url}

    try:
        response = requests.post(parse_api, json=payload, headers=headers)
        if response.status_code != 200:
            print(f"解析失败 (HTTP {response.status_code}): {response.text}")
            return

        parse_data = response.json()
        if parse_data.get("retcode") != 200:
            print(f"解析失败: {parse_data.get('retdesc')}")
            return

        video_info = parse_data.get("data")
        video_id = video_info.get("video_id")
        title = video_info.get("title")
        platform = video_info.get("platform")
        video_stream_url = video_info.get("video_url")
        audio_stream_url = video_info.get("audio_url")
        cover_url = video_info.get("cover_url")

        print(f"\n解析成功!")
        print(f"平台: {platform}")
        print(f"视频ID: {video_id}")
        print(f"标题: {title}")
        print(f"封面: {cover_url}")
        print(f"视频流: {video_stream_url[:80]}..." if video_stream_url else "视频流: 无")
        print(f"音频流: {audio_stream_url[:80]}..." if audio_stream_url else "音频流: 无")

        # 创建下载目录
        os.makedirs(save_dir, exist_ok=True)

        # 清理标题中的非法字符作为文件名
        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
        if not safe_title:
            safe_title = video_id
        if len(safe_title) > 50:
            safe_title = safe_title[:50]

        # 2. 下载封面
        print(f"\n[1/3] 下载封面...")
        cover_path = os.path.join(save_dir, f"{safe_title}_cover.jpg")
        download_file(cover_url, cover_path, referer=video_url)

        # 3. 下载视频轨道
        print(f"\n[2/3] 下载视频轨道...")
        video_temp_path = os.path.join(save_dir, f"{safe_title}_video.m4s")
        if not download_file(video_stream_url, video_temp_path, referer=video_url):
            print("视频轨道下载失败")
            return

        # 4. 下载音频轨道
        if audio_stream_url:
            print(f"\n[3/3] 下载音频轨道...")
            audio_temp_path = os.path.join(save_dir, f"{safe_title}_audio.m4s")
            if not download_file(audio_stream_url, audio_temp_path, referer=video_url):
                print("音频轨道下载失败")
                return

            # 5. 合并音视频
            print(f"\n合并音视频...")
            output_path = os.path.join(save_dir, f"{safe_title}.mp4")
            if merge_video_audio(video_temp_path, audio_temp_path, output_path):
                print(f"\n下载完成!")
                print(f"保存路径: {output_path}")
            else:
                print(f"\n音视频未合并，文件保存为:")
                print(f"  视频: {video_temp_path}")
                print(f"  音频: {audio_temp_path}")
        else:
            # 没有音频的情况，直接重命名视频文件
            output_path = os.path.join(save_dir, f"{safe_title}.mp4")
            os.rename(video_temp_path, output_path)
            print(f"\n下载完成!")
            print(f"保存路径: {output_path}")

        # 显示下载目录内容
        print(f"\n下载目录内容:")
        for f in os.listdir(save_dir):
            fpath = os.path.join(save_dir, f)
            if os.path.isfile(fpath):
                size = os.path.getsize(fpath) / 1024 / 1024
                print(f"  {f} ({size:.1f} MB)")

    except requests.exceptions.ConnectionError:
        print("错误: 无法连接到服务器，请确保 app.py 正在运行 (默认端口 7860)")
    except Exception as e:
        print(f"发生意外错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # B站视频链接
    target_url = "https://www.bilibili.com/video/BV1TaqYBcEJc"
    parse_and_download_bilibili(target_url)
