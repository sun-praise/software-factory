from __future__ import annotations

from typing import Any, Mapping

import pytest

from app.config import get_settings
from app.providers.registry import (
    OAUTH_PROVIDER_CATEGORY,
    ProviderLookupError,
    ProviderRegistrationError,
    get_oauth_provider,
    list_registered_provider_names,
    register_oauth_provider,
    reset_provider_registry,
    snapshot_registry,
)
from app.providers.types import OAuthProvider, OAuthUserInfo


@pytest.fixture(autouse=True)
def _reset_registry_state() -> None:
    get_settings.cache_clear()
    reset_provider_registry(include_defaults=True)
    yield
    get_settings.cache_clear()
    reset_provider_registry(include_defaults=True)


class _CustomOAuthProvider:
    name = "custom"

    @property
    def authorize_url(self) -> str:
        return "https://example.invalid/oauth/authorize"

    @property
    def token_url(self) -> str:
        return "https://example.invalid/oauth/token"

    @property
    def userinfo_url(self) -> str:
        return "https://example.invalid/api/user"

    @property
    def scopes(self) -> tuple[str, ...]:
        return ("read",)

    def build_authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
    ) -> str:
        return (
            f"{self.authorize_url}?client_id={client_id}"
            f"&redirect_uri={redirect_uri}&state={state}"
        )

    async def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> Mapping[str, Any]:
        return {"access_token": "test-token", "token_type": "bearer"}

    async def fetch_user_info(
        self,
        *,
        access_token: str,
    ) -> OAuthUserInfo:
        return OAuthUserInfo(
            provider=self.name,
            provider_user_id="42",
            username="testuser",
            display_name="Test User",
            email="test@example.com",
        )


def test_default_github_oauth_provider_is_registered() -> None:
    provider = get_oauth_provider()
    assert provider.name == "github"
    assert list_registered_provider_names(OAUTH_PROVIDER_CATEGORY) == ("github",)


def test_snapshot_includes_oauth_category() -> None:
    snap = snapshot_registry()
    assert "github" in snap.oauth


def test_register_custom_oauth_provider() -> None:
    custom = _CustomOAuthProvider()
    register_oauth_provider("custom", custom)

    assert get_oauth_provider("custom") is custom
    assert list_registered_provider_names(OAUTH_PROVIDER_CATEGORY) == (
        "custom",
        "github",
    )


def test_register_oauth_provider_rejects_duplicate() -> None:
    register_oauth_provider("custom", _CustomOAuthProvider())

    with pytest.raises(ProviderRegistrationError) as exc:
        register_oauth_provider("custom", _CustomOAuthProvider())

    assert "already registered" in str(exc.value)


def test_register_oauth_provider_can_replace_existing() -> None:
    first = _CustomOAuthProvider()
    second = _CustomOAuthProvider()

    register_oauth_provider("custom", first)
    register_oauth_provider("custom", second, replace=True)

    assert get_oauth_provider("custom") is second


def test_get_oauth_provider_raises_for_unknown_name() -> None:
    with pytest.raises(ProviderLookupError) as exc:
        get_oauth_provider("missing")

    assert "available: github" in str(exc.value)


def test_get_oauth_provider_uses_configured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    custom = _CustomOAuthProvider()
    register_oauth_provider("custom", custom)

    monkeypatch.setenv("OAUTH_PROVIDER", "custom")
    get_settings.cache_clear()

    assert get_oauth_provider() is custom


def test_get_oauth_provider_raises_for_unknown_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OAUTH_PROVIDER", "missing")
    get_settings.cache_clear()

    with pytest.raises(ProviderLookupError) as exc:
        get_oauth_provider()

    assert "oauth provider 'missing'" in str(exc.value)


def test_register_oauth_provider_rejects_invalid_protocol() -> None:
    with pytest.raises(ProviderRegistrationError) as exc:
        register_oauth_provider("invalid", object())  # type: ignore[arg-type]

    assert "does not implement required protocol" in str(exc.value)


def test_reset_registry_clears_oauth_providers() -> None:
    reset_provider_registry(include_defaults=False)

    snap = snapshot_registry()
    assert snap.oauth == ()

    with pytest.raises(ProviderLookupError):
        get_oauth_provider()


def test_github_oauth_provider_builds_authorize_url() -> None:
    provider = get_oauth_provider()
    url = provider.build_authorize_url(
        client_id="test-id",
        redirect_uri="http://localhost:8011/auth/callback",
        state="random-state",
    )
    assert "github.com/login/oauth/authorize" in url
    assert "client_id=test-id" in url
    assert "state=random-state" in url


def test_github_oauth_provider_has_expected_properties() -> None:
    provider = get_oauth_provider()
    assert provider.authorize_url == "https://github.com/login/oauth/authorize"
    assert provider.token_url == "https://github.com/login/oauth/access_token"
    assert provider.userinfo_url == "https://api.github.com/user"
    assert "read:user" in provider.scopes
