"""Tencent Cloud flash ASR for complete, timestamped audio transcripts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import requests


def transcribe(audio_path: str) -> list[dict]:
    app_id = os.getenv("TENCENT_ASR_APP_ID", "").strip()
    secret_id = os.getenv("TENCENT_ASR_SECRET_ID", "").strip()
    secret_key = os.getenv("TENCENT_ASR_SECRET_KEY", "").strip()
    if not all((app_id, secret_id, secret_key)):
        raise RuntimeError("腾讯云极速转写尚未配置 AppID、SecretID 和 SecretKey")

    path = Path(audio_path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise RuntimeError("提取后的音频超过腾讯云极速转写的 100 MB 上限")

    params = {
        "engine_type": os.getenv("TENCENT_ASR_ENGINE", "16k_zh").strip() or "16k_zh",
        "voice_format": "mp3",
        "timestamp": str(int(time.time())),
        "secretid": secret_id,
        "first_channel_only": "1",
        "filter_dirty": "0",
        "filter_modal": "0",
        "filter_punc": "0",
        "convert_num_mode": "1",
        "word_info": "0",
    }
    endpoint = f"asr.cloud.tencent.com/asr/flash/v1/{app_id}"
    query = urlencode(sorted(params.items()))
    signing_text = f"POST{endpoint}?{query}".encode("utf-8")
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), signing_text, hashlib.sha1).digest()
    ).decode("ascii")
    timeout = max(1, int(os.getenv("ASR_REQUEST_TIMEOUT_SECONDS", "120")))
    with path.open("rb") as audio_file:
        response = requests.post(
            f"https://{endpoint}?{query}",
            data=audio_file,
            headers={"Authorization": signature, "Content-Type": "application/octet-stream"},
            timeout=(10, timeout),
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(
            f"腾讯云极速转写失败：{payload.get('code')} {payload.get('message') or '未知错误'}"
        )
    channels = payload.get("flash_result") or []
    if not channels:
        return []
    channel = channels[0]
    sentences = channel.get("sentence_list") or []
    if sentences:
        return [
            {
                "start": float(item.get("start_time") or 0) / 1000,
                "end": float(item.get("end_time") or 0) / 1000,
                "text": str(item.get("text") or "").strip(),
            }
            for item in sentences
            if str(item.get("text") or "").strip()
        ]
    text = str(channel.get("text") or "").strip()
    if text:
        return [{"start": 0.0, "end": float(payload.get("audio_duration") or 0) / 1000, "text": text}]
    return []
