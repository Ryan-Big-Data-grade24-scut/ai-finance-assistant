"""独立于订阅连接的一轮：模型 → 闭合工具批 → 再调模型。单进程、每会话一轮。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator

from backend.ai import config as ai_config

from . import adapter, compact, prompt, registry, session, truncate
from .registry import ToolContext
from .store import new_id

log = logging.getLogger(__name__)
CONTEXT_LIMIT_TOKENS = 96_000  # 估算；真实窗口更小时处理 Provider 的溢出错误。
COMPACT_AT_RATIO = 0.80
RETRY_DELAY = 0.5
MODEL_RETRIES = 2
REPEATED_STEPS = 4
POLL_SECONDS = 0.25
_TOOL_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


class AgentcoreError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class _Stopped(Exception):
    pass


@dataclass
class _Run:
    info: dict
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    step: int = 0
    totals: dict = field(default_factory=lambda: adapter.Usage().as_dict())
    closed_steps: deque[int] = field(default_factory=lambda: deque(maxlen=2))
    messages_log: list[dict] = field(default_factory=list)

    @property
    def session_id(self) -> str:
        return self.info['session_id']

    def check_stop(self) -> None:
        if self.stop.is_set():
            raise _Stopped

    async def record(self, role: str, kind: str, payload: dict, *, model_visible: bool = False) -> int:
        eid = await asyncio.to_thread(session.append, self.session_id, role, kind, payload)
        if model_visible:
            self.messages_log.append({'id': eid, 'role': role, 'kind': kind, 'payload': payload})
        return eid

    async def status(self, text: str) -> None:
        await self.record('system', 'status', {'text': text, 'step': self.step})

    async def usage(self, usage: adapter.Usage) -> None:
        for key, value in usage.as_dict().items():
            self.totals[key] += value
        await self.record('system', 'usage', {'usage': usage.as_dict(), 'totals': dict(self.totals)})


_RUNNING: dict[str, _Run] = {}
_STARTING: set[asyncio.Task] = set()


def now_text() -> str:
    return datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')


def _json_args(raw: str):
    def reject_constant(value):
        raise ValueError(f'不是 JSON 常量：{value}')

    def unique_keys(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise ValueError('JSON 含重复字段。')
        return obj

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('JSON 数值超出有限范围。')
        return number

    return json.loads(raw, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_keys)


def _validated_calls(reply: adapter.ModelReply) -> list[tuple[dict, dict]]:
    """整个原生批先验完整性。不完整的尾调用不应让前面的调用先写入。"""
    if reply.stop_reason == 'max_tokens' or reply.dropped_tool_calls:
        raise AgentcoreError('OUTPUT_TRUNCATED', '模型输出达到长度上限；这一步的工具调用未执行。')
    if reply.stop_reason == 'blocked':
        raise AgentcoreError('MODEL_REFUSED', '模型拒绝或过滤了这次输出；这一步没有执行工具。')
    if (reply.stop_reason not in {'stop', 'tool_calls'}
            or (reply.stop_reason == 'tool_calls') != bool(reply.tool_calls)):
        raise AgentcoreError('INCOMPLETE_REPLY', '模型回复未完整结束；这一步没有执行工具。')
    seen = set()
    out = []
    for call in reply.tool_calls:
        if (not isinstance(call.id, str) or not call.id.strip() or call.id in seen
                or not isinstance(call.name, str) or not _TOOL_NAME.fullmatch(call.name)
                or not isinstance(call.arguments, str)):
            raise AgentcoreError('INCOMPLETE_TOOL_BATCH', '工具调用缺少有效 id、名称或参数；整批未执行。')
        try:
            args = _json_args(call.arguments)
            if not isinstance(args, dict):
                raise ValueError('工具参数必须是对象。')
            # 不允许 JSON 转义产生不可写入 SQLite / Provider 的孤立代理项。
            truncate.json_text(args).encode('utf-8')
        except (ValueError, RecursionError) as exc:
            raise AgentcoreError('INCOMPLETE_TOOL_BATCH', '工具参数不是完整 JSON 对象；整批未执行。') from exc
        seen.add(call.id)
        out.append(({'id': call.id, 'name': call.name, 'arguments': call.arguments}, args))
    return out


def _tool_context(info: dict, assistant_id: int, index: int) -> ToolContext:
    return ToolContext(session_id=info['session_id'], call_key=f"{info['id']}:{assistant_id}:{index}")


def _tool_result(call: dict, assistant_id: int, step: int, result: dict, is_error: bool,
                 *, recovered: bool = False) -> dict:
    return {'tool_call_id': call['id'], 'assistant_id': assistant_id, 'name': call['name'], 'step': step,
            'content': truncate.json_text(result), 'is_error': is_error, 'recovered': recovered}


def _close_unfinished(info: dict, *, stopped: bool = False) -> tuple[int, dict]:
    """先查真实提交回执，再封闭日志。没有执行入口，重启不会重放任何工具。"""
    evts = session.events(info['session_id'], info['start_id'] - 1)
    dispatched = {(e['payload']['assistant_id'], e['payload']['tool_call_id'])
                  for e in evts if e['kind'] == 'tool_started'}
    completed = {(e['payload']['assistant_id'], e['payload']['tool_call_id'])
                 for e in evts if e['kind'] == 'tool_result'}
    step = 0
    totals = adapter.Usage().as_dict()
    for event in evts:
        payload, eid = event['payload'], event['id']
        step = max(step, payload.get('step') or 0)
        if event['kind'] == 'usage':
            totals = payload['totals']
        if event['kind'] != 'assistant_text':
            continue
        step_at_call = payload.get('step') or 0
        for i, call in enumerate(payload.get('tool_calls') or []):
            if (eid, call['id']) in completed:
                continue
            receipt = None
            if (eid, call['id']) in dispatched:
                args = _json_args(call['arguments'])
                if isinstance(args, dict):
                    receipt = registry.recover(call['name'], args, _tool_context(info, eid, i))
                code, text = 'RECEIPT_UNAVAILABLE', '调用中断，没有可恢复的已提交回执；请重读状态。'
            else:
                code = 'STOPPED_BEFORE_CALL' if stopped else 'INTERRUPTED_BEFORE_CALL'
                text = '这次工具调用未派发。'
            result = truncate.ok_result(receipt) if receipt is not None else truncate.error_result(text, code=code)
            session.append(info['session_id'], 'tool', 'tool_result',
                           _tool_result(call, eid, step_at_call, result, receipt is None, recovered=receipt is not None))
    return step, totals


def recover_interrupted() -> None:
    """在单进程 Web 服务启动时执行，MCP 冷启动不封闭 Web 的运行。"""
    for info in session.open_runs():
        if info['id'] in _RUNNING:
            continue
        step, totals = _close_unfinished(info)
        session.finish_run(info, 'interrupted', totals=totals, step=step, code='PROCESS_INTERRUPTED',
                           message='服务曾中断。已核对可恢复的写入回执，没有重放工具')


async def _model(run: _Run, cfg, system: str, messages: list[dict], tools: list[dict]) -> adapter.ModelReply:
    """停止可以取消模型 HTTP；不能取消已派发的业务线程。重试只发生在此处。"""
    run.check_stop()
    stopper = asyncio.create_task(run.stop.wait())
    try:
        for attempt in range(MODEL_RETRIES + 1):
            run.check_stop()
            request = asyncio.create_task(adapter.complete(
                cfg, system=system, messages=messages, tools=tools, cache_key=run.session_id,
            ))
            try:
                await asyncio.wait((request, stopper), return_when=asyncio.FIRST_COMPLETED)
                run.check_stop()
                reply = request.result()
                if reply.stop_reason == 'stop' and not reply.tool_calls and not reply.text.strip():
                    raise adapter.AdapterError('EMPTY_RESPONSE', '模型返回了空内容。', retryable=True)
                await run.usage(reply.usage)
                return reply
            except adapter.AdapterError as exc:
                if not exc.retryable or attempt >= MODEL_RETRIES:
                    raise
                await run.status(f'模型请求暂未成功（{exc.code}），正在重试…')
                try:
                    await asyncio.wait_for(run.stop.wait(), timeout=RETRY_DELAY * (attempt + 1))
                except asyncio.TimeoutError:
                    pass
            finally:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    finally:
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
    raise AssertionError('模型重试状态未收束')


async def _compact(run: _Run, cfg, system: str, tools: list[dict], limit: int, *, overflow: bool) -> bool:
    evts = run.messages_log
    previous, _ = session.checkpoint(evts)
    history_end = max((e['id'] for e in evts if e['id'] < run.info['start_id'] and
                       e['kind'] in {'user_text', 'assistant_text', 'tool_result', 'run_finished'}), default=0)
    candidates = [i for i in (history_end, *run.closed_steps) if i > previous]
    if not candidates:
        return False
    # Provider 溢出说明实际窗口更小；优先保留后缀的估算此时不适用。
    boundary = candidates[-1]
    if not overflow:
        for i in candidates:
            retained = session.derive(evts, replacing_through=i)
            if compact.estimate(retained) < limit - 3_000:
                boundary = i
                break
    covered = [e for e in evts if e['id'] <= boundary or e['kind'] == 'compaction']
    history = session.derive(covered, include_context=False)
    await run.status('对话较长，正在整理已完成的步骤…')
    budget = min(compact.COMPACT_INPUT_TOKENS, max(4_000, limit // (3 if overflow else 2)))
    for attempt in range(2):
        request, omitted = compact.build_request(history, budget)
        try:
            reply = await _model(run, cfg, system, request, tools)
        except adapter.AdapterError as exc:
            if exc.code == 'CONTEXT_WINDOW_EXCEEDED' and attempt == 0 and budget > 4_000:
                budget = max(4_000, budget // 3)
                continue
            if overflow:
                raise
            await run.status('摘要请求失败，保留原有上下文继续。')
            return False
        run.check_stop()
        try:
            _validated_calls(reply)
            if reply.tool_calls:
                raise compact.CompactError('摘要回复试图调用工具。')
            summary = compact.extract(reply.text)
        except (AgentcoreError, compact.CompactError) as exc:
            if overflow:
                raise AgentcoreError('COMPACTION_FAILED', str(exc) + ' 已执行的回执仍保留。') from exc
            await run.status('未得到可用摘要，保留原有上下文继续。')
            return False
        await run.record('system', 'compaction', {'summary': summary, 'covers_through': boundary,
                                                  'omitted_segments': omitted}, model_visible=True)
        return True
    return False


async def _loop(run: _Run, cfg, system: str, tools: list[dict]) -> None:
    run.messages_log = await asyncio.to_thread(session.events, run.session_id)
    limit = int(CONTEXT_LIMIT_TOKENS * COMPACT_AT_RATIO) - truncate.approx_tokens(system + truncate.json_text(tools))
    seen: deque[str] = deque(maxlen=24)
    repeats = 0
    while True:
        run.check_stop()
        messages = session.derive(run.messages_log)
        if compact.estimate(messages) > limit and await _compact(run, cfg, system, tools, limit, overflow=False):
            messages = session.derive(run.messages_log)
        run.step += 1
        await run.status('正在等待模型回复…')
        try:
            reply = await _model(run, cfg, system, messages, tools)
        except adapter.AdapterError as exc:
            if exc.code != 'CONTEXT_WINDOW_EXCEEDED' or not await _compact(run, cfg, system, tools, limit, overflow=True):
                raise
            reply = await _model(run, cfg, system, session.derive(run.messages_log), tools)
        run.check_stop()
        calls = _validated_calls(reply)
        assistant = {'text': reply.text, 'tool_calls': [call for call, _ in calls],
                     'stop': reply.stop_reason, 'step': run.step}
        for key in ('reasoning_content', 'anthropic_blocks'):
            if (value := getattr(reply, key)) is not None:
                assistant[key] = value
        eid = await run.record('assistant', 'assistant_text', assistant, model_visible=True)
        if not calls:
            return

        signature = []
        result_id = None
        if all(registry.is_read_only(call['name']) for call, _ in calls):
            # 一批纯读互不依赖：先登记整批已派发，再真正并行执行。
            for call, _ in calls:
                await run.record('system', 'tool_started', {
                    'assistant_id': eid, 'tool_call_id': call['id'], 'name': call['name'], 'step': run.step,
                })
            if run.stop.is_set():
                for call, _ in calls:
                    skipped = truncate.error_result('这次工具调用未派发。', code='STOPPED_BEFORE_CALL')
                    await run.record('tool', 'tool_result',
                                     _tool_result(call, eid, run.step, skipped, True), model_visible=True)
                raise _Stopped
            outcomes = await asyncio.gather(*(
                asyncio.to_thread(registry.execute, call['name'], args, _tool_context(run.info, eid, i))
                for i, (call, args) in enumerate(calls)
            ))
            for (call, args), (result, is_error) in zip(calls, outcomes):
                result_id = await run.record(
                    'tool', 'tool_result', _tool_result(call, eid, run.step, result, is_error), model_visible=True)
                stable = {k: v for k, v in result.items() if k != 'elapsed_ms'}
                signature.append((call['name'], args, stable))
        else:
            # 任何写调用都保持模型给出的顺序；停止后不再派发后面的工具。
            for i, (call, args) in enumerate(calls):
                run.check_stop()
                await run.record('system', 'tool_started', {
                    'assistant_id': eid, 'tool_call_id': call['id'], 'name': call['name'], 'step': run.step,
                })
                if run.stop.is_set():
                    skipped = truncate.error_result('这次工具调用未派发。', code='STOPPED_BEFORE_CALL')
                    await run.record('tool', 'tool_result',
                                     _tool_result(call, eid, run.step, skipped, True), model_visible=True)
                    raise _Stopped
                result, is_error = await asyncio.to_thread(
                    registry.execute, call['name'], args, _tool_context(run.info, eid, i),
                )
                result_id = await run.record('tool', 'tool_result',
                                             _tool_result(call, eid, run.step, result, is_error), model_visible=True)
                stable = {k: v for k, v in result.items() if k != 'elapsed_ms'}
                signature.append((call['name'], args, stable))
        run.closed_steps.append(result_id)
        run.check_stop()
        fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, default=str).encode()).hexdigest()
        repeats = repeats + 1 if fingerprint in seen else 0
        seen.append(fingerprint)
        if repeats >= REPEATED_STEPS:
            raise AgentcoreError('NO_PROGRESS', '模型在重复相同调用，结果没有推进。已执行的回执和未完成清单仍保留。')


async def _drive(run: _Run, cfg, system: str, tools: list[dict]) -> None:
    state, code, message = 'completed', None, None
    try:
        try:
            await _loop(run, cfg, system, tools)
            run.check_stop()
        except _Stopped:
            state, code = 'stopped', 'USER_STOPPED'
            message = '已停止，不会派发新工具；停止前已经提交的操作没有回滚'
        except (AgentcoreError, adapter.AdapterError) as exc:
            state, code, message = 'failed', exc.code, exc.message
        except Exception:
            log.exception('AgentCore run failed (%s)', run.info['id'])
            state, code, message = 'failed', 'RUN_FAILED', '本轮中断，请核对已执行的回执'
        if state != 'completed':
            await asyncio.to_thread(_close_unfinished, run.info, stopped=state == 'stopped')
        await asyncio.to_thread(session.finish_run, run.info, state, totals=run.totals,
                                step=run.step, code=code, message=message)
    finally:
        _RUNNING.pop(run.info['id'], None)


def _report_crash(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error('AgentCore run could not persist its terminal state', exc_info=exc)


async def _start(session_id: str, text: str, page: dict | None) -> dict:
    cfg = await asyncio.to_thread(ai_config.get_secret_config)
    if cfg is None:
        raise AgentcoreError('NOT_CONFIGURED', '还没有配置可用的模型。请先到「AI 设置」填写接口、模型和 API Key。')
    if not cfg.allow_external_processing:
        raise AgentcoreError('NOT_AUTHORIZED', '尚未允许把账单数据发送给外部模型。请在「AI 设置」里打开外部处理开关。', status=403)
    system = prompt.system_prompt()
    tools = adapter.for_provider(cfg)
    snapshot = prompt.context_snapshot(now_text=now_text(), page=page, memory_text=prompt.memory_block())
    # 先登记 stop 事件。若另一个标签页在 SQLite 提交后、worker 启动前请求停止，也不会丢失。
    run_id = new_id('run')
    run = _Run({'id': run_id, 'session_id': session_id})
    _RUNNING[run_id] = run
    try:
        run.info = await asyncio.to_thread(session.begin_run, session_id, provider=cfg.provider, model=cfg.model,
                                          system=system, tools=tools, digest=prompt.prefix_fingerprint(system, tools),
                                          snapshot=snapshot, text=text, run_id=run_id)
    except KeyError as exc:
        raise AgentcoreError('SESSION_NOT_FOUND', '会话不存在。', status=404) from exc
    except session.RunActive as exc:
        raise AgentcoreError('RUN_ACTIVE', '这个会话还有一轮在运行，请接回该轮或先停止它。', status=409) from exc
    finally:
        if 'start_id' not in run.info:
            _RUNNING.pop(run_id, None)
    run.task = asyncio.create_task(_drive(run, cfg, system, tools))
    run.task.add_done_callback(_report_crash)
    return run.info


async def start_turn(session_id: str, text: str, page: dict | None = None) -> dict:
    # 一旦开始落盘，就要把 worker 接上；POST 断线不应留下无人执行的 active 行。
    start = asyncio.create_task(_start(session_id, text, page))
    _STARTING.add(start)
    start.add_done_callback(_STARTING.discard)
    try:
        return await asyncio.shield(start)
    except asyncio.CancelledError:
        start.add_done_callback(_report_crash)
        raise


async def request_stop(session_id: str, run_id: str) -> dict:
    run = _RUNNING.get(run_id)
    if run and run.session_id == session_id:
        run.stop.set()  # 先停止派发，再等待 SQLite 忙锁。
    return await asyncio.to_thread(session.stop_run, session_id, run_id)


async def shutdown() -> None:
    for run in _RUNNING.values():
        run.stop.set()
    await asyncio.gather(*_STARTING, return_exceptions=True)
    runs = list(_RUNNING.values())
    for run in runs:
        run.stop.set()
    await asyncio.gather(*(r.task for r in runs if r.task is not None), return_exceptions=True)


async def subscribe(session_id: str, run_id: str, after: int = 0) -> AsyncIterator[dict | None]:
    """订阅者只读日志；None 是保活注释，不属于游标。"""
    idle_for = 0.0
    while True:
        run, evts = await asyncio.to_thread(session.read_run, session_id, run_id, after)
        for event in evts:
            after = event['id']
            if (frame := session.stream_event(event)) is not None:
                yield frame
        if run['end_id'] is not None and after >= run['end_id']:
            return
        if evts:
            idle_for = 0.0
            continue
        await asyncio.sleep(POLL_SECONDS)
        idle_for += POLL_SECONDS
        if idle_for >= 10:
            idle_for = 0.0
            yield None
