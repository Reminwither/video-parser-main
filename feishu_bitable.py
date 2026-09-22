"""Archive structured video analysis into a Feishu Bitable.

This is a server-side adaptation of ui-gen-record-feishu for the video-agent
workflow. It uses the bot application's tenant token, so no interactive
``lark-cli auth login`` is required.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_ROOT = "https://open.feishu.cn/open-apis"
CONFIG_PATH = Path(__file__).with_name(".feishu_bitable.json")
TABLE_NAME = "视频爆款分析记录"
FIELD_NAMES = [
    "平台", "作品ID", "作者", "作者粉丝", "视频链接", "封面链接", "发布时间", "时长",
    "播放", "点赞", "评论", "收藏", "分享", "评论点赞比", "收藏点赞比", "分享点赞比",
    "核心议题", "一句话总结", "目标受众", "观众价值", "内容结构", "核心观点", "关键案例",
    "作者结论", "内容类型", "标题公式", "开头钩子", "核心情绪", "用户需求", "互动设计",
    "传播假设", "可复用基因", "不可复制因素", "适用场景", "失效条件", "创作约束",
    "数据缺口", "原话摘录", "结构化JSON", "ASR原文", "分析时间",
]


class BitableArchiveError(RuntimeError):
    pass


_TOKEN = ""
_TOKEN_EXPIRES_AT = 0.0


def _tenant_token() -> str:
    global _TOKEN, _TOKEN_EXPIRES_AT
    if _TOKEN and time.time() < _TOKEN_EXPIRES_AT:
        return _TOKEN
    body = json.dumps({
        "app_id": os.getenv("FEISHU_APP_ID"),
        "app_secret": os.getenv("FEISHU_APP_SECRET"),
    }).encode("utf-8")
    request = Request(
        f"{API_ROOT}/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise BitableArchiveError(f"获取 tenant_access_token 网络失败: {exc}") from exc
    token = payload.get("tenant_access_token")
    if not token:
        raise BitableArchiveError(f"获取 tenant_access_token 失败: {payload.get('msg')}")
    _TOKEN = token
    _TOKEN_EXPIRES_AT = time.time() + max(60, int(payload.get("expire") or 7200) - 120)
    return token


def _request(method: str, path: str, *, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{API_ROOT}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {_tenant_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BitableArchiveError(f"飞书多维表格 HTTP {exc.code}: {detail[:500]}") from exc
    except (URLError, TimeoutError) as exc:
        raise BitableArchiveError(f"飞书多维表格网络失败: {exc}") from exc
    if payload.get("code") != 0:
        raise BitableArchiveError(f"飞书多维表格 API 失败 [{payload.get('code')}]: {payload.get('msg')}")
    return payload.get("data") or {}


def _load_config() -> dict:
    app_token = os.getenv("FEISHU_BITABLE_APP_TOKEN", "").strip()
    table_id = os.getenv("FEISHU_BITABLE_TABLE_ID", "").strip()
    if app_token and table_id:
        return {
            "app_token": app_token,
            "table_id": table_id,
            "base_url": os.getenv("FEISHU_BITABLE_BASE_URL", "").strip(),
        }
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def _save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_video_analysis_table() -> dict:
    """Create the Bitable and a stable text-first schema on first use."""
    config = _load_config()
    if config.get("app_token") and config.get("table_id"):
        app_token = config["app_token"]
        table_id = config["table_id"]
    else:
        data = _request("POST", "/bitable/v1/apps", body={
            "name": "视频爆款分析库",
            "time_zone": "Asia/Shanghai",
        })
        app = data.get("app") or {}
        app_token = app.get("app_token")
        if not app_token:
            raise BitableArchiveError("创建多维表格后没有返回 app_token")

        tables = _request("GET", f"/bitable/v1/apps/{app_token}/tables?page_size=100").get("items") or []
        if not tables:
            raise BitableArchiveError("多维表格中没有默认数据表")
        table_id = tables[0].get("table_id") or tables[0].get("id")
        _request("PATCH", f"/bitable/v1/apps/{app_token}/tables/{table_id}", body={"name": TABLE_NAME})
        config = {
            "app_token": app_token,
            "table_id": table_id,
            "base_url": app.get("url") or f"https://feishu.cn/base/{app_token}",
            "table_name": TABLE_NAME,
        }
        # 先持久化表指针；字段创建若被限流，下次会在同一张表继续补齐。
        _save_config(config)

    fields = _request(
        "GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields?page_size=100"
    ).get("items") or []
    primary = next((item for item in fields if item.get("is_primary")), fields[0] if fields else None)
    if not primary:
        raise BitableArchiveError("默认数据表中没有主字段")
    primary_id = primary.get("field_id") or primary.get("id")
    # 飞书对“字段未发生变化”返回 DataNotChange，而不是把它视为成功。
    # 因此只在首次建表或字段名确实不同时执行重命名，保证每次归档幂等。
    if primary.get("field_name") != "标题":
        _request(
            "PUT",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{primary_id}",
            body={"field_name": "标题", "type": 1},
        )

    existing = {item.get("field_name") for item in fields}
    for name in FIELD_NAMES:
        if name not in existing:
            _request(
                "POST",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                body={"field_name": name, "type": 1},
            )

    config.setdefault("base_url", f"https://feishu.cn/base/{app_token}")
    config.setdefault("table_name", TABLE_NAME)
    _save_config(config)
    return config


def _items(payload: dict, key: str) -> list[str]:
    result = []
    for item in payload.get(key) or []:
        if isinstance(item, dict):
            value = item.get({"core_points": "point", "key_cases": "case", "author_conclusion": "conclusion"}.get(key, "text"))
        else:
            value = item
        if value:
            result.append(str(value))
    return result


def _pretty(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _text_list(value: Any, key: str = "") -> list[str]:
    result = []
    for item in value or []:
        if isinstance(item, dict):
            selected = item.get(key) if key else None
            if selected is None:
                selected = item.get("text") or item.get("name") or item.get("factor")
        else:
            selected = item
        if selected not in (None, ""):
            result.append(str(selected))
    return result


def _ratio_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def archive_video_analysis(
    *,
    url: str,
    metadata: dict,
    payload: dict,
    cleaned_asr: str,
    duration_seconds: float,
    viewer_open_id: str = "",
) -> dict:
    """Append one analysis row and return ``record_id`` plus the Base URL."""
    config = ensure_video_analysis_table()
    share_warning = ""
    if viewer_open_id:
        shared = set(config.get("shared_open_ids") or [])
        if viewer_open_id not in shared:
            try:
                _request(
                    "POST",
                    f"/drive/v1/permissions/{config['app_token']}/members?type=bitable&need_notification=false",
                    body={
                        "member_type": "openid",
                        "member_id": viewer_open_id,
                        "perm": "full_access",
                    },
                )
                shared.add(viewer_open_id)
                config["shared_open_ids"] = sorted(shared)
                _save_config(config)
            except Exception as exc:
                share_warning = str(exc)
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
    content = payload.get("content") if isinstance(payload.get("content"), dict) else payload
    mechanics = payload.get("mechanics") if isinstance(payload.get("mechanics"), dict) else {}
    transfer = payload.get("transfer") if isinstance(payload.get("transfer"), dict) else {}
    author = metadata.get("author") if isinstance(metadata.get("author"), dict) else {}
    source_author = source.get("author") if isinstance(source.get("author"), dict) else {}
    structures = [
        f"{item.get('stage', '内容推进')}｜{item.get('title', '')}：{item.get('summary', '')}"
        for item in (content.get("content_structure") or []) if isinstance(item, dict)
    ]
    title_mechanic = mechanics.get("title") if isinstance(mechanics.get("title"), dict) else {}
    opening = mechanics.get("opening_hook") if isinstance(mechanics.get("opening_hook"), dict) else {}
    emotion = mechanics.get("emotion") if isinstance(mechanics.get("emotion"), dict) else {}
    need = mechanics.get("audience_need") if isinstance(mechanics.get("audience_need"), dict) else {}
    cta = mechanics.get("cta") if isinstance(mechanics.get("cta"), dict) else {}
    hypotheses = [
        f"[{item.get('confidence', 'unknown')}] {item.get('hypothesis', '')}｜证据：{'；'.join(_text_list(item.get('evidence')))}"
        for item in (payload.get("viral_hypotheses") or []) if isinstance(item, dict)
    ]
    reusable = [
        f"{item.get('name', '')}：{item.get('mechanism', '')}｜条件：{'；'.join(_text_list(item.get('conditions')))}"
        for item in (transfer.get("reusable_genes") or []) if isinstance(item, dict)
    ]
    non_replicable = [
        f"{item.get('factor', '')}：{item.get('reason', '')}"
        for item in (transfer.get("non_replicable_factors") or []) if isinstance(item, dict)
    ]
    fields = {
        "标题": str(source.get("title") or metadata.get("title") or "未获取标题"),
        "平台": str(source.get("platform") or metadata.get("platform") or ""),
        "作品ID": str(source.get("video_id") or metadata.get("video_id") or ""),
        "作者": str(source_author.get("nickname") or author.get("nickname") or metadata.get("author_name") or ""),
        "作者粉丝": str(performance.get("follower_count") if performance.get("follower_count") is not None else ""),
        "视频链接": url,
        "封面链接": str(source.get("cover_url") or metadata.get("cover_url") or ""),
        "发布时间": str(source.get("published_at") or ""),
        "时长": f"{duration_seconds:.1f} 秒",
        "播放": str(performance.get("play_count") if performance.get("play_count") is not None else ""),
        "点赞": str(performance.get("like_count") if performance.get("like_count") is not None else ""),
        "评论": str(performance.get("comment_count") if performance.get("comment_count") is not None else ""),
        "收藏": str(performance.get("collect_count") if performance.get("collect_count") is not None else ""),
        "分享": str(performance.get("share_count") if performance.get("share_count") is not None else ""),
        "评论点赞比": _ratio_text(performance.get("comment_like_ratio")),
        "收藏点赞比": _ratio_text(performance.get("collect_like_ratio")),
        "分享点赞比": _ratio_text(performance.get("share_like_ratio")),
        "核心议题": str(content.get("topic") or ""),
        "一句话总结": str(content.get("one_sentence_summary") or ""),
        "目标受众": str(content.get("target_audience") or ""),
        "观众价值": str(content.get("viewer_value") or ""),
        "内容结构": "\n".join(structures),
        "核心观点": "\n".join(_items(content, "core_points")),
        "关键案例": "\n".join(_items(content, "key_cases")),
        "作者结论": "\n".join(_items(content, "author_conclusion")),
        "内容类型": "、".join(_text_list(mechanics.get("content_types"))),
        "标题公式": " + ".join(_text_list(title_mechanic.get("formulas"))),
        "开头钩子": str(opening.get("mechanism") or ""),
        "核心情绪": str(emotion.get("primary") or ""),
        "用户需求": str(need.get("primary") or ""),
        "互动设计": f"{cta.get('type', '')}｜{cta.get('purpose', '')}".strip("｜"),
        "传播假设": "\n".join(hypotheses),
        "可复用基因": "\n".join(reusable),
        "不可复制因素": "\n".join(non_replicable),
        "适用场景": "\n".join(_text_list(transfer.get("applicable_scenarios"))),
        "失效条件": "\n".join(_text_list(transfer.get("failure_conditions"))),
        "创作约束": "\n".join(_text_list(transfer.get("creation_constraints"))),
        "数据缺口": "\n".join(_text_list(payload.get("data_gaps") or payload.get("limitations"))),
        "原话摘录": "\n".join(str(item) for item in (payload.get("source_quotes") or [])),
        "结构化JSON": json.dumps(payload, ensure_ascii=False),
        "ASR原文": cleaned_asr,
        "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    data = _request(
        "POST",
        f"/bitable/v1/apps/{config['app_token']}/tables/{config['table_id']}/records",
        body={"fields": fields},
    )
    record = data.get("record") or {}
    return {
        "record_id": record.get("record_id"),
        "base_url": config.get("base_url"),
        "share_warning": share_warning,
    }
