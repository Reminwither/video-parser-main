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


def transcribe(audio_path: str, speaker_diarization: bool = False) -> list[dict]:
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
        "speaker_diarization": "1" if speaker_diarization else "0",
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
        out = []
        for item in sentences:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            rec = {
                "start": float(item.get("start_time") or 0) / 1000,
                "end": float(item.get("end_time") or 0) / 1000,
                "text": text,
            }
            sid = item.get("speaker_id")
            if sid is not None:
                rec["speaker_id"] = int(sid)
            out.append(rec)
        return out
    text = str(channel.get("text") or "").strip()
    if text:
        return [{"start": 0.0, "end": float(payload.get("audio_duration") or 0) / 1000, "text": text}]
    return []


def transcribe_with_speakers(audio_path: str, public_url: str | None = None) -> list[dict]:
    """腾讯云 ASR v3 CreateRecTask：极速版引擎 + 说话人分离。

    走官方 asr.tencentcloudapi.com，仅需 SecretId/SecretKey（无需 AppID）。
    16k_zh 极速版要求音频通过公网 Url 上传（SourceType=1）；本地文件仅在极少数
    支持 base64 的引擎下可回退（SourceType=0/Data）。返回句级带 speaker_id 的片段。
    """
    secret_id = os.getenv("TENCENT_ASR_SECRET_ID", "").strip()
    secret_key = os.getenv("TENCENT_ASR_SECRET_KEY", "").strip()
    if not (secret_id and secret_key):
        raise RuntimeError("腾讯云 ASR 说话人分离尚未配置 TENCENT_ASR_SECRET_ID / SECRET_KEY")

    path = Path(audio_path)
    if path.stat().st_size > 50 * 1024 * 1024:
        raise RuntimeError(
            "音频超过 50MB，腾讯云极速版按 base64/URL 上传有限制，长音频请先切片（后续支持 chunk 切片）。"
        )

    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.asr.v20190614 import asr_client, models

    engine = os.getenv("TENCENT_ASR_ENGINE", "16k_zh").strip() or "16k_zh"
    region = os.getenv("TENCENT_ASR_REGION", "ap-shanghai").strip()
    profile = ClientProfile(httpProfile=HttpProfile(endpoint="asr.tencentcloudapi.com"))
    client = asr_client.AsrClient(
        credential.Credential(secret_id, secret_key), region, profile
    )

    req = models.CreateRecTaskRequest()
    req.EngineModelType = engine
    req.ChannelNum = 1
    req.ResTextFormat = 0
    req.ConvertNumMode = 1
    req.FilterDirty = 0
    req.FilterModal = 0
    req.FilterPunc = 0
    req.SpeakerDiarization = 1
    # 16k 引擎不支持指定说话人人数，保持 SpeakerNumber=0（自动分离）
    if public_url:
        req.SourceType = 0
        req.Url = public_url
    else:
        req.SourceType = 1
        with path.open("rb") as audio_file:
            req.Data = base64.b64encode(audio_file.read()).decode("ascii")

    resp = client.CreateRecTask(req)
    task_id = resp.Data.TaskId

    deadline = time.time() + int(os.getenv("TENCENT_ASR_POLL_SECONDS", "300"))
    gr = None
    while True:
        if time.time() > deadline:
            raise RuntimeError("腾讯云 ASR 轮询超时，请稍后重试。")
        try:
            q = models.DescribeTaskStatusRequest()
            q.TaskId = task_id
            gr = client.DescribeTaskStatus(q)
        except Exception:  # noqa: BLE001 瞬时网络抖动重试，但不跳过超时判断
            time.sleep(1.0)
            continue
        status = gr.Data.Status
        if status == 2:
            break
        if status == 3:
            raise RuntimeError(f"腾讯云 ASR 识别失败: {gr.Data.ErrorMsg or '未知错误'}")
        time.sleep(1.5)

    result = gr.Data.Result
    rows: list[dict] = []
    for item in result.SentenceList or []:
        text = str(getattr(item, "Text", None) or "").strip()
        if not text:
            continue
        row = {
            "start": float(getattr(item, "StartTime", 0) or 0) / 1000,
            "end": float(getattr(item, "EndTime", 0) or 0) / 1000,
            "text": text,
        }
        sid = getattr(item, "SpeakerId", None)
        if sid is not None:
            row["speaker_id"] = int(sid)
        rows.append(row)
    return rows
