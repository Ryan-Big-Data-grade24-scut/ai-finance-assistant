"""本地 AI API Key 加密存储。主密钥放在项目目录之外。"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(RuntimeError):
    pass


def master_key_path() -> Path:
    override = os.getenv("AI_FINANCE_MASTER_KEY_FILE")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "AI Finance Assistant" / "master.key"


def _fernet() -> Fernet:
    path = master_key_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            key = Fernet.generate_key()
            # O_EXCL avoids two simultaneous first-starts overwriting each other's key.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
        key = path.read_bytes().strip()
        return Fernet(key)
    except FileExistsError:
        return Fernet(path.read_bytes().strip())
    except Exception as exc:
        raise SecretStoreError("无法初始化本机 AI 密钥加密存储。") from exc


def encrypt_secret(value: str) -> str:
    if not value:
        raise SecretStoreError("API Key 不能为空。")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise SecretStoreError("本机 AI 密钥无法解密，请重新保存 API Key。") from exc


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}{'•' * min(12, len(value) - 6)}{value[-3:]}"
