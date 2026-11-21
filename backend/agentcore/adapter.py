"""OpenAI-compatible 与 Anthropic 原生调用；不从正文猜工具或状态。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from backend.ai.config import AIConfig
from backend.ai.providers import _endpoint, _safe_provider_error

from .truncate import json_text

# 输出上限是 AgentCore 自己的请求参数，不是上下文窗口。旧值 8192 对长任务太紧：
# 一次复杂工具调用或较长推理就会在参数中途被截断。多数兼容服务接受 32768；
# 更小/更大的 Provider 用环境变量覆盖，不写死在这个工具里。
DEFAULT_MAX_TOKENS = max(1024, int(os.getenv('AGENTCORE_MAX_TOKENS', '32768')))
# 长上下文的一次回复可能超过两分钟；120s 会把仍在生成的模型请求当成断网重试。
# 这是本地 HTTP 请求的等待上限，不是任务步数或上下文窗口；Provider 慢时用环境变量调大。
DEFAULT_TIMEOUT = max(30.0, float(os.getenv('AGENTCORE_TIMEOUT', '600')))

_CONTEXT_HINTS = (
    'context_length_exceeded', 'maximum context length', 'prompt is too long',
    'context window', 'too many tokens', 'reduce the length',
)


class AdapterError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str | None
    name: str | None
    arguments: str | None  # OpenAI 原串持久化；不把缺失/截断的参数补成 {}。


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'cache_read_tokens': self.cache_read_tokens,
            'cache_write_tokens': self.cache_write_tokens,
        }


@dataclass
class ModelReply:
    text: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = 'stop'
    dropped_tool_calls: int = 0
    reasoning_content: str | None = None
    anthropic_blocks: list[dict] | None = None


async def complete(config: AIConfig, *, system: str, messages: list[dict],
                   tools: list[dict], cache_key: str | None = None,
                   timeout: float = DEFAULT_TIMEOUT) -> ModelReply:
    """一次完整 Provider 回复。页面 SSE 按步骤推进，不冒充 token 流。"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if config.provider == 'anthropic':
                return await _anthropic(client, config, system, messages, tools)
            return await _openai(client, config, system, messages, tools, cache_key)
    except httpx.TimeoutException as exc:
        raise AdapterError('NETWORK', '模型请求超时。', retryable=True) from exc
    except httpx.HTTPError as exc:
        raise AdapterError('NETWORK', f'无法连接模型服务：{exc.__class__.__name__}。', retryable=True) from exc
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise AdapterError('BAD_RESPONSE', '模型返回的响应结构不符合所选协议。') from exc


def _fail(response: httpx.Response, api_key: str) -> AdapterError:
    detail = _safe_provider_error(response, api_key)
    status = response.status_code
    if status in (400, 413, 422) and any(hint in detail.lower() for hint in _CONTEXT_HINTS):
        return AdapterError('CONTEXT_WINDOW_EXCEEDED', '上下文超出模型窗口，需要压缩。')
    if status in (401, 403):
        return AdapterError('AUTH', detail)
    if status == 404:
        return AdapterError('NOT_FOUND', detail)
    if status == 429:
        return AdapterError('RATE_LIMIT', detail, retryable=True)
    if status >= 500:
        return AdapterError('SERVER', detail, retryable=True)
    if status in (400, 413, 422):
        return AdapterError('BAD_REQUEST', detail)
    return AdapterError('HTTP', detail)


def _check(response: httpx.Response, api_key: str) -> dict:
    if response.status_code >= 400:
        raise _fail(response, api_key)
    return response.json()


def _require_content(reply: ModelReply) -> ModelReply:
    if reply.stop_reason == 'stop' and not reply.tool_calls and not reply.text.strip():
        raise AdapterError('EMPTY_RESPONSE', '模型返回了空内容。', retryable=True)
    return reply


def _stop_reason(reason: str | None, *, normal: set[str], calls: list, expects_calls: set[str]) -> str:
    if reason in {'length', 'max_tokens'}:
        return 'max_tokens'
    if reason in {'content_filter', 'refusal'}:
        return 'blocked'
    if reason in expects_calls:
        return 'tool_calls' if calls else 'incomplete'
    if reason in normal:
        return 'incomplete' if calls else 'stop'
    return 'incomplete'  # 没收到终止原因（断流）、未知协议状态，都不能派发写入。


# ── OpenAI-compatible ────────────────────────────────────────────────────────

def _openai_wire(messages: list[dict], *, replay_reasoning: bool = True) -> list[dict]:
    out: list[dict] = []
    for message in messages:
        role = message['role']
        if role == 'tool':
            out.append({'role': 'tool', 'tool_call_id': message['tool_call_id'], 'content': message['content']})
            continue
        calls = message.get('tool_calls') or []
        item = {'role': role, 'content': (message.get('content') or None) if calls else message['content']}
        if calls:
            item['tool_calls'] = [{
                'id': c['id'], 'type': 'function',
                'function': {'name': c['name'], 'arguments': c['arguments']},
            } for c in calls]
        if role == 'assistant' and replay_reasoning and 'reasoning_content' in message:
            item['reasoning_content'] = message['reasoning_content']
        out.append(item)
    return out


async def _openai(client: httpx.AsyncClient, config: AIConfig, system: str,
                  messages: list[dict], tools: list[dict], cache_key: str | None) -> ModelReply:
    official = (urlparse(config.base_url).hostname or '').lower() == 'api.openai.com'
    cap = 'max_completion_tokens' if official else 'max_tokens'
    body = {
        'model': config.model, cap: DEFAULT_MAX_TOKENS,
        'messages': [{'role': 'system', 'content': system}] + _openai_wire(messages, replay_reasoning=not official),
    }
    if tools:
        body.update(tools=tools, tool_choice='auto')
    if official and cache_key:
        body['prompt_cache_key'] = cache_key
    response = await client.post(
        _endpoint(config.base_url, 'chat/completions'),
        headers={'Authorization': f'Bearer {config.api_key}', 'content-type': 'application/json'}, json=body,
    )
    data = _check(response, config.api_key)
    choices = data.get('choices') or []
    if not choices:
        raise AdapterError('EMPTY_RESPONSE', '模型没有返回任何候选回复。', retryable=True)
    choice = choices[0]
    message = choice.get('message') or {}
    text = message.get('content') or message.get('refusal') or ''
    if isinstance(text, list):
        text = '\n'.join(part.get('text', '') for part in text if isinstance(part, dict))
    raw_calls = message.get('tool_calls') or []
    stop = _stop_reason(choice.get('finish_reason'), normal={'stop'}, calls=raw_calls, expects_calls={'tool_calls'})
    if message.get('refusal'):
        stop = 'blocked'
    calls = []
    if stop == 'tool_calls':
        for call in raw_calls:
            fn = call.get('function') or {}
            if call.get('type', 'function') != 'function':
                stop = 'incomplete'
                calls = []
                break
            calls.append(ToolCall(call.get('id'), fn.get('name'), fn.get('arguments')))
    usage_raw = data.get('usage') or {}
    details = usage_raw.get('prompt_tokens_details') or {}
    cache_read = int(details.get('cached_tokens') or usage_raw.get('prompt_cache_hit_tokens') or 0)
    prompt_tokens = int(usage_raw.get('prompt_tokens') or 0)
    usage = Usage(
        input_tokens=int(usage_raw.get('prompt_cache_miss_tokens', max(0, prompt_tokens - cache_read))),
        output_tokens=int(usage_raw.get('completion_tokens') or 0), cache_read_tokens=cache_read,
    )
    reasoning = message.get('reasoning_content')
    return _require_content(ModelReply(
        text=str(text), tool_calls=calls, usage=usage, stop_reason=stop,
        dropped_tool_calls=len(raw_calls) if stop == 'max_tokens' else 0,
        reasoning_content=reasoning if isinstance(reasoning, str) else None,
    ))


# ── Anthropic ────────────────────────────────────────────────────────────────

def _anthropic_wire(messages: list[dict]) -> list[dict]:
    """一个工具批对应一条 user 消息，tool_result 在该消息开头；签名块原样回带。"""
    out: list[dict] = []
    for message in messages:
        role = message['role']
        if role == 'assistant':
            blocks = message.get('anthropic_blocks')  # 原生 text/thinking/tool_use 顺序不可改。
            if blocks is None:
                blocks = []
                if message.get('content'):
                    blocks.append({'type': 'text', 'text': message['content']})
                for call in message.get('tool_calls') or []:
                    try:
                        parsed = json.loads(call['arguments'])
                    except (TypeError, ValueError):
                        parsed = {}  # 仅为回放旧坏日志的已拒绝调用；从不据此执行。
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['name'],
                                   'input': parsed if isinstance(parsed, dict) else {}})
            out.append({'role': 'assistant', 'content': blocks})
            continue
        if role == 'tool':
            block = {'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': message['content']}
            if message.get('is_error'):
                block['is_error'] = True
        else:
            block = {'type': 'text', 'text': message['content']}
        if out and out[-1]['role'] == 'user':
            out[-1]['content'].append(block)
        else:
            out.append({'role': 'user', 'content': [block]})
    return out


async def _anthropic(client: httpx.AsyncClient, config: AIConfig, system: str,
                     messages: list[dict], tools: list[dict]) -> ModelReply:
    body = {
        'model': config.model, 'max_tokens': DEFAULT_MAX_TOKENS,
        'system': [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
        'messages': _anthropic_wire(messages),
    }
    if tools:
        body['tools'] = tools
    response = await client.post(
        _endpoint(config.base_url, 'messages'),
        headers={'x-api-key': config.api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
        json=body,
    )
    data = _check(response, config.api_key)
    blocks = [b for b in data.get('content') or [] if b['type'] in {'text', 'tool_use', 'thinking', 'redacted_thinking'}]
    raw_calls = [b for b in blocks if b['type'] == 'tool_use']
    stop = _stop_reason(data.get('stop_reason'), normal={'end_turn', 'stop_sequence'},
                        calls=raw_calls, expects_calls={'tool_use'})
    calls = [ToolCall(b.get('id'), b.get('name'), json_text(b.get('input'))) for b in raw_calls] if stop == 'tool_calls' else []
    usage_raw = data.get('usage') or {}
    return _require_content(ModelReply(
        text='\n'.join(b['text'] for b in blocks if b['type'] == 'text'),
        tool_calls=calls,
        usage=Usage(input_tokens=int(usage_raw.get('input_tokens') or 0),
                    output_tokens=int(usage_raw.get('output_tokens') or 0),
                    cache_read_tokens=int(usage_raw.get('cache_read_input_tokens') or 0),
                    cache_write_tokens=int(usage_raw.get('cache_creation_input_tokens') or 0)),
        stop_reason=stop, dropped_tool_calls=len(raw_calls) if stop == 'max_tokens' else 0,
        anthropic_blocks=blocks if stop in {'stop', 'tool_calls'} else None,
    ))


def for_provider(config: AIConfig, *, scope: str = 'all') -> list[dict]:
    from . import registry
    if config.provider == 'anthropic':
        return registry.anthropic_tools(scope=scope)
    return registry.openai_tools(scope=scope)
