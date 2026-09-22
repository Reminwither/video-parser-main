import json
import os
import time
import urllib3
import warnings
import copy
import requests
from utils.web_fetcher import UrlParser
from utils.douyin_utils.bogus_sign_utils import CommonUtils
from configs.logging_config import logger
from src.downloaders.base_downloader import BaseDownloader

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)


class DouyinDownloader(BaseDownloader):
    def __init__(self, real_url):
        super().__init__(real_url)
        self.common_utils = CommonUtils()
        # 与默认 UA（Chrome 128）及 a_bogus 签名保持一致；可通过环境变量覆盖。
        chrome_version = self.common_utils.user_agent.split('Chrome/', 1)[-1].split('.', 1)[0]
        self.headers = {
            'sec-ch-ua': f'"Google Chrome";v="{chrome_version}", "Not:A-Brand";v="8", "Chromium";v="{chrome_version}"',
            'Accept': 'application/json, text/plain, */*',
            'sec-ch-ua-mobile': '?0',
            'User-Agent': self.common_utils.user_agent,
            'sec-ch-ua-platform': '"Windows"',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Dest': 'empty',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Origin': 'https://www.douyin.com',
        }
        self.ms_token = self.common_utils.get_ms_token()
        self.ttwid = '1%7C6Og-dGVVAJIG5I0Ld8Ma-rDiaOam441iT2kRoldHOfQ%7C1786067668%7C98ba7d7b8bc5ded7d3b9652bdb03bbbc0195f00e58986d75222818931ff975b3'
        self.webid = '7307457174287205926'
        self.aweme_id = UrlParser.get_video_id(self.real_url)
        if not self.aweme_id or not str(self.aweme_id).isdigit():
            raise ValueError(f"无法从抖音链接提取作品 ID: {self.real_url}")
        self.data = self.fetch_html_data()
        # 判断是视频还是图文笔记
        self.is_note = '/note/' in self.real_url

    def fetch_html_data(self):
        # TikHub 作为首选；网络/服务不稳定（如 ReadTimeout）时降级到网页接口，
        # 避免外部 API 一次抖动就拖垮整次获取。
        tikhub_key = os.getenv('TIKHUB_API_KEY', '').strip()
        if tikhub_key:
            try:
                return self._fetch_tikhub_data(tikhub_key)
            except RuntimeError as exc:
                logger.warning(f"TikHub 获取抖音详情失败，降级到网页接口: {exc}")
        return self._fetch_web_data()

    def _fetch_web_data(self):
        referer_url = f"https://www.douyin.com/video/{self.aweme_id}?previous_page=web_code_link"
        # 注意：不能带 msToken/webid 参数——无效 msToken 会触发风控(403)，webid 会导致空响应
        play_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={self.aweme_id}&pc_client_type=1&version_code=190500&cookie_enabled=true&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome&browser_version=128.0.0.0&browser_online=true&engine_name=Blink&engine_version=128.0.0.0&os_name=Windows&os_version=10&cpu_core_num=8&device_memory=8&platform=PC&downlink=10&effective_type=4g&round_trip_time=50"
        new_headers = copy.deepcopy(self.headers)
        new_headers['Referer'] = referer_url
        # Cookie 策略：优先用用户从「已登录浏览器」复制的完整 Cookie（含新鲜
        # ttwid + 设备指纹 ufid + sessionid 等），Argus 风控直接满意；
        # 没配时才退回内置写死 ttwid（该值可能过期，仅供无登录态降级，会 403）。
        # 注意：绝不能把"只含 sessionid 的短 Cookie"当完整 Cookie 用——缺 ufid 必 403。
        configured_cookie = os.getenv("DOUYIN_COOKIE", "").strip()
        new_headers['Cookie'] = configured_cookie or f"ttwid={self.ttwid}"
        last_error = None

        # 抖音偶尔会返回空正文、HTML 风控页或瞬时 5xx。必须把这些情况
        # 转成明确异常，否则上层会把全是 None 的结果错误标记为“成功”。
        for attempt in range(3):
            try:
                abogus = self.common_utils.get_abogus(play_url, self.common_utils.user_agent)
                url = f"{play_url}&a_bogus={abogus}"
                response = requests.get(url, headers=new_headers, verify=False, timeout=(5, 15))
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not response.text.strip():
                    raise RuntimeError("抖音详情接口返回空响应")
                if "json" not in content_type.lower() and not response.text.lstrip().startswith("{"):
                    raise RuntimeError(f"抖音详情接口返回非 JSON 内容: {content_type or 'unknown'}")
                payload = response.json()
                if payload.get("status_code") not in (None, 0):
                    raise RuntimeError(f"抖音详情接口状态异常: {payload.get('status_code')}")
                if not payload.get("aweme_detail"):
                    raise RuntimeError("抖音详情接口未返回 aweme_detail")
                return payload
            except (requests.RequestException, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
                logger.warning(f"抖音详情请求第 {attempt + 1}/3 次失败: {exc}")
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 403:
                    raise RuntimeError(
                        "抖音拒绝了详情请求（403）。请在 .env 配置 TIKHUB_API_KEY，"
                        "启用按链接获取视频的备用接口"
                    ) from exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))

        raise RuntimeError(f"获取抖音详情失败: {last_error}")

    def _fetch_tikhub_data(self, api_key):
        """通过作品 ID 获取详情，避免本机抖音网页接口的 403 风控。"""
        endpoints = (
            'https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video',
            'https://api.tikhub.io/api/v1/douyin/web/fetch_one_video_v2',
        )
        last_error = None
        for endpoint in endpoints:
            try:
                response = requests.get(
                    endpoint,
                    params={'aweme_id': self.aweme_id},
                    headers={'Authorization': f'Bearer {api_key}'},
                    timeout=(5, 20),
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get('data') or {}
                if payload.get('code') == 200 and data.get('aweme_detail'):
                    return data
                filter_list = data.get('filter_list') or []
                if filter_list:
                    raise RuntimeError(f"作品不可访问（原因代码 {filter_list[0].get('reason')}）")
                last_error = f"接口未返回作品详情（状态 {payload.get('code')}）"
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    raise RuntimeError("TikHub API 密钥无效或余额不足，请检查 TIKHUB_API_KEY") from exc
                last_error = f"HTTP {status}"
            except (requests.RequestException, ValueError) as exc:
                last_error = type(exc).__name__
            logger.warning(f"TikHub 抖音详情获取失败: {last_error}")
        raise RuntimeError(f"TikHub 未能获取抖音作品详情: {last_error}")

    @staticmethod
    def _url_list(resource):
        if not isinstance(resource, dict):
            return []
        return [item for item in (resource.get("url_list") or []) if item]

    @classmethod
    def _first_url(cls, resource):
        urls = cls._url_list(resource)
        return urls[0] if urls else None

    def get_real_video_url(self):
        try:
            data_dict = self.data
            # 尝试获取视频URL (视频类型)
            video_data = data_dict.get('aweme_detail', {}).get('video', {})

            # 方式1: 从 bit_rate 获取
            bit_rate = video_data.get('bit_rate')
            if bit_rate and len(bit_rate) > 0:
                play_addr_list = bit_rate[0].get('play_addr', {}).get('url_list', [])
                if len(play_addr_list) > 2:
                    return play_addr_list[2]
                elif len(play_addr_list) > 0:
                    return play_addr_list[0]

            # 方式2: 从 play_addr 直接获取
            play_addr = video_data.get('play_addr', {}).get('url_list', [])
            if play_addr:
                return play_addr[0] if len(play_addr) > 0 else None

            # 方式3: 如果是图文笔记，返回第一张图片URL
            images = data_dict.get('aweme_detail', {}).get('images', [])
            if images and len(images) > 0:
                url_list = images[0].get('url_list', [])
                if url_list:
                    return url_list[0]

            return None
        except (KeyError, json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse video URL: {e}")
            return None

    def get_transcription_video_url(self):
        """文字稿只需要音轨。抖音 bit_rate 里体积最小的流可能是无声的纯视频流，
        下载后会让 ffprobe 检测到"视频没有音轨"，因此退回带音轨的主流。"""
        return self.get_real_video_url()

    def get_title_content(self):
        try:
            data_dict = self.data
            title_content = data_dict['aweme_detail']['desc']
            return title_content
        except (KeyError, json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse title content: {e}")
            return None

    def get_cover_photo_url(self):
        try:
            data_dict = self.data
            video_data = data_dict.get('aweme_detail', {}).get('video', {})

            # 方式1: 从 cover_original_scale 获取
            cover = video_data.get('cover_original_scale', {}).get('url_list', [])
            if cover:
                return cover[0]

            # 方式2: 从 cover 获取
            cover = video_data.get('cover', {}).get('url_list', [])
            if cover:
                return cover[0]

            # 方式3: 如果是图文笔记，返回第一张图片
            images = data_dict.get('aweme_detail', {}).get('images', [])
            if images and len(images) > 0:
                url_list = images[0].get('url_list', [])
                if url_list:
                    return url_list[0]

            return None
        except (KeyError, json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse cover URL: {e}")
            return None

    def get_images(self):
        """获取图文笔记的所有图片URL"""
        try:
            data_dict = self.data
            images = data_dict.get('aweme_detail', {}).get('images', [])
            image_urls = []
            for img in images:
                url_list = img.get('url_list', [])
                if url_list:
                    image_urls.append(url_list[0])
            return image_urls
        except (KeyError, json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse images: {e}")
            return []

    def get_audio_url(self):
        """返回作品使用的原声音频地址。"""
        detail = (self.data or {}).get("aweme_detail", {})
        return self._first_url((detail.get("music") or {}).get("play_url"))

    def get_metadata(self):
        """返回 API 可安全公开、且保持来源字段语义的完整结构化作品信息。"""
        detail = (self.data or {}).get("aweme_detail", {})
        video = detail.get("video") or {}
        author = detail.get("author") or {}
        music = detail.get("music") or {}
        statistics = detail.get("statistics") or {}

        bit_rates = []
        all_video_urls = []
        for item in video.get("bit_rate") or []:
            play_addr = item.get("play_addr") or {}
            urls = self._url_list(play_addr)
            all_video_urls.extend(urls)
            bit_rates.append({
                "gear_name": item.get("gear_name"),
                "quality_type": item.get("quality_type"),
                "bit_rate": item.get("bit_rate"),
                "is_h265": item.get("is_h265"),
                "is_bytevc1": item.get("is_bytevc1"),
                "play_urls": urls,
                "width": play_addr.get("width"),
                "height": play_addr.get("height"),
                "data_size": play_addr.get("data_size"),
                "file_hash": play_addr.get("file_hash"),
            })

        direct_urls = self._url_list(video.get("play_addr"))
        all_video_urls.extend(direct_urls)
        # 顺序去重，保留抖音返回的优先级。
        all_video_urls = list(dict.fromkeys(all_video_urls))

        avatar = author.get("avatar_thumb") or {}
        music_play = music.get("play_url") or {}
        images = []
        for image in detail.get("images") or []:
            images.append({
                "urls": self._url_list(image),
                "width": image.get("width"),
                "height": image.get("height"),
            })

        return {
            "video_id": str(detail.get("aweme_id") or self.aweme_id),
            "platform": "抖音",
            "aweme_type": detail.get("aweme_type"),
            "media_type": detail.get("media_type"),
            "title": detail.get("desc"),
            "desc": detail.get("desc"),
            "share_url": detail.get("share_url") or (detail.get("share_info") or {}).get("share_url"),
            "create_time": detail.get("create_time"),
            "duration_ms": detail.get("duration") or video.get("duration"),
            "author": {
                "uid": author.get("uid"),
                "sec_uid": author.get("sec_uid"),
                "unique_id": author.get("unique_id"),
                "short_id": author.get("short_id"),
                "nickname": author.get("nickname"),
                "signature": author.get("signature"),
                "custom_verify": author.get("custom_verify"),
                "enterprise_verify_reason": author.get("enterprise_verify_reason"),
                "avatar_url": self._first_url(avatar),
                "avatar_urls": self._url_list(avatar),
                "follower_count": author.get("follower_count"),
                "following_count": author.get("following_count"),
                "total_favorited": author.get("total_favorited"),
            },
            "video_url": self.get_real_video_url(),
            "video_urls": all_video_urls,
            "audio_url": self.get_audio_url(),
            "audio_urls": self._url_list(music_play),
            "cover_url": self.get_cover_photo_url(),
            "cover_urls": self._url_list(video.get("cover_original_scale") or video.get("cover")),
            "dynamic_cover_url": self._first_url(video.get("dynamic_cover")),
            "dynamic_cover_urls": self._url_list(video.get("dynamic_cover")),
            "origin_cover_url": self._first_url(video.get("origin_cover")),
            "origin_cover_urls": self._url_list(video.get("origin_cover")),
            "width": video.get("width"),
            "height": video.get("height"),
            "ratio": video.get("ratio"),
            "format": video.get("format"),
            "has_watermark": video.get("has_watermark"),
            "is_h265": video.get("is_h265"),
            "bit_rates": bit_rates,
            "music": {
                "id": music.get("id_str") or music.get("id"),
                "title": music.get("title"),
                "author": music.get("author"),
                "duration": music.get("duration"),
                "is_original": music.get("is_original"),
                "is_original_sound": music.get("is_original_sound"),
                "play_url": self.get_audio_url(),
                "play_urls": self._url_list(music_play),
            },
            "statistics": {
                "play_count": statistics.get("play_count"),
                "digg_count": statistics.get("digg_count"),
                "comment_count": statistics.get("comment_count"),
                "share_count": statistics.get("share_count"),
                "collect_count": statistics.get("collect_count"),
                "admire_count": statistics.get("admire_count"),
            },
            "hashtags": [
                item.get("hashtag_name") for item in (detail.get("text_extra") or [])
                if item.get("hashtag_name")
            ],
            "images": images,
            "is_ads": detail.get("is_ads"),
            "is_aigc_media": detail.get("is_aigc_media"),
            "status": detail.get("status") or {},
        }


if __name__ == '__main__':
    real_url = 'https://www.douyin.com/video/7396822576074460467'
    dl = DouyinDownloader(real_url)
    print(dl.get_title_content())
    print(dl.get_cover_photo_url())
    print(dl.get_real_video_url())
