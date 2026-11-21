"""本地上传 → 已解析暂存 → 原始流水。导入不会自动初始化 V2 账本。"""
from __future__ import annotations

import json
import logging
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from backend.importer import import_rows, parse_file
from backend.importer.base import ParseIssues, Row
from backend.ledger2.tx import atomic

from .store import new_id, open_conn, stage_dir
from .truncate import page

STAGE_TTL_HOURS = 12
log = logging.getLogger(__name__)


class ImportError_(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _row_path(stage_id: str) -> Path:
    return stage_dir() / f'{stage_id}.json'


def _cleanup(stage_id: str) -> None:
    try:
        _row_path(stage_id).unlink(missing_ok=True)
    except OSError:
        # 清理失败不能把已提交的导入冒充成失败。
        log.warning('Could not remove AgentCore stage %s', stage_id)


def stage_file(path: Path, filename: str, *, password: str | None = None) -> dict:
    """附件由本地表单提供；只保存解析后的行，不记录解压密码。"""
    issues = ParseIssues()
    try:
        rows = parse_file(Path(path), alipay_password=password, issues=issues)
    except ValueError as exc:
        raise ImportError_('PARSE_FAILED', str(exc)) from exc
    if not rows:
        raise ImportError_('NO_ROWS', '解析成功但没有任何有效数据行。')

    stage_id = new_id('imp')
    dates = [r.date for r in rows]
    summary = {'warnings': issues.warnings[:5], 'failed': issues.failed,
               'first_error': issues.first_error}
    _row_path(stage_id).write_text(
        json.dumps([asdict(r) for r in rows], ensure_ascii=False), encoding='utf-8'
    )
    try:
        with closing(open_conn()) as conn, atomic(conn):
            expired = conn.execute(
                "SELECT id FROM agentcore_import_stage WHERE state='staged' AND created_at < datetime('now', ?)",
                (f'-{STAGE_TTL_HOURS} hours',),
            ).fetchall()
            conn.executemany("UPDATE agentcore_import_stage SET state='cancelled' WHERE id=?", expired)
            conn.execute(
                'INSERT INTO agentcore_import_stage'
                '(id,batch_id,platform_id,platform_name,filename,total,date_from,date_to,summary_json) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (stage_id, new_id('batch'), rows[0].platform_id, rows[0].source,
                 filename, len(rows), min(dates), max(dates), json.dumps(summary, ensure_ascii=False)),
            )
    except Exception:
        _cleanup(stage_id)
        raise
    for row in expired:
        _cleanup(row['id'])
    return {
        'stage_id': stage_id, 'state': 'staged', 'platform_id': rows[0].platform_id, 'platform_name': rows[0].source,
        'total': len(rows), 'date_from': min(dates), 'date_to': max(dates), **summary,
        'note': '文件已解析，原始流水还未入库。',
    }


def _stage(conn, stage_id: str) -> dict:
    row = conn.execute(
        "SELECT *, created_at < datetime('now', ?) AS expired "
        'FROM agentcore_import_stage WHERE id=?', (f'-{STAGE_TTL_HOURS} hours', stage_id),
    ).fetchone()
    if row is None:
        raise ImportError_('STAGE_NOT_FOUND', '导入暂存不存在，请重新选择文件。')
    return dict(row)


def _require_staged(stage: dict) -> None:
    if stage['state'] != 'staged':
        raise ImportError_('STAGE_CLOSED', f'这个导入已经处理过（{stage["state"]}）。')
    if stage['expired']:
        raise ImportError_('STAGE_EXPIRED', '导入暂存已过期，请重新选择文件。')


def _rows(stage_id: str) -> list[Row]:
    try:
        raw = json.loads(_row_path(stage_id).read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise ImportError_('STAGE_NOT_FOUND', '导入暂存文件已不存在，请重新选择文件。') from exc
    return [Row(**item) for item in raw]


def _summary(stage: dict) -> dict:
    out = {k: v for k, v in stage.items() if k not in {'summary_json', 'id', 'expired'}}
    return {'stage_id': stage['id'], **out, **json.loads(stage['summary_json'])}


def _receipt(conn, stage: dict) -> dict | None:
    batch = conn.execute('SELECT * FROM import_batches WHERE batch_id=?', (stage['batch_id'],)).fetchone()
    if batch is None:
        return None
    summary = json.loads(stage['summary_json'])
    return {'stage_id': stage['id'], 'state': 'committed', 'receipt': {
        **dict(batch), 'platform_name': stage['platform_name'],
        'first_error': summary.get('first_error') or next(iter(summary.get('warnings') or []), None),
    }}


def stage_status(stage_id: str) -> dict:
    """上传表单恢复用；不读明细、不执行导入。"""
    with closing(open_conn()) as conn:
        conn.execute('BEGIN')
        stage = _stage(conn, stage_id)
        if receipt := _receipt(conn, stage):
            return receipt
        _require_staged(stage)
        return {**_summary(stage), 'note': '文件已解析，原始流水还未入库。'}


def committed_receipt(stage_id: str) -> dict | None:
    """恢复中断的一轮时只查回执，不会触发导入。"""
    with closing(open_conn()) as conn:
        try:
            return _receipt(conn, _stage(conn, stage_id))
        except ImportError_:
            return None


def _commit(conn, stage_id: str) -> dict:
    stage = _stage(conn, stage_id)
    receipt = _receipt(conn, stage)
    if receipt is None:
        _require_staged(stage)
        summary = json.loads(stage['summary_json'])
        issues = ParseIssues(failed=summary.get('failed', 0),
                             first_error=summary.get('first_error'), warnings=summary.get('warnings') or [])
        try:
            import_rows(_rows(stage_id), stage['platform_id'], stage['filename'], issues,
                        batch_id=stage['batch_id'], conn=conn)
        except ValueError as exc:
            raise ImportError_('IMPORT_FAILED', str(exc)) from exc
        receipt = _receipt(conn, stage)
    conn.execute("UPDATE agentcore_import_stage SET state='committed' WHERE id=?", (stage_id,))
    return receipt


def commit(stage_id: str) -> dict:
    # 锁住从查批次到写入的整个过程；回执和流水同一事务提交。
    with closing(open_conn()) as conn, atomic(conn):
        receipt = _commit(conn, stage_id)
    _cleanup(stage_id)
    return receipt


def read(args: dict) -> dict:
    mode = args.get('mode')
    offset, limit = args.get('offset', 0), args.get('limit', 25)
    with closing(open_conn()) as conn:
        conn.execute('BEGIN')
        if mode == 'batches':
            batches = conn.execute('SELECT * FROM import_batches ORDER BY created_at DESC, batch_id').fetchall()
            return {'mode': mode, **page([dict(row) for row in batches], offset, limit)}
        if mode == 'stages':
            stages = conn.execute(
                "SELECT * FROM agentcore_import_stage WHERE state='staged' "
                "AND created_at >= datetime('now', ?) ORDER BY created_at DESC, id",
                (f'-{STAGE_TTL_HOURS} hours',),
            ).fetchall()
            return {'mode': mode, **page([_summary(dict(row)) for row in stages], offset, limit)}
        if mode != 'stage':
            raise ImportError_('BAD_MODE', '导入读取模式应为 stage / stages / batches。')
        stage = _stage(conn, args.get('stage_id') or '')
        receipt = _receipt(conn, stage)
        if receipt:
            return {'mode': mode, **receipt}
        _require_staged(stage)
        try:
            rows = _rows(stage['id'])
        except ImportError_:
            # 可能在我们开启读事务之后才提交并清理了文件，用新快照再查一次。
            conn.rollback()
            receipt = _receipt(conn, stage)
            if receipt:
                return {'mode': mode, **receipt}
            raise
        return {'mode': mode, 'stage': _summary(stage), **page([asdict(r) for r in rows], offset, limit)}


# 旧页面上的待确认卡仍可手工处理，但不进入新模型工具清单。

def prepare(stage_id: str, *, session_id: str | None) -> dict:
    with closing(open_conn()) as conn, atomic(conn):
        stage = _stage(conn, stage_id)
        _require_staged(stage)
        rows = _rows(stage_id)
        keys = sorted({r.dedupe_key for r in rows})
        duplicates = 0
        for start in range(0, len(keys), 400):
            chunk = keys[start:start + 400]
            marks = ','.join('?' * len(chunk))
            duplicates += conn.execute(
                f'SELECT COUNT(*) FROM transactions_raw WHERE dedupe_key IN ({marks})', chunk
            ).fetchone()[0]
        by_status: dict[str, int] = {}
        by_direction: dict[str, int] = {}
        for row in rows:
            status = row.status or '未知'
            by_status[status] = by_status.get(status, 0) + 1
            by_direction[row.direction] = by_direction.get(row.direction, 0) + 1
        preview = {
            **_summary(stage), 'by_status': by_status, 'by_direction': by_direction,
            'amount_sum_expense': round(sum(r.amount for r in rows if r.direction == '支出'), 2),
            'duplicate_estimate': duplicates,
        }
        action_id = new_id('act')
        conn.execute(
            'INSERT INTO agentcore_action(id, session_id, kind, title, op_json, preview_json) VALUES(?,?,?,?,?,?)',
            (action_id, session_id, 'import', f"导入 {stage['platform_name']}账单",
             json.dumps({'type': 'import', 'stage_id': stage_id, 'batch_id': stage['batch_id']}),
             json.dumps({'preview': preview}, ensure_ascii=False)),
        )
    return {'action_id': action_id, 'state': 'pending', 'preview': preview,
            'note': '这张旧式导入卡还没有入库；如果已用助手直接导入，确认只会读到同一回执。'}


def _pending_stage(conn, action_id: str, session_id: str | None) -> str:
    row = conn.execute('SELECT * FROM agentcore_action WHERE id=? AND kind=?', (action_id, 'import')).fetchone()
    if row is None:
        raise ImportError_('ACTION_NOT_FOUND', '这个待确认导入不存在。')
    if row['state'] != 'pending':
        raise ImportError_('ACTION_NOT_PENDING', f'这个操作已经处理过（{row["state"]}）。')
    if session_id and row['session_id'] and row['session_id'] != session_id:
        raise ImportError_('ACTION_SESSION_MISMATCH', '这个操作不属于当前会话。')
    return json.loads(row['op_json'])['stage_id']


def confirm(action_id: str, *, session_id: str | None = None) -> dict:
    with closing(open_conn()) as conn, atomic(conn):
        stage_id = _pending_stage(conn, action_id, session_id)
        result = _commit(conn, stage_id)
        conn.execute(
            "UPDATE agentcore_action SET state='confirmed', result_json=?, decided_at=datetime('now') WHERE id=?",
            (json.dumps(result['receipt'], ensure_ascii=False), action_id),
        )
    _cleanup(stage_id)
    return {'action_id': action_id, 'state': 'confirmed', 'receipt': result['receipt']}


def reject(action_id: str, *, session_id: str | None = None) -> dict:
    with closing(open_conn()) as conn, atomic(conn):
        stage_id = _pending_stage(conn, action_id, session_id)
        conn.execute(
            "UPDATE agentcore_action SET state='rejected', decided_at=datetime('now') WHERE id=?", (action_id,),
        )
        cancelled = conn.execute(
            "UPDATE agentcore_import_stage SET state='cancelled' WHERE id=? AND state='staged'",
            (stage_id,),
        ).rowcount
    if cancelled:
        _cleanup(stage_id)
    return {'action_id': action_id, 'state': 'rejected'}
