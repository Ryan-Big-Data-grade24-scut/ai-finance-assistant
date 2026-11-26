"""HTTP 接口：启动和停止运行、订阅持久事件、本地上传和处理旧卡。

订阅只读日志；连接断开不会停止运行。新工具直接执行，旧卡仍只由用户手工处理。
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.concurrency import run_in_threadpool

from backend import db, uploads
from backend.ai import privacy

from . import actions, import_tool, memory, runtime, session
from .store import open_conn

router = APIRouter(prefix='/api/agentcore', tags=['agentcore'])
log = logging.getLogger(__name__)

MAX_TURN_CHARS = 8_000


class CreateIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = ''


class RenameIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class TurnIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str
    page: dict | None = None


class DecideIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str | None = None


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slug: str
    title: str = ''
    body: str


class ImportPrepareIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stage_id: str
    session_id: str | None = None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={'code': code, 'message': message})


def _action_row(action_id: str) -> dict:
    with closing(open_conn()) as conn:
        row = conn.execute(
            'SELECT * FROM agentcore_action WHERE id=?', (action_id,)
        ).fetchone()
    if row is None:
        raise KeyError(action_id)
    return dict(row)


def _action_view(row: dict) -> dict:
    preview = json.loads(row['preview_json'] or '{}')
    return {
        'id': row['id'],
        'kind': row['kind'],
        'title': row['title'],
        'state': row['state'],
        'op': json.loads(row['op_json'] or '{}'),
        'preview': preview.get('preview') or preview.get('diff') or {},
        'result': json.loads(row['result_json']) if row['result_json'] else None,
        'created_at': row['created_at'],
        'decided_at': row['decided_at'],
    }


# ── 健康 ──────────────────────────────────────────────────────────────────────

@router.get('/health')
def health() -> dict:
    return {'ok': True, **db.health_snapshot(), 'context': runtime.context_limits()}


# ── 会话 ──────────────────────────────────────────────────────────────────────

@router.post('/sessions')
def create_session(data: CreateIn) -> dict:
    return session.create(data.title)


@router.get('/sessions')
def list_sessions() -> dict:
    return {'sessions': session.list_sessions()}


@router.patch('/sessions/{session_id}')
def rename_session(session_id: str, data: RenameIn):
    try:
        return session.rename(session_id, data.title)
    except KeyError as exc:
        return _error(404, 'SESSION_NOT_FOUND', str(exc))


@router.delete('/sessions/{session_id}')
def delete_session(session_id: str):
    try:
        session.delete(session_id)
    except KeyError as exc:
        return _error(404, 'SESSION_NOT_FOUND', str(exc))
    except session.RunActive:
        return _error(409, 'RUN_ACTIVE', '请等本轮结束后再删除会话。')
    return {'ok': True}


@router.get('/sessions/{session_id}')
def get_session(session_id: str):
    try:
        info, run, evts = session.snapshot(session_id)
    except KeyError as exc:
        return _error(404, 'SESSION_NOT_FOUND', str(exc))
    with closing(open_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM agentcore_action WHERE session_id=? ORDER BY created_at DESC LIMIT 50',
            (session_id,),
        ).fetchall()
    run_events = [e for e in evts if run and e['id'] >= run['start_id']]
    latest_status = next((frame['text'] for e in reversed(run_events)
                          if e['kind'] in {'status', 'tool_started'} and (frame := session.stream_event(e))), '')
    latest_usage = next((e['payload']['totals'] for e in reversed(run_events)
                         if e['kind'] in {'usage', 'run_finished'}), None)
    _, todos = session.latest_todos(evts)
    policy = privacy.current()
    return {
        'session': info,
        'run': run,
        'cursor': evts[-1]['id'] if evts else 0,
        'messages': [
            item if item['type'] == 'user' else policy.redact(item, data_keys=item['type'] == 'tool')
            for e in evts if (item := session.chat_item(e)) is not None
        ],
        'todos': policy.redact(todos),
        'status': latest_status if run and run['end_id'] is None else '',
        'step': next((e['payload']['step'] for e in reversed(run_events) if e['payload'].get('step') is not None), 0),
        'totals': latest_usage,
        'actions': [_action_view(dict(r)) for r in rows],
        'memory_index': memory.read_index(),
    }


# ── 运行和只读订阅 ──────────────────────────────────────────────────────────────

@router.post('/sessions/{session_id}/turns', status_code=202)
async def send_turn(session_id: str, data: TurnIn):
    text = data.text.strip()
    if not text:
        return _error(400, 'EMPTY_TEXT', '消息不能为空。')
    if len(text) > MAX_TURN_CHARS:
        return _error(400, 'TEXT_TOO_LONG', f'单条消息最多 {MAX_TURN_CHARS} 个字符。')
    try:
        return await runtime.start_turn(session_id, text, data.page)
    except runtime.AgentcoreError as exc:
        return _error(exc.status, exc.code, exc.message)


@router.get('/sessions/{session_id}/turns/{run_id}/events')
async def turn_events(session_id: str, run_id: str, after: int = Query(0, ge=0, le=2**63 - 1)):
    try:
        await run_in_threadpool(session.get_run, session_id, run_id)
    except KeyError:
        return _error(404, 'RUN_NOT_FOUND', '本轮不存在。')

    async def events():
        try:
            policy = await run_in_threadpool(privacy.current)
            async for frame in runtime.subscribe(session_id, run_id, after):
                if frame is None:
                    yield ': keep-alive\n\n'
                else:
                    if (item := frame.get('item')) and item['type'] != 'user':
                        frame['item'] = policy.redact(item, data_keys=item['type'] == 'tool')
                    yield f'id: {frame["cursor"]}\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n'
        except asyncio.CancelledError:
            raise  # 断开的是订阅，不是 worker。
        except Exception:
            log.exception('AgentCore subscription stopped (%s)', run_id)
            # 不伪造终态；客户端从已有游标重接。

    return StreamingResponse(
        events(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache, no-store', 'X-Accel-Buffering': 'no'},
    )


@router.post('/sessions/{session_id}/turns/{run_id}/stop')
async def stop_turn(session_id: str, run_id: str):
    try:
        return await runtime.request_stop(session_id, run_id)
    except KeyError:
        return _error(404, 'RUN_NOT_FOUND', '本轮不存在。')


# ── 上传与导入暂存 ────────────────────────────────────────────────────────────

@router.post('/upload')
async def upload(file: UploadFile = File(...),
                 alipay_password: str | None = Form(None)) -> dict:
    """用户在这张表单里选文件、填解压密码。两者都不进对话、不进工具参数。"""
    with TemporaryDirectory(prefix='agentcore_upload_') as temp_dir:
        path, name = await uploads.save_upload(file, Path(temp_dir))
        try:
            return await run_in_threadpool(import_tool.stage_file, path, name, password=alipay_password)
        except import_tool.ImportError_ as exc:
            return _error(400, exc.code, exc.message)


@router.get('/import/stages/{stage_id}')
def get_stage(stage_id: str):
    """刷新页面时检查之前的附件还在暂存、已入库，还是已过期。不需要重传。"""
    try:
        return import_tool.stage_status(stage_id)
    except import_tool.ImportError_ as exc:
        return _error(404, exc.code, exc.message)


@router.post('/import/prepare')
def prepare_import(data: ImportPrepareIn):
    """手工导入选项：不需要模型，生成卡片后由用户确认。"""
    try:
        return import_tool.prepare(data.stage_id, session_id=data.session_id)
    except import_tool.ImportError_ as exc:
        return _error(400, exc.code, exc.message)


# ── 待确认动作 ────────────────────────────────────────────────────────────────

@router.get('/actions')
def list_actions(state: str = 'pending', unclaimed: bool = False,
                 session_id: str | None = None) -> dict:
    """`unclaimed=true` 只列历史上没有会话归属的旧卡。"""
    if state not in {'pending', 'confirmed', 'rejected'}:
        return _error(400, 'BAD_STATE', 'state 只能是 pending / confirmed / rejected。')
    sql = 'SELECT * FROM agentcore_action WHERE state=?'
    params: list = [state]
    if session_id:
        sql += ' AND session_id=?'
        params.append(session_id)
    if unclaimed:
        sql += ' AND session_id IS NULL'
    sql += ' ORDER BY created_at DESC LIMIT 100'
    with closing(open_conn()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {'actions': [_action_view(dict(r)) for r in rows]}


@router.get('/actions/{action_id}')
def get_action(action_id: str) -> dict:
    try:
        return _action_view(_action_row(action_id))
    except KeyError:
        return _error(404, 'ACTION_NOT_FOUND', '这个待确认操作不存在。')


@router.post('/actions/{action_id}/confirm')
def confirm_action(action_id: str, data: DecideIn) -> dict:
    try:
        row = _action_row(action_id)
    except KeyError:
        return _error(404, 'ACTION_NOT_FOUND', '这个待确认操作不存在。')
    try:
        if row['kind'] == 'import':
            return import_tool.confirm(action_id, session_id=data.session_id)
        return actions.confirm(action_id, session_id=data.session_id)
    except (actions.ActionError, import_tool.ImportError_) as exc:
        return _error(400, exc.code, exc.message)


@router.post('/actions/{action_id}/reject')
def reject_action(action_id: str, data: DecideIn) -> dict:
    try:
        row = _action_row(action_id)
    except KeyError:
        return _error(404, 'ACTION_NOT_FOUND', '这个待确认操作不存在。')
    try:
        if row['kind'] == 'import':
            return import_tool.reject(action_id, session_id=data.session_id)
        return actions.reject(action_id, session_id=data.session_id)
    except (actions.ActionError, import_tool.ImportError_) as exc:
        return _error(400, exc.code, exc.message)


# ── 工作记忆（只允许用户写）──────────────────────────────────────────────────

@router.post('/memory')
def remember(data: MemoryIn) -> dict:
    try:
        return memory.remember(data.slug, data.title, data.body)
    except ValueError as exc:
        return _error(400, 'BAD_SLUG', str(exc))
