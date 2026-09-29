import re
import requests
import random
from urllib.parse import urlparse, parse_qs, urljoin, urlencode
from configs.logging_config import logger
from configs.general_constants import USER_AGENT_PC, DOMAIN_TO_NAME
from utils.url_safety import assert_public_http_url, open_public_once


class WebFetcher:
    headers = {
        "content-type": "application/json; charset=UTF-8",
        "User-Agent": random.choice(USER_AGENT_PC)
    }

    @staticmethod
    def fetch_redirect_url(url, max_redirects=5):
        try:
            current_url = url
            for _ in range(max_redirects):
                assert_public_http_url(current_url)
                host = (urlparse(current_url).hostname or "").lower().rstrip(".")
                supported_roots = (
                    "douyin.com", "iesdouyin.com", "bilibili.com", "b23.tv",
                    "xiaohongshu.com", "xhslink.com",
                    "weixin.qq.com", "channels.weixin.qq.com",
                )
                if not any(host == root or host.endswith("." + root) for root in supported_roots):
                    return None
                # 发送请求，禁止重定向
                resp = open_public_once(current_url, headers=WebFetcher.headers, timeout=5)
                try:
                    resp.raise_for_status()
                    redirect_url = resp.headers.get("location")
                finally:
                    resp.close()
                # 获取重定向后的URL
                if redirect_url:
                    # 处理相对路径重定向
                    if not redirect_url.startswith('http'):
                        redirect_url = urljoin(current_url, redirect_url)
                    assert_public_http_url(redirect_url)
                    next_host = (urlparse(redirect_url).hostname or "").lower().rstrip(".")
                    if not any(next_host == root or next_host.endswith("." + root) for root in supported_roots):
                        return None
                    if DOMAIN_TO_NAME.get(UrlParser.get_domain(redirect_url)):
                        break
                    else:
                        current_url = redirect_url
                else:
                    break
            else:
                return None
            if redirect_url:
                return UrlParser.extract_video_address(redirect_url)
            else:
                if not DOMAIN_TO_NAME.get(UrlParser.get_domain(url)):
                    return None
                else:
                    format_real_url = UrlParser.extract_video_address(url)
                    return format_real_url
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Failed to get the page: {e}")
            return None
        except Exception as e:
            logger.error(f"An error occurred: {e}")
            return None


class UrlParser:
    @staticmethod
    def convert_to_https(url):
        if url is None:
            return None
        if url.startswith('http://'):
            return 'https://' + url[7:]
        return url

    @staticmethod
    def get_url(text):
        url_pattern = re.compile(r'\bhttps?:\/\/(?:www\.|[-a-zA-Z0-9.@:%_+~#=]{1,256}\.[a-zA-Z0-9()]{1,6})\b(?:[-a-zA-Z0-9()@:%_+.~#?&//=]*)?')
        match = url_pattern.search(text)
        if match:
            return match.group().rstrip(').,，。】]}>》；;！!？?')
        else:
            return None

    @staticmethod
    def get_domain(url):
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        return domain

    @staticmethod
    def extract_video_address(url):
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        platform = DOMAIN_TO_NAME.get(domain)
        address = f"{parsed_url.scheme}://{domain}{parsed_url.path}"
        if address.endswith('/'):
            address = address[:-1]
        if platform == '好看视频':
            query_params = parse_qs(parsed_url.query)
            vid = query_params.get('vid', [None])[0]  # 使用 get 方法避免 KeyError
            if vid:
                address = f"{address}?vid={vid}"
        elif platform == "微视":
            query_params = parse_qs(parsed_url.query)
            vid = query_params.get('id', [None])[0]  # 使用 get 方法避免 KeyError
            if vid:
                address = f"{address}?id={vid}"
        elif platform == "小红书":
            query_params = parse_qs(parsed_url.query)
            xsec_token = query_params.get('xsec_token', [None])[0]
            xsec_source = query_params.get('xsec_source', [None])[0]
            params = {}
            if xsec_token:
                params['xsec_token'] = xsec_token
            if xsec_source:
                params['xsec_source'] = xsec_source
            if params:
                address = f"{address}?{urlencode(params)}"
        elif platform == "快手":
            address = address.replace('http://', 'https://')
        elif platform == "YouTube":
            query_params = parse_qs(parsed_url.query)
            video_id = query_params.get('v', [None])[0]
            if video_id:
                address = f"{parsed_url.scheme}://{domain}/watch?v={video_id}"
        elif platform == "哔哩哔哩":
            query_params = parse_qs(parsed_url.query)
            page = query_params.get('p', [None])[0]
            if page and page.isdigit() and int(page) > 1:
                address += f"?p={int(page)}"
        return address

    @staticmethod
    def get_video_id(url):
        try:
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            if parsed_url.netloc in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
                youtube_id = query_params.get('v', [None])[0]
                if youtube_id:
                    return youtube_id
            # 尝试从查询参数中获取视频ID
            params_vid = query_params.get('vid', [None])[0]
            if params_vid:
                return params_vid
            params_id = query_params.get('id', [None])[0]
            if params_id:
                return params_id
            # 尝试从URL路径中获取视频ID
            path_segments = parsed_url.path.strip('/').split('/')
            if path_segments:
                video_id = path_segments[-1]
                return video_id
            logger.warning(f'Unable to retrieve video ID from URL: {url}')
            return None
        except Exception as e:
            logger.error(f"An error occurred while extracting video ID: {e}")
            return None

    @staticmethod
    def generate_video_url(platform, video_id):
        # 定义映射表
        url_map = {
            '皮皮搞笑': 'https://h5.pipigx.com/pp/post/',
            '好看视频': 'https://haokan.hao123.com/v?vid=',
            '哔哩哔哩': 'https://www.bilibili.com/video/',
            '抖音': 'https://www.iesdouyin.com/share/video/',
            '快手': 'https://www.kuaishou.com/short-video/',
            '梨视频': 'https://www.pearvideo.com/'
        }
        # 检查platform是否在映射表中
        if platform not in url_map:
            return "Error: 不支持的平台"
        # 拼接URL
        base_url = url_map[platform]
        full_url = base_url + video_id
        return full_url


if __name__ == '__main__':
    # share_url = UrlParser.get_url('0.74 复制打开抖音，看看【珊珊的甜甜圈之小圈的作品】虽然桌拍我已经没问题了，这个月也可以保级了，我也不... https://v.douyin.com/ir94AJyD/ l@c.At qEh:/ 01/19 ')
    # redirect_url = WebFetcher.fetch_redirect_url(share_url)
    # if redirect_url:
    #     print(f'重定向后的链接：{redirect_url}')
    # else:
    #     print('未能获取重定向后的链接')
    video_id1 = UrlParser.get_video_id('https://haokan.hao123.com/v?vid=1770898033348505648')
    print(video_id1)
