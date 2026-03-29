import json
from pathlib import Path

from app.services.normalizer import (
    SEMANTIC_BLOCKING_DEFECT,
    SEMANTIC_CLARIFICATION,
    SEMANTIC_INFORMATIONAL,
    SEMANTIC_NEEDS_HUMAN_DECISION,
    SEMANTIC_NON_BLOCKING_SUGGESTION,
    _classify_semantic_type,
    _enhance_severity,
    _extract_keywords,
    _needs_human_review,
    normalize_review_events,
)


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "normalizer_events"


def _load_events_fixture(name: str) -> list[dict]:
    fixture_path = _FIXTURE_DIR / name
    with fixture_path.open("r", encoding="utf-8") as file:
        return json.load(file)


class TestSemanticTypeClassification:
    def test_suggestion_inline_comment(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Consider using a different variable name here",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_NON_BLOCKING_SUGGESTION
        assert confidence >= 0.7

    def test_question_inline_comment(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Why did you choose this approach?",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_CLARIFICATION
        assert confidence >= 0.7

    def test_informational_inline_comment(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Looks good to me!",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_INFORMATIONAL
        assert confidence >= 0.8

    def test_needs_human_decision(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "This depends on the performance requirements",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_NEEDS_HUMAN_DECISION
        assert confidence >= 0.5

    def test_short_text_low_confidence(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Hmm",
            "pull_request_review_comment",
            True,
        )
        assert confidence < 0.7

    def test_bug_report_blocking(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "This needs null check here, otherwise it will crash",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_BLOCKING_DEFECT
        assert confidence >= 0.7

    def test_security_issue_blocking(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Security issue: user input is not sanitized",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_BLOCKING_DEFECT
        assert confidence >= 0.7

    def test_nit_suggestion(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Nit: extra space on this line",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_NON_BLOCKING_SUGGESTION

    def test_conflicting_signals_needs_human(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Consider fixing this security issue by using prepared statements",
            "pull_request_review_comment",
            True,
        )
        assert semantic_type == SEMANTIC_NEEDS_HUMAN_DECISION

    def test_empty_text_informational(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "",
            "pull_request_review",
            False,
        )
        assert semantic_type == SEMANTIC_INFORMATIONAL
        assert confidence == 1.0

    def test_issue_comment_suggestion(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Please refactor this for maintainability",
            "issue_comment",
            False,
        )
        assert semantic_type == SEMANTIC_NON_BLOCKING_SUGGESTION

    def test_changes_requested_with_security(self) -> None:
        semantic_type, confidence = _classify_semantic_type(
            "Critical security issue in auth flow",
            "pull_request_review",
            True,
        )
        assert semantic_type == SEMANTIC_BLOCKING_DEFECT
        assert confidence >= 0.7


class TestEnhancedSeverity:
    def test_blocking_defect_promotes_p3_to_p1(self) -> None:
        assert _enhance_severity("P3", SEMANTIC_BLOCKING_DEFECT, 0.8) == "P1"

    def test_blocking_defect_keeps_p0(self) -> None:
        assert _enhance_severity("P0", SEMANTIC_BLOCKING_DEFECT, 0.9) == "P0"

    def test_blocking_defect_keeps_p1(self) -> None:
        assert _enhance_severity("P1", SEMANTIC_BLOCKING_DEFECT, 0.7) == "P1"

    def test_blocking_defect_low_confidence_p2(self) -> None:
        assert _enhance_severity("P3", SEMANTIC_BLOCKING_DEFECT, 0.5) == "P2"

    def test_clarification_always_p3(self) -> None:
        assert _enhance_severity("P0", SEMANTIC_CLARIFICATION, 0.8) == "P3"
        assert _enhance_severity("P1", SEMANTIC_CLARIFICATION, 0.8) == "P3"

    def test_informational_always_p3(self) -> None:
        assert _enhance_severity("P0", SEMANTIC_INFORMATIONAL, 0.9) == "P3"

    def test_suggestion_keeps_base(self) -> None:
        assert _enhance_severity("P2", SEMANTIC_NON_BLOCKING_SUGGESTION, 0.7) == "P2"

    def test_needs_human_keeps_base(self) -> None:
        assert _enhance_severity("P1", SEMANTIC_NEEDS_HUMAN_DECISION, 0.6) == "P1"


class TestNeedsHumanReview:
    def test_low_confidence(self) -> None:
        assert _needs_human_review("some text", SEMANTIC_BLOCKING_DEFECT, 0.3)

    def test_needs_human_decision_type(self) -> None:
        assert _needs_human_review(
            "This depends on requirements", SEMANTIC_NEEDS_HUMAN_DECISION, 0.8
        )

    def test_short_text(self) -> None:
        assert _needs_human_review("Hmm", SEMANTIC_BLOCKING_DEFECT, 0.5)

    def test_high_confidence_no_flag(self) -> None:
        assert not _needs_human_review(
            "This is a detailed bug report with context", SEMANTIC_BLOCKING_DEFECT, 0.8
        )

    def test_informational_never_flagged(self) -> None:
        assert not _needs_human_review("LGTM", SEMANTIC_INFORMATIONAL, 0.9)


class TestKeywordExtraction:
    def test_extracts_meaningful_words(self) -> None:
        keywords = _extract_keywords("Please fix the error handling and null checks")
        assert "error" in keywords
        assert "null" in keywords
        assert "fix" in keywords
        assert "handling" in keywords
        assert "check" in keywords
        assert "please" in keywords
        assert "the" not in keywords
        assert "and" not in keywords

    def test_ignores_short_words(self) -> None:
        keywords = _extract_keywords("fix bug in the code")
        assert "fix" in keywords
        assert "bug" in keywords
        assert "the" not in keywords
        assert "in" not in keywords


class TestSemanticIntegration:
    def test_suggestion_goes_to_should_fix_not_must_fix(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Consider using a different variable name here",
                        "path": "app/main.py",
                        "line": 10,
                    }
                },
            }
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert len(result["must_fix"]) == 0
        assert len(result["should_fix"]) == 1
        assert (
            result["should_fix"][0]["semantic_type"] == SEMANTIC_NON_BLOCKING_SUGGESTION
        )

    def test_informational_goes_to_ignore(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Looks good to me!",
                        "path": "app/main.py",
                        "line": 10,
                    }
                },
            }
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert len(result["must_fix"]) == 0
        assert len(result["should_fix"]) == 0
        assert len(result["ignore"]) == 1
        assert result["ignore"][0]["semantic_type"] == SEMANTIC_INFORMATIONAL

    def test_question_goes_to_should_fix(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Why is this using a for loop instead of map?",
                        "path": "app/main.py",
                        "line": 20,
                    }
                },
            }
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert len(result["must_fix"]) == 0
        assert len(result["should_fix"]) == 1
        assert result["should_fix"][0]["semantic_type"] == SEMANTIC_CLARIFICATION

    def test_bug_in_inline_goes_to_must_fix(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "This will throw an exception when user is None",
                        "path": "app/main.py",
                        "line": 15,
                    }
                },
            }
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert len(result["must_fix"]) == 1
        assert result["must_fix"][0]["semantic_type"] == SEMANTIC_BLOCKING_DEFECT

    def test_needs_human_decision_flagged(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "This depends on the performance requirements",
                        "path": "app/main.py",
                        "line": 30,
                    }
                },
            }
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert result["needs_human_review_count"] == 1
        assert result["should_fix"][0]["needs_human_review"] is True

    def test_backward_compatible_without_semantic(self) -> None:
        events = _load_events_fixture("mixed_events.json")
        result = normalize_review_events(
            "acme/widgets", 9, events, head_sha="abc123", enable_semantic=False
        )
        assert len(result["must_fix"]) == 2
        assert len(result["should_fix"]) == 1
        assert "needs_human_review_count" not in result
        assert "semantic_groups" not in result


class TestSummaryInlineDedup:
    def test_detects_summary_inline_overlap(self) -> None:
        events = [
            {
                "event_type": "pull_request_review",
                "payload": {
                    "review": {
                        "state": "changes_requested",
                        "body": "Please fix the error handling and null checks throughout this module",
                    }
                },
            },
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "This needs null check here, otherwise it will crash",
                        "path": "app/core/engine.py",
                        "line": 55,
                    }
                },
            },
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert len(result["semantic_groups"]) >= 1
        group = result["semantic_groups"][0]
        assert group["type"] == "summary_inline"
        assert group["overlap_score"] > 0

    def test_related_items_share_group_id(self) -> None:
        events = [
            {
                "event_type": "pull_request_review",
                "payload": {
                    "review": {
                        "state": "changes_requested",
                        "body": "Please fix the error handling and null checks throughout this module",
                    }
                },
            },
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "This needs null check here, otherwise it will crash",
                        "path": "app/core/engine.py",
                        "line": 55,
                    }
                },
            },
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        must_fix_items = result["must_fix"]
        group_ids = [i.get("group_id") for i in must_fix_items if i.get("group_id")]
        assert len(group_ids) >= 2
        assert group_ids[0] == group_ids[1]

    def test_no_groups_without_summary(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Fix this bug",
                        "path": "app/main.py",
                        "line": 10,
                    }
                },
            },
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Another issue here",
                        "path": "app/main.py",
                        "line": 20,
                    }
                },
            },
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert len(result["semantic_groups"]) == 0


class TestComplexReviewScenarios:
    def test_semantic_review_scenarios_fixture(self) -> None:
        events = _load_events_fixture("semantic_review_scenarios.json")
        result = normalize_review_events("acme/widgets", 1, events)

        assert result["needs_human_review_count"] >= 1

        must_fix = result["must_fix"]
        should_fix = result["should_fix"]
        ignore = result["ignore"]

        blocking_types = {i.get("semantic_type") for i in must_fix}
        suggestion_types = {i.get("semantic_type") for i in should_fix}
        informational_types = {i.get("semantic_type") for i in ignore}

        assert SEMANTIC_BLOCKING_DEFECT in blocking_types
        assert SEMANTIC_NON_BLOCKING_SUGGESTION in suggestion_types
        assert SEMANTIC_CLARIFICATION in suggestion_types
        assert SEMANTIC_INFORMATIONAL in informational_types

        assert len(result["semantic_groups"]) >= 1

    def test_mixed_review_types_properly_separated(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Security issue: user input is not sanitized before SQL",
                        "path": "app/api/users.py",
                        "line": 33,
                    }
                },
            },
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Consider using a constant here",
                        "path": "app/api/users.py",
                        "line": 10,
                    }
                },
            },
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Can you explain why this timeout value was chosen?",
                        "path": "app/api/users.py",
                        "line": 25,
                    }
                },
            },
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "lgtm",
                        "path": "app/api/users.py",
                        "line": 5,
                    }
                },
            },
        ]
        result = normalize_review_events("acme/widgets", 1, events)

        assert len(result["must_fix"]) == 1
        assert result["must_fix"][0]["semantic_type"] == SEMANTIC_BLOCKING_DEFECT
        assert result["must_fix"][0]["severity"] == "P0"

        assert len(result["should_fix"]) == 2
        types = {i["semantic_type"] for i in result["should_fix"]}
        assert SEMANTIC_NON_BLOCKING_SUGGESTION in types
        assert SEMANTIC_CLARIFICATION in types

        assert len(result["ignore"]) == 1
        assert result["ignore"][0]["semantic_type"] == SEMANTIC_INFORMATIONAL

    def test_confidence_field_present_on_non_default_items(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "This is a bug that needs fixing",
                        "path": "app/main.py",
                        "line": 10,
                    }
                },
            },
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "Hmm",
                        "path": "app/main.py",
                        "line": 20,
                    }
                },
            },
        ]
        result = normalize_review_events("acme/widgets", 1, events)

        assert len(result["must_fix"]) == 2
        must_fix_item = result["must_fix"][0]
        assert "confidence" in must_fix_item
        assert must_fix_item["confidence"] > 0

        hmm_item = result["must_fix"][1]
        assert hmm_item["needs_human_review"] is True
        assert hmm_item["confidence"] < 0.7

    def test_summary_includes_human_review_count(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "This depends on the team's decision",
                        "path": "app/main.py",
                        "line": 10,
                    }
                },
            },
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert "need human review" in result["summary"]

    def test_summary_without_human_review_unchanged(self) -> None:
        events = [
            {
                "event_type": "pull_request_review_comment",
                "payload": {
                    "comment": {
                        "body": "This will throw an exception when user is None",
                        "path": "app/main.py",
                        "line": 10,
                    }
                },
            },
        ]
        result = normalize_review_events("acme/widgets", 1, events)
        assert "1 blocking issues, 0 suggestions, 0 ignored" == result["summary"]
