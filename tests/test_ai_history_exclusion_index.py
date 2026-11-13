from __future__ import annotations

import asyncio

from backend.ai import import_classifier
from backend.ai.config import AIConfig
from backend.importer.base import Row


def test_special_transaction_exclusion_uses_zero_based_row_index(monkeypatch):
    """A special transaction must not remove the preceding normal row from grouping."""
    rows = [
        Row(
            platform_id="alipay", source="支付宝", tx_time="2026-10-01 10:00:00",
            direction="支出", amount=28.0, counterparty="STARBUCKS",
            item="咖啡", tx_type="餐饮",
        ),
        Row(
            platform_id="alipay", source="支付宝", tx_time="2026-10-01 11:00:00",
            direction="支出", amount=28.0, counterparty="SELF TRANSFER",
            item="本人账户转账", tx_type="转账",
        ),
    ]

    monkeypatch.setattr(
        import_classifier,
        "analyze_special_transactions",
        lambda _rows: {
            "refund_amounts_by_original": {},
            "refund_conflict_ids": set(),
            "transfer_zero_ids": {2},
            "refund_zero_ids": set(),
        },
    )

    async def fake_complete(_config, *, system_prompt, user_payload, timeout=90.0):
        if "整批交易分布" in system_prompt:
            return {
                "categories": [
                    {"major": "餐饮", "minor": "咖啡饮品", "description": "咖啡饮品"}
                ],
                "summary": "历史交易分类分析完成。",
            }
        return {
            "mappings": [
                {
                    "group_key": group["group_key"],
                    "major": "餐饮",
                    "minor": "咖啡饮品",
                    "confidence": 0.98,
                    "reason": "商户和描述表明是咖啡消费。",
                    "needs_review": False,
                }
                for group in user_payload["groups"]
            ]
        }

    monkeypatch.setattr(import_classifier, "complete_json", fake_complete)

    config = AIConfig(
        provider="openai_compatible",
        base_url="https://example.invalid/v1",
        model="mock-model",
        api_key="not-a-real-key",
        auto_apply_rules=False,
        confidence_threshold=0.90,
        allow_external_processing=True,
    )

    result = asyncio.run(
        import_classifier.classify_import_batch(
            rows,
            config=config,
            existing_categories=[],
            rules=[],
        )
    )

    suggestions = {item["row_index"]: item for item in result["suggestions"]}
    assert suggestions[0]["target_path"] == "餐饮 / 咖啡饮品"
    assert suggestions[0]["excluded"] is False
    assert suggestions[1]["excluded"] is True
    assert "内部转账" in suggestions[1]["reason"]
