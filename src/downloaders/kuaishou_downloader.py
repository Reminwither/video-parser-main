import json
import os
import re

from utils.web_fetcher import UrlParser
from src.downloaders.base_downloader import BaseDownloader


class KuaishouDownloader(BaseDownloader):
    def __init__(self, real_url):
        super().__init__(real_url)
        self.video_id = UrlParser.get_video_id(real_url)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
            "Referer": "https://www.kuaishou.com/",
        }
        cookie = os.getenv("KUAISHOU_COOKIE", "").strip()
        if cookie:
            self.headers["Cookie"] = cookie
        page = self.fetch_html_content()
        if not page:
            raise RuntimeError("快手页面暂不可用")
        match = re.search(r"window\.__APOLLO_STATE__\s*=\s*(\{.*\});", page, re.DOTALL)
        if not match:
            raise RuntimeError("快手未返回可解析的视频数据")
        self.data = json.loads(match.group(1)).get("defaultClient") or {}

    def get_real_video_url(self):
        stream = self.data.get("VisionVideoSetRepresentation:1") or {}
        return stream.get("url")

    def get_title_content(self):
        detail = self.data.get(f"VisionVideoDetailPhoto:{self.video_id}") or {}
        return detail.get("caption")

    def get_cover_photo_url(self):
        detail = self.data.get(f"VisionVideoDetailPhoto:{self.video_id}") or {}
        return detail.get("coverUrl")
