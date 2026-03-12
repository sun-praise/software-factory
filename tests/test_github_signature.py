from __future__ import annotations

from app.services.github_signature import (
    SignatureFailureReason,
    SignatureStatus,
    build_signature,
    verify_github_signature,
)


def test_verify_github_signature_skips_when_secret_empty() -> None:
    result = verify_github_signature(
        body=b'{"hello":"world"}',
        secret="",
        signature_header=None,
    )

    assert result.status == SignatureStatus.SKIPPED
    assert result.reason is None
    assert result.ok is True
    assert result.skipped is True


def test_verify_github_signature_accepts_valid_signature() -> None:
    body = b'{"action":"opened"}'
    secret = "top-secret"
    digest = build_signature(body=body, secret=secret)

    result = verify_github_signature(
        body=body,
        secret=secret,
        signature_header=f"sha256={digest}",
    )

    assert result.status == SignatureStatus.VERIFIED
    assert result.reason is None
    assert result.ok is True


def test_verify_github_signature_rejects_signature_mismatch() -> None:
    result = verify_github_signature(
        body=b"payload",
        secret="top-secret",
        signature_header="sha256=" + "0" * 64,
    )

    assert result.status == SignatureStatus.FAILED
    assert result.reason == SignatureFailureReason.SIGNATURE_MISMATCH
    assert result.ok is False


def test_verify_github_signature_rejects_missing_header() -> None:
    result = verify_github_signature(
        body=b"payload",
        secret="top-secret",
        signature_header=None,
    )

    assert result.status == SignatureStatus.FAILED
    assert result.reason == SignatureFailureReason.MISSING_HEADER


def test_verify_github_signature_rejects_invalid_prefix() -> None:
    body = b"payload"
    secret = "top-secret"
    digest = build_signature(body=body, secret=secret)

    result = verify_github_signature(
        body=body,
        secret=secret,
        signature_header=f"sha1={digest}",
    )

    assert result.status == SignatureStatus.FAILED
    assert result.reason == SignatureFailureReason.INVALID_PREFIX
