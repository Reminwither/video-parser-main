"""Read public Bilibili video metadata and DASH URLs from its JSON endpoints."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

from src.downloaders.base_downloader import BaseDownloader
from utils.url_safety import fetch_public_response


class BilibiliDownloader(BaseDownloader):
    def __init__(self, real_url):
        super().__init__(real_url)
        match = re.search(r"/video/(BV[0-9A-Za-z]{10})(?:/|$)", urlsplit(real_url).path)
        if not match:
            raise ValueError("请提供有效的 B 站 BV 视频链接")
        self.bvid = match.group(1)
        params = parse_qs(urlsplit(real_url).query)
        try:
            page = max(1, int(params.get("p", ["1"])[0]))
        except ValueError:
            page = 1
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
            "Referer": f"https://www.bilibili.com/video/{self.bvid}",
        }
        self.info = self._json("https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": self.bvid})).get("data") or {}
        pages = self.info.get("pages") or []
        if page > len(pages) or not pages:
            raise ValueError("B 站视频分集不存在")
        self.page = pages[page - 1]
        play_url = "https://api.bilibili.com/x/player/playurl?" + urlencode({
            "bvid": self.bvid, "cid": self.page["cid"], "qn": 80, "fnval": 16, "fourk": 1,
        })
        self.play = self._json(play_url).get("data") or {}

    def _json(self, url):
        response = fetch_public_response(url, headers=self.headers, timeout=(5, 15), max_bytes=3 * 1024 * 1024)
        try:
            payload = response.json()
        finally:
            response.close()
        if payload.get("code") != 0:
            raise RuntimeError(f"B 站接口暂不可用（{payload.get('code')}）")
        return payload

    def get_real_video_url(self):
        videos = (self.play.get("dash") or {}).get("video") or []
        if videos:
            return videos[0].get("baseUrl") or videos[0].get("base_url")
        direct = self.play.get("durl") or []
        return direct[0].get("url") if direct else None

    def get_audio_url(self):
        audio = (self.play.get("dash") or {}).get("audio") or []
        return (audio[0].get("baseUrl") or audio[0].get("base_url")) if audio else None

    def get_title_content(self):
        title = self.info.get("title") or ""
        if len(self.info.get("pages") or []) > 1:
            title += " - " + (self.page.get("part") or f"P{self.page.get('page', 1)}")
        return title

    def get_cover_photo_url(self):
        return self.info.get("pic")
