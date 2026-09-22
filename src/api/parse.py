from flask import Blueprint, request
from configs.logging_config import logger
from configs.general_constants import DOMAIN_TO_NAME
from utils.web_fetcher import WebFetcher, UrlParser
# from src.database.data_storage_manager import DataStorageManager  # 暂时注释数据库
from src.downloader_factory import DownloaderFactory
from utils.common_utils import make_response, validate_request

bp = Blueprint('parse', __name__)


@bp.route('/parse', methods=['POST'])
def parse():
    try:
        data = request.json
        text = data.get('text')
        wx_open_id = request.headers.get('WX-OPEN-ID', 'Guest')

        validation_result = validate_request(text)
        if validation_result:
            # 如果验证不通过，则返回错误代码
            return validation_result

        # 提取URL
        extracted_url = UrlParser.get_url(text)
        logger.info(f'extracted_url: {extracted_url}')

        # 获取重定向URL
        redirect_url = WebFetcher.fetch_redirect_url(extracted_url)
        logger.info(f'redirect_url: {redirect_url}')

        # 如果重定向失败，直接使用原始URL
        if not redirect_url:
            redirect_url = extracted_url
            logger.info(f'Using original URL: {redirect_url}')

        platform = DOMAIN_TO_NAME.get(UrlParser.get_domain(redirect_url))
        video_id = UrlParser.get_video_id(redirect_url)
        real_url = UrlParser.extract_video_address(redirect_url)
        logger.info(f'platform: {platform}, video_id: {video_id}, real_url: {real_url}')

        if not platform:
            logger.error(f'This link is not supported for extraction: {real_url}')
            return make_response(400, '该链接尚未支持提取', None, None, False), 400

        # 暂时注释数据库相关代码
        # user_id = DataStorageManager.get_or_create_user_id(wx_open_id)
        # manager = DataStorageManager(video_id, real_url, user_id)
        # get_data = manager.get_db_data()

        title = cover_url = video_url = None
        if platform == '小红书':
            max_attempts = 5
            attempts = 0
            while attempts < max_attempts:
                downloader = DownloaderFactory.create_downloader(platform, real_url)
                title = downloader.get_title_content()
                video_url = downloader.get_real_video_url()
                cover_url = downloader.get_cover_photo_url()
                if video_url:
                    break
                attempts += 1
                logger.debug(f"Attempt {attempts} failed. Retrying...")
            if not video_url:
                logger.error("Failed to retrieve video URL after 5 attempts.")
        else:
            downloader = DownloaderFactory.create_downloader(platform, real_url)
            title = downloader.get_title_content()
            video_url = downloader.get_real_video_url()
            cover_url = downloader.get_cover_photo_url()

        updated_video_url = UrlParser.convert_to_https(video_url)
        updated_cover_url = UrlParser.convert_to_https(cover_url)
        data_dict = {'video_id': video_id, 'platform': platform, 'title': title,
                     'video_url': updated_video_url, 'cover_url': updated_cover_url}

        # B站视频需要额外返回音频URL
        if platform == '哔哩哔哩' and hasattr(downloader, 'get_audio_url'):
            audio_url = downloader.get_audio_url()
            if audio_url:
                data_dict['audio_url'] = UrlParser.convert_to_https(audio_url)
        # manager.upsert_parse_add_records(data_dict, user_id, video_id)  # 暂时注释数据库
        logger.debug(f'{wx_open_id} {platform} Parse Success')
        return make_response(200, '成功', data_dict, None, True), 200

    except Exception as e:
        logger.error(e)
        return make_response(500, '功能太火爆啦，请稍后再试', None, None, False), 500
