"""Helpers for preventing server-side requests to loopback and private networks."""

import ipaddress
import socket
import requests
from urllib.parse import urlparse, urljoin


def assert_public_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持 HTTP/HTTPS 视频地址")
    if parsed.username or parsed.password:
        raise ValueError("视频地址不能包含用户名或密码")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("不允许访问本机地址")
    try:
        try:
            addresses = {ipaddress.ip_address(host)}
        except ValueError:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
    except (OSError, ValueError) as exc:
        raise ValueError("视频地址域名无法解析") from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("不允许访问内网或非公网地址")
    return url


def fetch_public_response(url: str, headers: dict | None = None, timeout: int = 15,
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
        return response
    except Exception:
        response.close()
        raise


def open_public_stream(url: str, headers: dict | None = None, timeout: int = 15,
                       max_redirects: int = 5):
    """Open a response stream after validating each redirect destination."""
    current_url = url
    for _ in range(max_redirects + 1):
        assert_public_http_url(current_url)
        response = requests.get(
            current_url,
            headers=headers or {},
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.RequestException("Redirect response did not include a Location")
            current_url = urljoin(current_url, location)
            continue

        try:
            response.raise_for_status()
            return response
        except Exception:
            response.close()
            raise
    raise requests.RequestException("Too many redirects")
