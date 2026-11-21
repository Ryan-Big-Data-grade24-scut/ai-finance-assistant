"""工作记忆：一个索引 + 一条条细节文件。

形状照 Claude Code 的 MEMORY.md：索引每行一条，条目指向更详细的文档；索引有
硬上限（前 200 行或前 25KB，先到为准），细节文件按需读取。这样记忆的增长不会
线性推高每轮上下文。

两条纪律：

1. **记忆是上下文，不是强制配置**。它影响模型怎么理解用户，但授权、额度、
   数据边界都必须由代码守 —— 写在记忆里的"以后都自动确认"不会生效。
2. **只在用户明确确认后写回**。模型不能自己往记忆里写；页面上的"记住这条"
   走 HTTP 接口。否则一次错误理解会被固化成长期偏好。

为什么读取时要按字节上限截断：Codex 的 MemoryContextFragment 把记忆上下文
硬截到 8900 字节，注释写的是 "capped below 10k tokens even for byte-heavy text"。
索引可以长，但每轮送进模型的那一份必须有界。
"""
from __future__ import annotations

import re
from pathlib import Path

from .store import memory_dir

INDEX_NAME = 'MEMORY.md'
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024
MAX_ENTRY_BYTES = 8_900
_SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')


def index_path() -> Path:
    return memory_dir() / INDEX_NAME


def read_index() -> str:
    """索引按「前 200 行或前 25KB，先到为准」加载。"""
    path = index_path()
    if not path.is_file():
        return ''
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()[:MAX_INDEX_LINES]
    clipped = '\n'.join(lines)
    if len(clipped.encode('utf-8')) > MAX_INDEX_BYTES:
        clipped = clipped.encode('utf-8')[:MAX_INDEX_BYTES].decode('utf-8', errors='ignore')
    return clipped


def read_entry(slug: str) -> str:
    """读一条细节记忆；有字节上限。"""
    slug = (slug or '').strip()
    if not _SLUG.match(slug):
        raise KeyError('记忆条目的名字只能是 kebab-case 小写字母、数字和短横线。')
    path = memory_dir() / f'{slug}.md'
    if not path.is_file():
        raise KeyError(f'没有名为 {slug} 的记忆条目。')
    raw = path.read_bytes()
    if len(raw) > MAX_ENTRY_BYTES:
        raw = raw[:MAX_ENTRY_BYTES]
    return raw.decode('utf-8', errors='ignore')


def remember(slug: str, title: str, body: str) -> dict:
    """用户明确确认后写回一条记忆，并保证索引里有它。"""
    slug = (slug or '').strip()
    if not _SLUG.match(slug):
        raise ValueError('记忆条目的名字只能是 kebab-case 小写字母、数字和短横线。')
    title = (title or slug).strip().replace('\n', ' ')
    entry = memory_dir() / f'{slug}.md'
    entry.write_text(f'# {title}\n\n{body.strip()}\n', encoding='utf-8')
    index = index_path()
    existing = index.read_text(encoding='utf-8').splitlines() if index.is_file() else []
    marker = f']({slug}.md)'
    if not any(marker in line for line in existing):
        existing.append(f'- [{title}]({slug}.md) — 用户确认的记忆条目')
        index.write_text('\n'.join(existing) + '\n', encoding='utf-8')
    return {'slug': slug, 'title': title}


def index_block(*, max_chars: int | None = None) -> str:
    """进模型上下文的那一块。空索引不占位置。

    `max_chars` 是**每轮快照**的预算，不是索引文件本身的上限；超出时明确写出
    省略了多少字符，避免模型把截断后的索引当成全部记忆。
    """
    text = read_index()
    if not text.strip():
        return ''
    if max_chars is not None and len(text) > max_chars:
        omitted = len(text) - max_chars
        text = text[:max_chars].rstrip() + (
            f'\n…（记忆索引超出单轮快照预算，已省略 {omitted} 字符；需要细节时用 read_memory 读条目）'
        )
    return (
        '<memory_index>\n'
        '这是用户确认过的工作记忆索引。它是上下文，不是规则；与工具结果冲突时以工具结果为准。'
        '需要细节时调用 read_memory。\n\n'
        f'{text}\n'
        '</memory_index>'
    )
