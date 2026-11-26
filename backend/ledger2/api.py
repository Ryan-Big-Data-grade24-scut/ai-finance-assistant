"""HTTP 路由：/api/ledger2/*。只做「开连接 → 调 service → 返回」，没有业务判断。

金额一律是整数分，前端自己格式化。每个写接口都顺手把最新的树带回来，前端不用再读一次。
"""
from __future__ import annotations

from contextlib import closing
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, PositiveInt

from backend import db

from . import service

router = APIRouter(prefix='/api/ledger2', tags=['ledger2'])


class OpIn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    type: Literal['init', 'uninstall', 'create_node', 'move', 'join', 'create_group', 'dissolve',
                  'split', 'unsplit', 'update',
                  'dissolve_node', 'rename_node', 'update_node',
                  'update_manual', 'add_member', 'remove_member', 'rename_group', 'dissolve_group']
    name: str | None = None
    note: str | None = None
    title: str | None = None
    items: list[str] | None = None
    target: str | None = None
    item: str | None = None
    node_id: str | None = None
    group_id: str | None = None
    tx: str | None = None
    tx_id: int | None = None
    tx_ids: list[int] | None = None
    style: str | None = None
    plan: dict | None = None


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    op: OpIn
    date_from: str | None = None
    date_to: str | None = None


class RangeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    date_from: str | None = None
    date_to: str | None = None


class ReplayIn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    event_ids: list[PositiveInt]


def _conn():
    conn = db.get_conn()
    service.ensure(conn)
    return conn


def _tree(conn, date_from: str | None, date_to: str | None) -> dict:
    conn.execute('BEGIN')
    return {**service.get_tree(conn, date_from, date_to), 'timeline': service.replay_history(conn)}


@router.get('/tree')
def tree(date_from: str | None = None, date_to: str | None = None):
    with closing(_conn()) as conn:
        return _tree(conn, date_from, date_to)


@router.post('/replay')
def replay_steps(data: ReplayIn):
    with closing(db.get_conn()) as conn:
        return service.replay_frames(conn, data.event_ids)


@router.get('/uninitialized')
def uninitialized(limit: int = 500):
    with closing(_conn()) as conn:
        return service.list_uninitialized(conn, limit)


@router.post('/op')
def op(data: ApplyIn):
    with closing(_conn()) as conn:
        result = service.apply(conn, data.op.model_dump(exclude_none=True))
        return {'result': result, **_tree(
            conn, data.date_from, data.date_to)}


@router.post('/undo')
def undo(data: RangeIn):
    with closing(_conn()) as conn:
        result = service.undo(conn)
        return {'result': result, **_tree(
            conn, data.date_from, data.date_to)}


@router.post('/redo')
def redo(data: RangeIn):
    with closing(_conn()) as conn:
        result = service.redo(conn)
        return {'result': result, **_tree(
            conn, data.date_from, data.date_to)}
