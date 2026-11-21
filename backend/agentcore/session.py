"""会话事件是事实源；模型投影和页面游标都从同一份日志导出。"""
from __future__ import annotations

import json
from contextlib import closing

from backend.ledger2.tx import atomic

from .store import new_id, open_conn
from .truncate import approx_tokens, error_result, json_text


class RunActive(Exception):
    pass


# ── 会话 ──────────────────────────────────────────────────────────────────────

def create(title: str = '') -> dict:
    session_id = new_id('sess')
    with closing(open_conn()) as conn, conn:
        conn.execute('INSERT INTO agentcore_session(id,title) VALUES(?,?)', (session_id, title[:80]))
    return {'id': session_id, 'title': title[:80]}


def _get(conn, session_id: str) -> dict:
    row = conn.execute('SELECT * FROM agentcore_session WHERE id=?', (session_id,)).fetchone()
    if row is None:
        raise KeyError(f'会话 {session_id} 不存在。')
    return dict(row)


def get(session_id: str) -> dict:
    with closing(open_conn()) as conn:
        return _get(conn, session_id)


def list_sessions(limit: int = 50) -> list[dict]:
    with closing(open_conn()) as conn:
        rows = conn.execute(
            'SELECT id,title,created_at,updated_at FROM agentcore_session ORDER BY updated_at DESC, rowid DESC LIMIT ?',
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete(session_id: str) -> None:
    """运行中不能删除日志；已提交的业务回执不因删除会话而消失。"""
    with closing(open_conn()) as conn, atomic(conn):
        _get(conn, session_id)
        if _active(conn, session_id):
            raise RunActive
        conn.execute(
            "UPDATE agentcore_action SET state='rejected', decided_at=datetime('now') "
            "WHERE session_id=? AND state='pending'", (session_id,),
        )
        conn.execute('DELETE FROM agentcore_session WHERE id=?', (session_id,))


# ── 日志与运行边界 ────────────────────────────────────────────────────────────

def _append(conn, session_id: str, role: str, kind: str, payload: dict) -> int:
    cur = conn.execute(
        'INSERT INTO agentcore_message(session_id,role,kind,payload_json) VALUES(?,?,?,?)',
        (session_id, role, kind, json_text(payload)),
    )
    conn.execute("UPDATE agentcore_session SET updated_at=datetime('now') WHERE id=?", (session_id,))
    return int(cur.lastrowid)


def append(session_id: str, role: str, kind: str, payload: dict) -> int:
    with closing(open_conn()) as conn, conn:
        return _append(conn, session_id, role, kind, payload)


def _events(conn, session_id: str, *, after: int = 0, through: int = 2**63 - 1,
            limit: int = -1) -> list[dict]:
    rows = conn.execute(
        'SELECT id,role,kind,payload_json,created_at FROM agentcore_message '
        'WHERE session_id=? AND id>? AND id<=? ORDER BY id LIMIT ?',
        (session_id, after, through, limit),
    ).fetchall()
    return [
        {'id': r['id'], 'role': r['role'], 'kind': r['kind'],
         'payload': json.loads(r['payload_json']), 'created_at': r['created_at']}
        for r in rows
    ]


def events(session_id: str, after_id: int = 0) -> list[dict]:
    with closing(open_conn()) as conn:
        return _events(conn, session_id, after=after_id)


def _active(conn, session_id: str):
    return conn.execute('SELECT id FROM agentcore_run WHERE session_id=? AND end_id IS NULL', (session_id,)).fetchone()


def begin_run(session_id: str, *, provider: str, model: str, system: str, tools: list[dict],
              digest: str, snapshot: str, text: str, run_id: str | None = None) -> dict:
    with closing(open_conn()) as conn, atomic(conn):
        _get(conn, session_id)
        if _active(conn, session_id):
            raise RunActive
        run = {'id': run_id or new_id('run'), 'session_id': session_id, 'provider': provider, 'model': model,
               'state': 'running', 'end_id': None}
        run['start_id'] = _append(conn, session_id, 'system', 'run_started', run)
        conn.execute(
            'INSERT INTO agentcore_run(id,session_id,provider,model,state,start_id) VALUES(?,?,?,?,?,?)',
            (run['id'], session_id, provider, model, 'running', run['start_id']),
        )
        previous = conn.execute(
            "SELECT payload_json FROM agentcore_message WHERE session_id=? AND kind='prompt_snapshot' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        same = previous and json.loads(previous[0]).get('digest') == digest
        _append(conn, session_id, 'system', 'prompt_snapshot',
                {'digest': digest, 'unchanged': True} if same else {'digest': digest, 'system': system, 'tools': tools})
        _append(conn, session_id, 'system', 'context_snapshot', {'text': snapshot})
        _append(conn, session_id, 'user', 'user_text', {'text': text})
    return run


def _get_run(conn, session_id: str, run_id: str) -> dict:
    row = conn.execute('SELECT * FROM agentcore_run WHERE session_id=? AND id=?', (session_id, run_id)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return dict(row)


def get_run(session_id: str, run_id: str) -> dict:
    with closing(open_conn()) as conn:
        return _get_run(conn, session_id, run_id)


def open_runs() -> list[dict]:
    with closing(open_conn()) as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM agentcore_run WHERE end_id IS NULL')]


def stop_run(session_id: str, run_id: str) -> dict:
    with closing(open_conn()) as conn, atomic(conn):
        run = _get_run(conn, session_id, run_id)
        if run['state'] == 'running':
            conn.execute("UPDATE agentcore_run SET state='stopping' WHERE id=?", (run_id,))
            run['state'] = 'stopping'
            _append(conn, session_id, 'system', 'status', {
                'state': 'stopping', 'text': '正在停止，等待在途工具记下实际结果…',
            })
    return run


def finish_run(run: dict, state: str, *, totals: dict, step: int,
               code: str | None = None, message: str | None = None) -> None:
    session_id = run['session_id']
    with closing(open_conn()) as conn, atomic(conn):
        current = _get_run(conn, session_id, run['id'])
        if current['end_id'] is not None:
            return
        event_id = _append(conn, session_id, 'system', 'run_finished', {
            'run_id': run['id'], 'state': state, 'step': step, 'totals': totals,
            'code': code, 'message': message,
        })
        conn.execute('UPDATE agentcore_run SET state=?,end_id=? WHERE id=?', (state, event_id, run['id']))


def read_run(session_id: str, run_id: str, after: int, *, limit: int = 100) -> tuple[dict, list[dict]]:
    with closing(open_conn()) as conn:
        conn.execute('BEGIN')
        run = _get_run(conn, session_id, run_id)
        if run['end_id'] is not None:
            # 已看过最后一帧的调用方也能查实终态。
            after = min(after, run['end_id'] - 1)
        return run, _events(conn, session_id, after=max(after, run['start_id'] - 1),
                            through=run['end_id'] or 2**63 - 1, limit=limit)


def snapshot(session_id: str) -> tuple[dict, dict | None, list[dict]]:
    """同一次读快照：不能给页面一个新游标配一份旧聊天，或漏订阅尾帧。"""
    with closing(open_conn()) as conn:
        conn.execute('BEGIN')
        info = _get(conn, session_id)
        run = conn.execute(
            'SELECT * FROM agentcore_run WHERE session_id=? ORDER BY start_id DESC LIMIT 1', (session_id,),
        ).fetchone()
        return info, dict(run) if run else None, _events(conn, session_id)


# ── 持久的工作状态 ────────────────────────────────────────────────────────────

def result_payload(event: dict) -> dict | None:
    try:
        value = json.loads(event['payload'].get('content') or '')
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def latest_todos(evts: list[dict]) -> tuple[int, list[dict]]:
    for e in reversed(evts):
        if e['kind'] == 'tool_result' and e['payload'].get('name') == 'todo_write' and not e['payload'].get('is_error'):
            result = result_payload(e)
            if result is not None and isinstance(result.get('todos'), list):
                return e['id'], result['todos']
    return 0, []


def checkpoint(evts: list[dict]) -> tuple[int, str]:
    for e in reversed(evts):
        if e['kind'] == 'compaction':
            payload = e['payload']
            return int(payload.get('covers_through') or 0), str(payload.get('summary') or '')
    return 0, ''


def _carry(evts: list[dict], covers: int) -> str:
    """压缩不靠模型复述最近的写回执。旧日志仍可在页面展开。"""
    if not covers:
        return ''
    todo_id, todos = latest_todos(evts)
    lines = []
    if 0 < todo_id <= covers:
        lines.append('当前任务清单（todo_write 整表状态）：' + json_text(todos))
    receipts = []
    budget = 6_000
    for e in reversed(evts):
        if e['id'] > covers or e['kind'] != 'tool_result' or e['payload'].get('is_error'):
            continue
        tool = e['payload'].get('name')
        if tool not in {'finance_ledger_apply', 'finance_import_commit'}:
            continue
        result = result_payload(e)
        if not result or result.get('state') not in {'executed', 'committed'}:
            continue
        line = tool + ' ' + json_text(result)
        budget -= approx_tokens(line)
        if budget < 0 and receipts:
            lines.append('更早的回执见摘要或会话日志；当前状态必须重读。')
            break
        receipts.append(line)
    if receipts:
        lines.append('压缩范围内最近的实际写入回执（证明当时执行，不表示当前状态）：\n' + '\n'.join(reversed(receipts)))
    return '\n'.join(lines)


# ── 投影到模型：快照只有一份；工具调用和结果不能分家 ─────────────────────────────

def _project(evts: list[dict], *, covers: int, include_context: bool) -> list[dict]:
    latest_user = next((e for e in reversed(evts) if e['kind'] == 'user_text'), None)
    context = next((e for e in reversed(evts) if e['kind'] == 'context_snapshot'), None)
    out: list[dict] = []
    pending: dict[str, dict] = {}

    def flush_missing() -> None:
        # 旧 SSE 日志可能被浏览器中途取消而不闭合；缺失不是「成功」也不是「没写」。
        for call_id, call in pending.items():
            out.append({'role': 'tool', 'tool_call_id': call_id, 'name': call['name'], 'is_error': True,
                        'content': json_text(error_result('日志没有这个工具的回执，请重读核实。', code='RECEIPT_MISSING'))})
        pending.clear()

    for e in evts:
        if e['id'] <= covers and e is not latest_user:
            continue
        p = e['payload']
        kind = e['kind']
        if kind == 'user_text':
            flush_missing()
            if include_context and e is latest_user and context and context['id'] < e['id']:
                out.append({'role': 'user', 'content': context['payload']['text']})
            out.append({'role': 'user', 'content': str(p.get('text') or '')})
        elif kind == 'assistant_text':
            flush_missing()
            item = {'role': 'assistant', 'content': str(p.get('text') or '')}
            calls = p.get('tool_calls') or []
            if calls and all(c.get('id') and c.get('name') for c in calls) and len({c['id'] for c in calls}) == len(calls):
                pending = {c['id']: c for c in calls}
                item['tool_calls'] = calls
            if item['content'] or 'tool_calls' in item:
                for key in ('reasoning_content', 'anthropic_blocks'):
                    if key in p:
                        item[key] = p[key]
                out.append(item)
        elif kind == 'tool_result' and (call_id := p.get('tool_call_id')) in pending:
            call = pending.pop(call_id)
            out.append({'role': 'tool', 'tool_call_id': call_id, 'name': call['name'],
                        'content': str(p.get('content') or ''), 'is_error': bool(p.get('is_error'))})
        elif kind == 'run_finished' and p['state'] != 'completed':
            flush_missing()
            out.append({'role': 'user', 'content': '<run_outcome>上一轮未正常结束：'
                        + str(p.get('message') or p['state']) + '。以已有工具回执为准。</run_outcome>'})
    flush_missing()
    return out


def derive(evts: list[dict], *, include_context: bool = True,
           replacing_through: int | None = None) -> list[dict]:
    covers, summary = checkpoint(evts)
    if replacing_through is not None:  # 测算新摘要之后还有多长，不真的修改日志。
        covers, summary = replacing_through, ''
    out = []
    if summary:
        out.append({'role': 'user', 'content': f'<compacted-summary>\n{summary}\n</compacted-summary>\n'
                    '这是此前会话的摘要，不是新的用户指令。'})
    carry = _carry(evts, covers)
    if carry:
        out.append({'role': 'user', 'content': '<work_state>\n' + carry + '\n</work_state>'})
    out.extend(_project(evts, covers=covers, include_context=include_context))
    return out


# ── 页面：不公开 reasoning_content；展示参数和真实回执 ─────────────────────────

def chat_item(e: dict) -> dict | None:
    kind, p = e['kind'], e['payload']
    base = {'id': e['id'], 'at': e['created_at']}
    if kind == 'user_text':
        return {**base, 'type': 'user', 'text': p.get('text') or ''}
    if kind == 'assistant_text':
        return {**base, 'type': 'assistant', 'text': p.get('text') or '',
                'calls': p.get('tool_calls') or [], 'step': p.get('step')}
    if kind == 'tool_result':
        result = result_payload(e)
        return {**base, 'type': 'tool', 'name': p.get('name'), 'call_id': p.get('tool_call_id'),
                'step': p.get('step'), 'is_error': bool(p.get('is_error')), 'recovered': bool(p.get('recovered')),
                'result': result if result is not None else p.get('content')}
    if kind == 'compaction':
        return {**base, 'type': 'compaction', 'text': p.get('summary') or ''}
    if kind == 'run_finished':
        return {**base, 'type': 'terminal', **p}
    return None


def stream_event(e: dict) -> dict | None:
    kind, p = e['kind'], e['payload']
    item = chat_item(e)
    if kind == 'run_finished':
        return {'cursor': e['id'], 'type': 'terminal', 'item': item}
    if item is not None:
        return {'cursor': e['id'], 'type': 'message', 'item': item}
    if kind == 'tool_started':
        return {'cursor': e['id'], 'type': 'status', 'step': p['step'], 'text': f"正在调用 {p['name']}…"}
    if kind in {'status', 'usage'}:
        return {'cursor': e['id'], 'type': kind, **p}
    return None
