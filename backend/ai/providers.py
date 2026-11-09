"""Provider adapter for OpenAI-compatible Chat Completions and Anthropic Messages.

Qianwen's JSON mode requires thinking to be disabled for thinking-capable Qwen
models. This adapter sends that provider-specific parameter only to Qianwen's
OpenAI-compatible endpoint, leaving other providers unchanged.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import AIConfig


class AIProviderError(RuntimeError):
    pass


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(suffix):
        return base
    return f"{base}/{suffix.lstrip('/')}"


def _is_qianwen_qwen_model(config: AIConfig) -> bool:
    host = (urlparse(config.base_url).hostname or "").lower()
    model = (config.model or "").strip().lower()
    return host.endswith("qianwenaiapi.com") and model.startswith(("qwen", "qwq"))


def _safe_provider_error(response: httpx.Response, api_key: str) -> str:
    """Extract a concise provider error without ever echoing the API key."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
            elif isinstance(error, str):
                detail = error
            if not detail:
                detail = str(body.get("message") or body.get("code") or "")
    except (ValueError, TypeError):
        detail = ""

    detail = re.sub(r"\\s+", " ", detail).strip()
    if api_key:
        detail = detail.replace(api_key, "[API KEY 已隐藏]")
    detail = detail[:360]

    status = response.status_code
    if status in (401, 403):
        prefix = "API Key 认证失败或没有调用权限"
    elif status == 404:
        prefix = "模型不存在、接口地址错误，或该模型尚未开通"
    elif status == 400:
        prefix = "模型请求参数不被服务商接受"
    elif status == 429:
        prefix = "请求频率受限或账户额度不足"
    elif status >= 500:
        prefix = "模型服务暂时不可用"
    else:
        prefix = "模型请求失败"

    return f"{prefix}（HTTP {status}）" + (f"：{detail}" if detail else "。请检查模型服务商的错误详情。")


def _parse_json_text(text: str) -> dict[str, Any]:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", value, flags=re.IGNORECASE)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AIProviderError("模型返回内容不是有效 JSON，请重试或更换模型。") from exc
    if not isinstance(parsed, dict):
        raise AIProviderError("模型返回的 JSON 顶层必须是对象。")
    return parsed


async def complete_json(
    config: AIConfig,
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Request JSON from a model; never log request headers or raw bill data."""
    user_text = "请根据下面的 JSON 数据完成任务，只返回 JSON 对象，不要输出 Markdown。\n" + json.dumps(
        user_payload, ensure_ascii=False, separators=(",", ":")
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if config.provider == "anthropic":
                response = await client.post(
                    _endpoint(config.base_url, "messages"),
                    headers={
                        "x-api-key": config.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": config.model,
                        "max_tokens": 8192,
                        "system": system_prompt + "\n输出必须是可解析的 JSON 对象。",
                        "messages": [{"role": "user", "content": user_text}],
                    },
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise AIProviderError(_safe_provider_error(response, config.api_key)) from exc
                body = response.json()
                text_parts = [
                    block.get("text", "") for block in body.get("content", [])
                    if block.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            else:
                request_body: dict[str, Any] = {
                    "model": config.model,
                    "messages": [
                        {"role": "system", "content": system_prompt + "\n请严格输出 JSON 对象。"},
                        {"role": "user", "content": user_text},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                }
                # 千问 Qwen 思考模型默认可能开启 thinking，但其 JSON Object
                # 模式要求关闭 thinking。只对千问官方域名 + Qwen 模型添加此参数。
                if _is_qianwen_qwen_model(config):
                    request_body["enable_thinking"] = False

                # 千问结构化输出文档建议 JSON mode 不设置 max_tokens，
                # 避免模型输出 JSON 被截断。其他 OpenAI 兼容服务也使用默认输出上限。
                response = await client.post(
                    _endpoint(config.base_url, "chat/completions"),
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "content-type": "application/json",
                    },
                    json=request_body,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise AIProviderError(_safe_provider_error(response, config.api_key)) from exc
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "\n".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
    except AIProviderError:
        raise
    except httpx.TimeoutException as exc:
        raise AIProviderError("模型请求超时，请检查网络或缩小账单批次。") from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIProviderError("无法读取模型响应，请检查服务商协议、Base URL 和响应格式。") from exc
    return _parse_json_text(str(content or ""))


async def test_connection(config: AIConfig) -> dict[str, Any]:
    result = await complete_json(
        config,
        system_prompt="你是连接测试助手。只返回 JSON：{\"ok\":true,\"message\":\"连接成功\"}。",
        user_payload={"task": "connection_test", "instruction": "返回 ok=true 和简短 message"},
        timeout=30.0,
    )
    if result.get("ok") is not True:
        raise AIProviderError("模型已响应，但没有通过 JSON 连接测试。")
    return {"ok": True, "model": config.model, "message": str(result.get("message") or "连接成功")}
