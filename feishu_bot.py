#!/usr/bin/env python3
"""
飞书机器人模块

功能：
- 通过 lark-oapi SDK 建立 WebSocket 长连接，订阅 im.message.receive_v1 事件
- 识别 @机器人 的消息，提取其中的 URL
- 立即回复"正在分析"卡片，异步派发分析任务
- 分析完成后更新卡片为完整分析报告
- 支持视频平台链接和文本类链接（公众号文章/通用网页）

架构：
    飞书事件 -> _on_message_receive (去重 + @识别 + URL提取)
             -> _send_card (loading)
             -> ThreadPoolExecutor.submit (_process_analysis_task)
                 -> content_fetcher.fetch_content
                 -> video_analysis / text_analyzer
                 -> _update_card (result)
"""

import os
import json
import re
import time
import datetime
from pathlib import Path
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Timer

from dotenv import load_dotenv
load_dotenv()

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from configs.logging_config import logger

# ==================== 配置 ====================

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BOT_OPEN_ID = os.getenv("FEISHU_BOT_OPEN_ID", "")

MAX_CONCURRENT_TASKS = int(os.getenv("FEISHU_MAX_CONCURRENT", "3"))
TASK_TIMEOUT_SECONDS = int(os.getenv("FEISHU_TASK_TIMEOUT_SECONDS", "900"))
FEISHU_API_TIMEOUT_SECONDS = float(os.getenv("FEISHU_API_TIMEOUT_SECONDS", "15"))
MODEL_TIMEOUT_SECONDS = float(os.getenv("MODEL_TIMEOUT_SECONDS", "180"))

# 飞书单条消息字符上限（留余量）
_MAX_CARD_CONTENT = 28000


# ==================== 全局单例 ====================

_client: lark.Client | None = None
_executor: ThreadPoolExecutor | None = None

# 事件去重 LRU 缓存
_event_cache: OrderedDict = OrderedDict()
_EVENT_CACHE_SIZE = 200
_event_cache_lock = Lock()


def _get_client() -> lark.Client:
    """获取或创建 lark API 客户端。"""
    global _client
    if _client is None:
        _client = (
            lark.Client.builder()
            .app_id(FEISHU_APP_ID)
            .app_secret(FEISHU_APP_SECRET)
            .timeout(FEISHU_API_TIMEOUT_SECONDS)
            .build()
        )
    return _client


def _get_executor() -> ThreadPoolExecutor:
    """获取或创建线程池。"""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_TASKS,
            thread_name_prefix="feishu-task",
        )
    return _executor


# ==================== 去重 ====================

def _is_duplicate(event_id: str) -> bool:
    """LRU 幂等去重，防止飞书重试导致重复处理。"""
    with _event_cache_lock:
        if event_id in _event_cache:
            return True
        _event_cache[event_id] = time.time()
        if len(_event_cache) > _EVENT_CACHE_SIZE:
            _event_cache.popitem(last=False)
        return False


# ==================== 卡片模板 ====================

def _build_loading_card(url: str) -> dict:
    transcript_only = os.getenv("FEISHU_VIDEO_MODE", "analysis").strip().lower() == "transcript"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "正在提取文字稿" if transcript_only else "正在分析"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**链接**：{url}\n\n正在获取视频并提取完整文字稿..." if transcript_only else f"**链接**：{url}\n\nAI 正在解析内容，请稍候...",
                },
            }
        ],
    }


def _build_progress_card(url: str, progress: float, stage: str) -> dict:
    percent = max(0, min(100, round(progress * 100)))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"正在分析 · {percent}%"},
            "template": "blue",
        },
        "elements": [{
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**链接**：{url}\n\n**当前阶段**：{stage}\n\n"
                    f"任务最长等待 {TASK_TIMEOUT_SECONDS} 秒；超时或异常会自动返回明确结果。"
                ),
            },
        }],
    }


def _build_result_card(url: str, analysis: str, content_type: str = "text") -> dict:
    type_label = "视频分析" if content_type == "video" else "内容分析"
    # 截断超长内容
    if len(analysis) > _MAX_CARD_CONTENT:
        analysis = analysis[:_MAX_CARD_CONTENT] + "\n\n...(内容过长已截断)"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"分析报告 - {type_label}"},
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**来源**：{url}\n\n---\n\n{analysis}",
                },
            }
        ],
    }


def _build_asr_loading_card(url: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "正在整理 ASR 原文"},
            "template": "blue",
        },
        "elements": [{
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**来源**：{url}\n\n正在修正断句、标点和明显识别错误...",
            },
        }],
    }


_ARCHIVE_ROOT = Path(os.getenv("ASR_ARCHIVE_DIR", r"E:\人生战略\视频内容整理"))


def _archive_name(title: str, video_id: str) -> str:
    name = (title or "").strip()
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name).strip(" _")
    if not name:
        name = (video_id or "").strip()
    if not name:
        name = f"video_{int(time.time())}"
    return name[:80]


def _build_archive_md(name, media, cues, cleaned, status, warning) -> str:
    from video_analysis import format_timestamp, format_asr_transcript_for_delivery

    def _f(sec):
        try:
            return format_timestamp(float(sec))
        except Exception:
            m, s = divmod(int(sec), 60)
            return f"{m:02d}:{s:02d}"

    dur = getattr(media, "duration", None)
    dur_str = _f(dur) if dur else "未知"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 文字稿：{name}\n",
        "> | 项 | 值 |",
        "> |---|---|",
        f"> | 来源 | 飞书分享 · 抖音 |",
        f"> | 时长 | {dur_str} |",
        f"> | 转写模型 | faster-whisper {os.getenv('ASR_MODEL_ID', '')} ({os.getenv('ASR_LANGUAGE', 'zh')}) |",
        f"> | 清洗模型 | {os.getenv('ASR_CLEAN_MODEL_ID', 'qwen2.5:7b')}（本地） |",
        f"> | 整理时间 | {now} |",
    ]
    if status:
        lines.append(f"> | 转写状态 | {status} |")
    if warning:
        lines.append(f"> | 备注 | {warning} |")
    lines.append("")

    readable = format_asr_transcript_for_delivery(cleaned)
    lines.append("## 正文\n")
    lines.append(readable.strip() if readable and readable.strip() else "（转写未产生可用文本）")
    lines.append("")

    if cues:
        lines.append("---\n")
        lines.append("### 原始时间戳稿（ASR 机器转写，未清洗）\n")
        lines.append("| 时间 | 文本 |")
        lines.append("|---|---|")
        for cue in cues:
            t = _f(getattr(cue, "start", 0))
            lines.append(f"| {t} | {getattr(cue, 'text', '').strip()} |")
        lines.append("")
    return "\n".join(lines)


def _refresh_archive_index(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        has_t = (d / "文字稿.md").exists()
        has_a = (d / "分析.md").exists()
        if not (has_t or has_a):
            continue
        tag = []
        if has_t:
            tag.append("[文字稿]")
        if has_a:
            tag.append("[分析]")
        entries.append(
            f"- **{d.name}** {' '.join(tag)} — "
            f"[文字稿]({d.name}/文字稿.md)"
            + (f" · [分析]({d.name}/分析.md)" if has_a else "")
        )
    idx = "# 视频内容整理 · 索引\n\n> 自动维护。P0 文字稿必出，P1 分析选出。\n\n" + (
        "\n".join(entries) if entries else "_（暂无整理记录）_") + "\n"
    idx_path = root / "索引.md"
    idx_path.write_text(idx, encoding="utf-8")
    return idx_path


def _archive_transcript_file(result: dict, media, cues, cleaned, status, warning) -> Path:
    name = _archive_name(result.get("title") or "", result.get("video_id") or "")
    md = _build_archive_md(name, media, cues, cleaned, status, warning)
    out_dir = _ARCHIVE_ROOT / name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "文字稿.md"
    out_file.write_text(md, encoding="utf-8")
    _refresh_archive_index(_ARCHIVE_ROOT)
    logger.info(f"[feishu] archived transcript -> {out_file}")
    return out_file


def _build_asr_card(url: str, transcript: str, part: int = 1, total: int = 1) -> dict:
    suffix = f"（{part}/{total}）" if total > 1 else ""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"ASR 转写原文{suffix}"},
            "template": "turquoise",
        },
        "elements": [{
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**来源**：{url}\n\n"
                    "> 完整机器转写，已按段落排版；专有名词和同音字可能需要校对。\n\n"
                    f"{transcript}"
                ),
            },
        }],
    }


def _build_bitable_card(base_url: str, record_id: str | None = None) -> dict:
    record_note = f"\n\n记录 ID：`{record_id}`" if record_id else ""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "已写入视频爆款分析库"},
            "template": "green",
        },
        "elements": [{
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "本次分析、爆款机制、结构化 JSON 与 ASR 逐字稿已新增为一行。"
                    f"{record_note}\n\n[打开飞书多维表格]({base_url})"
                ),
            },
        }],
    }


def _split_long_text(text: str, limit: int = 24000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines():
        line_length = len(line) + 1
        if current and current_length + line_length > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += line_length
    if current:
        chunks.append("\n".join(current))
    return chunks


def _build_error_card(url: str, error_msg: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "分析失败"},
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**链接**：{url}\n\n**错误**：{error_msg}",
                },
            }
        ],
    }


class TaskDeadlineExceeded(TimeoutError):
    pass


class _TaskGuard:
    """确保超时后卡片会结束 loading，且迟到结果不会覆盖超时提示。"""

    def __init__(self, url: str, message_id: str):
        self.url = url
        self.message_id = message_id
        self._lock = Lock()
        self._finished = False
        self._expired = False
        self._timer = Timer(TASK_TIMEOUT_SECONDS, self._expire)
        self._timer.daemon = True
        self._last_update = 0.0
        self._last_stage = ""

    def start(self):
        self._timer.start()

    def _expire(self):
        with self._lock:
            if self._finished:
                return
            self._expired = True
        transcript_only = os.getenv("FEISHU_VIDEO_MODE", "analysis").strip().lower() == "transcript"
        task_name = "文字稿提取" if transcript_only else "分析"
        logger.error(f"[feishu] {task_name} timeout after {TASK_TIMEOUT_SECONDS}s: {self.url}")
        _update_card(
            self.message_id,
            _build_error_card(
                self.url,
                f"{task_name}超过 {TASK_TIMEOUT_SECONDS} 秒，任务已超时。请稍后重试。",
            ),
        )

    def ensure_active(self):
        with self._lock:
            if self._expired:
                raise TaskDeadlineExceeded(f"任务超过 {TASK_TIMEOUT_SECONDS} 秒")

    def progress(self, value: float, stage: str):
        self.ensure_active()
        now = time.monotonic()
        # 阶段变化立即回写；同一阶段最多每 10 秒一次，避免触发飞书限流。
        if stage != self._last_stage or now - self._last_update >= 10:
            if _update_card(self.message_id, _build_progress_card(self.url, value, stage)):
                self._last_update = now
                self._last_stage = stage

    def update_result(self, card: dict) -> bool:
        with self._lock:
            if self._expired:
                return False
        return _update_card(self.message_id, card)

    def finish(self):
        with self._lock:
            self._finished = True
        self._timer.cancel()


def _build_help_card() -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "使用提示"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "请在消息中 **@我** 并附带一个链接，我会自动解析并生成分析报告。\n\n"
                        "**支持的链接类型**：\n"
                        "- 视频平台：抖音、B站、YouTube、小红书、快手、好看视频\n"
                        "- 微信公众号文章\n"
                        "- 通用网页/文章\n\n"
                        "**示例**：\n"
                        "```\n"
                        "@视频分析助手 https://mp.weixin.qq.com/s/xxxxx\n"
                        "```"
                    ),
                },
            }
        ],
    }


# ==================== 消息 API ====================

def _send_card(chat_id: str, card_content: dict) -> str | None:
    """发送卡片消息，返回 message_id。"""
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card_content, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = _get_client().im.v1.message.create(request)
    if response.success():
        return response.data.message_id
    else:
        logger.error(f"[feishu] send card failed: code={response.code}, msg={response.msg}")
        return None


def _update_card(message_id: str, card_content: dict) -> bool:
    """更新已有卡片消息内容。"""
    request = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps(card_content, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = _get_client().im.v1.message.patch(request)
    if not response.success():
        logger.error(f"[feishu] update card failed: code={response.code}, msg={response.msg}")
        return False
    return True


# ==================== 消息解析 ====================

def _is_bot_mentioned(data) -> bool:
    """检查机器人是否被 @。"""
    message = data.event.message
    mentions = message.mentions
    if not mentions:
        return False
    for mention in mentions:
        # mention.id 是 UserId 对象，有 open_id 属性
        if mention.id and mention.id.open_id == FEISHU_BOT_OPEN_ID:
            return True
    return False


def _extract_text_from_message(data) -> str:
    """
    从消息中提取纯文本（用于 URL 提取）。
    支持 text / post / share 等消息类型。
    """
    message = data.event.message
    msg_type = message.message_type
    raw_content = message.content or ""

    # content 是 JSON 字符串
    try:
        content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
    except (json.JSONDecodeError, TypeError):
        content = {}

    parts = []

    if msg_type == "text":
        parts.append(content.get("text", ""))

    elif msg_type == "post":
        # 富文本：{"title": "...", "content": [[{...}, ...], ...]}
        parts.append(content.get("title", ""))
        body = content.get("content", [])
        for paragraph in body:
            if isinstance(paragraph, list):
                for element in paragraph:
                    if isinstance(element, dict):
                        parts.append(element.get("text", ""))
            elif isinstance(paragraph, dict):
                parts.append(paragraph.get("text", ""))

    elif msg_type == "share":
        # 分享卡片：尝试从 url 字段提取
        share_url = content.get("url", "")
        if share_url:
            parts.append(share_url)
        # 有时 URL 在其他字段
        for key in ("href", "link", "share_url"):
            val = content.get(key, "")
            if val:
                parts.append(val)

    else:
        # 其他类型：降级为原始 content 字符串
        parts.append(raw_content)

    return " ".join(p for p in parts if p)


def _extract_url(data) -> str | None:
    """从消息中提取 URL，复用现有 UrlParser。"""
    from utils.web_fetcher import UrlParser
    text = _extract_text_from_message(data)
    return UrlParser.get_url(text)


# ==================== 异步分析任务 ====================

def _process_analysis_task(
    url: str,
    chat_id: str,
    message_id: str,
    guard: _TaskGuard | None = None,
    viewer_open_id: str = "",
):
    """
    后台线程执行：抓取内容 -> AI 分析 -> 更新卡片。
    """
    video_path = None
    report_sent = False
    archive = None
    asr_delivery = ""
    guard = guard or _TaskGuard(url, message_id)
    try:
        from content_fetcher import fetch_content

        logger.info(f"[feishu] analysis started: {url}")

        # 1) 抓取内容
        guard.progress(0.02, "解析链接并获取平台结构化数据")
        result = fetch_content(url)
        if result is None:
            guard.update_result(_build_error_card(url, "无法解析该链接，平台请求失败或链接类型不受支持"))
            return

        # 2) AI 分析
        if result["type"] == "video":
            from video_analysis import analyze_video_evidence_first, format_asr_transcript_for_delivery
            from openai import OpenAI

            video_path = result["video_path"]
            if os.getenv("FEISHU_VIDEO_MODE", "analysis").strip().lower() == "transcript":
                from tempfile import TemporaryDirectory
                from video_analysis import (
                    inspect_video, transcribe_audio, clean_asr_transcript,
                    format_asr_transcript_for_delivery,
                )

                # 仅腾讯云后端需要云 API 凭据；faster-whisper GPU 模式无需配置。
                guard.progress(0.10, "视频已下载，正在提取音频")
                media = inspect_video(video_path)
                with TemporaryDirectory(prefix="feishu_transcript_") as temp_dir:
                    cues, status, warning = transcribe_audio(
                        video_path, media, temp_dir, progress=guard.progress
                    )
                if status != "timestamped" or not cues or warning and "仅转写前" in warning:
                    raise RuntimeError(warning or f"完整文字稿获取失败：{status}")

                # LLM 清洗：修正错别字/专有名词，并按语义话题用空行分段。
                guard.progress(0.93, "正在清洗并整理文字稿")
                clean_model = os.getenv("ASR_CLEAN_MODEL_ID", "").strip() or os.getenv(
                    "QWEN_MODEL_ID", ""
                ).strip()
                if clean_model:
                    clean_client = OpenAI(
                        base_url=os.getenv("QWEN_API_BASE_URL", "http://localhost:11434/v1"),
                        api_key=os.getenv("QWEN_API_KEY", "ollama"),
                    )
                    cleaned = clean_asr_transcript(clean_client, clean_model, cues)
                else:
                    cleaned = "\n".join(cue.text for cue in cues)
                guard.progress(0.95, "文字稿已整理完成，正在发送")
                parts = _split_long_text(format_asr_transcript_for_delivery(cleaned))
                if not guard.update_result(_build_asr_card(url, parts[0], 1, len(parts))):
                    logger.warning(f"[feishu] late transcript discarded after timeout: {url}")
                    return
                for index, part in enumerate(parts[1:], start=2):
                    guard.ensure_active()
                    if not _send_card(chat_id, _build_asr_card(url, part, index, len(parts))):
                        raise RuntimeError(f"文字稿第 {index}/{len(parts)} 部分发送失败")
                guard.finish()
                logger.info(f"[feishu] full transcript sent: parts={len(parts)}, url={url}")
                try:
                    _archive_transcript_file(result, media, cues, cleaned, status, warning)
                except Exception as _ae:
                    logger.warning(f"[feishu] transcript 归档失败: {_ae}")
                return
            guard.progress(0.10, "视频已下载，正在读取音轨、字幕与画面时间轴")

            qwen_client = OpenAI(
                base_url=os.getenv("QWEN_API_BASE_URL"),
                api_key=os.getenv("QWEN_API_KEY"),
                timeout=MODEL_TIMEOUT_SECONDS,
                max_retries=1,
            )

            analysis, evidence = analyze_video_evidence_first(
                video_path,
                qwen_client,
                os.getenv("QWEN_MODEL_ID"),
                title=result.get("title"),
                video_info=result.get("metadata"),
                progress=guard.progress,
            )
            content_type = "video"
            if evidence.cleaned_transcript.strip():
                asr_delivery = format_asr_transcript_for_delivery(evidence.cleaned_transcript)

            # 报告内容同时作为稳定 JSON 入库，供后续 AI 做跨视频爆款研究。
            try:
                from feishu_bitable import archive_video_analysis

                guard.progress(0.97, "正在把结构化分析归档到飞书多维表格")
                archive = archive_video_analysis(
                    url=url,
                    metadata=result.get("metadata") or {},
                    payload=evidence.analysis_payload,
                    cleaned_asr=asr_delivery,
                    duration_seconds=evidence.media.duration,
                    viewer_open_id=viewer_open_id,
                )
                if archive.get("base_url"):
                    analysis += f"\n\n---\n\n**已归档**：[打开视频爆款分析库]({archive['base_url']})"
                logger.info(f"[feishu] bitable archived: record_id={archive.get('record_id')}")
            except Exception as archive_error:
                logger.error(f"[feishu] bitable archive failed for {url}: {archive_error}")
                analysis += "\n\n---\n\n**归档状态**：多维表格写入失败，报告与 ASR 不受影响。"
        else:
            from text_analyzer import analyze_text_content

            analysis = analyze_text_content(
                title=result.get("title", ""),
                content=result.get("content", ""),
                url=url,
            )
            content_type = "text"

        # 3) 更新卡片
        if not guard.update_result(_build_result_card(url, analysis, content_type)):
            logger.warning(f"[feishu] late result discarded after timeout: {url}")
            return
        report_sent = True
        guard.finish()

        if archive and archive.get("base_url"):
            _send_card(
                chat_id,
                _build_bitable_card(archive["base_url"], archive.get("record_id")),
            )

        # ASR 是独立交付物：报告先返回，再单独发送清洗后的完整转写。
        if content_type == "video" and evidence.transcript:
            from video_analysis import clean_asr_transcript, format_asr_transcript_for_delivery

            asr_message_id = _send_card(chat_id, _build_asr_loading_card(url))
            if asr_message_id:
                try:
                    cleaned_asr = asr_delivery
                    if not cleaned_asr:
                        timestamped_asr = evidence.cleaned_transcript or clean_asr_transcript(
                            qwen_client,
                            os.getenv("ASR_CLEAN_MODEL_ID", "").strip()
                            or os.getenv("SYNTHESIS_MODEL_ID", "").strip()
                            or os.getenv("QWEN_MODEL_ID"),
                            evidence.transcript,
                        )
                        cleaned_asr = format_asr_transcript_for_delivery(timestamped_asr)
                    parts = _split_long_text(cleaned_asr)
                    _update_card(asr_message_id, _build_asr_card(url, parts[0], 1, len(parts)))
                    for index, part in enumerate(parts[1:], start=2):
                        _send_card(chat_id, _build_asr_card(url, part, index, len(parts)))
                    logger.info(f"[feishu] ASR transcript sent: parts={len(parts)}, url={url}")
                except Exception as asr_error:
                    logger.error(f"[feishu] ASR cleanup/send failed for {url}: {asr_error}")
                    _update_card(
                        asr_message_id,
                        _build_error_card(url, f"ASR 原文整理失败: {asr_error}"),
                    )
        logger.info(f"[feishu] analysis completed: {url}")

    except TaskDeadlineExceeded as e:
        logger.warning(f"[feishu] task stopped at deadline: {url}: {e}")
    except Exception as e:
        logger.error(f"[feishu] analysis failed for {url}: {e}")
        import traceback
        traceback.print_exc()
        error_card = _build_error_card(url, f"{type(e).__name__}: {e}")
        if report_sent:
            _send_card(chat_id, error_card)
        else:
            guard.update_result(error_card)

    finally:
        guard.finish()
        # 清理临时视频文件
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
                logger.info(f"[feishu] cleaned up video: {video_path}")
            except Exception:
                pass


# ==================== 事件处理 ====================

def _on_message_receive(data) -> None:
    """
    im.message.receive_v1 事件处理器。
    在 lark-oapi SDK 的 WebSocket 线程中同步调用。
    """
    try:
        event_id = data.header.event_id

        # 去重
        if _is_duplicate(event_id):
            logger.info(f"[feishu] duplicate event skipped: {event_id}")
            return

        message = data.event.message
        chat_id = message.chat_id
        chat_type = message.chat_type  # "p2p" 或 "group"
        sender = getattr(data.event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        viewer_open_id = getattr(sender_id, "open_id", "") or ""

        # 群聊中必须 @机器人才处理
        if chat_type == "group":
            if not _is_bot_mentioned(data):
                return

        logger.info(f"[feishu] message received: chat_id={chat_id}, type={message.message_type}")

        # 提取 URL
        url = _extract_url(data)

        if not url:
            _send_card(chat_id, _build_help_card())
            return

        logger.info(f"[feishu] extracted URL: {url}")

        # 发送"正在分析"卡片
        loading_msg_id = _send_card(chat_id, _build_loading_card(url))
        if not loading_msg_id:
            logger.error("[feishu] failed to send loading card, abort")
            return

        # 异步派发分析任务
        guard = _TaskGuard(url, loading_msg_id)
        guard.start()
        try:
            _get_executor().submit(
                _process_analysis_task,
                url,
                chat_id,
                loading_msg_id,
                guard,
                viewer_open_id,
            )
        except Exception as exc:
            guard.finish()
            _update_card(loading_msg_id, _build_error_card(url, f"后台任务提交失败: {exc}"))

    except Exception as e:
        logger.error(f"[feishu] event handler error: {e}")
        import traceback
        traceback.print_exc()


# ==================== 启动入口 ====================

def start_feishu_bot():
    """
    启动飞书机器人 WebSocket 长连接（阻塞，应在后台线程中调用）。
    """
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        logger.warning("[feishu] credentials not configured, bot not started")
        return

    logger.info("[feishu] starting bot...")

    try:
        # 注册事件处理器
        # builder() 需要两个参数：encrypt_key, verification_token
        # 长连接模式下两者均可为空字符串
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message_receive)
            .build()
        )

        # 创建 WebSocket 客户端
        ws_client = lark.ws.Client(
            FEISHU_APP_ID,
            FEISHU_APP_SECRET,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # 阻塞启动（SDK 内部自动处理重连）
        ws_client.start()
    except Exception as e:
        logger.error(f"[feishu] bot crashed: {e}")
        import traceback
        traceback.print_exc()

    logger.info("[feishu] bot stopped")
