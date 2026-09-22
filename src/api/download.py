import os
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, ChunkedEncodingError, ConnectionError
from urllib3.util.retry import Retry
from flask import Blueprint, request
from configs.logging_config import logger
from utils.common_utils import make_response, validate_request
from utils.web_fetcher import UrlParser
from configs.general_constants import MINI_PROGRAM_LEGAL_DOMAIN, SAVE_VIDEO_PATH, DOMAIN

bp = Blueprint('download', __name__)


@bp.route('/download', methods=['POST'])
def download():
    try:
        data = request.json
        request_video_url = data.get('video_url')
        request_video_id = data.get('video_id')
        wx_open_id = request.headers.get('WX-OPEN-ID', 'Guest')

        validation_result = validate_request(request_video_url)
        if validation_result:
            # 如果验证不通过，则返回错误代码
            return validation_result

        # 判断视频链接的域名是否为小程序的合法域名
        domain = UrlParser.get_domain(request_video_url)
        logger.debug(f'Checking domain: {domain}')
        if domain in MINI_PROGRAM_LEGAL_DOMAIN:
            # 如果是，直接返回视频链接
            logger.debug(f'{wx_open_id} 直接返回视频链接')
            return make_response(200, '成功', {'download_url': request_video_url}, None, True), 200
        else:
            # 如果不是，服务器端下载视频并保存
            # 生成保存视频的文件名
            video_filename = f'{request_video_id}.mp4'
            video_path = os.path.join(SAVE_VIDEO_PATH, video_filename)
            
            # 如果 DOMAIN 未配置，使用本地测试地址
            current_domain = DOMAIN or "http://localhost:7860"
            
            if not os.path.exists(video_path):
                logger.info(f"Starting server-side download for {request_video_url}")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
                    'Referer': 'https://www.pearvideo.com/'
                }
                # 如果是梨视频，必须带 Referer
                if "pearvideo.com" in request_video_url:
                    headers['Referer'] = 'https://www.pearvideo.com/'

                # 创建带重试机制的session
                session = requests.Session()
                retries = Retry(
                    total=5,
                    backoff_factor=1,
                    status_forcelist=[500, 502, 503, 504],
                    allowed_methods=["GET"]
                )
                session.mount('http://', HTTPAdapter(max_retries=retries))
                session.mount('https://', HTTPAdapter(max_retries=retries))

                try:
                    response = session.get(request_video_url, headers=headers, stream=True, timeout=120)
                    response.raise_for_status()
                except (RequestException, ConnectionError) as e:
                    logger.error(f'{wx_open_id} Failed to connect: {e}')
                    return make_response(200, '服务器下载失败，返回原始链接', {'download_url': request_video_url}, None, True), 200

                try:
                    # 保存视频到服务器
                    with open(video_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                except (ChunkedEncodingError, IOError) as e:
                    logger.error(f'{wx_open_id} Failed to save video: {e}')
                    # 删除可能不完整的文件
                    if os.path.exists(video_path):
                        os.remove(video_path)
                    return make_response(200, '服务器下载失败，返回原始链接', {'download_url': request_video_url}, None, True), 200

                # 返回视频的 URL
                download_url = f'{current_domain}/static/videos/{video_filename}'
                logger.debug(f'{wx_open_id} 返回视频地址: {download_url}')
                return make_response(200, '成功', {'download_url': download_url}, None, True), 200
            else:
                # 返回视频的 URL
                download_url = f'{current_domain}/static/videos/{video_filename}'
                logger.debug(f'{wx_open_id} 返回视频地址: {download_url}')
                return make_response(200, '成功', {'download_url': download_url}, None, True), 200

    except Exception as e:
        logger.error(e)
        return make_response(500, '功能太火爆啦，请稍后再试', None, None, False), 500
