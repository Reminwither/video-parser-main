#!/usr/bin/env python3
"""
小红书视频下载客户端
直接调用下载器实现视频解析和下载
"""

import os
import re
import json
import requests
from bs4 import BeautifulSoup


def get_xhs_video_info(url: str) -> dict:
    """获取小红书视频信息"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'Referer': 'https://www.xiaohongshu.com/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Sec-Ch-Ua': '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1'
    }

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    # 解析页面数据
    pattern = re.compile(r'window\.__INITIAL_STATE__\s*=\s*(\{.*\})', re.DOTALL)
    page_obj = BeautifulSoup(resp.text, 'lxml')

    for script in page_obj.find_all('script'):
        if script.string:
            match = pattern.search(script.string)
            if match:
                json_data = match.group(1).rstrip(';').replace('undefined', 'null')
                data = json.loads(json_data)

                first_note_id = data['note'].get('firstNoteId')
                if not first_note_id or first_note_id not in data['note'].get('noteDetailMap', {}):
                    raise Exception("无法获取笔记详情，可能是链接已失效或被限制访问")

                note = data['note']['noteDetailMap'][first_note_id]['note']

                # 获取标题和描述
                title = note.get('title', '') + note.get('desc', '')

                # 获取封面
                cover_url = None
                if note.get('imageList'):
                    cover_url = note['imageList'][0].get('urlDefault', '').replace('\\u002F', '/')

                # 获取视频URL
                video_url = None
                if note.get('video') and note['video'].get('consumer'):
                    origin_key = note['video']['consumer'].get('originVideoKey', '').replace('\\u002F', '/')
                    if origin_key:
                        video_url = f"http://sns-video-bd.xhscdn.com/{origin_key}"

                return {
                    'title': title,
                    'cover_url': cover_url,
                    'video_url': video_url
                }

    raise Exception("无法解析页面数据")


def download_file(url: str, save_path: str, referer: str = None) -> bool:
    """下载文件到本地"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
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


def main():
    """主函数"""
    # 小红书视频链接 - 只保留必要的参数
    # 原始链接: https://www.xiaohongshu.com/explore/68ab2dd1000000001c0045d0?app_platform=android&ignoreEngage=true&app_version=9.13.1&share_from_user_hidden=true&xsec_source=app_share&type=video&xsec_token=CBLONm9tab3493BJUXCtvU8ScDzfYqa3cGwJstW8eSF3Y=&author_share=1&xhsshare=&shareRedId=OD04RDk1PUw2NzUyOTgwNjc7OThISj5O&apptime=1766754031&share_id=46cc1edc70704cd1ae467afb90d0e8e6&share_channel=wechat&wechatWid=94270060b457f72e6e3a2df13090e1d6&wechatOrigin=menu
    xiaohongshu_url = "https://www.xiaohongshu.com/explore/68ab2dd1000000001c0045d0?xsec_token=CBLONm9tab3493BJUXCtvU8ScDzfYqa3cGwJstW8eSF3Y=&xsec_source=app_share"
    save_path = "./downloads"

    os.makedirs(save_path, exist_ok=True)

    print("=" * 60)
    print("小红书视频下载客户端")
    print("=" * 60)

    # 解析视频
    print(f"\n正在解析: {xiaohongshu_url[:80]}...")

    try:
        info = get_xhs_video_info(xiaohongshu_url)
    except Exception as e:
        print(f"\n错误: {e}")
        return

    title = info['title']
    cover_url = info['cover_url']
    video_url = info['video_url']

    # 清理文件名（移除非法字符）
    safe_title = ''.join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.', '，', '。', '！', '？')).strip()[:50] if title else "xiaohongshu_video"

    print(f"\n视频信息:")
    print(f"  标题: {title[:100]}..." if len(title) > 100 else f"  标题: {title}")
    print(f"  封面: {cover_url}")
    print(f"  视频: {video_url}")

    if not video_url:
        print("\n错误: 无法获取视频链接")
        return

    # 下载封面
    if cover_url:
        print(f"\n[1/2] 下载封面...")
        cover_path = os.path.join(save_path, f"{safe_title}_cover.jpg")
        download_file(cover_url, cover_path, referer='https://www.xiaohongshu.com/')

    # 下载视频
    print(f"\n[2/2] 下载视频...")
    video_path = os.path.join(save_path, f"{safe_title}.mp4")
    if download_file(video_url, video_path, referer='https://www.xiaohongshu.com/'):
        print(f"\n下载完成!")
        print(f"保存路径: {video_path}")

    # 显示最终文件
    print(f"\n下载目录内容:")
    for f in os.listdir(save_path):
        fpath = os.path.join(save_path, f)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath) / 1024 / 1024
            print(f"  {f} ({size:.1f} MB)")


if __name__ == "__main__":
    main()
