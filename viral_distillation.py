"""Deterministic platform signals for evidence-first viral-content distillation.

This module deliberately performs only calculations and schema normalization.
It does not decide *why* a post went viral; that remains a model hypothesis that
must cite content evidence and, where available, the signals produced here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _number(value: Any) -> int | float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _ratio(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round(float(numerator) / float(denominator), 4)


def _iso_time(value: Any) -> str | None:
    timestamp = _number(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def normalize_platform_evidence(
    metadata: dict | None,
    *,
    url: str = "",
    duration_seconds: float = 0.0,
) -> dict:
    """Return source, performance, deterministic signals and explicit gaps."""
    metadata = metadata or {}
    author = metadata.get("author") if isinstance(metadata.get("author"), dict) else {}
    statistics = metadata.get("statistics") if isinstance(metadata.get("statistics"), dict) else {}

    play = _number(statistics.get("play_count"))
    likes = _number(statistics.get("digg_count"))
    comments = _number(statistics.get("comment_count"))
    collects = _number(statistics.get("collect_count"))
    shares = _number(statistics.get("share_count"))
    followers = _number(author.get("follower_count"))
    # Some platform detail endpoints use 0 as an unavailable play count even
    # when interactions are non-zero. Treat that contradictory value as absent.
    if play == 0 and any((value or 0) > 0 for value in (likes, comments, collects, shares)):
        play = None
    known_interactions = [value for value in (likes, comments, collects, shares) if value is not None]
    interaction_total = sum(known_interactions) if known_interactions else None

    performance = {
        "play_count": play,
        "like_count": likes,
        "comment_count": comments,
        "collect_count": collects,
        "share_count": shares,
        "follower_count": followers,
        "interaction_total": interaction_total,
        # These are ratios to likes, not exposure conversion rates.
        "comment_like_ratio": _ratio(comments, likes),
        "collect_like_ratio": _ratio(collects, likes),
        "share_like_ratio": _ratio(shares, likes),
        "interaction_follower_ratio": _ratio(interaction_total, followers),
        # Only call them rates when the exposure denominator is available.
        "like_view_rate": _ratio(likes, play),
        "comment_view_rate": _ratio(comments, play),
        "collect_view_rate": _ratio(collects, play),
        "share_view_rate": _ratio(shares, play),
    }

    signals = []
    if likes is not None and comments is not None:
        signals.append({
            "name": "comment_over_like",
            "value": comments > likes,
            "evidence": f"评论 {comments}，点赞 {likes}",
            "meaning": "若为真，说明讨论/求助属性值得进一步检查；不能单独证明爆款原因。",
        })
    if followers is not None and interaction_total is not None:
        low_follower_high_interaction = followers < 1000 and interaction_total > followers * 0.5
        signals.append({
            "name": "low_follower_high_interaction",
            "value": low_follower_high_interaction,
            "evidence": f"粉丝 {followers}，已知互动总量 {interaction_total}",
            "meaning": "启发式信号：粉丝少于1000且已知互动超过粉丝数50%；不是平台官方爆款标准。",
        })

    gaps = []
    labels = {
        "play_count": "播放量",
        "like_count": "点赞数",
        "comment_count": "评论数",
        "collect_count": "收藏数",
        "share_count": "分享数",
        "follower_count": "作者粉丝数",
    }
    for key, label in labels.items():
        if performance.get(key) is None:
            gaps.append(f"缺少{label}")
    gaps.extend(["缺少完播率", "缺少平均观看时长", "缺少流量来源", "缺少增长曲线", "缺少评论区内容"])

    duration_ms = _number(metadata.get("duration_ms"))
    duration = float(duration_seconds or 0.0) or (float(duration_ms) / 1000 if duration_ms else 0.0)
    source = {
        "platform": metadata.get("platform") or "",
        "video_id": str(metadata.get("video_id") or ""),
        "url": url or metadata.get("share_url") or "",
        "share_url": metadata.get("share_url") or url,
        "title": metadata.get("title") or metadata.get("desc") or "",
        "description": metadata.get("desc") or "",
        "author": {
            "id": author.get("uid") or author.get("sec_uid") or "",
            "nickname": author.get("nickname") or metadata.get("author_name") or "",
            "signature": author.get("signature") or "",
        },
        "cover_url": metadata.get("cover_url") or "",
        "video_url": metadata.get("video_url") or "",
        "audio_url": metadata.get("audio_url") or "",
        "duration_seconds": round(duration, 3),
        "published_at": _iso_time(metadata.get("create_time")),
        "hashtags": list(metadata.get("hashtags") or []),
        "music": metadata.get("music") if isinstance(metadata.get("music"), dict) else {},
    }
    return {
        "source": source,
        "performance": performance,
        "distribution_signals": signals,
        "data_gaps": list(dict.fromkeys(gaps)),
    }


def compact_signal_context(platform_evidence: dict) -> dict:
    """Keep prompts small while preserving facts, definitions and missing data."""
    return {
        "performance": platform_evidence.get("performance") or {},
        "distribution_signals": platform_evidence.get("distribution_signals") or [],
        "data_gaps": platform_evidence.get("data_gaps") or [],
    }
