import os, copy, sys
import requests

# 复现代码里的关键参数
aweme_id = "7683395975825640738"
play_url = (f"https://www.douyin.com/aweme/v1/web/aweme/detail/"
            f"?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={aweme_id}"
            f"&pc_client_type=1&version_code=190500&cookie_enabled=true"
            f"&screen_width=1536&screen_height=864&browser_language=zh-CN"
            f"&browser_platform=Win32&browser_name=Chrome&browser_version=128.0.0.0"
            f"&os_name=Windows&os_version=10&cpu_core_num=8&device_memory=8"
            f"&platform=PC&downlink=10&effective_type=4g&round_trip_time=50")
ttwid = '1%7C6Og-dGVVAJIG5I0Ld8Ma-rDiaOam441iT2kRoldHOfQ%7C1786067668%7C98ba7d7b8bc5ded7d3b9652bdb03bbbc0195f00e58986d75222818931ff975b3'
headers = {
    'sec-ch-ua': '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
    'Accept': 'application/json, text/plain, */*',
    'sec-ch-ua-mobile': '?0',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'sec-ch-ua-platform': '"Windows"',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Dest': 'empty',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': f'https://www.douyin.com/video/{aweme_id}?previous_page=web_code_link',
    'Cookie': f'ttwid={ttwid}',
}

def show(tag, resp):
    body = resp.text or ''
    print(f"\n===== {tag} =====")
    print(f"status_code = {resp.status_code}")
    print(f"url         = {resp.url[:80]}...")
    for k in ('content-type','server','x-tt-logid','set-cookie','date'):
        if k in resp.headers:
            v = resp.headers[k]
            print(f"header {k} = {v[:120]}")
    print(f"body[:400]  = {body[:400]!r}")

# 案例1：走代理（代码默认行为，requests 读 HTTPS_PROXY）
print(">>> CASE 1: 走系统代理 (HTTPS_PROXY=%s)" % os.getenv('HTTPS_PROXY'))
try:
    r1 = requests.get(play_url, headers=headers, timeout=(8,20))
    show("CASE1 走代理", r1)
except Exception as e:
    print(f"CASE1 异常: {type(e).__name__}: {e}")

# 案例2：绕过代理
print("\n>>> CASE 2: 绕过代理 (proxies=None)")
try:
    r2 = requests.get(play_url, headers=headers, proxies={"http":None,"https":None}, timeout=(8,20))
    show("CASE2 绕过代理", r2)
except Exception as e:
    print(f"CASE2 异常: {type(e).__name__}: {e}")

# 案例3：裸 GET 抖音首页（看代理对该域的全局行为）
print("\n>>> CASE 3: 裸 GET 抖音首页(走代理)")
try:
    r3 = requests.get("https://www.douyin.com/", headers={'User-Agent':headers['User-Agent']}, timeout=(8,20))
    show("CASE3 抖音首页走代理", r3)
except Exception as e:
    print(f"CASE3 异常: {type(e).__name__}: {e}")
