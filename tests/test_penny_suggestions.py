from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from backend import db
from backend.ai import chat_suggestions
from backend.main import app


def test_question_suggestions_rank_supported_prompts_without_sharing_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    monkeypatch.setattr(chat_suggestions, "get_secret_config", lambda: None)
    with TestClient(app) as client:
        conversation = client.post("/api/ai/chat/conversations", json={}).json()["id"]
        url = f"/api/ai/chat/conversations/{conversation}/suggestions"
        context = {"page": "transactions", "date": "2026-10-30", "category": "private category"}
        fallback = client.post(url, json={"context": context})
        assert fallback.status_code == 200
        questions = fallback.json()["questions"]
        assert len(questions) == 3
        assert questions[0] == "2026-10-30有哪些交易明细？"

        model = AsyncMock(return_value={"ids": ["anomalies", "unknown", "anomalies", "categories"]})
        monkeypatch.setattr(chat_suggestions, "complete_json", model)
        monkeypatch.setattr(chat_suggestions, "get_secret_config", lambda: SimpleNamespace(allow_external_processing=True))
        ranked = client.post(url, json={"context": context}).json()["questions"]
        assert len(set(ranked)) == 3
        assert "疑似重复" in ranked[0]
        assert "支出分类" in ranked[1]
        payload = model.call_args.kwargs["user_payload"]
        assert set(payload) == {"page", "topics", "candidate_ids"}
        assert payload["page"] == "transactions" and payload["topics"] == []
        assert payload["candidate_ids"][0] == "day"
        assert client.get(f"/api/ai/chat/conversations/{conversation}").json()["messages"] == []
