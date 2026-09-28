import os
import uuid
from bs4 import BeautifulSoup
from configs.general_constants import SAVE_VIDEO_PATH, SAVE_IMAGE_PATH
from configs.logging_config import logger
from utils.url_safety import fetch_public_response, open_public_stream


class BaseDownloader:
    def __init__(self, real_url):
        self.real_url = real_url
        self.headers = None
        self.html_content = None

    def get_real_video_url(self):
        raise NotImplementedError

    def get_title_content(self):
        raise NotImplementedError

    def get_cover_photo_url(self):
        raise NotImplementedError

    def fetch_html_content(self):
        try:
            resp = fetch_public_response(self.real_url, headers=self.headers, timeout=(5, 15), max_bytes=5 * 1024 * 1024)
            try:
                return resp.text
            finally:
                resp.close()
        except Exception as e:
            logger.error(f"Failed to get the page: {e}")

    @staticmethod
    def parse_html_data(html_content, pattern):
        page_obj = BeautifulSoup(html_content, 'lxml')
        script_tags = page_obj.find_all('script')
        for script in script_tags:
            if script.string:
                match = pattern.search(script.string)
                if match:
                    json_data = match.group(1)
                    json_data = json_data.rstrip(';')  # 部分需要去除分号
                    json_data = json_data.replace('undefined', 'null')  # 小红书需要这步骤
                    return json_data
        logger.error("Video object not found")

    @staticmethod
    def mkdir(folder):
        if not os.path.exists(folder):
            os.makedirs(folder, 0o777)
            return True
        return False

    def download_and_save(self, folder, url, file_extension):
        BaseDownloader.mkdir(folder)
        limit_mb = max(1, int(os.getenv("MAX_VIDEO_DOWNLOAD_MB", "500"))) if file_extension == "mp4" else 8
        max_bytes = limit_mb * 1024 * 1024
        full_name = os.path.abspath(os.path.join(folder, f'{uuid.uuid4()}.{file_extension}'))
        temporary_path = full_name + ".part"
        response = None
        try:
            response = open_public_stream(url, headers=self.headers, timeout=(10, 30))
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError("文件超过下载大小限制")
            written = 0
            with open(temporary_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError("文件超过下载大小限制")
                        f.write(chunk)
            os.replace(temporary_path, full_name)
            return full_name
        except Exception as e:
            logger.error(f"Failed to download the resource: {e}")
            return None
        finally:
            if response is not None:
                response.close()
            if os.path.exists(temporary_path):
                os.remove(temporary_path)

    def download_and_save_video(self):
        video_url = self.get_real_video_url()
        logger.debug(f'视频下载地址：{video_url}')
        return self.download_and_save(SAVE_VIDEO_PATH, video_url, 'mp4')

    def download_and_save_image(self):
        photo_url = self.get_cover_photo_url()
        if photo_url:
            logger.debug(f'封面下载地址：{photo_url}')
            return self.download_and_save(SAVE_IMAGE_PATH, photo_url, 'jpg')
        else:
            logger.debug(f'未获取到封面下载地址')
            return None
