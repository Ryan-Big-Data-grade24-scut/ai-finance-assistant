"""稳定的系统前缀，与每轮单独落日志的动态页面/记忆快照。

五段系统提示词都在 `prompts/` 下的 Markdown 文件里；改措辞不用改代码。
Skill 目录仍由 skills.catalog_text() 生成，05_skills.md 只保留固定标题和占位符。
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from . import memory, skills
from .truncate import json_text

PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'
SKILL_CATALOG_PLACEHOLDER = '{{skill_catalog}}'

# 每轮快照的硬边界。页面数据来自浏览器，不能假设它一定短、一定合法。
# 这些上限针对的是“送进模型的那一份”，不是页面本身能选多少项。
MAX_ROUTE_CHARS = 200
# 真库当前 3112 笔；5000 留出增长空间，保证“全选后交给助手”仍是一次完整选区。
MAX_SELECTION_REFS = 5_000
MAX_SELECTION_CHARS = 60_000
MAX_SELECTION_REF_CHARS = 80
MAX_STAGE_ID_CHARS = 120
MAX_STAGE_NAME_CHARS = 80
MAX_STAGE_TOTAL = 10_000_000
MAX_MEMORY_CHARS = 12_000
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')


def _text(value, max_chars: int) -> str:
    """去掉控制字符和首尾空白；长度按字符计。"""
    if not isinstance(value, str):
        return ''
    return _CONTROL.sub(' ', value).strip()[:max_chars].strip()


def _date(value) -> str:
    text = _text(value, 10)
    if not _DATE.fullmatch(text):
        return ''
    try:
        date.fromisoformat(text)
    except ValueError:
        return ''
    return text


def _selection(raw) -> tuple[list[str], int, int]:
    """保留完整引用，最多 5000 项 / 60k 字符；返回 (refs, 原始总数, 未注入数)。"""
    if not isinstance(raw, list):
        return [], 0, 0
    total = len(raw)
    kept: list[str] = []
    seen: set[str] = set()
    size = 0
    for item in raw:
        if not isinstance(item, str):
            continue
        if len(_CONTROL.sub(' ', item).strip()) > MAX_SELECTION_REF_CHARS:
            continue
        ref = _text(item, MAX_SELECTION_REF_CHARS)
        if not ref or ref in seen:
            continue
        if len(kept) >= MAX_SELECTION_REFS:
            continue
        if kept and size + len(ref) > MAX_SELECTION_CHARS:
            continue
        kept.append(ref)
        seen.add(ref)
        size += len(ref)
    return kept, total, total - len(kept)


def _staged(raw) -> dict | None:
    """只接受导入暂存的固定字段，忽略浏览器可能多传的任何内容。"""
    if not isinstance(raw, dict):
        return None
    stage_id = _text(raw.get('stage_id'), MAX_STAGE_ID_CHARS)
    if not stage_id:
        return None
    out: dict = {'stage_id': stage_id}
    name = _text(raw.get('platform_name'), MAX_STAGE_NAME_CHARS)
    if name:
        out['platform_name'] = name
    total = raw.get('total')
    if isinstance(total, int) and not isinstance(total, bool) and 0 <= total <= MAX_STAGE_TOTAL:
        out['total'] = total
    for key in ('date_from', 'date_to'):
        value = _date(raw.get(key))
        if value:
            out[key] = value
    return out


def _part(name: str) -> str:
    """读一段系统提示词。文件缺失直接报错，不静默退回旧字符串。"""
    return (PROMPTS_DIR / name).read_text(encoding='utf-8').strip()


def system_prompt() -> str:
    catalog = skills.catalog_text()
    skills_part = _part('05_skills.md')
    if catalog:
        skills_part = skills_part.replace(SKILL_CATALOG_PLACEHOLDER, catalog)
    else:
        skills_part = ''  # 没有 Skill 时整段不出现，保持旧行为
    return '\n\n'.join(part for part in (
        _part('01_identity.md'),
        _part('02_business.md'),
        _part('03_work.md'),
        _part('04_examples.md'),
        skills_part,
    ) if part)


def prefix_fingerprint(system: str, tools: list[dict]) -> str:
    """真实缓存前缀的指纹（含工具顺序/参数），不声称服务端一定命中。"""
    return hashlib.sha256((system + '\n--tools--\n' + json_text(tools)).encode('utf-8')).hexdigest()[:16]


def context_snapshot(*, now_text: str, page: dict | None = None, memory_text: str = '') -> str:
    """一轮只生成一次的系统快照。

    `page` 来自浏览器：完整助手传 `surface=page`，跨界面的宠物传 `surface=pet`。
    两者使用同一套路由/范围/选区/暂存字段，差别只在旁栏是否收起；模型因此能
    在宠物里直接接着处理大页面上看到的那批对象。
    """
    page = page if isinstance(page, dict) else {}
    lines = [f'当前时间：{_text(now_text, 80)}']
    route = _text(page.get('route'), MAX_ROUTE_CHARS)
    if route:
        lines.append(f'用户所在页面：{route}')
    if _text(page.get('surface'), 16) == 'pet':
        lines.append('对话界面：ATLAS 浮窗（旁栏已收起，页面范围与选区同完整 ATLAS 页面）')

    scope = page.get('scope') if isinstance(page.get('scope'), dict) else {}
    date_from = _date(scope.get('date_from'))
    date_to = _date(scope.get('date_to'))
    if date_from or date_to:
        lines.append(f'页面视图范围（不是写入边界）：{date_from or "不限"} ~ {date_to or "不限"}')

    refs, total, omitted = _selection(page.get('selection'))
    if refs:
        line = '页面完整选区（对象引用）：' + json_text(refs)
        if omitted > 0:
            line += (f'（本页共 {total} 项；已注入 {len(refs)} 项，其余 {omitted} 项因数量/格式/长度'
                     '上限未注入；不要据此认为选区只有这些）')
        lines.append(line)
    elif omitted > 0:
        lines.append(f'页面完整选区（对象引用）：本页共 {total} 项，均未注入（格式或长度超出单轮预算）；'
                     '不要据此认为没有选区。')

    staged = _staged(page.get('staged_import'))
    if staged:
        lines.append('本页附件曾解析成功（入库状态以工具回执为准）：' + json_text(staged))

    if memory_text.strip():
        lines.append('## 工作记忆索引\n' + memory_text.strip())
    return (
        '<turn_context>\n'
        '这是当前系统快照，不是用户的追加请求；它替代旧页面快照。\n\n'
        + '\n'.join(lines) + '\n</turn_context>'
    )


def memory_block(*, max_chars: int = MAX_MEMORY_CHARS) -> str:
    return memory.index_block(max_chars=max_chars)
