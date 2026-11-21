"""账本 V2 · 结构性护栏：不测功能，测「结构上不可达」。

语义对应 tests/test_ledger_guardrails.py（V1），按 backend/ledger2/ 的真实布局改写。
V1 的文件清单护栏不动；这里只守 ledger2。
"""
from __future__ import annotations

import ast
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'backend'
LEDGER2 = BACKEND / 'ledger2'
LEDGER1 = BACKEND / 'ledger'

EXPECTED_FILES = {
    '__init__.py', 'algebra.py', 'api.py', 'errors.py', 'facts.py', 'invariants.py',
    'migrate.py', 'model.py', 'ops.py', 'presets.py', 'reader.py', 'service.py', 'store.py',
    'tx.py',  # AgentCore 预览用：有外层事务时用 SAVEPOINT 参加，不自行提交
}
# 只有这两个文件可以写 ledger2_* 表：ops 是唯一的写入口（原语），store 只建表与种子。
WRITERS = {'ops.py', 'store.py'}
# 纯计算层：不碰数据库、不碰 HTTP。
PURE = {'algebra.py', 'model.py', 'reader.py', 'invariants.py'}
ALLOWED_BACKEND_MODULES = {'backend.db', 'backend.ledger.atoms', 'backend.ledger.amount'}

WRITE_PAT = re.compile(r'\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|(?<!DO )UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+(\w+)', re.I)
BUSINESS_TABLES = {'transactions_raw', 'tx_overrides', 'tx_deletions'}


def _py() -> list[Path]:
    return sorted(LEDGER2.glob('*.py'))


def _text(p: Path) -> str:
    return p.read_text(encoding='utf-8')


def _import_targets(path: Path, node: ast.AST) -> list[str]:
    """绝对化后的 import 目标（相对导入按包路径展开）。"""
    pkg = ['backend', 'ledger2']
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            base = pkg[: len(pkg) - (node.level - 1)]
            mod = '.'.join(base + ([node.module] if node.module else []))
        else:
            mod = node.module or ''
        out = [mod]
        out += [f'{mod}.{a.name}' for a in node.names]
        return out
    return []


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(_text(path))):
        found.update(_import_targets(path, node))
    return found


def test_ledger2_file_list_is_fixed():
    assert {p.name for p in _py()} == EXPECTED_FILES
    assert (LEDGER2 / 'schema.sql').exists()


def test_ledger2_does_not_import_v1_first_round_or_legacy_modules():
    banned = ('backend.reports', 'backend.overrides', 'backend.importer', 'backend.main')
    for p in _py():
        for target in _imports(p):
            assert not target.startswith(banned), f'{p.name} 越界导入 {target}'


def test_ledger2_imports_only_allowed_backend_modules():
    for p in _py():
        for node in ast.walk(ast.parse(_text(p))):
            for target in _import_targets(p, node):
                if not target.startswith('backend'):
                    continue
                if target == 'backend' or target.startswith('backend.ledger2'):
                    continue
                ok = any(target == m or target.startswith(m + '.') for m in ALLOWED_BACKEND_MODULES) \
                    or target in {'backend.ledger'}                      # `from backend.ledger import atoms` 的包本身
                assert ok, f'{p.name} 越界导入 {target}'


def test_only_facts_touches_v1_ledger_atoms():
    for p in _py():
        if p.name == 'facts.py':
            continue
        assert not any(t.startswith('backend.ledger.') or t == 'backend.ledger' for t in _imports(p)), p.name


def test_v1_ledger_does_not_import_ledger2():
    for p in sorted(LEDGER1.glob('*.py')):
        assert 'ledger2' not in ' '.join(_imports_any(p)), p.name


def _imports_any(p: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(_text(p))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or '')
            out.update(a.name for a in node.names)
    return out


def test_pure_layers_have_no_io_imports():
    forbidden = {'sqlite3', 'fastapi', 'pydantic', 'backend.db'}
    for name in PURE:
        got = {t.split('.')[0] if t.split('.')[0] in forbidden else t for t in _imports(LEDGER2 / name)}
        assert not (got & forbidden), f'{name} 是纯计算层，却引入 {got & forbidden}'


def test_no_direct_db_path_import_or_use():
    pat = re.compile(r'from\s+[\w.]*db\s+import\s+[^\n]*\bDB_PATH\b')
    for p in _py():
        t = _text(p)
        assert not pat.search(t), f'{p.name} 直接 import DB_PATH'
        assert 'DB_PATH' not in t, f'{p.name} 引用 DB_PATH（连接必须从 db.get_conn 来）'


def test_does_not_use_v1_counted_view():
    for p in _py():
        assert 'v_tx_counted' not in _text(p), p.name


def test_only_ops_and_store_write_ledger2_tables():
    for p in _py():
        hits = [m.group(2) for m in WRITE_PAT.finditer(_text(p)) if m.group(2).startswith('ledger2_')]
        if p.name in WRITERS:
            continue
        assert not hits, f'{p.name} 写了 {hits}'
    # 反向：写入口确实存在（防止护栏空转）
    assert any(WRITE_PAT.search(_text(LEDGER2 / n)) for n in WRITERS)


def test_ledger2_never_writes_business_or_v1_ledger_tables():
    for p in _py():
        for m in WRITE_PAT.finditer(_text(p)):
            table = m.group(2)
            assert table not in BUSINESS_TABLES, f'{p.name} 写业务表 {table}'
            assert not table.startswith('ledger_'), f'{p.name} 写 V1 账本表 {table}'
            assert table.startswith('ledger2_'), f'{p.name} 写了未知表 {table}'


def test_nobody_else_writes_ledger2_tables():
    for p in sorted(BACKEND.rglob('*.py')):
        if LEDGER2 in p.parents:
            continue
        for m in WRITE_PAT.finditer(_text(p)):
            assert not m.group(2).startswith('ledger2_'), f'{p.relative_to(ROOT)} 越权写 {m.group(2)}'


def test_ledger2_schema_only_defines_ledger2_tables():
    sql = _text(LEDGER2 / 'schema.sql')
    names = re.findall(r'CREATE\s+(?:TABLE|INDEX|VIEW|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)', sql, re.I)
    assert names and all(n.startswith(('ledger2_', 'idx_ledger2')) or 'ledger2' in n for n in names), names
    assert not re.search(r'\b(DROP|ALTER)\s+TABLE\s+(?!ledger2_)', sql, re.I)


def test_ledger2_has_no_print_to_stdout():
    for p in _py():
        for node in ast.walk(ast.parse(_text(p))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'print':
                kw = {k.arg: k.value for k in node.keywords}
                to = kw.get('file')
                to_stderr = isinstance(to, ast.Attribute) and to.attr == 'stderr'
                assert to_stderr, f'{p.name}:{node.lineno} print 到 stdout'


def test_only_migrate_writes_stdout():
    for p in _py():
        if p.name == 'migrate.py':
            continue
        assert 'sys.stdout' not in _text(p), p.name


def test_raw_layer_is_immutable_to_ledger2(tmp_path):
    """行为级验证：跑完一串 op 之后，原始层与删除/覆盖表一字不变。"""
    from backend.ledger2 import service
    from ledger2_support import make_db

    c = make_db(tmp_path / 'raw.db')

    def dump():
        return [
            [tuple(r) for r in c.execute(f'SELECT * FROM {t} ORDER BY 1')]
            for t in ('transactions_raw', 'tx_overrides', 'tx_deletions')
        ]

    before = dump()
    nid = service.apply(c, {'type': 'create_node', 'name': 'A', 'items': ['tx:1', 'tx:3']})['created']['node_id']
    service.apply(c, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})
    service.apply(c, {'type': 'split', 'tx_id': 3})
    service.apply(c, {'type': 'move', 'items': ['tx:5'], 'target': nid})
    service.undo(c)
    service.redo(c)
    service.get_tree(c, '2026-01-01', '2026-01-31')
    assert dump() == before
    c.close()


def test_get_tree_is_read_only(tmp_path):
    from backend.ledger2 import service
    from ledger2_support import make_db

    c = make_db(tmp_path / 'ro.db')
    service.apply(c, {'type': 'create_node', 'name': 'A', 'items': ['tx:1']})
    before = c.total_changes
    service.get_tree(c)
    service.get_tree(c, '2026-01-01', '2026-01-31')
    service.status(c)
    assert c.total_changes == before
    c.close()


def test_migrate_requires_explicit_db_and_does_not_default_to_real_db():
    r = subprocess.run([sys.executable, '-m', 'backend.ledger2.migrate'], cwd=ROOT,
                       capture_output=True, text=True, encoding='utf-8', errors='replace', env=_env())
    assert r.returncode != 0 and '--db' in r.stderr


def test_migrate_creates_tables_on_the_given_copy_only(tmp_path):
    db = tmp_path / 'copy.db'
    sqlite3.connect(db).close()
    r = subprocess.run([sys.executable, '-m', 'backend.ledger2.migrate', '--db', str(db)], cwd=ROOT,
                       capture_output=True, text=True, encoding='utf-8', errors='replace', env=_env())
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(db)
    names = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert {'ledger2_node', 'ledger2_tx', 'ledger2_net_group', 'ledger2_split_tx', 'ledger2_op_log'} <= names
    assert 'ledger2' in r.stdout


def _env():
    import os
    e = dict(os.environ)
    e['PYTHONPATH'] = str(ROOT)
    e['PYTHONIOENCODING'] = 'utf-8'
    return e
