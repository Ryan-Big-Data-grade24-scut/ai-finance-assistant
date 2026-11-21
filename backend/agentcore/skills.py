"""Skill：目录只放摘要，正文靠工具载入。

为什么不做成"把四份 Skill 全文塞进系统提示词"：
- 四份正文加起来会占掉大量前缀，而多数轮次只需要其中一份；
- 更关键的是缓存：系统提示词在缓存前缀里，内容变化会让整段前缀失效。把正文
  作为**对话消息**追加，前缀保持不变（Claude Code 官方缓存文档记录了这个做法）。

所以系统提示词里只有一份目录，并明确写"载入前不要执行"。载入后的正文包在
`<skill_content>` 里并对散文做 HTML 转义 —— 防止文档里的尖括号破坏框架，
也防止模型把文档内容误当成用户指令。

上限来自真实实现：单个 Skill ≤5000 token、合计 ≤25000 token，超了丢最旧的；
截断保留文件开头，所以**最重要的指令要写在 SKILL.md 最前面**。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from .truncate import approx_tokens

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / '.claude' / 'skills'
MAX_SKILL_TOKENS = 5_000
MAX_TOTAL_TOKENS = 25_000

_FRONTMATTER = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str
    when_to_use: str
    path: Path


def _parse(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding='utf-8')
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict = {}
    for line in match.group(1).splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[match.end():]


def catalog() -> list[SkillSummary]:
    if not SKILLS_DIR.is_dir():
        return []
    out: list[SkillSummary] = []
    for skill_md in sorted(SKILLS_DIR.glob('*/SKILL.md')):
        meta, _body = _parse(skill_md)
        name = meta.get('name') or skill_md.parent.name
        out.append(SkillSummary(
            name=name,
            description=meta.get('description') or '',
            when_to_use=meta.get('when_to_use') or meta.get('whenToUse') or '',
            path=skill_md,
        ))
    return out


def catalog_text() -> str:
    """进系统提示词的目录正文。只有摘要，不含正文。

    固定标题与使用提醒在 `prompts/05_skills.md`；这里只生成动态条目。
    """
    lines = []
    for skill in catalog():
        line = f'- `{skill.name}` — {skill.description}'
        if skill.when_to_use:
            line += f'（何时用：{skill.when_to_use}）'
        lines.append(line)
    return '\n'.join(lines)


def load(skill_id: str) -> dict:
    skill_id = (skill_id or '').strip()
    skills = {s.name: s for s in catalog()}
    if skill_id not in skills:
        raise KeyError(f'没有名为 {skill_id} 的 Skill。可用：{", ".join(sorted(skills)) or "无"}')
    skill = skills[skill_id]
    meta, body = _parse(skill.path)
    if meta.get('description') and meta['description'] not in body[:200]:
        body = f"{meta['description']}\n\n{body}"
    max_chars = int(MAX_SKILL_TOKENS * 2.5)
    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    return {
        'name': skill.name,
        'content': render(skill.name, body, truncated=truncated),
        'tokens': approx_tokens(body),
        'truncated': truncated,
        'resources': [],
    }


def render(name: str, body: str, *, truncated: bool = False) -> str:
    escaped = html.escape(body, quote=False)
    note = '\n（正文超出单份 Skill 上限，已截断；最前面的内容完整保留。）' if truncated else ''
    return (
        f'<skill_content name="{html.escape(name)}">\n'
        f'<skill_resources></skill_resources>\n'
        f'<skill_instructions>\n{escaped}{note}\n</skill_instructions>\n'
        f'</skill_content>'
    )


def enforce_total_budget(loaded: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """已载入的 Skill 正文按时间排序；超总量时丢最旧的。"""
    kept: list[tuple[str, str]] = []
    total = 0
    for name, content in reversed(loaded):
        cost = approx_tokens(content)
        if total + cost > MAX_TOTAL_TOKENS:
            continue
        kept.append((name, content))
        total += cost
    kept.reverse()
    return kept
