from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend import db
from backend.ai import config as ai_config
from backend.ai import import_classifier
from backend.ai import import_workflow
from backend.ai.config import AIConfig
from backend.importer.base import Row
from backend.ledger import loader as ledger_loader
from backend.ledger import service as ledger_service


@pytest.fixture
def isolated_ai_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ai-web-test.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def test_ai_api_key_is_encrypted_and_never_returned(isolated_ai_db):
    public = ai_config.save_config(
        provider="openai_compatible", base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini", api_key="DUMMY_TEST_API_KEY",
        auto_apply_rules=False, confidence_threshold=0.92,
    )
    assert public["configured"] is True
    assert public["api_key_masked"] == "已保存"
    assert "DUMMY_TEST_API_KEY" not in json.dumps(public)
    conn = db.get_conn()
    try:
        encrypted = conn.execute("SELECT encrypted_api_key FROM ai_provider_config WHERE id=1").fetchone()[0]
    finally:
        conn.close()
    assert encrypted != "DUMMY_TEST_API_KEY"
    assert ai_config.get_secret_config().api_key == "DUMMY_TEST_API_KEY"


def test_import_classifier_only_auto_applies_unique_authorized_rule(monkeypatch):
    rows = [
        Row("alipay", "支付宝", "2026-10-01 10:00:00", "支出", 28.0,
            counterparty="STARBUCKS", item="咖啡", tx_type="餐饮"),
        Row("alipay", "支付宝", "2026-10-01 11:00:00", "支出", 66.0,
            counterparty="UNKNOWN SHOP", item="商品", tx_type="购物"),
    ]
    async def fake_complete(_config, *, system_prompt, user_payload, timeout=90.0):
        if "整批交易分布" in system_prompt:
            return {"categories": [
                {"major": "餐饮", "minor": "咖啡饮品", "description": "咖啡和饮品"},
                {"major": "购物", "minor": "日用品", "description": "日常商品"},
            ], "summary": "按整批交易建立分类。"}
        return {"mappings": [
            {"group_key": group["group_key"], "major": "餐饮" if group["platform_category"] == "餐饮" else "购物",
             "minor": "咖啡饮品" if group["platform_category"] == "餐饮" else "日用品",
             "confidence": 0.99, "reason": "商户和描述一致。", "needs_review": False}
            for group in user_payload["groups"]
        ]}
    monkeypatch.setattr(import_classifier, "complete_json", fake_complete)
    config = AIConfig("openai_compatible", "https://api.example.com/v1", "mock-model", "secret", True, 0.90)
    result = asyncio.run(import_classifier.classify_import_batch(
        rows, config=config,
        existing_categories=[{"node_id": "node-coffee", "path": "餐饮 / 咖啡饮品"},
                             {"node_id": "node-daily", "path": "购物 / 日用品"}],
        rules=[{"id": "rule-1", "match_field": "counterparty", "pattern_norm": "starbucks",
                "target_node_id": "node-coffee"}],
    ))
    by_index = {item["row_index"]: item for item in result["suggestions"]}
    assert by_index[0]["auto_apply_eligible"] is True
    assert by_index[0]["confidence"] == 1.0
    assert by_index[0]["matched_rule_ids"] == ["rule-1"]
    # A model confidence of 99% is not enough without an explicit user rule.
    assert by_index[1]["confidence"] == 0.99
    assert by_index[1]["auto_apply_eligible"] is False
    assert by_index[1]["needs_review"] is True


def _write_alipay_csv(path: Path) -> Path:
    content = (
        "支付宝交易记录明细查询\n"
        "收/支,交易时间,交易分类,交易对方,商品说明,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
        "支出,2026-10-01 10:00:00,其他,STARBUCKS,咖啡,28.00,余额宝,交易成功,ai-web-tx-001,,\n"
    )
    path.write_bytes(content.encode("gb18030"))
    return path


def test_ai_import_session_calibration_and_commit(monkeypatch, isolated_ai_db):
    config = AIConfig("openai_compatible", "https://api.example.com/v1", "mock-model", "secret", False, 0.90, True)
    monkeypatch.setattr(import_workflow, "get_secret_config", lambda: config)

    async def fake_classify(rows, *, config, existing_categories, rules, history_examples=None):
        return {
            "summary": "整批交易主要是咖啡消费。",
            "taxonomy": [{"major": "餐饮", "minor": "咖啡饮品", "path": "餐饮 / 咖啡饮品",
                          "description": "咖啡和饮品", "is_new": True}],
            "suggestions": [{
                "row_index": 0, "target_major": "餐饮", "target_minor": "咖啡饮品",
                "target_path": "餐饮 / 咖啡饮品", "confidence": 0.97,
                "reason": "商户和商品描述都指向咖啡消费。", "needs_review": True,
                "question": None, "excluded": False, "is_new_category": True,
                "auto_apply_eligible": False, "matched_rule_ids": [],
                "counterparty": "STARBUCKS", "item": "咖啡", "amount": 28.0,
                "refunded_amount": 0.0, "effective_amount": 28.0, "direction": "支出",
            }],
            "counts": {"total": 1, "excluded": 0, "auto_apply": 0, "review": 1, "new_categories": 1},
        }
    monkeypatch.setattr(import_workflow, "classify_import_batch", fake_classify)
    source = _write_alipay_csv(isolated_ai_db / "alipay.csv")
    preview = asyncio.run(import_workflow.create_session(source, "alipay.csv"))
    session_id = preview["session_id"]
    assert preview["total"] == 1
    assert preview["analysis"]["suggestions"][0]["target_path"] == "餐饮 / 咖啡饮品"

    async def fake_calibration(_config, *, system_prompt, user_payload, timeout=90.0):
        return {
            "assistant_message": "已将这笔交易校准为餐饮 / 咖啡饮品。",
            "changes": [{"row_index": 0, "major": "餐饮", "minor": "咖啡饮品",
                         "confidence": 0.99, "reason": "用户通过对话确认。"}],
            "new_categories": [], "rule_suggestions": [],
        }
    monkeypatch.setattr(import_workflow, "complete_json", fake_calibration)
    calibrated = asyncio.run(import_workflow.calibrate_session(session_id, "星巴克应该归到咖啡饮品"))
    assert calibrated["analysis"]["suggestions"][0]["calibrated"] is True
    assert len(calibrated["messages"]) >= 3

    result = import_workflow.commit_session(
        session_id,
        approved_category_paths=["餐饮 / 咖啡饮品"],
        approved_row_indices=[0],
        remember_rule_row_indices=[0],
    )
    assert result["inserted"] == 1
    assert result["manually_applied"] == 1
    assert result["pending_review"] == 0
    assert result["pending_proposal_ids"] == []

    conn = db.get_conn()
    try:
        session = conn.execute("SELECT status,rows_json FROM ai_import_sessions WHERE id=?", (session_id,)).fetchone()
        assert session["status"] == "committed"
        assert session["rows_json"] is None
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_rules WHERE enabled=1").fetchone()[0] == 1
    finally:
        conn.close()

    state_conn = db.get_conn()
    try:
        state = ledger_loader.read_state(state_conn, ledger_service.LIVE_BOOK_ID)
        paths = {node_id: node["name"] for node_id, node in
                 ((node["id"], node) for node in state["nodes"])}
        tx_id = state["members"][0]
        assigned_node = paths[state["placements"][str(tx_id)]]
        assert assigned_node == "咖啡饮品"
    finally:
        state_conn.close()

    repeated = import_workflow.commit_session(session_id)
    assert repeated == result
    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 1
    finally:
        conn.close()


def test_ai_schema_has_one_authoritative_definition():
    schema = (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text(encoding="utf-8")
    assert schema.count("CREATE TABLE IF NOT EXISTS ai_classification_proposals") == 1
    assert schema.count("CREATE TABLE IF NOT EXISTS ai_classification_suggestions") == 1
    assert schema.count("CREATE TABLE IF NOT EXISTS ai_classification_reviews") == 1
    assert schema.count("CREATE TABLE IF NOT EXISTS ai_classification_rules") == 1


def test_openai_compatible_provider_parses_json_without_leaking_key(monkeypatch):
    from backend.ai import providers

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
    config = AIConfig("openai_compatible", "https://api.example.com/v1", "mock", "secret-key", False, 0.9)
    result = asyncio.run(providers.complete_json(
        config, system_prompt="Return JSON", user_payload={"task": "test"}
    ))
    assert result == {"ok": True, "message": "connected"}
    assert calls["url"] == "https://api.example.com/v1/chat/completions"
    assert calls["headers"]["Authorization"] == "Bearer secret-key"
    assert "secret-key" not in json.dumps(calls["body"])


def test_ai_import_session_expires_and_purges_staged_rows(monkeypatch, isolated_ai_db):
    config = AIConfig("openai_compatible", "https://api.example.com/v1", "mock-model", "secret", False, 0.9, True)
    monkeypatch.setattr(import_workflow, "get_secret_config", lambda: config)

    async def fake_classify(rows, *, config, existing_categories, rules, history_examples=None):
        return {"summary": "summary", "taxonomy": [], "suggestions": [],
                "counts": {"total": len(rows), "excluded": len(rows), "auto_apply": 0, "review": 0, "new_categories": 0}}
    monkeypatch.setattr(import_workflow, "classify_import_batch", fake_classify)
    source = _write_alipay_csv(isolated_ai_db / "expire.csv")
    preview = asyncio.run(import_workflow.create_session(source, "expire.csv"))
    conn = db.get_conn()
    try:
        conn.execute("UPDATE ai_import_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                     (preview["session_id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(import_workflow.AIImportError) as exc:
        import_workflow.get_session(preview["session_id"])
    assert exc.value.status_code == 410
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT status,rows_json FROM ai_import_sessions WHERE id=?",
                           (preview["session_id"],)).fetchone()
        assert row["status"] == "expired"
        assert row["rows_json"] is None
    finally:
        conn.close()


def test_only_enabled_user_rule_can_auto_apply_on_later_import(monkeypatch, isolated_ai_db):
    config_holder = [AIConfig("openai_compatible", "https://api.example.com/v1", "mock", "secret", False, 0.9, True)]
    monkeypatch.setattr(import_workflow, "get_secret_config", lambda: config_holder[0])

    async def fake_classify(rows, *, config, existing_categories, rules, history_examples=None):
        if not existing_categories:
            taxonomy = [{"major": "餐饮", "minor": "咖啡饮品", "path": "餐饮 / 咖啡饮品",
                         "description": "咖啡饮品", "is_new": True}]
            target = "餐饮 / 咖啡饮品"
            is_new = True
        else:
            taxonomy = [{"major": "餐饮", "minor": "咖啡饮品", "path": "餐饮 / 咖啡饮品",
                         "description": "咖啡饮品", "is_new": False}]
            target = "餐饮 / 咖啡饮品"
            is_new = False
        return {
            "summary": "咖啡消费分类。", "taxonomy": taxonomy,
            "suggestions": [{"row_index": 0, "target_major": "餐饮", "target_minor": "咖啡饮品",
                "target_path": target, "confidence": 1.0, "reason": "命中用户已授权规则。",
                "needs_review": False, "question": None, "excluded": False,
                "is_new_category": is_new, "auto_apply_eligible": bool(config.auto_apply_rules and rules),
                "matched_rule_ids": [rules[0]["id"]] if rules else [], "counterparty": "STARBUCKS",
                "item": "咖啡", "amount": 29.0, "refunded_amount": 0.0,
                "effective_amount": 29.0, "direction": "支出"}],
            "counts": {"total": 1, "excluded": 0, "auto_apply": int(bool(config.auto_apply_rules and rules)),
                       "review": int(not (config.auto_apply_rules and rules)), "new_categories": int(is_new)},
        }
    monkeypatch.setattr(import_workflow, "classify_import_batch", fake_classify)

    first_file = _write_alipay_csv(isolated_ai_db / "first-rule.csv")
    first = asyncio.run(import_workflow.create_session(first_file, "first-rule.csv"))
    import_workflow.commit_session(
        first["session_id"], approved_category_paths=["餐饮 / 咖啡饮品"],
        approved_row_indices=[0], remember_rule_row_indices=[0],
    )
    config_holder[0] = AIConfig("openai_compatible", "https://api.example.com/v1", "mock", "secret", True, 0.9, True)

    second_file = isolated_ai_db / "second-rule.csv"
    content = (
        "支付宝交易记录明细查询\n"
        "收/支,交易时间,交易分类,交易对方,商品说明,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
        "支出,2026-10-02 10:00:00,其他,STARBUCKS,咖啡,29.00,余额宝,交易成功,ai-web-tx-002,,\n"
    )
    second_file.write_bytes(content.encode("gb18030"))
    second = asyncio.run(import_workflow.create_session(second_file, "second-rule.csv"))
    result = import_workflow.commit_session(second["session_id"])
    assert result["auto_applied"] == 1
    assert result["pending_review"] == 0


def test_ai_settings_http_api_masks_secret(monkeypatch, isolated_ai_db):
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app) as client:
        before = client.get("/api/ai/settings")
        assert before.status_code == 200
        assert before.json()["configured"] is False
        saved = client.put("/api/ai/settings", json={
            "provider": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "api_key": "DUMMY_HTTP_API_KEY",
            "auto_apply_rules": False,
            "allow_external_processing": True,
            "confidence_threshold": 0.91,
        })
        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert saved.json()["ai_ready"] is True
        assert "DUMMY_HTTP_API_KEY" not in saved.text
        assert "encrypted_api_key" not in saved.text


def test_ai_import_low_confidence_stays_pending_until_web_confirmation(monkeypatch, isolated_ai_db):
    from fastapi.testclient import TestClient
    from backend.classification import service as classification_service
    from backend.main import app

    config = AIConfig("openai_compatible", "https://api.example.com/v1", "mock-model", "secret", False, 0.9, True)
    monkeypatch.setattr(import_workflow, "get_secret_config", lambda: config)

    async def fake_classify(rows, *, config, existing_categories, rules, history_examples=None):
        return {
            "summary": "该交易需要用户审核。",
            "taxonomy": [{"major": "餐饮", "minor": "咖啡饮品", "path": "餐饮 / 咖啡饮品",
                          "description": "咖啡消费", "is_new": True}],
            "suggestions": [{"row_index": 0, "target_major": "餐饮", "target_minor": "咖啡饮品",
                "target_path": "餐饮 / 咖啡饮品", "confidence": 0.72, "reason": "交易描述较短，建议用户审核。",
                "needs_review": True, "question": None, "excluded": False,
                "is_new_category": True, "auto_apply_eligible": False, "matched_rule_ids": [],
                "counterparty": "SMALL CAFE", "item": "消费", "amount": 45.0,
                "refunded_amount": 0.0, "effective_amount": 45.0, "direction": "支出"}],
            "counts": {"total": 1, "excluded": 0, "auto_apply": 0, "review": 1, "new_categories": 1},
        }
    monkeypatch.setattr(import_workflow, "classify_import_batch", fake_classify)
    source = _write_alipay_csv(isolated_ai_db / "pending.csv")
    preview = asyncio.run(import_workflow.create_session(source, "pending.csv"))
    committed = import_workflow.commit_session(
        preview["session_id"], approved_category_paths=["餐饮 / 咖啡饮品"], approved_row_indices=[]
    )
    assert committed["pending_review"] == 1
    assert len(committed["pending_proposal_ids"]) == 1

    proposal_id = committed["pending_proposal_ids"][0]
    detail = classification_service.get_proposal(proposal_id)
    assert detail["ready_count"] == 1
    assert detail["clarification_count"] == 0
    assert detail["suggestions"][0]["counterparty"] == "STARBUCKS"
    assert detail["suggestions"][0]["target_path"] == "餐饮 / 咖啡饮品"

    # 无关账本操作使草案版本变旧，但草案交易归属未变时允许安全 rebase。
    book_conn = db.get_conn()
    try:
        book = ledger_loader.read_book(book_conn, ledger_service.LIVE_BOOK_ID)
    finally:
        book_conn.close()
    ledger_service.apply_ops_batch(
        ledger_service.LIVE_BOOK_ID,
        [{"op": "create_node", "parent_id": f"{ledger_service.LIVE_BOOK_ID}:root", "name": "临时审核分类"}],
        expected_seq=int(book["current_seq"]), label="测试无关账本变更",
    )
    stale_detail = classification_service.get_proposal(proposal_id)
    assert stale_detail["stale"] is True
    with TestClient(app) as client:
        drafts = client.get("/api/ai/classification/proposals?status=draft")
        assert drafts.status_code == 200
        assert drafts.json()["returned"] == 1
        applied = client.post(
            f"/api/ai/classification/proposals/{proposal_id}/confirm",
            json={"approved_tx_ids": [detail["suggestions"][0]["tx_id"]]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"
        report = client.get("/api/report/by-category?type=expense&period=month&value=2026-10")
        assert report.status_code == 200
        assert report.json()["majors"][0]["major"] == "餐饮"
    conn = db.get_conn()
    try:
        review = conn.execute("SELECT confirmed_by FROM ai_classification_reviews WHERE proposal_id=?",
                              (proposal_id,)).fetchone()
        assert review["confirmed_by"] == "web_user_confirmed"
    finally:
        conn.close()


def test_ai_import_requires_explicit_data_processing_consent(monkeypatch, isolated_ai_db):
    config = AIConfig("openai_compatible", "https://api.example.com/v1", "mock-model", "secret", False, 0.9, False)
    monkeypatch.setattr(import_workflow, "get_secret_config", lambda: config)
    source = _write_alipay_csv(isolated_ai_db / "consent.csv")
    with pytest.raises(import_workflow.AIImportError) as exc:
        asyncio.run(import_workflow.create_session(source, "consent.csv"))
    assert exc.value.status_code == 409
    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM ai_import_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 0
    finally:
        conn.close()


def test_ai_can_propose_new_subcategory_under_existing_major():
    categories = import_classifier._validate_categories(
        [{"major": "餐饮", "minor": "咖啡饮品", "description": "咖啡"}],
        {"餐饮", "购物 / 日用品"},
    )
    assert categories[0]["path"] == "餐饮 / 咖啡饮品"
    assert categories[0]["is_new"] is True


def test_existing_ai_config_table_gets_consent_column_migration(tmp_path):
    import sqlite3
    old_db = tmp_path / "old-ai-config.db"
    conn = sqlite3.connect(old_db)
    conn.execute(
        "CREATE TABLE ai_provider_config (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, "
        "base_url TEXT NOT NULL, model TEXT NOT NULL, encrypted_api_key TEXT, "
        "auto_apply_rules INTEGER NOT NULL DEFAULT 0, confidence_threshold REAL NOT NULL DEFAULT 0.90, "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.commit()
    conn.close()
    db.init_db(old_db)
    conn = sqlite3.connect(old_db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ai_provider_config)")}
        assert "allow_external_processing" in columns
    finally:
        conn.close()
