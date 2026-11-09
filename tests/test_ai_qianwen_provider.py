import asyncio
import json

from backend.ai.config import AIConfig
from backend.ai import providers


def test_qianwen_json_mode_disables_thinking_and_omits_max_tokens(monkeypatch):
    calls = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"ok":true,"message":"connected"}'}}]}

    class FakeClient:
        def __init__(self, timeout):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers, json):
            calls.update({"url": url, "headers": headers, "body": json})
            return FakeResponse()

    monkeypatch.setattr(providers.httpx, "AsyncClient", FakeClient)
    config = AIConfig(
        "openai_compatible",
        "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "qwen3.8-flash",
        "sk-ws-test-key",
        False,
        0.9,
        True,
    )

    result = asyncio.run(providers.complete_json(
        config,
        system_prompt="Return a JSON object.",
        user_payload={"task": "connection_test"},
    ))

    assert result["ok"] is True
    assert calls["url"] == "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions"
    assert calls["headers"]["Authorization"] == "Bearer sk-ws-test-key"
    assert calls["body"]["enable_thinking"] is False
    assert calls["body"]["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in calls["body"]
    assert "sk-ws-test-key" not in json.dumps(calls["body"])
