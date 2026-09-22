#!/usr/bin/env python3
"""
B站视频下载客户端
直接调用下载器实现视频解析和下载（包含音视频合并）
"""

import os
import subprocess
import requests


def download_file(url: str, save_path: str, referer: str = None) -> bool:
    """下载文件到本地"""
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
        print("请安装ffmpeg: sudo apt install ffmpeg")
        return False


def main():
    """主函数"""
    from src.downloaders.bilibili_downloader import BilibiliDownloader

    # B站视频链接
    bilibili_url = "https://www.bilibili.com/video/BV1TaqYBcEJc"
    save_path = "./downloads"

    os.makedirs(save_path, exist_ok=True)

    print("=" * 60)
    print("B站视频下载客户端")
    print("=" * 60)

    # 解析视频
    print(f"\n正在解析: {bilibili_url}")
    dl = BilibiliDownloader(bilibili_url)

    title = dl.get_title_content()
    cover_url = dl.get_cover_photo_url()
    video_url = dl.get_real_video_url()
    audio_url = dl.get_audio_url()

    # 清理文件名
    safe_title = ''.join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).strip()[:50]

    print(f"\n视频信息:")
    print(f"  标题: {title}")
    print(f"  封面: {cover_url}")
    print(f"  视频: {video_url[:80]}..." if video_url else "  视频: 无")
    print(f"  音频: {audio_url[:80]}..." if audio_url else "  音频: 无")

    # 下载封面
    print(f"\n[1/3] 下载封面...")
    cover_path = os.path.join(save_path, f"{safe_title}_cover.jpg")
    download_file(cover_url, cover_path, referer=bilibili_url)

    # 下载视频轨道
    print(f"\n[2/3] 下载视频轨道...")
    video_temp_path = os.path.join(save_path, f"{safe_title}_video.m4s")
    download_file(video_url, video_temp_path, referer=bilibili_url)

    # 下载音频轨道
    print(f"\n[3/3] 下载音频轨道...")
    audio_temp_path = os.path.join(save_path, f"{safe_title}_audio.m4s")
    download_file(audio_url, audio_temp_path, referer=bilibili_url)

    # 合并音视频
    output_path = os.path.join(save_path, f"{safe_title}.mp4")
    if merge_video_audio(video_temp_path, audio_temp_path, output_path):
        print(f"\n下载完成!")
        print(f"保存路径: {output_path}")
    else:
        print(f"\n音视频未合并，文件保存为:")
        print(f"  视频: {video_temp_path}")
        print(f"  音频: {audio_temp_path}")

    # 显示最终文件
    print(f"\n下载目录内容:")
    for f in os.listdir(save_path):
        fpath = os.path.join(save_path, f)
        size = os.path.getsize(fpath) / 1024 / 1024
        print(f"  {f} ({size:.1f} MB)")


if __name__ == "__main__":
    main()
