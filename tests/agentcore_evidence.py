"""CSV 证据只读测试目录里的合成库；同一场景不覆盖、每个观察点各自取一致快照。"""
from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from backend import db


def _tables(directory: Path) -> dict:
    source = directory / 'synthetic.db'
    assert db.DB_PATH == source and db.DB_DIR == directory and not source.is_symlink()
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('BEGIN')
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
            cur = conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
            out[name] = ([col[0] for col in cur.description], sorted(cur.fetchall(), key=repr))
        return out


def _tree_text(item: dict, depth: int = 0) -> str:
    name = (item.get('title') or item.get('item') or item['ref']) if item['type'] == 'tx' else item['name']
    ref = item.get('ref') or item['id']
    line = f"{'  ' * depth}{name} [{ref}] e={item['e']} i={item['i']}"
    return '\n'.join([line, *(_tree_text(child, depth + 1) for child in item.get('children', item.get('members', [])))])


class Evidence:
    def __init__(self, root: Path | None, directory: Path, name: str):
        self.out = root / name if root is not None else None
        self.directory = directory
        self.name = name
        self.steps = []
        self.snapshots = {}
        if self.out is not None:
            self.out.mkdir()
            self.initial = _tables(directory)

    def step(self, command: str, why: str, observed: str) -> None:
        self.steps.append((command, why, observed))

    def checkpoint(self, name: str) -> None:
        if self.out is not None:
            self.snapshots[name] = _tables(self.directory)

    @staticmethod
    def _csv(path: Path, columns, rows) -> None:
        with path.open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)

    def finish(self, tree: dict, ids, *, artifacts=None) -> None:
        if self.out is None:
            return
        result = _tables(self.directory)
        assert result.keys() == self.initial.keys(), '证据需要在应用建表后开始记录'
        changed = [t for t in result if any(s[t] != self.initial[t] for s in [*self.snapshots.values(), result])]
        unchanged = sorted(result.keys() - set(changed))
        reverted = [t for t in changed if result[t] == self.initial[t]]
        out = self.out
        for folder, snapshot, names in [('初始', self.initial, list(self.initial)), ('结果', result, changed)]:
            dest = out / folder
            dest.mkdir()
            for t in names:
                self._csv(dest / f'{t}.csv', *snapshot[t])
        for label, snapshot in self.snapshots.items():
            names = [t for t in changed if snapshot[t] != self.initial[t] and snapshot[t] != result[t]]
            if names:
                dest = out / '中间' / label
                dest.mkdir(parents=True)
                for t in names:
                    self._csv(dest / f'{t}.csv', *snapshot[t])
        changes = '、'.join(changed) or '无'
        (out / '结果' / '说明.md').write_text(
            f'# 表变化\n\n有变化：{changes}。\n\n'
            f'中途变过、终态复原：{"、".join(reverted) or "无"}。\n\n'
            f'各观察点均未变（只导出初始一份）：{"、".join(unchanged) or "无"}。\n', encoding='utf-8',
        )
        lines = [f'# {self.name}', '', '仅使用虚构账单和临时库；如有模型调用，使用本地脚本，不外发。']
        for i, (command, why, observed) in enumerate(self.steps, 1):
            lines += ['', f'## {i}. {command}', '', f'- 为什么：{why}', f'- 实测：{observed}']
        (out / '00_操作说明.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self._csv(out / 'id对照.csv', ['ID', '含义'], ids)
        (out / '读出树.json').write_text(json.dumps(tree, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (out / '读出树.txt').write_text('金额单位：分\n' + _tree_text(tree['tree']) + '\n', encoding='utf-8')
        for filename, value in (artifacts or {}).items():
            (out / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
