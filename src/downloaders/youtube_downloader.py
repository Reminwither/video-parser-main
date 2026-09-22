import glob
import os

from configs.logging_config import logger
from src.downloaders.base_downloader import BaseDownloader


class YoutubeDownloader(BaseDownloader):
    """YouTube adapter backed by yt-dlp.

    yt-dlp resolves YouTube's frequently changing player formats more reliably than
    page-specific HTML selectors. Metadata extraction does not download media.
    """

    def __init__(self, real_url):
        super().__init__(real_url)
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("YouTube 支持需要安装 yt-dlp") from exc

        self._yt_dlp = yt_dlp
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 30,
            "retries": 3,
            "extractor_retries": 5,
            "source_address": "0.0.0.0",
            "nocheckcertificate": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                self.info = ydl.extract_info(real_url, download=False)
        except Exception as exc:
            raise RuntimeError(f"YouTube 元数据解析失败: {exc}") from exc

    @staticmethod
    def _requested_format_url(info: dict, media_type: str) -> str | None:
        for item in info.get("requested_formats") or []:
            if media_type == "video" and item.get("vcodec") not in (None, "none"):
                return item.get("url")
            if media_type == "audio" and item.get("acodec") not in (None, "none"):
                return item.get("url")
        return None

    def get_real_video_url(self):
        return self._requested_format_url(self.info, "video") or self.info.get("url")

    def get_audio_url(self):
        return self._requested_format_url(self.info, "audio")

    def get_title_content(self):
        return self.info.get("title")

    def get_cover_photo_url(self):
        return self.info.get("thumbnail")

    def get_metadata(self):
        info = self.info
        thumbnails = info.get("thumbnails") or []
        return {
            "video_id": str(info.get("id") or ""),
            "platform": "YouTube",
            "title": info.get("title"),
            "desc": info.get("description"),
            "share_url": info.get("webpage_url") or self.real_url,
            "create_time": info.get("timestamp"),
            "upload_date": info.get("upload_date"),
            "duration_ms": round(float(info.get("duration") or 0) * 1000),
            "author": {
                "uid": info.get("channel_id") or info.get("uploader_id"),
                "nickname": info.get("channel") or info.get("uploader"),
                "channel_url": info.get("channel_url") or info.get("uploader_url"),
                "follower_count": info.get("channel_follower_count"),
            },
            "video_url": self.get_real_video_url(),
            "video_urls": [self.get_real_video_url()] if self.get_real_video_url() else [],
            "audio_url": self.get_audio_url(),
            "audio_urls": [self.get_audio_url()] if self.get_audio_url() else [],
            "cover_url": self.get_cover_photo_url(),
            "cover_urls": [item.get("url") for item in thumbnails if item.get("url")],
            "width": info.get("width"),
            "height": info.get("height"),
            "format": info.get("ext"),
            "statistics": {
                "play_count": info.get("view_count"),
                "digg_count": info.get("like_count"),
                "comment_count": info.get("comment_count"),
            },
            "hashtags": info.get("tags") or [],
            "categories": info.get("categories") or [],
            "language": info.get("language"),
            "availability": info.get("availability"),
        }

    def download_to(self, output_dir: str, safe_id: str, timestamp: int) -> str:
        """Download and merge the selected YouTube audio/video tracks."""
        os.makedirs(output_dir, exist_ok=True)
        output_template = os.path.join(output_dir, f"feishu_{safe_id}_{timestamp}.%(ext)s")
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "socket_timeout": 30,
            "retries": 3,
            "extractor_retries": 5,
            "source_address": "0.0.0.0",
            "nocheckcertificate": True,
        }
        ffmpeg_path = os.getenv("FFMPEG_PATH", "").strip()
        if ffmpeg_path and ffmpeg_path.lower() != "ffmpeg":
            options["ffmpeg_location"] = ffmpeg_path
        try:
            with self._yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([self.real_url])
        except Exception as exc:
            raise RuntimeError(f"YouTube 视频下载失败: {exc}") from exc

        expected = os.path.join(output_dir, f"feishu_{safe_id}_{timestamp}.mp4")
        if os.path.exists(expected):
            return expected
        candidates = sorted(glob.glob(os.path.join(output_dir, f"feishu_{safe_id}_{timestamp}.*")))
        candidates = [path for path in candidates if not path.endswith((".part", ".ytdl"))]
        if not candidates:
            logger.error("YouTube download completed without a media file")
            raise RuntimeError("YouTube 下载完成但未找到媒体文件")
        return candidates[0]
