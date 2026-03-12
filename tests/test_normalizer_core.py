import json
from pathlib import Path

import pytest

from app.services.normalizer import classify_severity, normalize_review_events


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "normalizer_events"


def _load_events_fixture(name: str) -> list[dict]:
    fixture_path = _FIXTURE_DIR / name
    with fixture_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def test_normalize_aggregates_three_event_types() -> None:
    events = _load_events_fixture("mixed_events.json")

    result = normalize_review_events("acme/widgets", 9, events, head_sha="abc123")

    assert result["repo"] == "acme/widgets"
    assert result["pr_number"] == 9
    assert result["head_sha"] == "abc123"
    assert len(result["must_fix"]) == 2
    assert len(result["should_fix"]) == 1
    assert len(result["ignore"]) == 0


def test_dedupe_uses_source_path_line_and_normalized_text() -> None:
    events = _load_events_fixture("duplicates_and_noise.json")

    result = normalize_review_events("acme/widgets", 11, events)

    assert len(result["must_fix"]) == 1
    assert len(result["ignore"]) == 2
    assert result["should_fix"] == []
    assert result["summary"] == "1 blocking issues, 0 suggestions, 2 ignored"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Critical security issue", "P0"),
        ("This causes exception on None", "P1"),
        ("Needs refactor for maintainability", "P2"),
        ("Nit: rename variable", "P3"),
    ],
)
def test_classify_severity(text: str, expected: str) -> None:
    assert classify_severity(text) == expected


def test_summary_and_ignore_text() -> None:
    events = [
        {
            "event_type": "pull_request_review",
            "payload": {
                "review": {
                    "state": "changes_requested",
                    "body": "error handling is broken",
                }
            },
        },
        {
            "event_type": "issue_comment",
            "payload": {
                "issue": {"pull_request": {"url": "https://example.test/pr/9"}},
                "comment": {"body": "thanks"},
            },
        },
    ]

    result = normalize_review_events("acme/widgets", 9, events)

    assert result["summary"] == "1 blocking issues, 0 suggestions, 1 ignored"


def test_issue_comment_without_pr_reference_is_ignored() -> None:
    events = [
        {
            "event_type": "issue_comment",
            "payload": {
                "issue": {"number": 9},
                "comment": {"body": "this should not be included"},
            },
        }
    ]

    result = normalize_review_events("acme/widgets", 9, events)

    assert result["must_fix"] == []
    assert result["should_fix"] == []
    assert result["ignore"] == []
    assert result["summary"] == "0 blocking issues, 0 suggestions, 0 ignored"


def test_huge_line_number_string_is_treated_as_none() -> None:
    events = [
        {
            "event_type": "pull_request_review_comment",
            "payload": {
                "comment": {
                    "body": "null handling issue",
                    "path": "app/main.py",
                    "line": str(2**200),
                }
            },
        }
    ]

    result = normalize_review_events("acme/widgets", 9, events)

    assert len(result["must_fix"]) == 1
    assert result["must_fix"][0]["line"] is None
