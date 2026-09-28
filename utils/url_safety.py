"""Bounded public HTTP requests with DNS pinning across redirects.

The DNS answer is checked once and the connection is made to that exact IP.
For HTTPS, urllib3 still verifies the certificate against the original host.
"""

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from requests.structures import CaseInsensitiveDict


class _PinnedResponse(requests.Response):
    def __init__(self, pool):
        super().__init__()
        self._pinned_pool = pool

    def close(self):
        try:
            super().close()
        finally:
            self._pinned_pool.close()


def _public_endpoint(url: str):
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("视频地址过长或格式无效")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持 HTTP/HTTPS 视频地址")
    if parsed.username or parsed.password:
        raise ValueError("视频地址不能包含用户名或密码")

    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("视频地址域名或端口无效") from exc
    if port not in {80, 443}:
        raise ValueError("视频地址端口不受支持")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("不允许访问本机地址")

    try:
        try:
            addresses = [ipaddress.ip_address(hostname)]
        except ValueError:
            addresses = list(dict.fromkeys(
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            ))
    except (OSError, ValueError) as exc:
        raise ValueError("视频地址域名无法解析") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("不允许访问内网或非公网地址")
    return parsed, hostname, port, str(addresses[0])


def assert_public_http_url(url: str) -> str:
    _public_endpoint(url)
    return url


def open_public_once(url: str, headers: dict | None = None, timeout: int | tuple = 15,
                     method: str = "GET", body: bytes | None = None):
    """Return one streamed response without following redirects or re-resolving DNS."""
    parsed, hostname, port, address = _public_endpoint(url)
    pool_class = urllib3.HTTPSConnectionPool if parsed.scheme.lower() == "https" else urllib3.HTTPConnectionPool
    pool_options = {
        "host": address,
        "port": port,
        "maxsize": 1,
        "block": True,
    }
    if parsed.scheme.lower() == "https":
        pool_options.update(server_hostname=hostname, assert_hostname=hostname, cert_reqs="CERT_REQUIRED")
    pool = pool_class(**pool_options)
    if method not in {"GET", "POST"}:
        pool.close()
        raise ValueError("不支持的公网请求方法")
    request_headers = {key: value for key, value in (headers or {}).items() if key.lower() not in {"host", "content-length", "transfer-encoding"}}
    request_headers["Host"] = parsed.netloc
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    if isinstance(timeout, tuple):
        socket_timeout = urllib3.Timeout(connect=timeout[0], read=timeout[1])
    else:
        socket_timeout = urllib3.Timeout(connect=min(timeout, 10), read=timeout)
    try:
        raw = pool.urlopen(
            method, path, body=body, headers=request_headers, timeout=socket_timeout,
            retries=False, redirect=False, preload_content=False,
            assert_same_host=False,
        )
    except Exception as exc:
        pool.close()
        raise requests.RequestException(f"公网请求失败: {type(exc).__name__}") from exc
    response = _PinnedResponse(pool)
    response.status_code = raw.status
    response.headers = CaseInsensitiveDict(raw.headers)
    response.url = url
    response.reason = raw.reason
    response.raw = raw
    return response


def open_public_stream(url: str, headers: dict | None = None, timeout: int | tuple = 15,
                       max_redirects: int = 5):
    """Open a streamed response after checking each redirect destination."""
    current_url = url
    for _ in range(max_redirects + 1):
        response = open_public_once(current_url, headers=headers, timeout=timeout)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.RequestException("Redirect response did not include a Location")
            next_url = urljoin(current_url, location)
            if (urlsplit(next_url).scheme.lower(), urlsplit(next_url).netloc.lower()) != (
                urlsplit(current_url).scheme.lower(), urlsplit(current_url).netloc.lower()
            ):
                headers = {key: value for key, value in (headers or {}).items()
                           if key.lower() not in {"authorization", "cookie", "proxy-authorization"}}
            current_url = next_url
            continue
        try:
            response.raise_for_status()
            return response
        except Exception:
            response.close()
            raise
    raise requests.RequestException("Too many redirects")


def fetch_public_response(url: str, headers: dict | None = None, timeout: int | tuple = 15,
                          max_bytes: int = 5 * 1024 * 1024, max_redirects: int = 5):
    """Fetch a bounded response while validating every redirect destination."""
    response = open_public_stream(url, headers=headers, timeout=timeout, max_redirects=max_redirects)
    try:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("网页内容超过抓取大小限制")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("网页内容超过抓取大小限制")
        response._content = bytes(content)
        response._content_consumed = True
        response.close()
        return response
    except Exception:
        response.close()
        raise
