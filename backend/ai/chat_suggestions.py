"""Suggest supported, read-only questions without sharing transaction details."""
from __future__ import annotations

import asyncio
import re
from datetime import date

from .config import get_secret_config
from .providers import AIProviderError, complete_json

_PROMPT = """你是 Penny 财务助手的提问推荐器。根据所在页面和近期提问主题，从候选中选出最可能被点击的三条，按相关性排序。优先提供不同角度的查询。候选 ID 含义：summary 月度收支，categories 支出分类，merchants 高频商户，trend 环比，anomalies 大额及疑似重复，pending 待确认分类，day 当日明细。只返回候选 ID。格式：{"ids":["summary","categories","merchants"]}。"""


async def suggest_questions(
    context: dict[str, str], month: str | None, recent_questions: list[str],
) -> dict:
    month_label = f"{month[:4]}年{int(month[5:])}月" if month else "最近有账单的月份"
    candidates = {
        "summary": f"{month_label}收入和支出各是多少？",
        "categories": f"{month_label}有哪些支出分类，各占多少？",
        "merchants": f"{month_label}消费最多的商户有哪些？",
        "trend": f"{month_label}的支出和上月相比有什么变化？",
        "anomalies": f"{month_label}有哪些大额消费或疑似重复扣款？",
        "pending": "还有哪些历史交易需要确认 AI 分类？",
    }
    day = context.get("date", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        try:
            selected_day = date.fromisoformat(day)
        except ValueError:
            pass
        else:
            candidates["day"] = f"{selected_day:%Y-%m-%d}有哪些交易明细？"

    page = context.get("page", "")
    priorities = {
        "transactions": ("day", "merchants", "anomalies", "categories"),
        "star": ("categories", "merchants", "pending"),
        "ai-history": ("pending", "categories", "summary"),
        "import": ("pending", "anomalies", "summary"),
    }
    topic_markers = {
        "categories": ("分类", "类别"),
        "merchants": ("商户", "商家"),
        "trend": ("对比", "相比", "变化", "趋势"),
        "anomalies": ("异常", "重复扣款", "大额"),
    }
    topics = [topic for topic, markers in topic_markers.items()
              if any(marker in question for question in recent_questions for marker in markers)]
    order = [*priorities.get(page, ("summary",)), *topics, "categories", "trend", "merchants", "summary"]
    asked = {question.strip().rstrip("?？。") for question in recent_questions}
    available = {key: question for key, question in candidates.items() if question.rstrip("？") not in asked}
    if len(available) < 3:
        available = candidates
    ranked = [key for key in dict.fromkeys([*order, *available]) if key in available]
    config = get_secret_config()
    if config and config.allow_external_processing:
        try:
            result = await asyncio.wait_for(complete_json(
                config,
                system_prompt=_PROMPT,
                user_payload={
                    "page": page if page in (*priorities, "bill") else "other",
                    "topics": topics,
                    "candidate_ids": ranked,
                },
                timeout=3.0,
            ), timeout=3.0)
            picks = result.get("ids")
            if isinstance(picks, list):
                ranked = list(dict.fromkeys([
                    *(key for key in picks if isinstance(key, str) and key in available), *ranked,
                ]))
        except (TimeoutError, AIProviderError):
            pass
    return {"questions": [available[key] for key in ranked[:3]]}
