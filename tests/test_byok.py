from __future__ import annotations

import os
import sqlite3

import pytest

from app.db import connect_db, init_db
from app.services.byok import (
    SUPPORTED_PROVIDERS,
    UserApiKeyCreatePayload,
    _decrypt,
    _encrypt,
    _mask_key,
    add_api_key,
    build_byok_env_overrides,
    delete_api_key,
    list_api_keys,
    resolve_api_key,
    resolve_all_api_keys,
    toggle_api_key,
)


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret-for-byok")
    from app.config import get_settings

    get_settings.cache_clear()
    init_db()
    conn = connect_db()
    yield conn
    conn.close()
    get_settings.cache_clear()


class TestEncryptDecrypt:
    def test_roundtrip(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret-for-byok")
        get_settings.cache_clear()
        original = "sk-test-key-12345"
        encrypted = _encrypt(original)
        assert encrypted != original
        assert _decrypt(encrypted) == original
        get_settings.cache_clear()

    def test_different_keys_produce_different_ciphertext(self, monkeypatch):
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret-a")
        get_settings.cache_clear()
        encrypted_a = _encrypt("test-key")

        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret-b")
        get_settings.cache_clear()
        encrypted_b = _encrypt("test-key")

        assert encrypted_a != encrypted_b
        get_settings.cache_clear()

    def test_same_plaintext_different_ciphertexts(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret-for-byok")
        get_settings.cache_clear()
        original = "sk-same-key"
        encrypted_a = _encrypt(original)
        encrypted_b = _encrypt(original)
        assert encrypted_a != encrypted_b
        assert _decrypt(encrypted_a) == original
        assert _decrypt(encrypted_b) == original
        get_settings.cache_clear()

    def test_missing_encryption_key_raises(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
            _encrypt("test")
        get_settings.cache_clear()

    def test_wrong_key_decrypt_fails(self, monkeypatch):
        from cryptography.fernet import InvalidToken

        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret-a")
        from app.config import get_settings

        get_settings.cache_clear()
        encrypted = _encrypt("test-key")

        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret-b")
        get_settings.cache_clear()
        with pytest.raises(InvalidToken):
            _decrypt(encrypted)
        get_settings.cache_clear()


class TestMaskKey:
    def test_short_key(self):
        assert _mask_key("short") == "****"

    def test_long_key(self):
        assert _mask_key("sk-1234567890abcdef") == "sk-1****cdef"

    def test_exactly_8_chars(self):
        assert _mask_key("12345678") == "****"


class TestAddApiKey:
    def test_add_key_success(self, db_conn):
        payload = UserApiKeyCreatePayload(
            provider="anthropic", api_key="sk-ant-test123456", label="Test Key"
        )
        entry = add_api_key(db_conn, payload)
        assert entry.id > 0
        assert entry.provider == "anthropic"
        assert entry.label == "Test Key"
        assert "****" in entry.masked_key
        assert entry.enabled is True

    def test_add_key_unsupported_provider(self, db_conn):
        payload = UserApiKeyCreatePayload(
            provider="invalid_provider", api_key="test-key", label=""
        )
        with pytest.raises(ValueError, match="unsupported provider"):
            add_api_key(db_conn, payload)

    def test_add_key_empty_api_key(self, db_conn):
        payload = UserApiKeyCreatePayload(
            provider="anthropic", api_key="  ", label=""
        )
        with pytest.raises(ValueError, match="must not be empty"):
            add_api_key(db_conn, payload)

    def test_add_key_default_label(self, db_conn):
        payload = UserApiKeyCreatePayload(
            provider="openai", api_key="sk-test-key", label=""
        )
        entry = add_api_key(db_conn, payload)
        assert entry.label == "OpenAI"

    def test_add_key_all_providers(self, db_conn):
        for provider in SUPPORTED_PROVIDERS:
            payload = UserApiKeyCreatePayload(
                provider=provider, api_key=f"key-for-{provider}", label=""
            )
            entry = add_api_key(db_conn, payload)
            assert entry.provider == provider


class TestListApiKeys:
    def test_empty_list(self, db_conn):
        keys = list_api_keys(db_conn)
        assert keys == []

    def test_list_multiple_keys(self, db_conn):
        add_api_key(db_conn, UserApiKeyCreatePayload("anthropic", "key1", "A"))
        add_api_key(db_conn, UserApiKeyCreatePayload("openai", "key2", "B"))
        keys = list_api_keys(db_conn)
        assert len(keys) == 2
        providers = {k.provider for k in keys}
        assert providers == {"anthropic", "openai"}


class TestDeleteApiKey:
    def test_delete_existing(self, db_conn):
        entry = add_api_key(
            db_conn, UserApiKeyCreatePayload("anthropic", "key-to-delete", "")
        )
        assert delete_api_key(db_conn, entry.id) is True
        assert list_api_keys(db_conn) == []

    def test_delete_nonexistent(self, db_conn):
        assert delete_api_key(db_conn, 99999) is False


class TestToggleApiKey:
    def test_disable_key(self, db_conn):
        entry = add_api_key(
            db_conn, UserApiKeyCreatePayload("anthropic", "key-to-disable", "")
        )
        assert toggle_api_key(db_conn, entry.id, enabled=False) is True
        keys = list_api_keys(db_conn)
        assert keys[0].enabled is False

    def test_re_enable_key(self, db_conn):
        entry = add_api_key(
            db_conn, UserApiKeyCreatePayload("anthropic", "key-to-toggle", "")
        )
        toggle_api_key(db_conn, entry.id, enabled=False)
        toggle_api_key(db_conn, entry.id, enabled=True)
        keys = list_api_keys(db_conn)
        assert keys[0].enabled is True

    def test_toggle_nonexistent(self, db_conn):
        assert toggle_api_key(db_conn, 99999, enabled=False) is False


class TestResolveApiKey:
    def test_resolve_existing_key(self, db_conn):
        add_api_key(
            db_conn, UserApiKeyCreatePayload("zhipu", "my-zhipu-key", "")
        )
        resolved = resolve_api_key(db_conn, "zhipu")
        assert resolved == "my-zhipu-key"

    def test_resolve_missing_key(self, db_conn):
        assert resolve_api_key(db_conn, "anthropic") is None

    def test_resolve_disabled_key(self, db_conn):
        entry = add_api_key(
            db_conn, UserApiKeyCreatePayload("deepseek", "disabled-key", "")
        )
        toggle_api_key(db_conn, entry.id, enabled=False)
        assert resolve_api_key(db_conn, "deepseek") is None

    def test_resolve_prefers_latest(self, db_conn):
        add_api_key(db_conn, UserApiKeyCreatePayload("openai", "old-key", "Old"))
        add_api_key(db_conn, UserApiKeyCreatePayload("openai", "new-key", "New"))
        assert resolve_api_key(db_conn, "openai") == "new-key"


class TestResolveAllApiKeys:
    def test_resolve_multiple_providers(self, db_conn):
        add_api_key(db_conn, UserApiKeyCreatePayload("anthropic", "ant-key", ""))
        add_api_key(db_conn, UserApiKeyCreatePayload("openai", "oai-key", ""))
        all_keys = resolve_all_api_keys(db_conn)
        assert all_keys["anthropic"] == "ant-key"
        assert all_keys["openai"] == "oai-key"

    def test_resolve_skips_disabled(self, db_conn):
        entry = add_api_key(
            db_conn, UserApiKeyCreatePayload("anthropic", "disabled-key", "")
        )
        toggle_api_key(db_conn, entry.id, enabled=False)
        add_api_key(db_conn, UserApiKeyCreatePayload("openai", "active-key", ""))
        all_keys = resolve_all_api_keys(db_conn)
        assert "anthropic" not in all_keys
        assert all_keys["openai"] == "active-key"


class TestBuildByokEnvOverrides:
    def test_builds_env_map(self, db_conn):
        add_api_key(
            db_conn, UserApiKeyCreatePayload("anthropic", "sk-ant-test", "")
        )
        add_api_key(
            db_conn, UserApiKeyCreatePayload("openai", "sk-oai-test", "")
        )
        add_api_key(
            db_conn, UserApiKeyCreatePayload("zhipu", "zhipu-test-key", "")
        )
        overrides = build_byok_env_overrides(db_conn)
        assert overrides["ANTHROPIC_API_KEY"] == "sk-ant-test"
        assert overrides["OPENAI_API_KEY"] == "sk-oai-test"
        assert overrides["ZHIPU_API_KEY"] == "zhipu-test-key"

    def test_empty_overrides(self, db_conn):
        overrides = build_byok_env_overrides(db_conn)
        assert overrides == {}
