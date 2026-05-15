from __future__ import annotations

import base64
import hashlib
import logging
import sqlite3
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_LOG = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = (
    "anthropic",
    "openai",
    "zhipu",
    "deepseek",
    "openrouter",
)

_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "zhipu": "ZhipuAI",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
}

_PROVIDER_ENV_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _get_fernet() -> Fernet:
    settings = get_settings()
    secret = settings.byok_encryption_key or settings.github_webhook_secret
    if not secret:
        raise RuntimeError(
            "BYOK_ENCRYPTION_KEY or GITHUB_WEBHOOK_SECRET must be configured "
            "for BYOK encryption"
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def _decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


@dataclass(frozen=True)
class UserApiKeyEntry:
    id: int
    provider: str
    label: str
    masked_key: str
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UserApiKeyCreatePayload:
    provider: str
    api_key: str
    label: str


def list_api_keys(conn: sqlite3.Connection) -> list[UserApiKeyEntry]:
    rows = conn.execute(
        """
        SELECT id, provider, encrypted_key, label, enabled, created_at, updated_at
        FROM user_api_keys
        ORDER BY provider, id
        """
    ).fetchall()
    entries: list[UserApiKeyEntry] = []
    for row in rows:
        try:
            plain = _decrypt(str(row["encrypted_key"]))
        except InvalidToken:
            _LOG.warning("failed to decrypt key id=%s", row["id"])
            plain = ""
        except Exception as e:
            _LOG.error("unexpected error decrypting key id=%s: %s: %s", row["id"], type(e).__name__, e)
            plain = ""
        entries.append(
            UserApiKeyEntry(
                id=int(row["id"]),
                provider=str(row["provider"]),
                label=str(row["label"] or ""),
                masked_key=_mask_key(plain),
                enabled=bool(row["enabled"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        )
    return entries


def add_api_key(
    conn: sqlite3.Connection,
    payload: UserApiKeyCreatePayload,
) -> UserApiKeyEntry:
    provider = payload.provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"unsupported provider '{provider}'. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    api_key = payload.api_key.strip()
    if not api_key:
        raise ValueError("API key must not be empty")
    encrypted = _encrypt(api_key)
    label = payload.label.strip() or _PROVIDER_LABELS.get(provider, provider)
    conn.execute(
        """
        INSERT OR REPLACE INTO user_api_keys (provider, encrypted_key, label, enabled)
        VALUES (?, ?, ?, 1)
        """,
        (provider, encrypted, label),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT id, provider, encrypted_key, label, enabled, created_at, updated_at
        FROM user_api_keys WHERE provider = ?
        ORDER BY id DESC LIMIT 1
        """,
        (provider,),
    ).fetchone()
    plain = _decrypt(str(row["encrypted_key"]))
    return UserApiKeyEntry(
        id=int(row["id"]),
        provider=str(row["provider"]),
        label=str(row["label"] or ""),
        masked_key=_mask_key(plain),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def delete_api_key(conn: sqlite3.Connection, key_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM user_api_keys WHERE id = ?",
        (key_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def toggle_api_key(conn: sqlite3.Connection, key_id: int, *, enabled: bool) -> bool:
    cursor = conn.execute(
        """
        UPDATE user_api_keys SET enabled = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (1 if enabled else 0, key_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def resolve_api_key(conn: sqlite3.Connection, provider: str) -> str | None:
    provider = provider.strip().lower()
    row = conn.execute(
        """
        SELECT encrypted_key FROM user_api_keys
        WHERE provider = ? AND enabled = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (provider,),
    ).fetchone()
    if row is None:
        return None
    try:
        return _decrypt(str(row["encrypted_key"]))
    except InvalidToken:
        _LOG.warning("failed to decrypt BYOK key for provider %s", provider)
        return None
    except Exception as e:
        _LOG.error("unexpected error decrypting BYOK key for provider %s: %s: %s", provider, type(e).__name__, e)
        return None


def resolve_all_api_keys(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT provider, encrypted_key FROM user_api_keys
        WHERE enabled = 1
        ORDER BY id DESC
        """
    ).fetchall()
    result: dict[str, str] = {}
    for row in rows:
        provider = str(row["provider"]).strip().lower()
        if provider in result:
            continue
        try:
            result[provider] = _decrypt(str(row["encrypted_key"]))
        except InvalidToken:
            _LOG.warning("failed to decrypt BYOK key for provider %s", provider)
        except Exception as e:
            _LOG.error("unexpected error decrypting BYOK key for provider %s: %s: %s", provider, type(e).__name__, e)
    return result


def build_byok_env_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    byok_keys = resolve_all_api_keys(conn)
    env_overrides: dict[str, str] = {}
    for provider, api_key in byok_keys.items():
        env_var = _PROVIDER_ENV_MAP.get(provider)
        if env_var:
            env_overrides[env_var] = api_key
    return env_overrides
