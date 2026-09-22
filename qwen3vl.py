#!/usr/bin/env python3
"""
基于 Qwen3-VL 的证据优先视频分析工具。
支持媒体检查、混合时间轴采样、字幕/可选 ASR 对齐和分级结论。
"""

import os
import sys
import argparse
import subprocess
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv
from video_analysis import analyze_video_evidence_first

# 加载环境变量
load_dotenv()

# API 配置（从环境变量读取）
API_BASE_URL = os.getenv('QWEN_API_BASE_URL', 'https://api-inference.modelscope.cn/v1')
API_KEY = os.getenv('QWEN_API_KEY', '')
MODEL_ID = os.getenv('QWEN_MODEL_ID', 'Qwen/Qwen3-VL-8B-Instruct')

# 强制检查：如果 MODEL_ID 看起来像一个 URL，则重置为默认值
if MODEL_ID.startswith('http'):
    MODEL_ID = 'Qwen/Qwen3-VL-8B-Instruct'

# 帧提取配置
MAX_ANALYSIS_FRAMES = int(os.getenv('MAX_ANALYSIS_FRAMES', '24'))


def get_video_files(directory: str = None) -> list:
    """
    获取指定目录下的所有 MP4 视频文件

    Args:
        directory: 目录路径，默认为当前项目的 downloads 和 cache 目录

    Returns:
        视频文件路径列表
    """
    video_files = []

    if directory:
        search_dirs = [directory]
    else:
        # 默认搜索目录
        base_dir = Path(__file__).parent
        search_dirs = [
            base_dir / 'downloads',
            base_dir / 'cache',
            base_dir / 'static' / 'videos'
        ]

    for search_dir in search_dirs:
        if Path(search_dir).exists():
            for file in Path(search_dir).glob('*.mp4'):
                video_files.append(str(file))

    return video_files


def get_video_duration(video_path: str) -> float:
    """获取视频时长（秒）"""
    try:
        result = subprocess.run(
            [
                os.getenv('FFPROBE_PATH', 'ffprobe'), '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                video_path
            ],
            capture_output=True,
            text=True
        )
        return float(result.stdout.strip())
    except Exception:
        return 0


def analyze_video(video_path: str, prompt: str = None, stream: bool = True, num_frames: int = MAX_ANALYSIS_FRAMES) -> str:
    """
    使用 Qwen3-VL 模型分析视频内容

    Args:
        video_path: 视频文件路径
        prompt: 额外分析关注点（不会覆盖证据约束）
        stream: 兼容旧参数；证据流水线固定使用非流式分阶段调用
        num_frames: 时间轴视觉采样上限

    Returns:
        视频内容描述
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    file_size = os.path.getsize(video_path) / (1024 * 1024)
    print(f"正在读取视频文件: {video_path} ({file_size:.1f}MB)")
    print("分析模式: 时间轴级证据提取 → 多模态对齐 → 论证重建")

    client = OpenAI(
        base_url=API_BASE_URL,
        api_key=API_KEY,
    )
    def print_progress(value: float, message: str) -> None:
        print(f"[{value:>5.0%}] {message}")

    result, evidence = analyze_video_evidence_first(
        video_path,
        client,
        MODEL_ID,
        custom_focus=prompt,
        max_frames=num_frames,
        progress=print_progress,
    )
    print("-" * 50)
    print(result)
    print("-" * 50)
    print(
        f"覆盖: {len(evidence.frames)} 帧 / {len(evidence.subtitles)} 条字幕 / "
        f"ASR={evidence.transcript_status}"
    )
    return result


def list_videos():
    """列出项目中所有可用的视频文件"""
    video_files = get_video_files()

    if not video_files:
        print("未找到任何视频文件")
        return

    print("=" * 60)
    print("项目中的视频文件:")
    print("=" * 60)

    for i, video_file in enumerate(video_files, 1):
        file_size = os.path.getsize(video_file) / (1024 * 1024)
        file_name = os.path.basename(video_file)
        duration = get_video_duration(video_file)
        duration_str = f"{duration:.1f}s" if duration > 0 else "未知"
        print(f"{i}. [{file_size:.1f}MB, {duration_str}] {file_name}")
        print(f"   路径: {video_file}")

    print("=" * 60)
    return video_files


def interactive_mode():
    """交互式模式，让用户选择视频进行分析"""
    video_files = list_videos()

    if not video_files:
        return

    print("\n请输入要分析的视频编号 (输入 q 退出):")

    while True:
        try:
            user_input = input("> ").strip()

            if user_input.lower() == 'q':
                print("退出程序")
                break

            index = int(user_input) - 1
            if 0 <= index < len(video_files):
                video_path = video_files[index]
                print(f"\n选择的视频: {os.path.basename(video_path)}")

                # 询问自定义提示词
                custom_prompt = input("输入自定义提示词 (直接回车使用默认): ").strip()

                print("\n" + "=" * 60)
                analyze_video(video_path, custom_prompt if custom_prompt else None)
                print("=" * 60)

                print("\n继续选择其他视频，或输入 q 退出:")
            else:
                print(f"无效的编号，请输入 1-{len(video_files)} 之间的数字")

        except ValueError:
            print("请输入有效的数字")
        except KeyboardInterrupt:
            print("\n退出程序")
            break
        except Exception as e:
            print(f"分析出错: {e}")
            import traceback
            traceback.print_exc()


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='基于 Qwen3-VL 模型的视频内容分析工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 列出所有视频文件
  python qwen3vl.py --list

  # 分析指定视频
  python qwen3vl.py --video downloads/video.mp4

  # 使用自定义提示词分析
  python qwen3vl.py --video video.mp4 --prompt "这个视频讲的是什么故事？"

  # 指定提取帧数
  python qwen3vl.py --video video.mp4 --frames 12

  # 交互式模式
  python qwen3vl.py --interactive
        """
    )

    parser.add_argument(
        '--video', '-v',
        type=str,
        help='要分析的视频文件路径'
    )

    parser.add_argument(
        '--prompt', '-p',
        type=str,
        default=None,
        help='自定义分析提示词'
    )

    parser.add_argument(
        '--frames', '-f',
        type=int,
        default=MAX_ANALYSIS_FRAMES,
        help=f'时间轴画面采样上限 (默认: {MAX_ANALYSIS_FRAMES})'
    )

    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='列出项目中所有视频文件'
    )

    parser.add_argument(
        '--interactive', '-i',
        action='store_true',
        help='交互式模式'
    )

    parser.add_argument(
        '--no-stream',
        action='store_true',
        help='禁用流式输出'
    )

    args = parser.parse_args()

    # 如果没有任何参数，显示帮助
    if len(sys.argv) == 1:
        parser.print_help()
        print("\n" + "=" * 60)
        list_videos()
        return

    if args.list:
        list_videos()
    elif args.interactive:
        interactive_mode()
    elif args.video:
        analyze_video(
            args.video,
            prompt=args.prompt,
            stream=not args.no_stream,
            num_frames=args.frames
        )
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
