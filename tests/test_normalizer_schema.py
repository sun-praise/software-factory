import pytest
from pydantic import ValidationError

from app.schemas.normalizer import NormalizedReview


def test_normalized_review_valid_object() -> None:
    model = NormalizedReview(
        repo="  acme/widgets  ",
        pr_number=42,
        head_sha="  abc123  ",
        must_fix=[
            {
                "source": "  review_comment  ",
                "path": "app/main.py",
                "line": 10,
                "severity": "P1",
                "text": "  Please handle timeout.  ",
            }
        ],
        should_fix=[],
        ignore=[],
        summary="  Needs one critical fix.  ",
    )

    dumped = model.model_dump(mode="json")

    assert dumped == {
        "repo": "acme/widgets",
        "pr_number": 42,
        "head_sha": "abc123",
        "review_batch_id": None,
        "must_fix": [
            {
                "source": "review_comment",
                "path": "app/main.py",
                "line": 10,
                "severity": "P1",
                "text": "Please handle timeout.",
            }
        ],
        "should_fix": [],
        "ignore": [],
        "summary": "Needs one critical fix.",
    }


def test_issue_item_invalid_severity_raises_error() -> None:
    with pytest.raises(ValidationError):
        NormalizedReview(
            repo="acme/widgets",
            pr_number=42,
            head_sha=None,
            must_fix=[
                {
                    "source": "review_body",
                    "path": None,
                    "line": 1,
                    "severity": "P4",
                    "text": "invalid severity",
                }
            ],
            should_fix=[],
            ignore=[],
            summary="x",
        )


def test_issue_item_non_positive_line_raises_error() -> None:
    with pytest.raises(ValidationError):
        NormalizedReview(
            repo="acme/widgets",
            pr_number=42,
            head_sha=None,
            must_fix=[
                {
                    "source": "review_body",
                    "path": "app/main.py",
                    "line": 0,
                    "severity": "P2",
                    "text": "line must be positive",
                }
            ],
            should_fix=[],
            ignore=[],
            summary="x",
        )


def test_extra_field_raises_error() -> None:
    with pytest.raises(ValidationError):
        NormalizedReview(
            repo="acme/widgets",
            pr_number=42,
            head_sha=None,
            must_fix=[
                {
                    "source": "review_body",
                    "path": None,
                    "line": None,
                    "severity": "P2",
                    "text": "ok",
                    "extra": "not allowed",
                }
            ],
            should_fix=[],
            ignore=[],
            summary="x",
        )
