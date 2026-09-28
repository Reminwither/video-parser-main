import json
import random
from utils.url_safety import open_public_once
from utils.web_fetcher import UrlParser
from src.downloaders.base_downloader import BaseDownloader
from configs.general_constants import USER_AGENT_PC
from configs.logging_config import logger


class PipigaoxiaoDownloader(BaseDownloader):
    def __init__(self, url):
        super().__init__(url)
        self.headers = {
            "content-type": "application/json; charset=UTF-8",
            'User-Agent': random.choice(USER_AGENT_PC),
            'referer': self.real_url
        }
        self.video_pid = UrlParser.get_video_id(self.real_url)
        self.data = self.fetch_html_data()

    def fetch_html_data(self):
        jsp_url = "https://h5.pipigx.com/ppapi/share/fetch_content"
        params = {
            'mid': 'null',
            'pid': int(self.video_pid),
            'type': "post"
        }
        response = open_public_once(jsp_url, headers=self.headers, timeout=(5, 15),
                                    method="POST", body=json.dumps(params).encode("utf-8"))
        try:
            if response.status_code != 200:
                logger.warning(f"请求失败，状态码: {response.status_code}")
                return None
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("平台响应过大")
            return json.loads(data)
        finally:
            response.close()

    def get_real_video_url(self):
        try:
            data_dict = self.data
            imgs_id = data_dict['data']['post']['imgs'][0]['id']
            video_url = data_dict['data']['post']['videos'][str(imgs_id)]['url']
            return video_url
        except (KeyError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse video URL: {e}")

    def get_title_content(self):
        try:
            data_dict = self.data
            title_content = data_dict['data']['post']['content']
            return title_content
        except (KeyError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse title content: {e}")

    def get_cover_photo_url(self):
        try:
            data_dict = self.data
            imgs_id = data_dict['data']['post']['imgs'][0]['id']
            cover_url = f'https://file.ippzone.com/img/view/id/{imgs_id}'
            return cover_url
        except (KeyError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse cover URL: {e}")


if __name__ == '__main__':
    real_url = 'https://h5.pipigx.com/pp/post/815491325984?pid=815491325984&type=post'
    dl = PipigaoxiaoDownloader(real_url)
    print(dl.get_title_content())
    print(dl.get_cover_photo_url())
    print(dl.get_real_video_url())
