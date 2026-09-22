#!/usr/bin/env python3
"""
文本内容 AI 分析器
接收从网页/公众号文章提取的纯文本，调用 Qwen LLM 生成结构化分析报告。
"""

import os
from openai import OpenAI
from dotenv import load_dotenv
from configs.logging_config import logger

load_dotenv()

QWEN_API_BASE_URL = os.getenv('QWEN_API_BASE_URL', 'https://api-inference.modelscope.cn/v1')
QWEN_API_KEY = os.getenv('QWEN_API_KEY', '')
QWEN_MODEL_ID = os.getenv('QWEN_MODEL_ID', 'Qwen/Qwen3-VL-8B-Instruct')

# 纯文本分析可使用更快的模型，若配置了 TEXT_MODEL_ID 则优先使用
TEXT_MODEL_ID = os.getenv('TEXT_MODEL_ID', '') or QWEN_MODEL_ID

# 单次送入 LLM 的最大字符数（控制 token 用量）
MAX_CONTENT_CHARS = 12000


def analyze_text_content(title: str, content: str, url: str,
                         custom_focus: str = None) -> str:
    """
    对文本内容进行结构化 AI 分析。

    Args:
        title: 内容标题
        content: 正文纯文本
        url: 来源链接
        custom_focus: 用户额外关注点（可选）

    Returns:
        Markdown 格式的分析报告
    """
    client = OpenAI(
        base_url=QWEN_API_BASE_URL,
        api_key=QWEN_API_KEY,
        timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "180")),
        max_retries=1,
    )

    truncated = content[:MAX_CONTENT_CHARS]
    if len(content) > MAX_CONTENT_CHARS:
        truncated += "\n\n...(正文过长，已截断)"

    focus = custom_focus.strip() if custom_focus and custom_focus.strip() else "无额外分析要求"

    prompt = f"""你是严谨的内容分析员。请根据下方原始内容用中文生成结构化分析报告。

核心纪律：
1. Source-first：每个重要结论必须能回到原文段落；无法定位的内容不得写成事实。
2. Evidence / Inference 分离：E1=原文直接陈述，E2=上下文支持的合理释义，E3=你的分析推论。
3. 不得把 E2/E3 写成作者原话；不得编造数据、引用或因果关系。
4. 每个观点至少结合上下文，不得孤立截句。
5. 原文可能存在偏见或错误，请保持批判性视角。

输出结构（严格按此顺序）：

## 内容概要
用 3-5 句话概括全文核心内容。

## 核心论点
逐条列出作者试图传达的主要观点，每条标注 [E1/E2/E3]。

## 关键信息提取
提取人名、公司、产品、数字、价格、日期、工具与流程；逐项给出来源段落，未出现则写"未发现"。

## 论证结构分析
按 Problem -> Cause -> Evidence -> Solution -> Result 重建；缺失环节写"原文未提供"。

## 批判性评价
指出逻辑漏洞、证据缺口、潜在偏见或无法验证之处，不能为了完整而补齐。

## 行动建议
把可迁移建议与原文事实分开；每项说明适用前提。如无可写"暂无明确行动建议"。

---

标题：{title or '无标题'}
来源：{url}

正文：
{truncated}

用户额外关注点：{focus}
"""

    logger.info(f"Text analysis started | model={TEXT_MODEL_ID} | content_len={len(content)}")

    response = client.chat.completions.create(
        model=TEXT_MODEL_ID,
        messages=[{"role": "user", "content": prompt}],
        stream=False,
    )

    result = response.choices[0].message.content.strip()
    logger.info(f"Text analysis completed | result_len={len(result)}")
    return result
