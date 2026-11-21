"""工具结果与上下文的截断。

两条规则来自真实 harness 的教训（Codex `utils/output-truncation`、dsh 的
tool-result-pruner）：

1. **从中间截，保留头尾**。头通常是表头/结论，尾通常是汇总/错误，砍掉哪一端
   都会让模型得出错误结论；中间是可以牺牲的部分。
2. **截断必须写进结果本身**。给原 token 数、原行数和一句明确的警告 —— 否则模型
   会以为它看到了全部数据，然后基于前几行给出全量结论。这是最危险的失败模式。

错误结果被截断后仍然标记为错误，绝不能因为截断变成"成功"。
"""
from __future__ import annotations

import json
import math


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def page(items: list, offset: int, limit: int, *, max_bytes: int = 40_000) -> dict:
    """按完整条目分页，不改写字段。至少给一项，游标总能前进。"""
    out = []
    size = 0
    for item in items[offset:offset + limit]:
        size += len(json_text(item).encode()) + 1
        if out and size > max_bytes:
            break
        out.append(item)
    end = offset + len(out)
    return {'items': out, 'total': len(items), 'next_offset': end if end < len(items) else None}

# 近似：中文与英文混合时约 2.5 字符/token；只用于预算，不用于计费。
CHARS_PER_TOKEN = 2.5


def approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def middle_truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """保留头尾、替换中间。返回 (文本, 是否截断)。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    head = max_chars // 2
    tail = max_chars - head
    removed = len(text) - head - tail
    marker = f"\n…（中间省略 {removed} 字符）…\n"
    return text[:head] + marker + text[-tail:], True


def tool_result_text(text: str, *, max_chars: int = 24_000) -> str:
    """给模型看的工具结果。超限时带显式警告头。"""
    original_tokens = approx_tokens(text)
    total_lines = text.count("\n") + 1
    clipped, truncated = middle_truncate(text, max_chars)
    if not truncated:
        return text
    header = (
        f"Warning: truncated output (original token count: {original_tokens})\n"
        f"Total output lines: {total_lines}\n\n"
    )
    return header + clipped


def error_result(message: str, *, code: str = "TOOL_ERROR") -> dict:
    """统一的工具错误形状：模型能读到 code，用户能看到 message。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def ok_result(payload: dict) -> dict:
    return {"ok": True, **payload}
