#!/usr/bin/env python3
import os
import time
import random
import string
import requests

class VigenereCipher:
    """
    用于 API 认证的加密类
    """
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
        'WX-OPEN-ID': 'Client-Haokan',
        'Content-Type': 'application/json'
    }

def parse_and_download_haokan(video_url):
    """
    调用 app.py 的 API 接口解析并下载好看视频
    """
    api_base = "http://localhost:7860/api"
    
    # 1. 调用 /api/parse 接口解析视频信息
    print(f"正在解析视频链接: {video_url}")
    parse_api = f"{api_base}/parse"
    headers = get_auth_headers()
    payload = {
        "text": video_url
    }
    
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
        real_video_url = video_info.get("video_url")
        video_id = video_info.get("video_id")
        title = video_info.get("title")
        
        print(f"解析成功!")
        print(f"视频标题: {title}")
        print(f"Video ID: {video_id}")
        
        # 2. 调用 /api/download 接口下载视频
        print(f"正在调用下载接口...")
        download_api = f"{api_base}/download"
        download_payload = {
            "video_url": real_video_url,
            "video_id": video_id
        }
        
        # 重新生成认证头（因为时间戳可能已经变化）
        download_headers = get_auth_headers()
        
        download_response = requests.post(download_api, json=download_payload, headers=download_headers)
        if download_response.status_code != 200:
            print(f"下载接口调用失败 (HTTP {download_response.status_code}): {download_response.text}")
            return
            
        download_data = download_response.json()
        if download_data.get("retcode") == 200:
            server_download_url = download_data.get("data", {}).get("download_url")
            print(f"下载接口调用成功!")
            print(f"服务器下载地址: {server_download_url}")
            
            # 3. 将视频从服务器下载到本地
            save_dir = "downloads"
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            
            # 清理标题中的非法字符作为文件名
            safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
            if not safe_title:
                safe_title = video_id
            
            local_filename = f"{safe_title}.mp4"
            local_path = os.path.join(save_dir, local_filename)
            
            print(f"正在下载视频到本地: {local_path}...")
            
            # 下载文件
            file_resp = requests.get(server_download_url, stream=True)
            file_resp.raise_for_status()
            
            total_size = int(file_resp.headers.get('content-length', 0))
            downloaded_size = 0
            
            with open(local_path, 'wb') as f:
                for chunk in file_resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress = (downloaded_size / total_size) * 100
                            print(f"\r下载进度: {progress:.1f}%", end='', flush=True)
            
            print(f"\n视频已成功保存到: {local_path}")
        else:
            print(f"下载失败: {download_data.get('retdesc')}")
            
    except requests.exceptions.ConnectionError:
        print("错误: 无法连接到服务器，请确保 app.py 正在运行 (默认端口 7860)")
    except Exception as e:
        print(f"发生意外错误: {e}")

if __name__ == "__main__":
    # 用户提供的好看视频链接
    target_url = "https://haokan.baidu.com/v?vid=13766973483433940333&tab=recommend"
    parse_and_download_haokan(target_url)
