#!/usr/bin/env python3
"""
端到端测试：抖音链接 → 解析 → 下载 → 时间轴证据分析
通过 REST API 调用运行中的 video-parser 服务 (localhost:7860)
视觉分析使用本地 Ollama qwen2.5vl:7b；ASR 未配置时验证诚实降级
"""
import os
import sys
import time
import random
import string
import json
import requests

# ==================== 认证相关（复制自项目逻辑）====================

def generate_complex_text(length=32):
    characters = string.ascii_letters
    return ''.join(random.choice(characters) for _ in range(length))

class VigenereCipher:
    def __init__(self, timestamp):
        self.key = self.timestamp_to_letters(timestamp)

    @staticmethod
    def timestamp_to_letters(timestamp):
        digits_to_letters = 'abcdefghijklmnopqrstuvwxyz'
        result = ''
        for char in str(timestamp):
            if char.isdigit():
                index = int(char)
                result += digits_to_letters[index] if 0 <= index < len(digits_to_letters) else 'a'
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

def get_auth_headers():
    timestamp = str(int(time.time() * 1000))
    original_text = generate_complex_text()
    cipher = VigenereCipher(timestamp)
    encrypted_text = cipher.vigenere_encrypt(original_text)
    return {
        'X-Timestamp': timestamp,
        'X-GCLT-Text': original_text,
        'X-EGCT-Text': encrypted_text,
        'WX-OPEN-ID': 'test-client',
        'Content-Type': 'application/json'
    }

# ==================== 测试流程 ====================

BASE_URL = "http://localhost:7860"
DOUYIN_URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.douyin.com/note/7580598241298069157"

def step1_health_check():
    print("\n" + "=" * 60)
    print("第 1 步：健康检查")
    print("=" * 60)
    resp = requests.get(f"{BASE_URL}/health", timeout=10)
    print(f"  GET /health → {resp.status_code}")
    print(f"  响应: {resp.json()}")
    assert resp.status_code == 200, "健康检查失败"
    print("  ✅ 服务在线")
    return True

def step2_parse():
    print("\n" + "=" * 60)
    print("第 2 步：解析抖音链接")
    print("=" * 60)
    print(f"  URL: {DOUYIN_URL}")

    headers = get_auth_headers()
    payload = {"text": DOUYIN_URL}

    print(f"  认证头: X-Timestamp={headers['X-Timestamp'][:10]}...")
    print("  发送解析请求...")

    resp = requests.post(f"{BASE_URL}/api/parse", json=payload, headers=headers, timeout=60)
    print(f"  POST /api/parse → {resp.status_code}")

    result = resp.json()
    print(f"  retcode: {result.get('retcode')}")
    print(f"  retdesc: {result.get('retdesc')}")

    if result.get('succ') or result.get('retcode') == 200:
        data = result.get('data', {})
        print(f"  标题: {data.get('title', 'N/A')}")
        print(f"  平台: {data.get('platform', 'N/A')}")
        print(f"  video_id: {data.get('video_id', 'N/A')}")
        print(f"  video_url: {data.get('video_url', 'N/A')[:80]}...")
        print(f"  cover_url: {data.get('cover_url', 'N/A')[:80]}...")
        print("  ✅ 解析成功")
        return data
    else:
        print(f"  ❌ 解析失败: {result.get('retdesc')}")
        print(f"  完整响应: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return None

def step3_download(parsed_data):
    print("\n" + "=" * 60)
    print("第 3 步：获取下载链接并下载视频")
    print("=" * 60)

    video_url = parsed_data.get('video_url', '')
    video_id = parsed_data.get('video_id', 'unknown')

    if not video_url:
        print("  ❌ 无视频直链")
        return None

    # 调用 /api/download 获取可访问的下载链接
    headers = get_auth_headers()
    payload = {"video_url": video_url, "video_id": video_id}

    print(f"  请求下载链接 (video_id={video_id})...")
    resp = requests.post(f"{BASE_URL}/api/download", json=payload, headers=headers, timeout=60)
    result = resp.json()
    print(f"  POST /api/download → {resp.status_code}")

    download_url = result.get('data', {}).get('download_url', '')
    if not download_url:
        # 直接用解析得到的 video_url
        print("  API 未返回 download_url，直接使用解析的 video_url")
        download_url = video_url

    print(f"  下载链接: {download_url[:80]}...")

    # 下载视频文件
    download_dir = os.path.join(os.path.dirname(__file__), "downloads")
    os.makedirs(download_dir, exist_ok=True)
    safe_id = "".join(c for c in str(video_id) if c.isalnum())[:30] or "video"
    video_path = os.path.join(download_dir, f"{safe_id}_test.mp4")

    print(f"  开始下载到: {video_path}")
    dl_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    }
    if 'douyin.com' in DOUYIN_URL:
        dl_headers['Referer'] = 'https://www.douyin.com/'

    resp = requests.get(download_url, headers=dl_headers, timeout=120, stream=True)
    print(f"  下载响应: {resp.status_code}, Content-Type: {resp.headers.get('content-type', 'N/A')}")

    if resp.status_code == 200:
        total = 0
        with open(video_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                total += len(chunk)
        size_mb = total / (1024 * 1024)
        print(f"  ✅ 下载完成: {size_mb:.2f} MB")
        return video_path
    else:
        print(f"  ❌ 下载失败: HTTP {resp.status_code}")
        print(f"  响应头: {dict(resp.headers)}")
        return None

def step4_inspect_video(video_path):
    print("\n" + "=" * 60)
    print("第 4 步：检查视频媒体与证据通道")
    print("=" * 60)
    from video_analysis import inspect_video
    media = inspect_video(video_path)
    print(f"  时长: {media.duration:.1f} 秒")
    print(f"  画面: {media.width}x{media.height} @ {media.fps:.2f} FPS")
    print(f"  音轨: {media.audio_tracks}；字幕轨: {len(media.subtitle_tracks)}")
    print("  ✅ 媒体检查完成")
    return media

def step5_ai_extract(video_path, title=None):
    print("\n" + "=" * 60)
    print("第 5 步：时间轴证据分析（本地 Ollama qwen2.5vl:7b）")
    print("=" * 60)

    from openai import OpenAI
    from video_analysis import analyze_video_evidence_first

    base_url = os.getenv('QWEN_API_BASE_URL', 'http://localhost:11434/v1')
    api_key = os.getenv('QWEN_API_KEY', 'ollama')
    model_id = os.getenv('QWEN_MODEL_ID', 'qwen2.5vl:7b')

    print(f"  模型: {model_id}")
    print(f"  接口: {base_url}")
    print("  正在建立字幕/可选转写/视觉证据时间轴（首次可能需要 60s+）...")
    start = time.time()

    client = OpenAI(base_url=base_url, api_key=api_key)
    result, evidence = analyze_video_evidence_first(
        video_path,
        client,
        model_id,
        title=title,
        max_frames=int(os.getenv('MAX_ANALYSIS_FRAMES', '24')),
        progress=lambda value, message: print(f"  [{value:.0%}] {message}"),
    )

    elapsed = time.time() - start

    print(f"  AI 耗时: {elapsed:.1f} 秒")
    print("\n" + "-" * 60)
    print("时间轴证据分析结果:")
    print("-" * 60)
    print(result)
    print("-" * 60)
    print("  ✅ 证据优先分析完成")
    return result, evidence

# ==================== 主流程 ====================

if __name__ == "__main__":
    # 加载 .env
    from dotenv import load_dotenv
    load_dotenv()

    print("=" * 60)
    print("  抖音链接端到端测试")
    print("  目标: 完整跑通 解析 → 下载 → 时间轴证据分析")
    print("=" * 60)

    # 加载环境变量
    os.environ.setdefault('QWEN_API_BASE_URL', 'http://localhost:11434/v1')
    os.environ.setdefault('QWEN_API_KEY', 'ollama')
    os.environ.setdefault('QWEN_MODEL_ID', 'qwen2.5vl:7b')
    os.environ.setdefault('MAX_ANALYSIS_FRAMES', '12')

    try:
        step1_health_check()
        parsed = step2_parse()
        if not parsed:
            print("\n❌ 流程中断：解析失败")
            sys.exit(1)

        video_path = step3_download(parsed)
        if not video_path:
            print("\n❌ 流程中断：下载失败")
            sys.exit(1)

        step4_inspect_video(video_path)
        result, evidence = step5_ai_extract(video_path, parsed.get('title'))

        print("\n" + "=" * 60)
        print("  ✅ 全流程跑通！")
        print("=" * 60)
        print(f"  抖音链接: {DOUYIN_URL}")
        print(f"  视频标题: {parsed.get('title', 'N/A')}")
        print(f"  下载大小: {os.path.getsize(video_path)/(1024*1024):.2f} MB")
        print(f"  时间轴帧数: {len(evidence.frames)}")
        print(f"  字幕条数: {len(evidence.subtitles)}；ASR: {evidence.transcript_status}")
        print(f"  AI 模型: {os.environ['QWEN_MODEL_ID']}")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
