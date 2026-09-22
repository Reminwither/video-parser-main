#!/usr/bin/env python3
"""Evidence-first multimodal video analysis pipeline.

The module deliberately separates extraction from interpretation:

    media inspection -> timed visual/audio evidence -> evidence reconstruction
    -> summary/critique/application

It never treats a page title as video evidence and never claims to have heard
speech when neither a subtitle track nor an ASR transcript is available.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence

from viral_distillation import compact_signal_context, normalize_platform_evidence

if TYPE_CHECKING:
    from openai import OpenAI


ProgressCallback = Optional[Callable[[float, str], None]]


@dataclass
class TimedText:
    start: float
    end: float
    text: str
    source: str


@dataclass
class FrameSample:
    timestamp: float
    path: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class MediaInspection:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    format_name: str = "unknown"
    video_codec: str = "unknown"
    audio_codec: Optional[str] = None
    audio_tracks: int = 0
    subtitle_tracks: list[dict] = field(default_factory=list)


@dataclass
class EvidenceBundle:
    media: MediaInspection
    frames: list[FrameSample]
    subtitles: list[TimedText]
    transcript: list[TimedText]
    transcript_status: str
    cleaned_transcript: str = ""
    visual_observations: str = ""
    analysis_payload: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _enter_asr_low_priority() -> Optional[Callable[[], None]]:
    """Lower this process while local ASR runs so the desktop stays responsive."""
    if os.name != "nt" or os.getenv("ASR_LOW_PRIORITY", "1").strip().lower() not in {"1", "true", "yes"}:
        return None


def _configure_cuda_dll_paths() -> None:
    """Make pip-installed CUDA 12 libraries visible to CTranslate2 on Windows."""
    if os.name != "nt":
        return
    site_packages = Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages"
    if not site_packages.exists():
        site_packages = Path(os.sys.prefix) / "Lib" / "site-packages"
    nvidia = site_packages / "nvidia"
    paths = [nvidia / "cuda_runtime" / "bin", nvidia / "cuda_nvrtc" / "bin", nvidia / "cublas" / "bin", nvidia / "cudnn" / "bin"]
    existing = os.environ.get("PATH", "").split(os.pathsep)
    for path in paths:
        if path.exists():
            path_text = str(path)
            if path_text not in existing:
                os.environ["PATH"] = path_text + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(path_text)
            except (AttributeError, OSError):
                pass
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        if not kernel32.SetPriorityClass(handle, 0x00004000):  # BELOW_NORMAL_PRIORITY_CLASS
            return None

        def restore() -> None:
            try:
                kernel32.SetPriorityClass(handle, 0x00000020)  # NORMAL_PRIORITY_CLASS
            except Exception:
                pass

        return restore
    except Exception:
        return None


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def _parse_rate(value: str) -> float:
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator) if float(denominator) else 0.0
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def inspect_video(video_path: str) -> MediaInspection:
    """Read container, video, audio and subtitle metadata with ffprobe."""
    command = [
        os.getenv("FFPROBE_PATH", "ffprobe"),
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        video_path,
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe 无法读取视频: {completed.stderr.strip() or '未知错误'}")

    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    subtitle_streams = [item for item in streams if item.get("codec_type") == "subtitle"]
    duration = payload.get("format", {}).get("duration") or video_stream.get("duration") or 0

    return MediaInspection(
        duration=float(duration or 0),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=_parse_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0"),
        format_name=payload.get("format", {}).get("format_name") or "unknown",
        video_codec=video_stream.get("codec_name") or "unknown",
        audio_codec=(audio_streams[0].get("codec_name") if audio_streams else None),
        audio_tracks=len(audio_streams),
        subtitle_tracks=[
            {
                "index": int(item.get("index", 0)),
                "codec": item.get("codec_name") or "unknown",
                "language": (item.get("tags") or {}).get("language") or "unknown",
                "title": (item.get("tags") or {}).get("title") or "",
            }
            for item in subtitle_streams
        ],
    )


_SRT_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+\s*\n)?"
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}).*?\n"
    r"(?P<text>.*?)(?=\n\s*\n|\Z)",
    re.DOTALL,
)


def _subtitle_seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_srt(text: str, source: str = "subtitle_track") -> list[TimedText]:
    cues: list[TimedText] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    for match in _SRT_BLOCK_RE.finditer(normalized):
        cue_text = re.sub(r"<[^>]+>", "", match.group("text"))
        cue_text = " ".join(line.strip() for line in cue_text.splitlines() if line.strip())
        if cue_text:
            cues.append(TimedText(
                start=_subtitle_seconds(match.group("start")),
                end=_subtitle_seconds(match.group("end")),
                text=cue_text,
                source=source,
            ))
    return cues


def extract_subtitle_track(
    video_path: str,
    media: MediaInspection,
    temp_dir: str,
) -> tuple[list[TimedText], Optional[str]]:
    """Extract the first text-compatible embedded subtitle stream."""
    if not media.subtitle_tracks:
        return [], "未检测到独立字幕轨；烧录字幕仍会由视觉帧读取"

    errors: list[str] = []
    for track in media.subtitle_tracks:
        output_path = os.path.join(temp_dir, f"subtitle_{track['index']}.srt")
        command = [
            os.getenv("FFMPEG_PATH", "ffmpeg"), "-y", "-v", "error",
            "-i", video_path,
            "-map", f"0:{track['index']}", output_path,
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        if completed.returncode == 0 and os.path.exists(output_path):
            raw = Path(output_path).read_text(encoding="utf-8", errors="replace")
            cues = parse_srt(raw, source=f"subtitle_track:{track['language']}")
            if cues:
                return cues, None
        errors.append(f"stream {track['index']} ({track['codec']})")
    return [], f"字幕轨无法转换为文本（{', '.join(errors)}）；可能是图片字幕"


def detect_change_timestamps(
    video_path: str,
    threshold: float,
    duration: float,
    prefilter: Optional[str] = None,
) -> list[float]:
    """Detect image changes; failure is non-fatal because baseline sampling remains."""
    threshold = min(max(threshold, 0.05), 0.95)
    detection_fps = _env_float("CHANGE_DETECTION_FPS", 2.0, 0.5)
    filters = [f"fps={detection_fps:.3f}"]
    if prefilter:
        filters.append(prefilter)
    filters.extend([f"select='gt(scene,{threshold:.3f})'", "showinfo"])
    filter_expr = ",".join(filters)
    command = [
        os.getenv("FFMPEG_PATH", "ffmpeg"), "-hide_banner", "-i", video_path,
        "-vf", filter_expr, "-an", "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    timestamps = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr)]
    return sorted({round(value, 3) for value in timestamps if 0 <= value <= duration})


def detect_scene_timestamps(video_path: str, threshold: float, duration: float) -> list[float]:
    return detect_change_timestamps(video_path, threshold, duration)


def detect_text_region_change_timestamps(video_path: str, threshold: float, duration: float) -> list[float]:
    """Find lower-screen changes likely to include burned subtitles or UI text.

    This is deliberately called a *candidate* detector: motion in the lower part
    of the image may also trigger it. The vision stage decides whether text is
    actually present.
    """
    return detect_change_timestamps(
        video_path,
        threshold,
        duration,
        prefilter="crop=iw:ih*0.45:0:ih*0.55",
    )


def _merge_candidates(candidates: Iterable[tuple[float, str]], duration: float) -> list[tuple[float, set[str]]]:
    merged: list[tuple[float, set[str]]] = []
    for timestamp, reason in sorted(candidates, key=lambda item: item[0]):
        timestamp = min(max(float(timestamp), 0.0), max(duration, 0.0))
        if merged and abs(timestamp - merged[-1][0]) <= 0.25:
            old_timestamp, reasons = merged[-1]
            reasons.add(reason)
            merged[-1] = ((old_timestamp + timestamp) / 2, reasons)
        else:
            merged.append((timestamp, {reason}))
    return merged


def choose_sample_timestamps(
    duration: float,
    scene_timestamps: Sequence[float],
    subtitle_cues: Sequence[TimedText],
    interval: float,
    max_frames: int,
    text_region_timestamps: Sequence[float] = (),
) -> list[tuple[float, set[str]]]:
    """Combine baseline, scene-change and text-change sampling with a hard cap."""
    if duration <= 0:
        return [(0.0, {"baseline"})]

    interval = max(0.5, interval)
    baseline = [min(duration, index * interval) for index in range(int(math.floor(duration / interval)) + 1)]
    if not baseline or duration - baseline[-1] > 0.35:
        baseline.append(max(0.0, duration - 0.05))

    candidates: list[tuple[float, str]] = [(value, "baseline") for value in baseline]
    for value in scene_timestamps:
        candidates.append((value, "scene_change"))
        candidates.append((value - 0.5, "scene_context"))
        candidates.append((value + 0.5, "scene_context"))
    for value in text_region_timestamps:
        candidates.append((value, "text_region_change"))
        candidates.append((value - 0.35, "text_region_context"))
        candidates.append((value + 0.35, "text_region_context"))
    for cue in subtitle_cues:
        candidates.append((cue.start, "subtitle_change"))
        if cue.end - cue.start >= 2.0:
            candidates.append(((cue.start + cue.end) / 2, "subtitle_context"))

    merged = _merge_candidates(candidates, duration)
    if len(merged) <= max_frames:
        return merged

    # Farthest-point selection preserves coverage while slightly preferring cuts/text.
    selected = [merged[0]]
    if merged[-1][0] - merged[0][0] > 0.25:
        selected.append(merged[-1])
    bonus = {
        "scene_change": interval * 0.65,
        "scene_context": interval * 0.25,
        "text_region_change": interval * 0.55,
        "text_region_context": interval * 0.15,
        "subtitle_change": interval * 0.5,
        "subtitle_context": interval * 0.2,
    }
    while len(selected) < max_frames:
        remaining = [item for item in merged if item not in selected]
        if not remaining:
            break

        def score(item: tuple[float, set[str]]) -> float:
            distance = min(abs(item[0] - chosen[0]) for chosen in selected)
            return distance + max((bonus.get(reason, 0.0) for reason in item[1]), default=0.0)

        selected.append(max(remaining, key=score))
    return sorted(selected, key=lambda item: item[0])


def extract_timeline_frames(
    video_path: str,
    media: MediaInspection,
    subtitle_cues: Sequence[TimedText],
    temp_dir: str,
    max_frames: Optional[int] = None,
) -> tuple[list[FrameSample], int, int]:
    interval = _env_float("FRAME_INTERVAL_SECONDS", 2.0, 0.5)
    limit = max_frames or _env_int("MAX_ANALYSIS_FRAMES", 24)
    threshold = _env_float("SCENE_CHANGE_THRESHOLD", 0.32, 0.05)
    scene_timestamps = detect_scene_timestamps(video_path, threshold, media.duration)
    text_threshold = _env_float("TEXT_REGION_CHANGE_THRESHOLD", 0.10, 0.05)
    text_region_timestamps = detect_text_region_change_timestamps(video_path, text_threshold, media.duration)
    samples = choose_sample_timestamps(
        media.duration,
        scene_timestamps,
        subtitle_cues,
        interval,
        limit,
        text_region_timestamps=text_region_timestamps,
    )

    frames: list[FrameSample] = []
    for index, (timestamp, reasons) in enumerate(samples):
        output_path = os.path.join(temp_dir, f"frame_{index:03d}_{int(timestamp * 1000):010d}.jpg")
        command = [
            os.getenv("FFMPEG_PATH", "ffmpeg"), "-y", "-v", "error",
            "-ss", f"{timestamp:.3f}", "-i", video_path,
            "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3", output_path,
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        if completed.returncode == 0 and os.path.exists(output_path):
            frames.append(FrameSample(timestamp=timestamp, path=output_path, reasons=sorted(reasons)))
    return frames, len(scene_timestamps), len(text_region_timestamps)


def _object_to_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {}


def _transcribe_with_faster_whisper(
    audio_files: Sequence[Path],
    media: MediaInspection,
    model_name: str,
    chunk_seconds: float,
    use_chunks: bool,
    progress: ProgressCallback = None,
) -> tuple[list[TimedText], str, Optional[str]]:
    """Run local faster-whisper with GPU-first, CPU-safe fallback."""
    _configure_cuda_dll_paths()
    try:
        from faster_whisper import WhisperModel, BatchedInferencePipeline
    except ImportError:
        return [], "backend_missing", "已选择 faster-whisper，但当前 Python 环境尚未安装该依赖"

    requested_device = os.getenv("ASR_DEVICE", "auto").strip() or "auto"
    requested_compute = os.getenv("ASR_COMPUTE_TYPE", "auto").strip() or "auto"
    language = os.getenv("ASR_LANGUAGE", "").strip() or None
    beam_size = _env_int("ASR_BEAM_SIZE", 5)

    if requested_device == "cuda":
        try:
            probe = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            free_mb = int(probe.stdout.strip().splitlines()[0]) if probe.returncode == 0 else None
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            free_mb = None
        min_free_mb = _env_int("ASR_GPU_MIN_FREE_MB", 4000)
        if free_mb is not None and free_mb < min_free_mb:
            return [], "asr_failed", (
                f"GPU 剩余显存只有 {free_mb} MB，需要至少 {min_free_mb} MB 才能启动当前转写模型；"
                "请先关闭占用显存的应用后重试"
            )

    def run(device: str, compute_type: str) -> list[TimedText]:
        runtime_options = {}
        if device == "cpu":
            runtime_options["cpu_threads"] = _env_int("ASR_CPU_THREADS", max(1, os.cpu_count() or 4))
        runtime_options["num_workers"] = _env_int("ASR_NUM_WORKERS", 1)
        whisper_model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            **runtime_options,
        )
        whisper = BatchedInferencePipeline(model=whisper_model)
        output: list[TimedText] = []
        total_chunks = max(1, len(audio_files))
        batch_size = _env_int("ASR_BATCH_SIZE", 8)
        for chunk_index, chunk_path in enumerate(audio_files):
            if progress:
                progress(
                    0.14 + 0.10 * (chunk_index / total_chunks),
                    f"正在进行语音识别（{chunk_index + 1}/{total_chunks} 段）",
                )
            offset = chunk_index * chunk_seconds if use_chunks else 0.0
            segments, _ = whisper.transcribe(
                str(chunk_path),
                language=language,
                beam_size=beam_size,
                vad_filter=True,
                condition_on_previous_text=os.getenv("ASR_CONDITION_ON_PREVIOUS_TEXT", "1").strip() == "1",
                batch_size=batch_size,
            )
            for segment in segments:
                text = str(segment.text or "").strip()
                if text:
                    output.append(TimedText(
                        start=offset + float(segment.start),
                        end=min(media.duration, offset + float(segment.end)),
                        text=text,
                        source="asr:faster-whisper",
                    ))
            if progress:
                progress(
                    0.14 + 0.10 * ((chunk_index + 1) / total_chunks),
                    f"正在进行语音识别（已完成 {chunk_index + 1}/{total_chunks} 段）",
                )
        return output

    restore_priority = _enter_asr_low_priority()
    try:
        try:
            cues = run(requested_device, requested_compute)
            fallback_warning = None
        except Exception as primary_error:
            allow_cpu_fallback = os.getenv("ASR_ALLOW_CPU_FALLBACK", "0").strip() == "1"
            if requested_device == "cpu" or (requested_device == "cuda" and not allow_cpu_fallback):
                return [], "asr_failed", f"faster-whisper 转写失败: {primary_error}"
            try:
                cues = run("cpu", "int8")
                fallback_warning = f"GPU ASR 不可用，已自动回退 CPU int8：{primary_error}"
            except Exception as fallback_error:
                return [], "asr_failed", (
                    f"faster-whisper GPU/自动模式失败: {primary_error}；"
                    f"CPU 回退也失败: {fallback_error}"
                )
    finally:
        if restore_priority:
            restore_priority()

    if cues:
        return cues, "timestamped", fallback_warning
    return [], "empty", fallback_warning or "faster-whisper 未识别到可用语音"


def transcribe_audio(
    video_path: str,
    media: MediaInspection,
    temp_dir: str,
    progress: ProgressCallback = None,
) -> tuple[list[TimedText], str, Optional[str]]:
    """Run optional OpenAI-compatible ASR and retain segment timestamps."""
    if not media.audio_tracks:
        return [], "no_audio", "视频没有音轨"

    model = os.getenv("ASR_MODEL_ID", "").strip()
    if not model:
        return [], "not_configured", "检测到音轨，但未配置 ASR_MODEL_ID；不会臆造口播内容"

    backend = os.getenv("ASR_BACKEND", "openai").strip().lower()
    chunk_seconds = _env_float("ASR_CHUNK_SECONDS", 0.0, 0.0)
    max_duration = _env_float("ASR_MAX_DURATION_SECONDS", 0.0, 0.0)
    transcription_duration = min(media.duration, max_duration) if max_duration > 0 else media.duration
    truncated = max_duration > 0 and media.duration > max_duration
    cache_key = _asr_cache_key(video_path, backend, model, chunk_seconds, max_duration)
    cached = _load_asr_cache(cache_key)
    if cached:
        return cached

    use_chunks = chunk_seconds > 0 and transcription_duration > chunk_seconds
    audio_path = os.path.join(temp_dir, "audio_%05d.wav" if use_chunks else "audio.wav")
    command = [
        os.getenv("FFMPEG_PATH", "ffmpeg"), "-y", "-v", "error",
        "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
        "-af", "loudnorm", "-c:a", "pcm_s16le",
    ]
    if truncated:
        command.extend(["-t", f"{max_duration:.3f}"])
    if use_chunks:
        command.extend([
            "-f", "segment", "-segment_format", "wav",
            "-segment_time", f"{chunk_seconds:.3f}",
            "-reset_timestamps", "1", audio_path,
        ])
    else:
        command.extend(["-f", "wav", audio_path])
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600
    )
    audio_files = sorted(Path(temp_dir).glob("audio_*.wav")) if use_chunks else [Path(audio_path)]
    audio_files = [item for item in audio_files if item.exists() and item.stat().st_size > 0]
    if completed.returncode != 0 or not audio_files:
        return [], "extract_failed", f"音轨分离失败: {completed.stderr.strip() or '未知错误'}"

    truncation_warning = (
        f"视频时长 {media.duration / 60:.1f} 分钟，出于性能保护仅转写前 {max_duration / 60:.1f} 分钟"
        if truncated else None
    )
    if progress:
        progress(0.14, f"音轨已提取，共 {len(audio_files)} 段，开始语音识别")

    if backend in {"tencent-flash", "tencent_flash"}:
        from tencent_flash_asr import transcribe

        if len(audio_files) != 1:
            return [], "asr_failed", "腾讯云极速转写需要单个音频文件，请将 ASR_CHUNK_SECONDS 设为 0"
        if progress:
            progress(0.45, "音频已提取，正在极速转写完整音频")
        try:
            sentences = transcribe(str(audio_files[0]))
        except Exception as exc:
            return [], "asr_failed", str(exc)
        cues = [TimedText(item["start"], item["end"], item["text"], "asr:tencent-flash") for item in sentences]
        if not cues:
            return [], "empty", "云端未识别到语音"
        result = (cues, "timestamped", truncation_warning)
        _save_asr_cache(cache_key, result)
        return result

    if backend in {"faster-whisper", "faster_whisper", "local"}:
        result = _transcribe_with_faster_whisper(
            audio_files, media, model, chunk_seconds, use_chunks, progress=progress
        )
        if truncation_warning:
            result = (result[0], result[1], "; ".join(item for item in (truncation_warning, result[2]) if item))
        _save_asr_cache(cache_key, result)
        return result

    from openai import OpenAI

    client = OpenAI(
        base_url=os.getenv("ASR_API_BASE_URL") or os.getenv("QWEN_API_BASE_URL"),
        api_key=os.getenv("ASR_API_KEY") or os.getenv("QWEN_API_KEY") or "not-required",
    )
    cues: list[TimedText] = []
    errors: list[str] = []
    coarse_chunks = 0
    asr_language = os.getenv("ASR_LANGUAGE", "").strip()
    total_chunks = max(1, len(audio_files))
    for chunk_index, chunk_path in enumerate(audio_files):
        if progress:
            progress(
                0.14 + 0.10 * (chunk_index / total_chunks),
                f"正在进行语音识别（{chunk_index + 1}/{total_chunks} 段）",
            )
        offset = chunk_index * chunk_seconds if use_chunks else 0.0
        try:
            with open(chunk_path, "rb") as audio_file:
                request_options = {
                    "model": model,
                    "file": audio_file,
                    "response_format": "verbose_json",
                }
                if asr_language:
                    request_options["language"] = asr_language
                try:
                    response = client.audio.transcriptions.create(
                        **request_options,
                        timestamp_granularities=["segment"],
                    )
                except Exception:
                    audio_file.seek(0)
                    response = client.audio.transcriptions.create(**request_options)
        except Exception as exc:
            errors.append(f"第 {chunk_index + 1} 段: {exc}")
            continue

        payload = _object_to_dict(response)
        segments = payload.get("segments") or getattr(response, "segments", None) or []
        segment_count_before = len(cues)
        for segment in segments:
            item = _object_to_dict(segment)
            text = str(item.get("text") or "").strip()
            if text:
                start = offset + float(item.get("start") or 0)
                end = offset + float(item.get("end") or item.get("start") or 0)
                cues.append(TimedText(start=start, end=end, text=text, source="asr"))

        if len(cues) == segment_count_before:
            text = str(payload.get("text") or getattr(response, "text", "") or "").strip()
            if text:
                coarse_chunks += 1
                end = min(media.duration, offset + (chunk_seconds if use_chunks else media.duration))
                cues.append(TimedText(offset, end, text, "asr_chunk" if use_chunks else "asr_coarse"))
        if progress:
            progress(
                0.14 + 0.10 * ((chunk_index + 1) / total_chunks),
                f"正在进行语音识别（已完成 {chunk_index + 1}/{total_chunks} 段）",
            )

    if cues:
        if errors:
            warning = "部分 ASR 分块失败：" + "；".join(errors)
            if truncation_warning:
                warning = f"{truncation_warning}；{warning}"
            result = (cues, "partial", warning)
            _save_asr_cache(cache_key, result)
            return result
        if coarse_chunks:
            if use_chunks:
                warning = f"ASR 未返回句级时间戳；已按 {chunk_seconds:g} 秒音频分块对齐"
                if truncation_warning:
                    warning = f"{truncation_warning}；{warning}"
                result = (cues, "chunk_timestamped", warning)
                _save_asr_cache(cache_key, result)
                return result
            warning = "ASR 返回了文本，但没有分段时间戳"
            if truncation_warning:
                warning = f"{truncation_warning}；{warning}"
            result = (cues, "coarse", warning)
            _save_asr_cache(cache_key, result)
            return result
        result = (cues, "timestamped", truncation_warning)
        _save_asr_cache(cache_key, result)
        return result

    if errors:
        warning = "ASR 调用失败：" + "；".join(errors)
        if truncation_warning:
            warning = f"{truncation_warning}；{warning}"
        return [], "asr_failed", warning
    return [], "empty", truncation_warning or "ASR 未返回可用文本"


def _asr_cache_key(
    video_path: str,
    backend: str,
    model: str,
    chunk_seconds: float,
    max_duration: float = 0.0,
) -> str:
    digest = hashlib.sha256()
    with open(video_path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    language = os.getenv("ASR_LANGUAGE", "").strip()
    beam_size = os.getenv("ASR_BEAM_SIZE", "5").strip()
    context_mode = os.getenv("ASR_CONDITION_ON_PREVIOUS_TEXT", "1").strip()
    settings = f"{backend}|{model}|{language}|{chunk_seconds:g}|beam={beam_size}|context={context_mode}|max={max_duration:g}"
    digest.update(settings.encode("utf-8"))
    return digest.hexdigest()


def _asr_cache_path(cache_key: str) -> Path:
    configured = os.getenv("ASR_CACHE_DIR", "").strip()
    cache_dir = Path(configured) if configured else Path(__file__).resolve().parent / "cache" / "asr"
    return cache_dir / f"{cache_key}.json"


def _load_asr_cache(cache_key: str) -> Optional[tuple[list[TimedText], str, Optional[str]]]:
    path = _asr_cache_path(cache_key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cues = [TimedText(**item) for item in payload.get("cues") or []]
        if not cues:
            return None
        warning = payload.get("warning")
        cache_note = "ASR 使用了同一视频与模型配置的本地缓存"
        warning = f"{warning}；{cache_note}" if warning else cache_note
        return cues, str(payload.get("status") or "timestamped"), warning
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _save_asr_cache(
    cache_key: str,
    result: tuple[list[TimedText], str, Optional[str]],
) -> None:
    cues, status, warning = result
    if not cues:
        return
    path = _asr_cache_path(cache_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": status,
            "warning": warning,
            "cues": [
                {"start": cue.start, "end": cue.end, "text": cue.text, "source": cue.source}
                for cue in cues
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 缓存失败不能影响主分析链路。
        return


def _image_data_url(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _nearby_text(cues: Sequence[TimedText], timestamp: float, padding: float = 1.0) -> list[TimedText]:
    return [cue for cue in cues if cue.start - padding <= timestamp <= cue.end + padding]


def build_visual_observation_prompt(frames: Sequence[FrameSample], timed_text: Sequence[TimedText]) -> list[dict]:
    content: list[dict] = [{
        "type": "text",
        "text": (
            "你现在只做证据抄录，不做视频总结。下面每张图都带有真实时间戳。\n"
            "逐帧输出 Markdown 表格，列为：时间｜画面直接可见事实｜可辨认的原文文字｜界面/场景类型｜置信度。\n"
            "规则：只记录图中能直接看到的内容；不要根据人物外貌猜身份；不要补全帧外动作；"
            "不要把附近字幕当作画面 OCR；文字看不清就写“不可辨认”；不要推断视频主题或作者观点。"
        ),
    }]
    for frame in frames:
        nearby = _nearby_text(timed_text, frame.timestamp)
        context = "；".join(f"[{cue.source}] {cue.text}" for cue in nearby) or "无"
        content.append({
            "type": "text",
            "text": (
                f"\nFRAME {format_timestamp(frame.timestamp)} "
                f"[采样原因: {', '.join(frame.reasons)}]\n"
                f"同时间附近的独立文本证据（仅供对齐，不是 OCR）：{context}"
            ),
        })
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(frame.path)}})
    return content


def observe_visual_timeline(
    client: OpenAI,
    model: str,
    frames: Sequence[FrameSample],
    timed_text: Sequence[TimedText],
    progress: ProgressCallback = None,
) -> str:
    batch_size = _env_int("VISION_BATCH_SIZE", 6)
    observations: list[str] = []
    total_batches = max(1, math.ceil(len(frames) / batch_size))
    failed_batches = 0
    for batch_index, offset in enumerate(range(0, len(frames), batch_size)):
        batch = frames[offset:offset + batch_size]
        prompt = build_visual_observation_prompt(batch, timed_text)
        cached_observation = _load_model_text_cache("vision", model, prompt)
        if cached_observation is None:
            last_error = None
            for attempt in range(_env_int("VISION_BATCH_RETRIES", 2)):
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        stream=False,
                        max_tokens=_env_int("VISION_MAX_TOKENS", 900),
                    )
                    cached_observation = response.choices[0].message.content.strip()
                    _save_model_text_cache("vision", model, prompt, cached_observation)
                    break
                except Exception as exc:
                    last_error = exc
            if cached_observation is None:
                failed_batches += 1
                observations.append(
                    f"### 视觉证据批次 {batch_index + 1}\n"
                    f"该批次视觉模型暂时不可用，已跳过，不影响 ASR 与正文分析。错误：{last_error}"
                )
                if progress:
                    progress(
                        0.48 + 0.24 * ((batch_index + 1) / total_batches),
                        "部分画面识别失败，已降级为 ASR 优先分析",
                    )
                continue
        observations.append(
            f"### 视觉证据批次 {batch_index + 1}\n{cached_observation}"
        )
        if progress:
            progress(0.48 + 0.24 * ((batch_index + 1) / total_batches), "正在建立视觉证据时间轴")
    if failed_batches == total_batches:
        return "视觉模型本次不可用，报告仅依据 ASR/字幕生成。"
    return "\n\n".join(observations)


def _model_text_cache_path(namespace: str, model: str, prompt: str) -> Path:
    digest = hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()
    return Path(__file__).resolve().parent / "cache" / namespace / f"{digest}.txt"


def _load_model_text_cache(namespace: str, model: str, prompt: str) -> Optional[str]:
    if os.getenv("AI_STAGE_CACHE", "1").strip().lower() in {"0", "false", "no"}:
        return None
    path = _model_text_cache_path(namespace, model, prompt)
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _save_model_text_cache(namespace: str, model: str, prompt: str, value: str) -> None:
    if not value or os.getenv("AI_STAGE_CACHE", "1").strip().lower() in {"0", "false", "no"}:
        return
    path = _model_text_cache_path(namespace, model, prompt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        return


def _serialize_cues(cues: Sequence[TimedText], label: str) -> str:
    if not cues:
        return f"{label}: 无"
    lines = [f"{label}:"]
    for cue in cues:
        lines.append(
            f"- {format_timestamp(cue.start)}–{format_timestamp(cue.end)} "
            f"[{cue.source}] {cue.text}"
        )
    return "\n".join(lines)


def coverage_report(bundle: EvidenceBundle, scene_count: int, text_change_count: int = 0) -> str:
    media = bundle.media
    frame_times = ", ".join(format_timestamp(frame.timestamp) for frame in bundle.frames)
    warning_text = "；".join(bundle.warnings) if bundle.warnings else "无"
    return (
        f"- 时长：{format_timestamp(media.duration)}\n"
        f"- 视频：{media.width}×{media.height}，{media.fps:.2f} FPS，{media.video_codec}\n"
        f"- 音频：{media.audio_tracks} 条音轨，编码 {media.audio_codec or '无'}\n"
        f"- 字幕轨：{len(media.subtitle_tracks)} 条；成功读取 {len(bundle.subtitles)} 条字幕\n"
        f"- ASR：{bundle.transcript_status}；获得 {len(bundle.transcript)} 个语音片段\n"
        f"- 视觉采样：{len(bundle.frames)} 帧；检测到 {scene_count} 个场景变化候选、"
        f"{text_change_count} 个字幕/屏幕文字区域变化候选\n"
        f"- 实际帧时间：{frame_times or '无'}\n"
        f"- 已知限制：{warning_text}"
    )


def build_synthesis_prompt(
    bundle: EvidenceBundle,
    scene_count: int,
    custom_focus: Optional[str] = None,
    text_change_count: int = 0,
) -> str:
    has_speech_evidence = bool(bundle.subtitles or bundle.transcript)
    speech_guard = (
        "存在带时间戳的字幕/转写，可据此重建口播，但仍须区分逐字证据与释义。"
        if has_speech_evidence else
        "没有任何可用字幕或 ASR 转写。严禁声称作者“说了/认为/主张”什么，严禁生成逐字稿；"
        "只能给出视觉层面的暂定描述，并明确无法恢复完整论证。"
    )
    focus = custom_focus.strip() if custom_focus and custom_focus.strip() else "无额外分析要求"
    return f"""你是严谨的视频证据分析员。请根据下方“原始证据包”用中文生成最终报告。

核心纪律：
1. Source-first：每个重要结论必须能回到时间戳；无法定位的内容不得写成事实。
2. Timestamp-first：重要观点、案例、数字、产品名都标注时间。
3. Evidence / Inference 分离：E1=直接证据，E2=上下文支持的强释义，E3=你的外部分析推论。
4. 不得把 E2/E3 写成作者原话；不得编造口播、OCR、身份、数字或因果关系。
5. 每个观点至少结合前后相邻证据，不得孤立截句。
6. 本轮没有提供标题。不得猜标题，也不得借助文件名推断正文。
7. {speech_guard}
8. ASR 是机器转写，不等于人工核验逐字稿；引用 ASR 时必须标注“ASR”。
9. 禁止自行展开 FDE 等缩写；只有 ASR/字幕逐字出现全称时才能写全称，否则原样保留缩写。
10. “视频明确表达（E1）”中每一条都必须采用 `[时间段][ASR/字幕/画面] 证据内容` 格式；
    缺少时间戳或证据通道的条目不得进入 E1。

系统会在你的回答前自动添加确定性的“解析覆盖与限制”，你从以下结构开始输出：
## 时间轴证据
用表格输出：时间段｜声音/字幕证据｜画面证据｜语义作用（Hook/Claim/Explanation/Example/Evidence/Method/Conclusion/Unknown）。

## 核心总结
只总结证据充分的内容；证据不足时明确写“无法确认”。

## 视频明确表达（E1）
逐条列出直接证据。每条必须有时间段、证据通道及尽量贴近原文的短证据；不得使用无时间戳项目符号。

## 事实清单
提取人名、公司、产品、数字、价格、日期、工具与流程；逐项给时间戳和证据来源，未出现则写“未发现”。

## 合理释义（E2）
逐条说明释义及其证据组合。

## 分析推论（E3）
必须显式标为你的推论；没有必要可写“无”。

## 论证结构
按 Problem → Cause → Evidence → Solution → Result 重建；缺失环节写“视频未提供”。

## 批判性检查
指出证据缺口、跳步、反例或无法验证之处，不能为了完整而补齐。

## 适用性与行动清单
把可迁移建议与原视频事实分开；每项说明适用前提。

用户额外关注点：{focus}

以下是原始证据包。

[媒体覆盖]
{coverage_report(bundle, scene_count, text_change_count)}

[独立字幕轨]
{_serialize_cues(bundle.subtitles, '字幕')}

[ASR 转写]
{_serialize_cues(bundle.transcript, '转写')}

[逐帧视觉观察]
{bundle.visual_observations or '无可用视觉观察'}
"""


def check_title_alignment(client: OpenAI, model: str, title: str, analysis: str) -> str:
    """Check title only after core analysis, preventing title-to-content leakage."""
    prompt = f"""只做标题与已完成分析的一致性核对，不得修改或扩写正文。
标题是元数据，不是视频证据。只返回 JSON，不要 Markdown：
{{"judgment":"准确/部分准确/误导/无法判断", "basis":"只引用已有时间戳与结论", "leakage":"标题是否包含正文未支持的信息"}}
禁止自行展开任何英文缩写。

[标题元数据]
{title}

[已经在未见标题条件下完成的正文分析]
{analysis[:12000]}

现在只返回上述三个键的 JSON 对象。
"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        max_tokens=_env_int("TITLE_CHECK_MAX_TOKENS", 500),
        response_format={"type": "json_object"},
    )
    try:
        payload = _json_object_from_model(response.choices[0].message.content)
    except (ValueError, TypeError, json.JSONDecodeError):
        return "- 判断：无法判断\n- 依据：标题核对模型未返回有效结构\n- 标题泄漏检查：无法判断"
    judgment = str(payload.get("judgment") or "无法判断")
    if judgment not in {"准确", "部分准确", "误导", "无法判断"}:
        judgment = "无法判断"
    source_text = analysis
    basis = _strip_unsupported_acronym_expansions(_clean_cell(payload.get("basis")), source_text)
    leakage = _strip_unsupported_acronym_expansions(_clean_cell(payload.get("leakage")), source_text)
    return f"- 判断：{judgment}\n- 依据：{basis}\n- 标题泄漏检查：{leakage}"


def detect_analysis_quality_issues(analysis: str) -> list[str]:
    """对模型输出做确定性合约检查，发现问题时触发证据约束修订。"""
    issues: list[str] = []
    match = re.search(
        r"## 视频明确表达（E1）\s*(.*?)(?=\n## |\Z)",
        analysis,
        flags=re.S,
    )
    if not match:
        issues.append("缺少“视频明确表达（E1）”章节")
    else:
        bullets = [line.strip() for line in match.group(1).splitlines() if line.strip().startswith("-")]
        timestamp_pattern = re.compile(r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d{1,3})?")
        missing = [line for line in bullets if not timestamp_pattern.search(line)]
        if not bullets or missing:
            issues.append(f"E1 条目缺少时间戳或为空（不合格 {len(missing)}/{len(bullets)} 条）")
    if re.search(
        r"\b[A-Z]{2,}\s*[（(][A-Za-z][A-Za-z\s-]{3,}[)）]",
        analysis,
    ):
        issues.append("出现了可能未经原始证据支持的英文缩写展开")
    return issues


def revise_analysis_with_evidence(
    client: OpenAI,
    model: str,
    bundle: EvidenceBundle,
    scene_count: int,
    text_change_count: int,
    candidate: str,
    issues: Sequence[str],
    custom_focus: Optional[str],
) -> str:
    prompt = f"""你是视频分析质检员。候选报告违反了证据合约，请完整重写，而不是解释错误。

必须修复的问题：
{chr(10).join(f'- {item}' for item in issues)}

硬性规则：
- 只能使用下方原始证据包，不得补充常识性全称、背景、案例、数据或因果。
- FDE 等缩写若原始证据没有逐字给出全称，绝不展开。
- E1 每条严格写成 `[开始时间–结束时间][ASR/字幕/画面] 证据内容`。
- E2/E3 与作者原话分开；不确定就删除或写“无法确认”。
- 保留候选报告要求的全部二级标题，但内容必须以证据为准。

[原始证据包与输出规范]
{build_synthesis_prompt(bundle, scene_count, custom_focus, text_change_count)}

[待修订候选报告]
{candidate}
"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        max_tokens=_env_int("QUALITY_REWRITE_MAX_TOKENS", 2800),
    )
    return response.choices[0].message.content.strip()


def build_structured_synthesis_prompt(
    bundle: EvidenceBundle,
    scene_count: int,
    text_change_count: int,
    custom_focus: Optional[str],
) -> str:
    focus = custom_focus.strip() if custom_focus and custom_focus.strip() else "无额外分析要求"
    return f"""你是视频证据抽取器。标题未提供，不能用标题推断正文。
只返回一个合法 JSON 对象，不要 Markdown，不要解释。所有结论只能来自证据包。

强制规则：
1. 禁止展开 FDE 等缩写，除非 ASR/字幕中逐字出现该全称。
2. e1 只能放原始证据直接支持的短句；必须给 start/end（秒）、channel（ASR/字幕/画面）和 evidence。
3. e2 是上下文支持的释义，e3 才是外部分析推论；三者不得混写。
4. 不得补充证据中没有的人名、公司、工具、定义、案例、数字或因果。
5. timeline 覆盖开头、中段、结尾，role 只能是 Hook/Claim/Explanation/Example/Evidence/Method/Conclusion/Unknown。
6. 无证据的字段用空数组或字符串“视频未提供”，不能为了完整而编造。

JSON 结构必须严格为：
{{
  "timeline": [{{"start": 0.0, "end": 10.0, "speech": "ASR短证据", "visual": "画面短证据", "role": "Hook"}}],
  "core_summary": [{{"start": 0.0, "end": 10.0, "text": "有证据的概括"}}],
  "e1": [{{"start": 0.0, "end": 2.0, "channel": "ASR", "evidence": "尽量贴近原文的短证据"}}],
  "facts": [{{"name": "项目数量", "value": "16个", "start": 2.0, "end": 4.0, "channel": "ASR"}}],
  "e2": [{{"start": 0.0, "end": 10.0, "text": "合理释义", "basis": "证据组合"}}],
  "e3": [{{"text": "分析推论", "premise": "适用前提"}}],
  "argument": {{"problem": "...", "cause": "...", "evidence": "...", "solution": "...", "result": "..."}},
  "critique": ["证据缺口或跳步"],
  "actions": [{{"action": "可迁移行动", "prerequisite": "适用前提"}}]
}}

用户额外关注点：{focus}

[媒体覆盖]
{coverage_report(bundle, scene_count, text_change_count)}

[字幕]
{_serialize_cues(bundle.subtitles, '字幕')}

[ASR]
{_serialize_cues(bundle.transcript, '转写')}

[视觉观察]
{bundle.visual_observations or '无'}

[输出前最终检查]
现在只输出 JSON 对象，第一字符必须是 `{{`，最后一字符必须是 `}}`。
首层键必须且只能是：timeline、core_summary、e1、facts、e2、e3、argument、critique、actions。
禁止输出 transcript、visualEvidences、analysis 等其他自创键。e1 不得为空。
"""


def _json_object_from_model(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        # Some OpenAI-compatible local servers still wrap JSON with a short
        # preface despite response_format=json_object. Decode the first object
        # instead of discarding an otherwise valid structured result.
        start = cleaned.find("{")
        if start < 0:
            raise
        payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(payload, dict):
        raise ValueError("结构化分析不是 JSON 对象")
    return payload


def generate_structured_analysis(
    client: OpenAI,
    model: str,
    bundle: EvidenceBundle,
    scene_count: int,
    text_change_count: int,
    custom_focus: Optional[str],
) -> dict:
    response = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": build_structured_synthesis_prompt(
                bundle, scene_count, text_change_count, custom_focus
            ),
        }],
        response_format={"type": "json_object"},
        stream=False,
        max_tokens=_env_int("SYNTHESIS_MAX_TOKENS", 2800),
    )
    return _json_object_from_model(response.choices[0].message.content)


def _seconds(value, duration: float) -> float:
    try:
        if isinstance(value, str) and ":" in value:
            parts = [float(item) for item in value.split(":")]
            number = 0.0
            for part in parts:
                number = number * 60 + part
        else:
            number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(0.0, min(duration, number))


def _group_transcript_evidence(cues: Sequence[TimedText]) -> list[TimedText]:
    """把过短 ASR 片段合并成可回看的证据单元，不改写原文。"""
    groups: list[TimedText] = []
    current: list[TimedText] = []
    for cue in cues:
        if current and (
            cue.start - current[-1].end > 1.2
            or cue.end - current[0].start > 9.0
            or sum(len(item.text) for item in current) >= 90
        ):
            groups.append(TimedText(
                current[0].start,
                current[-1].end,
                "".join(item.text for item in current),
                current[0].source,
            ))
            current = []
        current.append(cue)
    if current:
        groups.append(TimedText(
            current[0].start,
            current[-1].end,
            "".join(item.text for item in current),
            current[0].source,
        ))
    return groups


def normalize_structured_payload(payload: dict, bundle: EvidenceBundle) -> dict:
    """将模型可能偏离的 JSON 归一成强制证据结构；E1 始终取原始时间戳文本。"""
    duration = bundle.media.duration
    source_cues = bundle.subtitles or bundle.transcript
    groups = _group_transcript_evidence(source_cues)

    visual_by_time: list[tuple[float, str]] = []
    raw_timeline = payload.get("timeline")
    if isinstance(raw_timeline, dict):
        for key, value in raw_timeline.items():
            visual_by_time.append((_seconds(key, duration), str(value or "")))
    elif isinstance(raw_timeline, list):
        for item in raw_timeline:
            if isinstance(item, dict) and item.get("visual"):
                visual_by_time.append((_seconds(item.get("start"), duration), str(item.get("visual"))))

    timeline = []
    if groups:
        window = max(30.0, duration / 10 if duration else 30.0)
        cursor = 0.0
        while cursor < duration:
            selected = [item for item in groups if cursor <= item.start < cursor + window]
            if selected:
                speech = " ".join(item.text for item in selected)[:240]
                start, end = selected[0].start, selected[-1].end
            else:
                start, end, speech = cursor, min(duration, cursor + window), "无可用语音证据"
            visual = "无对应采样画面"
            nearby = [(abs(ts - start), text) for ts, text in visual_by_time if abs(ts - start) <= window]
            if nearby:
                visual = min(nearby, key=lambda item: item[0])[1]
            timeline.append({
                "start": start,
                "end": end,
                "speech": speech,
                "visual": visual,
                "role": "Unknown",
            })
            cursor += window

    keywords = ("FDE", "赚钱", "技术", "客户", "案例", "规模", "销售", "交付", "获客", "创业", "数据库", "agent", "skill")
    scored = []
    for index, item in enumerate(groups):
        score = sum(2 for keyword in keywords if keyword.lower() in item.text.lower())
        if re.search(r"\d", item.text):
            score += 2
        if index in {0, 1, max(0, len(groups) - 2), max(0, len(groups) - 1)}:
            score += 1
        scored.append((score, index, item))
    candidates = sorted(scored, key=lambda value: (-value[0], value[1]))
    selected_groups: list[TimedText] = []
    for score, _, item in candidates:
        if score <= 0 and len(selected_groups) >= 12:
            continue
        if all(abs(item.start - existing.start) >= 8 for existing in selected_groups):
            selected_groups.append(item)
        if len(selected_groups) >= 20:
            break
    selected_groups.sort(key=lambda item: item.start)
    e1 = [{
        "start": item.start,
        "end": item.end,
        "channel": "字幕" if bundle.subtitles else "ASR",
        "evidence": item.text,
    } for item in selected_groups]

    core_value = payload.get("core_summary")
    if isinstance(core_value, list):
        core_summary = core_value
    elif core_value:
        core_summary = [{"start": 0.0, "end": duration, "text": f"[E2] {core_value}"}]
    else:
        core_summary = []

    def normalize_interpretations(value, *, inference: bool = False):
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        # 模型未遵守结构合约时，宁可留空，也不把不可定位的自由文本伪装成分析结论。
        return []

    facts = [item for item in (payload.get("facts") or []) if isinstance(item, dict)] \
        if isinstance(payload.get("facts"), list) else []
    if not facts:
        for item in groups:
            if re.search(r"\d", item.text) or any(
                keyword in item.text.lower() for keyword in ("数据库", "agent", "skill")
            ):
                facts.append({
                    "name": "原始事实证据",
                    "value": item.text,
                    "start": item.start,
                    "end": item.end,
                    "channel": "字幕" if bundle.subtitles else "ASR",
                })
            if len(facts) >= 10:
                break
    critique_value = payload.get("critique")
    critique = critique_value if isinstance(critique_value, list) else []
    action_value = payload.get("actions")
    raw_actions = action_value if isinstance(action_value, list) else ([action_value] if action_value else [])
    actions = [item for item in raw_actions if isinstance(item, dict)]
    argument = payload.get("argument") if isinstance(payload.get("argument"), dict) else {}
    return {
        "timeline": timeline,
        "core_summary": core_summary,
        "e1": e1,
        "facts": facts,
        "e2": normalize_interpretations(payload.get("e2")),
        "e3": normalize_interpretations(payload.get("e3"), inference=True),
        "argument": argument,
        "critique": critique,
        "actions": actions,
    }


def _clean_cell(value) -> str:
    return str(value or "未提供").replace("|", "／").replace("\r", " ").replace("\n", " ").strip()


def _strip_unsupported_acronym_expansions(text: str, source_text: str) -> str:
    source_lower = source_text.lower()

    def parenthetical(match: re.Match) -> str:
        return match.group(0) if match.group(0).lower() in source_lower else match.group(1)

    text = re.sub(r"(?<![A-Za-z])([A-Z]{2,})\s*[（(][^）)\n]{2,50}[）)]", parenthetical, text)

    def colon(match: re.Match) -> str:
        return match.group(0) if match.group(0).lower() in source_lower else match.group(1)

    return re.sub(r"(?<![A-Za-z])([A-Z]{2,})\s*[:：=]\s*[A-Za-z][A-Za-z\s-]{3,60}", colon, text)


def render_structured_analysis(payload: dict, bundle: EvidenceBundle) -> str:
    duration = bundle.media.duration
    source_text = "\n".join(
        cue.text for cue in [*bundle.subtitles, *bundle.transcript]
    ) + "\n" + bundle.visual_observations

    def clean(value) -> str:
        return _strip_unsupported_acronym_expansions(_clean_cell(value), source_text)

    lines = ["## 时间轴证据", "", "| 时间段 | 声音/字幕证据 | 画面证据 | 语义作用 |", "|---|---|---|---|"]
    timeline = payload.get("timeline") if isinstance(payload.get("timeline"), list) else []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        start = _seconds(item.get("start"), duration)
        end = max(start, _seconds(item.get("end"), duration))
        role = clean(item.get("role"))
        if role not in {"Hook", "Claim", "Explanation", "Example", "Evidence", "Method", "Conclusion", "Unknown"}:
            role = "Unknown"
        lines.append(
            f"| {format_timestamp(start)}–{format_timestamp(end)} | {clean(item.get('speech'))} | "
            f"{clean(item.get('visual'))} | {role} |"
        )
    if len(lines) == 4:
        lines.append("| 无法确认 | 无 | 无 | Unknown |")

    lines.extend(["", "## 核心总结", ""])
    summaries = payload.get("core_summary") if isinstance(payload.get("core_summary"), list) else []
    for item in summaries:
        if isinstance(item, dict):
            start = _seconds(item.get("start"), duration)
            end = max(start, _seconds(item.get("end"), duration))
            lines.append(f"- [{format_timestamp(start)}–{format_timestamp(end)}] {clean(item.get('text'))}")
    if not summaries:
        lines.append("- 无法确认。")

    lines.extend(["", "## 视频明确表达（E1）", ""])
    e1_items = payload.get("e1") if isinstance(payload.get("e1"), list) else []
    valid_e1 = 0
    for item in e1_items:
        if not isinstance(item, dict) or not item.get("evidence"):
            continue
        start = _seconds(item.get("start"), duration)
        end = max(start, _seconds(item.get("end"), duration))
        channel = clean(item.get("channel"))
        if channel not in {"ASR", "字幕", "画面"}:
            channel = "ASR" if bundle.transcript else ("字幕" if bundle.subtitles else "画面")
        lines.append(
            f"- [{format_timestamp(start)}–{format_timestamp(end)}][{channel}] {clean(item.get('evidence'))}"
        )
        valid_e1 += 1
    if valid_e1 == 0:
        raise ValueError("结构化分析没有任何有效 E1 证据")

    lines.extend(["", "## 事实清单", ""])
    facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
    for item in facts:
        if not isinstance(item, dict):
            continue
        start = _seconds(item.get("start"), duration)
        end = max(start, _seconds(item.get("end"), duration))
        lines.append(
            f"- **{clean(item.get('name'))}**：{clean(item.get('value'))} "
            f"[{format_timestamp(start)}–{format_timestamp(end)}][{clean(item.get('channel'))}]"
        )
    if not facts:
        lines.append("- 未发现可稳定提取的事实项。")

    for heading, key in (("合理释义（E2）", "e2"), ("分析推论（E3）", "e3")):
        lines.extend(["", f"## {heading}", ""])
        items = payload.get(key) if isinstance(payload.get(key), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            if key == "e2":
                start = _seconds(item.get("start"), duration)
                end = max(start, _seconds(item.get("end"), duration))
                lines.append(
                    f"- [{format_timestamp(start)}–{format_timestamp(end)}] {clean(item.get('text'))}；"
                    f"依据：{clean(item.get('basis'))}"
                )
            else:
                lines.append(f"- {clean(item.get('text'))}；适用前提：{clean(item.get('premise'))}")
        if not items:
            lines.append("- 无。")

    lines.extend(["", "## 论证结构", ""])
    argument = payload.get("argument") if isinstance(payload.get("argument"), dict) else {}
    for label, key in (("Problem", "problem"), ("Cause", "cause"), ("Evidence", "evidence"), ("Solution", "solution"), ("Result", "result")):
        lines.append(f"- **{label}**：{clean(argument.get(key) or '视频未提供')}")

    lines.extend(["", "## 批判性检查", ""])
    critique = payload.get("critique") if isinstance(payload.get("critique"), list) else []
    lines.extend(f"- {clean(item)}" for item in critique if item)
    if not critique:
        lines.append("- 视频未提供足够信息进行额外核验。")

    lines.extend(["", "## 适用性与行动清单", ""])
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    for item in actions:
        if isinstance(item, dict):
            lines.append(f"- {clean(item.get('action'))}；适用前提：{clean(item.get('prerequisite'))}")
    if not actions:
        lines.append("- 暂无证据充分的行动建议。")
    return "\n".join(lines)


def _build_evidence_windows(bundle: EvidenceBundle, count: int = 8) -> list[dict]:
    cues = bundle.subtitles or bundle.transcript
    if not cues:
        return []
    duration = max(bundle.media.duration, cues[-1].end)
    window_seconds = max(30.0, duration / count)
    windows = []
    cursor = 0.0
    while cursor < duration:
        selected = [cue for cue in cues if cursor <= cue.start < cursor + window_seconds]
        if selected:
            windows.append({
                "start": selected[0].start,
                "end": selected[-1].end,
                "source": " ".join(cue.text for cue in selected),
            })
        cursor += window_seconds
    return windows


def _summarize_evidence_window(client: OpenAI, model: str, window: dict) -> dict:
    prompt = f"""只分析下面这一小段视频 ASR，不使用外部知识。不要展开 FDE 等缩写。
返回 JSON：
{{
  "title":"本段标题",
  "summary":"本段内容概括",
  "themes":["主题"],
  "core_points":[{{"text":"忠实观点", "evidence":"ASR中连续出现的原句"}}],
  "key_cases":[{{"text":"本段明确讲到的具体案例", "evidence":"ASR中连续出现的原句"}}],
  "author_conclusion":[{{"text":"作者明确结论", "evidence":"ASR中连续出现的原句"}}]
}}
普通说明不是案例；没有案例或结论就返回空数组。evidence 必须能在 ASR 中逐字找到，不能概括或改写。

时间：{format_timestamp(window['start'])}–{format_timestamp(window['end'])}
ASR：{window['source']}

现在只返回上述 JSON。
"""
    cached = _load_model_text_cache("segment_summary", model, prompt)
    if cached:
        try:
            return _json_object_from_model(cached)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            stream=False,
            max_tokens=_env_int("SEGMENT_SUMMARY_MAX_TOKENS", 700),
        )
        content = response.choices[0].message.content.strip()
        payload = _json_object_from_model(content)
        _save_model_text_cache("segment_summary", model, prompt, content)
        return payload
    except Exception:
        return {
            "title": "内容片段",
            "summary": window["source"][:120],
            "themes": [],
            "core_points": [],
            "key_cases": [],
            "author_conclusion": [],
        }


def summarize_evidence_windows(client: OpenAI, model: str, bundle: EvidenceBundle) -> list[dict]:
    segments = []
    for window in _build_evidence_windows(bundle):
        summary = _summarize_evidence_window(client, model, window)
        summary["start"] = window["start"]
        summary["end"] = window["end"]
        summary["_source"] = window["source"]
        segments.append(summary)
    return segments


def build_audience_report_prompt(
    segments: Sequence[dict],
    custom_focus: Optional[str],
    video_info: Optional[dict] = None,
) -> str:
    focus = custom_focus.strip() if custom_focus and custom_focus.strip() else "无额外要求"
    info = video_info or {}
    title = str(info.get("title") or "未获取")
    platform_evidence = normalize_platform_evidence(info)
    signal_context = compact_signal_context(platform_evidence)
    # The raw `_source` transcript is intentionally excluded here. Content
    # segments already contain grounded evidence, while repeating the whole
    # transcript can make small local models echo the input instead of obeying
    # the requested distillation schema.
    distilled_segments = []
    for segment in segments:
        distilled_segments.append({
            key: value for key, value in segment.items()
            if key in {
                "start", "end", "title", "summary", "themes",
                "core_points", "key_cases", "author_conclusion",
            }
        })
    return f"""你在为“视频爆款研究库”生成稳定的结构化数据。
根据标题元数据、确定性互动信号和已经按顺序整理的内容段落，分析内容机制与传播假设。
标题只能用于分析包装方式，不能当作正文证据；不得使用外部知识，不得展开缩写。
传播原因只能写成假设，每条都要给具体证据和置信度。互动比值只是信号，不代表因果。
禁止把收藏/点赞比叫“收藏率”，禁止只说“内容好、有共鸣”。只返回 JSON：
严格控制总输出在1800个汉字以内：传播假设最多3条、可复用基因最多4条、不可复制因素最多3条，单个字段尽量一句话。
{{
  "one_sentence_summary": "不超过60字，直接说作者讲了什么、给出什么判断",
  "topic": "视频讨论的核心问题",
  "target_audience": "最适合观看的人",
  "viewer_value": "观众看完获得的具体认知或方法",
  "mechanics": {{
    "title": {{"formulas":["数字型/痛点型/身份代入/悬念型/反差型/利益承诺型/对比型/提问型/自由创作型"], "evidence":"原标题中的具体词", "search_terms":["真实出现的搜索词"]}},
    "cover": {{"strategy":"能从画面确认的封面策略；无法确认写信息不足", "evidence":"画面证据"}},
    "opening_hook": {{"type":"钩子类型", "mechanism":"如何抓住注意力", "evidence":"开场原句或画面"}},
    "content_types": ["干货型/故事型/测评型/情绪型/求助型/观点型"],
    "information_rhythm": "内容如何推进、在哪里强化或转折",
    "retention_devices": ["推动继续观看的结构设计"],
    "emotion": {{"primary":"主导情绪", "intensity":1, "evidence":"原句或画面"}},
    "audience_need": {{"primary":"用户深层需求", "evidence":"内容证据"}},
    "credibility_devices": ["建立可信度的方式"],
    "language_style": ["表达与节奏特征"],
    "visual_style": ["能确认的画面包装"],
    "cta": {{"type":"直接型/引导型/隐性型/无", "evidence":"结尾原句", "purpose":"评论/收藏/转发/关注/无"}}
  }},
  "viral_hypotheses": [{{
    "hypothesis":"可能促进点击、留存、评论、收藏或转发的具体机制",
    "evidence":["内容原句/画面/确定性互动信号"],
    "confidence":"high/medium/low",
    "affects":"click/retention/comment/collect/share"
  }}],
  "transfer": {{
    "reusable_genes": [{{"name":"可执行的基因名", "mechanism":"为什么起作用", "evidence":["证据"], "conditions":["适用条件"]}}],
    "non_replicable_factors": [{{"factor":"作者/时机/资源/算法等因素", "reason":"为什么不能直接复制"}}],
    "applicable_scenarios": ["适用内容类型或赛道"],
    "failure_conditions": ["复制后容易失效的条件"],
    "creation_constraints": ["给下一个创作AI的结构、情绪、CTA约束"]
  }},
  "limitations": ["视频论证局限或当前数据缺口"]
}}

标题元数据：{title}
确定性互动数据与缺口：
{json.dumps(signal_context, ensure_ascii=False)}
分段内容：
{json.dumps(distilled_segments, ensure_ascii=False)}

用户要求：{focus}
现在只返回上述 JSON。
"""


def generate_audience_report_payload(
    client: OpenAI,
    model: str,
    bundle: EvidenceBundle,
    custom_focus: Optional[str],
    video_info: Optional[dict] = None,
) -> dict:
    segments = summarize_evidence_windows(client, model, bundle)
    platform_evidence = normalize_platform_evidence(
        video_info,
        url=str((video_info or {}).get("share_url") or ""),
        duration_seconds=bundle.media.duration,
    )
    prompt = build_audience_report_prompt(segments, custom_focus, video_info)
    overview: dict = {}
    try:
        raw_overview = _load_model_text_cache("audience_report_v3", model, prompt)
        if raw_overview is None:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                stream=False,
                max_tokens=_env_int("AUDIENCE_REPORT_MAX_TOKENS", 3000),
            )
            raw_overview = response.choices[0].message.content
            _save_model_text_cache("audience_report_v3", model, prompt, raw_overview)
        overview = _json_object_from_model(raw_overview)
        valid_overview = (
            isinstance(overview.get("mechanics"), dict)
            and bool(overview.get("mechanics"))
            and isinstance(overview.get("viral_hypotheses"), list)
            and bool(overview.get("viral_hypotheses"))
            and isinstance(overview.get("transfer"), dict)
            and bool(overview.get("transfer", {}).get("reusable_genes"))
        )
        if not valid_overview:
            repair_prompt = f"""上一次输出不合格：你返回了内容片段或缺少必需字段。
重新完成爆款蒸馏，只返回一个 JSON 对象，禁止复制输入，禁止出现 text、_source、start、end 作为顶层键。
顶层必须包含且只包含：one_sentence_summary、topic、target_audience、viewer_value、mechanics、viral_hypotheses、transfer、limitations。
mechanics 必须非空；viral_hypotheses 必须有1-3条且含 hypothesis/evidence/confidence/affects；transfer.reusable_genes 必须有1-4条且含 name/mechanism/evidence/conditions。
传播原因是待验证假设而非因果结论。总输出不超过1800个汉字。

原任务：
{prompt}
"""
            repair_raw = _load_model_text_cache("audience_report_v3_repair", model, repair_prompt)
            if repair_raw is None:
                repair_response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": repair_prompt}],
                    response_format={"type": "json_object"},
                    stream=False,
                    temperature=0.1,
                    max_tokens=_env_int("AUDIENCE_REPORT_MAX_TOKENS", 3000),
                )
                repair_raw = repair_response.choices[0].message.content
                _save_model_text_cache("audience_report_v3_repair", model, repair_prompt, repair_raw)
            overview = _json_object_from_model(repair_raw)
            if not (
                isinstance(overview.get("mechanics"), dict)
                and bool(overview.get("mechanics"))
                and isinstance(overview.get("viral_hypotheses"), list)
                and bool(overview.get("viral_hypotheses"))
                and isinstance(overview.get("transfer"), dict)
                and bool(overview.get("transfer", {}).get("reusable_genes"))
            ):
                raise ValueError("模型未按 v3 爆款蒸馏结构返回必需字段")
    except Exception as exc:
        bundle.warnings.append(f"爆款蒸馏结构化输出解析失败: {exc}")
        overview = {}

    themes = []
    for segment in segments:
        themes.extend(str(item) for item in _as_list(segment.get("themes")) if item)
    themes = list(dict.fromkeys(themes))[:6]

    structures = []
    points = []
    cases = []
    conclusions = []
    for index, segment in enumerate(segments):
        start, end = segment["start"], segment["end"]
        title = str(segment.get("title") or "内容片段")
        summary = str(segment.get("summary") or "")
        if title in {"内容片段", "本段标题", "未提供标题", "无标题", "未知"} and summary:
            title = summary[:18] + ("…" if len(summary) > 18 else "")
        if index == 0:
            stage = "开场钩子"
        elif index == len(segments) - 1:
            stage = "结论收束"
        elif _as_list(segment.get("key_cases")):
            stage = "案例与证据"
        elif index <= max(1, len(segments) // 3):
            stage = "问题展开"
        else:
            stage = "观点论证"
        structures.append({"stage": stage, "title": title, "summary": summary})
        normalized_source = re.sub(r"\s+", "", str(segment.get("_source") or "")).lower()

        def grounded_items(key: str, output_key: str, predicate=None, maximum: int = 2):
            output = []
            for item in _as_list(segment.get(key)):
                if not isinstance(item, dict):
                    continue
                raw_evidence = str(item.get("evidence") or "").strip()
                evidence = re.sub(r"\s+", "", raw_evidence).lower()
                text = str(item.get("text") or "").strip()
                if (
                    text
                    and len(evidence) >= 4
                    and evidence in normalized_source
                    and (predicate is None or predicate(evidence, text))
                ):
                    output.append({output_key: text, "evidence": raw_evidence})
                if len(output) >= maximum:
                    break
            return output

        points.extend(grounded_items("core_points", "point"))
        cases.extend(grounded_items(
            "key_cases",
            "case",
            predicate=lambda evidence, text: bool(re.search(
                r"\d|小张|老板|我认识|例如|比如|案例|公司|招聘",
                evidence + text,
            )),
        ))
        conclusions.extend(grounded_items(
            "author_conclusion",
            "conclusion",
            predicate=lambda evidence, text: (
                start >= bundle.media.duration * 0.65
                or bool(re.search(r"最后|结论|我的态度|我相信|所以|只有|最大的障碍", evidence + text))
            ),
        ))

    one_sentence = overview.get("one_sentence_summary")
    if not isinstance(one_sentence, str) or not one_sentence.strip():
        one_sentence = "；".join(str(item.get("summary") or "") for item in segments[:2])[:50]
    mechanics = overview.get("mechanics") if isinstance(overview.get("mechanics"), dict) else {}
    hypotheses = [item for item in _as_list(overview.get("viral_hypotheses")) if isinstance(item, dict)]
    transfer = overview.get("transfer") if isinstance(overview.get("transfer"), dict) else {}
    content = {
        "one_sentence_summary": one_sentence,
        "topic": overview.get("topic") or (themes[0] if themes else "暂未提取到明确主题"),
        "themes": themes,
        "target_audience": overview.get("target_audience") or "暂未明确",
        "viewer_value": overview.get("viewer_value") or "暂未明确",
        "content_structure": structures,
        "core_points": points[:16],
        "key_cases": cases[:8],
        "author_conclusion": conclusions[-6:],
    }
    data_gaps = list(platform_evidence.get("data_gaps") or [])
    data_gaps.extend(str(item) for item in _as_list(overview.get("limitations")) if item)
    return {
        "schema_version": "video-analysis.v3",
        "source": platform_evidence.get("source") or {},
        "performance": platform_evidence.get("performance") or {},
        "distribution_signals": platform_evidence.get("distribution_signals") or [],
        "content": content,
        "mechanics": mechanics,
        "viral_hypotheses": hypotheses[:8],
        "transfer": transfer,
        "data_gaps": list(dict.fromkeys(data_gaps)),
        "source_quotes": [item.text for item in _select_quote_evidence(bundle)],
    }


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _select_quote_evidence(bundle: EvidenceBundle, maximum: int = 10) -> list[TimedText]:
    groups = _group_transcript_evidence(bundle.subtitles or bundle.transcript)
    keywords = ("FDE", "赚钱", "技术", "客户", "案例", "规模", "销售", "交付", "获客", "结论", "数据库", "agent", "skill")
    scored = []
    for index, item in enumerate(groups):
        score = sum(2 for keyword in keywords if keyword.lower() in item.text.lower())
        if re.search(r"\d", item.text):
            score += 1
        if index in {0, len(groups) - 1}:
            score += 1
        scored.append((score, index, item))
    selected: list[TimedText] = []
    for score, _, item in sorted(scored, key=lambda value: (-value[0], value[1])):
        if score <= 0 and len(selected) >= max(4, maximum // 2):
            continue
        if all(abs(item.start - existing.start) >= 15 for existing in selected):
            selected.append(item)
        if len(selected) >= maximum:
            break
    return sorted(selected, key=lambda item: item.start)


def render_audience_report(
    payload: dict,
    bundle: EvidenceBundle,
    video_info: Optional[dict] = None,
    title: Optional[str] = None,
) -> str:
    """渲染给人阅读的内容洞察；不展示时间轴，机器数据另行入库。"""
    info = video_info or {}
    author = info.get("author") if isinstance(info.get("author"), dict) else {}
    duration = bundle.media.duration
    content = payload.get("content") if isinstance(payload.get("content"), dict) else payload
    source_info = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
    mechanics = payload.get("mechanics") if isinstance(payload.get("mechanics"), dict) else {}
    # v2 compatibility for already archived payloads.
    legacy_viral = payload.get("viral_analysis") if isinstance(payload.get("viral_analysis"), dict) else {}
    transfer = payload.get("transfer") if isinstance(payload.get("transfer"), dict) else {}
    legacy_playbook = payload.get("reusable_playbook") if isinstance(payload.get("reusable_playbook"), dict) else {}

    def clean(value) -> str:
        source = "\n".join(cue.text for cue in (bundle.subtitles or bundle.transcript))
        return _strip_unsupported_acronym_expansions(_clean_cell(value), source)

    def values(items, key: str) -> list[str]:
        result = []
        for item in _as_list(items):
            value = item.get(key) if isinstance(item, dict) else item
            if value:
                result.append(clean(value))
        return result

    def number(value) -> str:
        if value is None:
            return "未获取"
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return clean(value)

    def percent(value) -> str:
        if value is None:
            return "未获取"
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return clean(value)

    lines = ["## 视频速览", ""]
    basic = [
        ("标题", source_info.get("title") or info.get("title") or title or "未获取"),
        ("作者", (source_info.get("author") or {}).get("nickname") or author.get("nickname") or info.get("author_name") or "未获取"),
        ("平台", source_info.get("platform") or info.get("platform") or "未获取"),
        ("时长", format_timestamp(duration)),
    ]
    lines.extend(f"- **{label}**：{clean(value)}" for label, value in basic)

    if any(performance.get(key) is not None for key in (
        "play_count", "like_count", "comment_count", "collect_count", "share_count"
    )):
        lines.extend(["", "## 数据表现", ""])
        lines.append(
            f"- **播放**：{number(performance.get('play_count'))}　"
            f"**点赞**：{number(performance.get('like_count'))}　"
            f"**评论**：{number(performance.get('comment_count'))}"
        )
        lines.append(
            f"- **收藏**：{number(performance.get('collect_count'))}　"
            f"**分享**：{number(performance.get('share_count'))}　"
            f"**作者粉丝**：{number(performance.get('follower_count'))}"
        )
        lines.append(
            f"- **评论/点赞**：{percent(performance.get('comment_like_ratio'))}　"
            f"**收藏/点赞**：{percent(performance.get('collect_like_ratio'))}　"
            f"**分享/点赞**：{percent(performance.get('share_like_ratio'))}"
        )
        lines.append("- 上述互动比值不是曝光转化率，只用于比较互动结构。")

    summary = content.get("one_sentence_summary")
    if isinstance(summary, (dict, list)):
        summary = ""
    lines.extend(["", "## 这条视频讲了什么", "", clean(summary or "暂时无法生成可靠总结。")])
    lines.extend([
        "",
        f"- **核心议题**：{clean(content.get('topic') or '暂未明确')}",
        f"- **适合谁看**：{clean(content.get('target_audience') or '暂未明确')}",
        f"- **看完得到什么**：{clean(content.get('viewer_value') or '暂未明确')}",
    ])

    structure = [item for item in _as_list(content.get("content_structure")) if isinstance(item, dict)]
    lines.extend(["", "## 内容是怎么讲清楚的", ""])
    for item in structure:
        lines.append(
            f"- **{clean(item.get('stage') or '内容推进')}｜{clean(item.get('title'))}**："
            f"{clean(item.get('summary'))}"
        )
    if not structure:
        lines.append("- 暂未可靠重建内容结构。")

    points = _as_list(content.get("core_points"))
    lines.extend(["", "## 值得记住的核心观点", ""])
    lines.extend(f"- {item}" for item in values(points, "point"))
    if not points:
        lines.append("- 暂未提取到证据充分的核心观点。")

    cases = _as_list(content.get("key_cases"))
    lines.extend(["", "## 关键案例与证据", ""])
    lines.extend(f"- {item}" for item in values(cases, "case"))
    if not cases:
        lines.append("- 视频未提供明确案例，或现有证据不足以确认。")

    conclusions = _as_list(content.get("author_conclusion"))
    lines.extend(["", "## 作者最后想让你相信什么", ""])
    lines.extend(f"- {item}" for item in values(conclusions, "conclusion"))
    if not conclusions:
        lines.append("- 暂未识别到作者明确给出的结论。")

    lines.extend(["", "## 内容为什么可能传播", ""])
    title_mechanic = mechanics.get("title") if isinstance(mechanics.get("title"), dict) else {}
    opening = mechanics.get("opening_hook") if isinstance(mechanics.get("opening_hook"), dict) else {}
    emotion = mechanics.get("emotion") if isinstance(mechanics.get("emotion"), dict) else {}
    need = mechanics.get("audience_need") if isinstance(mechanics.get("audience_need"), dict) else {}
    cta = mechanics.get("cta") if isinstance(mechanics.get("cta"), dict) else {}
    formulas = values(title_mechanic.get("formulas"), "text")
    lines.append(f"- **标题公式**：{' + '.join(formulas) if formulas else clean(legacy_viral.get('title_hook') or '信息不足')}")
    lines.append(f"- **开头钩子**：{clean(opening.get('mechanism') or legacy_viral.get('opening_hook') or '信息不足')}")
    content_types = values(mechanics.get("content_types"), "text")
    lines.append(f"- **内容类型**：{'、'.join(content_types) if content_types else '暂未明确'}")
    lines.append(f"- **信息推进**：{clean(mechanics.get('information_rhythm') or '暂未明确')}")
    lines.append(f"- **核心情绪**：{clean(emotion.get('primary') or '暂未明确')}（证据：{clean(emotion.get('evidence') or '信息不足')}）")
    lines.append(f"- **用户需求**：{clean(need.get('primary') or '暂未明确')}")
    lines.append(f"- **互动设计**：{clean(cta.get('type') or '暂未明确')}；目的：{clean(cta.get('purpose') or '暂未明确')}")

    hypotheses = [item for item in _as_list(payload.get("viral_hypotheses")) if isinstance(item, dict)]
    lines.extend(["", "## 传播假设（不是因果结论）", ""])
    confidence_labels = {"high": "高", "medium": "中", "low": "低"}
    for item in hypotheses:
        evidence = values(item.get("evidence"), "text")
        confidence = confidence_labels.get(str(item.get("confidence") or "").lower(), "未标注")
        lines.append(f"- **{clean(item.get('hypothesis') or '未命名假设')}**（置信度：{confidence}）")
        if evidence:
            lines.append(f"  证据：{'；'.join(evidence)}")
    if not hypotheses:
        lines.append("- 现有证据不足，暂不生成确定性传播归因。")

    reusable = [item for item in _as_list(transfer.get("reusable_genes")) if isinstance(item, dict)]
    lines.extend(["", "## 可以复用的爆款基因", ""])
    for item in reusable:
        conditions = values(item.get("conditions"), "text")
        lines.append(f"- **{clean(item.get('name') or '未命名基因')}**：{clean(item.get('mechanism') or '暂未说明')}")
        if conditions:
            lines.append(f"  适用条件：{'；'.join(conditions)}")
    if not reusable and legacy_playbook:
        lines.append(f"- **内容公式**：{clean(legacy_playbook.get('formula') or '暂未提取')}")
    elif not reusable:
        lines.append("- 暂未提取到证据充分、可执行的复用基因。")

    non_replicable = [item for item in _as_list(transfer.get("non_replicable_factors")) if isinstance(item, dict)]
    failures = values(transfer.get("failure_conditions"), "text")
    lines.extend(["", "## 复用边界", ""])
    for item in non_replicable:
        lines.append(f"- **不可直接复制｜{clean(item.get('factor') or '未知因素')}**：{clean(item.get('reason') or '暂未说明')}")
    for item in failures:
        lines.append(f"- **容易失效**：{item}")
    gaps = values(payload.get("data_gaps") or payload.get("limitations"), "text")
    for item in gaps[:8]:
        lines.append(f"- **数据缺口**：{item}")
    if not non_replicable and not failures and not gaps:
        lines.append("- 仍需结合完播、观看时长、评论内容和账号历史基线验证。")

    lines.extend(["", "## 原话摘录", ""])
    quotes = payload.get("source_quotes") or [item.text for item in _select_quote_evidence(bundle)]
    for quote in quotes:
        lines.append(f"> {clean(quote)}")
    if not quotes:
        lines.append("> 没有可用的字幕或 ASR 原文。")
    return "\n".join(lines)


def clean_asr_transcript(
    client: OpenAI,
    model: str,
    cues: Sequence[TimedText],
) -> str:
    """对 ASR 做一次独立可读性清洗；失败时返回带时间戳的原始合并稿。"""
    groups = _group_transcript_evidence(cues)
    if not groups:
        return "没有可用的 ASR 转写。"
    source_lines = [
        f"[{format_timestamp(item.start)}–{format_timestamp(item.end)}] {item.text}"
        for item in groups
    ]
    batch_size = _env_int("ASR_CLEAN_BATCH_LINES", 10)
    cleaned_batches: list[str] = []
    for offset in range(0, len(source_lines), batch_size):
        batch = source_lines[offset:offset + batch_size]
        source = "\n".join(batch)
        prompt = f"""把下面这组 ASR 逐行整理成可直接阅读的中文稿。

强制要求：
- 每一行都要补全逗号、句号、问号等标点，并按语义断句；不能原样输出无标点长句。
- 只修正明显同音字和错别字（尤其是人名、公司名、专业术语），不总结、不删减、不添加信息。
- 相邻且属于同一话题的句子组织成同一个段落；话题一旦切换，必须在切换处插入一个空行来分隔段落，同一话题的连续句之间不要插入空行。
- 每行仍必须逐行单独输出并原样保留该行时间戳；行数、顺序、时间戳都不能改变，不能合并也不能拆分行。
- FDE、AI、agent、skill 等术语原样保留，不自行展开。
- 只输出整理后的正文，不要标题或说明。

{source}
"""
        cleaned = _load_model_text_cache("asr_clean_batches", model, prompt)
        if cleaned is None:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    stream=False,
                    max_tokens=_env_int("ASR_CLEAN_BATCH_MAX_TOKENS", 1800),
                )
                candidate = response.choices[0].message.content.strip()
                candidate_lines = [line.strip() for line in candidate.splitlines()]
                expected_timestamps = [line.split("]", 1)[0] + "]" for line in batch]
                actual_timestamps = [line.split("]", 1)[0] + "]" for line in candidate_lines if "]" in line]
                has_punctuation = bool(re.search(r"[，。？！；：]", candidate))
                if actual_timestamps == expected_timestamps and has_punctuation:
                    cleaned = "\n".join(candidate_lines)
                    _save_model_text_cache("asr_clean_batches", model, prompt, cleaned)
            except Exception:
                cleaned = None
        if cleaned is None:
            cleaned = "\n".join(
                line if re.search(r"[。？！]$", line) else line + "。"
                for line in batch
            )
        cleaned_batches.append(cleaned)
    return "\n\n".join(cleaned_batches)


def parse_cleaned_asr_transcript(text: str, source: str = "asr:cleaned") -> list[TimedText]:
    cues: list[TimedText] = []

    def parse_time(value: str) -> float:
        parts = [float(item) for item in value.split(":")]
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds

    pattern = re.compile(
        r"^\s*\[(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d{1,3})?)"
        r"\s*[–—-]\s*"
        r"(?P<end>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d{1,3})?)\]\s*(?P<text>.+?)\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        content = match.group("text").strip()
        if content:
            cues.append(TimedText(
                parse_time(match.group("start")),
                parse_time(match.group("end")),
                content,
                source,
            ))
    return cues


def format_asr_transcript_for_delivery(text: str, paragraph_chars: int = 420) -> str:
    """移除内部时间码，把清洗后的 ASR 排成自然段落供飞书直接阅读。
    清洗稿中的空行视为语义话题的分段边界；无空行时退回按字符数堆段。"""
    timestamp = re.compile(r"^\s*\[[^\]]+\]\s*")
    paragraphs: list[str] = []
    current: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal current, length
        if current:
            paragraphs.append("".join(current))
            current, length = [], 0

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        content = timestamp.sub("", stripped).strip()
        if not content:
            continue
        current.append(content)
        length += len(content)
        if length >= paragraph_chars:
            flush()
    flush()

    if not paragraphs:
        return "没有可用的 ASR 逐字稿。"
    return "\n\n".join(paragraphs)


def analyze_video_evidence_first(
    video_path: str,
    client: OpenAI,
    model: str,
    *,
    title: Optional[str] = None,
    video_info: Optional[dict] = None,
    custom_focus: Optional[str] = None,
    max_frames: Optional[int] = None,
    progress: ProgressCallback = None,
) -> tuple[str, EvidenceBundle]:
    """Run the complete evidence-first analysis and return report + evidence."""
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    def update(value: float, message: str) -> None:
        if progress:
            progress(value, message)

    with tempfile.TemporaryDirectory(prefix="video_evidence_") as temp_dir:
        update(0.05, "正在读取视频媒体信息")
        media = inspect_video(video_path)
        subtitles, subtitle_warning = extract_subtitle_track(video_path, media, temp_dir)

        update(0.13, "正在处理音轨与字幕")
        transcript, transcript_status, transcript_warning = transcribe_audio(
            video_path, media, temp_dir, progress=progress
        )
        warnings = [item for item in (subtitle_warning, transcript_warning) if item]

        update(0.25, "正在进行连续、场景变化与文字变化采样")
        frames, scene_count, text_change_count = extract_timeline_frames(
            video_path, media, subtitles or transcript, temp_dir, max_frames=max_frames
        )
        if not frames:
            raise RuntimeError("没有提取到任何可分析画面，请检查 ffmpeg 配置")

        bundle = EvidenceBundle(
            media=media,
            frames=frames,
            subtitles=subtitles,
            transcript=transcript,
            transcript_status=transcript_status,
            warnings=warnings,
        )

        synthesis_model = os.getenv("SYNTHESIS_MODEL_ID", "").strip() or model
        if transcript:
            update(0.34, "正在独立清洗 ASR 断句、标点与明显识别错误")
            bundle.cleaned_transcript = clean_asr_transcript(
                client,
                os.getenv("ASR_CLEAN_MODEL_ID", "").strip() or synthesis_model,
                transcript,
            )
            cleaned_cues = parse_cleaned_asr_transcript(bundle.cleaned_transcript)
            if len(cleaned_cues) >= max(3, len(_group_transcript_evidence(transcript)) // 2):
                bundle.transcript = cleaned_cues

        update(0.43, f"已提取 {len(frames)} 个带时间戳画面，正在逐段观察")
        bundle.visual_observations = observe_visual_timeline(
            client, model, frames, [*subtitles, *transcript], progress=progress
        )

        update(0.78, "正在生成面向读者的结构化报告")
        structured = generate_audience_report_payload(
            client,
            synthesis_model,
            bundle,
            custom_focus,
            video_info,
        )
        bundle.analysis_payload = structured
        result = render_audience_report(
            structured,
            bundle,
            video_info=video_info,
            title=title,
        )

        update(1.0, "证据优先的视频分析完成")
        return result, bundle
