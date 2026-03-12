from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import Enum

GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


class SignatureStatus(str, Enum):
    SKIPPED = "skipped"
    VERIFIED = "verified"
    FAILED = "failed"


class SignatureFailureReason(str, Enum):
    MISSING_HEADER = "missing_header"
    INVALID_FORMAT = "invalid_format"
    INVALID_PREFIX = "invalid_prefix"
    SIGNATURE_MISMATCH = "signature_mismatch"


@dataclass(frozen=True)
class SignatureVerificationResult:
    status: SignatureStatus
    reason: SignatureFailureReason | None = None

    @property
    def ok(self) -> bool:
        return self.status in {SignatureStatus.SKIPPED, SignatureStatus.VERIFIED}

    @property
    def skipped(self) -> bool:
        return self.status == SignatureStatus.SKIPPED


def verify_github_signature(
    body: bytes,
    secret: str | None,
    signature_header: str | None,
) -> SignatureVerificationResult:
    if _is_secret_empty(secret):
        return SignatureVerificationResult(status=SignatureStatus.SKIPPED)

    normalized_secret = secret.strip() if secret is not None else ""
    expected_digest = build_signature(body=body, secret=normalized_secret)
    parsed_signature = parse_signature_header(signature_header)
    if parsed_signature is None:
        return SignatureVerificationResult(
            status=SignatureStatus.FAILED,
            reason=SignatureFailureReason.MISSING_HEADER,
        )

    if "=" not in parsed_signature:
        return SignatureVerificationResult(
            status=SignatureStatus.FAILED,
            reason=SignatureFailureReason.INVALID_FORMAT,
        )

    if not parsed_signature.startswith(SIGNATURE_PREFIX):
        return SignatureVerificationResult(
            status=SignatureStatus.FAILED,
            reason=SignatureFailureReason.INVALID_PREFIX,
        )

    digest = parsed_signature[len(SIGNATURE_PREFIX) :]
    if not _is_valid_hex_digest(digest):
        return SignatureVerificationResult(
            status=SignatureStatus.FAILED,
            reason=SignatureFailureReason.INVALID_FORMAT,
        )

    if not hmac.compare_digest(expected_digest, digest):
        return SignatureVerificationResult(
            status=SignatureStatus.FAILED,
            reason=SignatureFailureReason.SIGNATURE_MISMATCH,
        )

    return SignatureVerificationResult(status=SignatureStatus.VERIFIED)


def build_signature(body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    return mac.hexdigest()


def parse_signature_header(signature_header: str | None) -> str | None:
    if signature_header is None:
        return None

    normalized = signature_header.strip()
    if not normalized:
        return None

    return normalized


def _is_secret_empty(secret: str | None) -> bool:
    return secret is None or not secret.strip()


def _is_valid_hex_digest(digest: str) -> bool:
    if len(digest) != 64:
        return False
    try:
        int(digest, 16)
    except ValueError:
        return False
    return True
