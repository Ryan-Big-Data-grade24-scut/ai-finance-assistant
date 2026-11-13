"""AI 模型配置的唯一读写入口。API Key 只以密文形式进入 SQLite。"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .. import db
from .security import decrypt_secret, encrypt_secret, mask_secret

PROVIDERS = {
    "openai_compatible": {
        "label": "OpenAI 兼容接口",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1-mini",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-5",
    },
}


@dataclass(frozen=True)
class AIConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    auto_apply_rules: bool = False
    confidence_threshold: float = 0.90
    allow_external_processing: bool = False


def validate_config(provider: str, base_url: str, model: str, threshold: float) -> None:
    if provider not in PROVIDERS:
        raise ValueError("暂不支持该模型协议。")
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API Base URL 必须是有效的 http/https 地址。")
    if len(base_url) > 500 or len(model.strip()) > 160 or not model.strip():
        raise ValueError("模型地址或模型名称不合法。")
    if not 0.70 <= float(threshold) <= 0.99:
        raise ValueError("自动规则置信度阈值必须在 0.70 到 0.99 之间。")


def get_public_config() -> dict:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM ai_provider_config WHERE id=1").fetchone()
        if not row:
            default = PROVIDERS["openai_compatible"]
            return {
                "configured": False,
                "provider": "openai_compatible",
                "provider_label": PROVIDERS["openai_compatible"]["label"],
                "base_url": default["default_base_url"],
                "model": default["default_model"],
                "api_key_masked": None,
                "auto_apply_rules": False,
                "allow_external_processing": False,
                "ai_ready": False,
                "confidence_threshold": 0.90,
            }
        encrypted = row["encrypted_api_key"]
        # 不向浏览器返回密钥明文，也不返回密文。
        has_key = bool(encrypted)
        return {
            "configured": has_key,
            "provider": row["provider"],
            "provider_label": PROVIDERS[row["provider"]]["label"],
            "base_url": row["base_url"],
            "model": row["model"],
            "api_key_masked": "已保存" if has_key else None,
            "auto_apply_rules": bool(row["auto_apply_rules"]),
            "allow_external_processing": bool(row["allow_external_processing"]),
            "ai_ready": bool(has_key and row["allow_external_processing"]),
            "confidence_threshold": float(row["confidence_threshold"]),
        }
    finally:
        conn.close()


def get_secret_config() -> AIConfig | None:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM ai_provider_config WHERE id=1").fetchone()
        if not row or not row["encrypted_api_key"]:
            return None
        return AIConfig(
            provider=row["provider"],
            base_url=row["base_url"],
            model=row["model"],
            api_key=decrypt_secret(row["encrypted_api_key"]),
            auto_apply_rules=bool(row["auto_apply_rules"]),
            confidence_threshold=float(row["confidence_threshold"]),
            allow_external_processing=bool(row["allow_external_processing"]),
        )
    finally:
        conn.close()


def save_config(
    *, provider: str, base_url: str, model: str, api_key: str | None,
    auto_apply_rules: bool, confidence_threshold: float, clear_api_key: bool = False,
    allow_external_processing: bool = False,
) -> dict:
    validate_config(provider, base_url, model, confidence_threshold)
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT encrypted_api_key FROM ai_provider_config WHERE id=1").fetchone()
        encrypted = current["encrypted_api_key"] if current else None
        if clear_api_key:
            encrypted = None
        elif api_key and api_key.strip():
            encrypted = encrypt_secret(api_key.strip())
        conn.execute(
            "INSERT INTO ai_provider_config"
            "(id,provider,base_url,model,encrypted_api_key,auto_apply_rules,allow_external_processing,confidence_threshold,updated_at) "
            "VALUES(1,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,base_url=excluded.base_url,"
            "model=excluded.model,encrypted_api_key=excluded.encrypted_api_key,"
            "auto_apply_rules=excluded.auto_apply_rules,"
            "allow_external_processing=excluded.allow_external_processing,"
            "confidence_threshold=excluded.confidence_threshold,updated_at=datetime('now')",
            (provider, base_url.strip().rstrip("/"), model.strip(), encrypted,
             int(auto_apply_rules), int(allow_external_processing), float(confidence_threshold)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_public_config()
