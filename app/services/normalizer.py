from __future__ import annotations

import logging
import re
import sys
from typing import Any, Mapping


logger = logging.getLogger(__name__)


SEMANTIC_BLOCKING_DEFECT = "blocking_defect"
SEMANTIC_NON_BLOCKING_SUGGESTION = "non_blocking_suggestion"
SEMANTIC_CLARIFICATION = "clarification"
SEMANTIC_INFORMATIONAL = "informational"
SEMANTIC_NEEDS_HUMAN_DECISION = "needs_human_decision"

_SEVERITY_BLOCKING_TERMS = (
    "security",
    "critical",
    "crash",
    "data loss",
    "null",
    "none",
    "exception",
    "error",
    "fail",
    "bug",
    "incorrect",
    "wrong",
    "broken",
    "invalid",
    "vulnerability",
    "exploit",
    "injection",
)

_SEVERITY_P0_TERMS = ("security", "critical", "crash", "data loss")
_SEVERITY_P1_TERMS = ("null", "none", "exception", "error", "fail", "bug")
_SEVERITY_P2_TERMS = ("refactor", "maintainability", "performance")

_IGNORE_TEXTS = {
    "+1",
    "lgtm",
    "thanks",
    "thank you",
    "thx",
    "ty",
}

_QUESTION_MARKERS = (
    re.compile(r"\?$"),
    re.compile(
        r"^(what|why|how|when|where|who|which|can you|could you|is it|does this|do we)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:wondering|curious|unsure|unclear|confused)\b"),
)

_SUGGESTION_MARKERS = (
    re.compile(
        r"\b(?:consider|could you |might want to|suggest|recommend|perhaps|maybe)\b"
    ),
    re.compile(r"\bnit[:\s]", re.IGNORECASE),
    re.compile(r"\b(?:minor|cosmetic|style|formatting|naming|nitpick)\b"),
    re.compile(r"\b(?:alternatively|instead (?:of|you could)|another option)\b"),
)

_INFORMATIONAL_MARKERS = (
    re.compile(
        r"^(?:lgtm|looks good|looks great|nice|well done|ack|acknowledged|agreed)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^(\+1|👍|👍🏻)\s*$"),
)

_NEEDS_HUMAN_MARKERS = (
    re.compile(
        r"\b(?:depends? on|up to you|your call|tbd|to be determined)\b", re.IGNORECASE
    ),
    re.compile(r"\b(?:trade[- ]off|pros? and cons?)\b"),
    re.compile(r"\b(?:not sure|conflicted|confusing|uncertain)\b"),
    re.compile(r"\b(?:either way|whichever|debatable)\b"),
)

_BLOCKING_MARKERS = (
    re.compile(r"\b(?:must|need to|has to|required|mandatory|critical|blocking)\b"),
    re.compile(
        r"\b(?:security|vulnerability|exploit|injection|xss|csrf|sqli)\b", re.IGNORECASE
    ),
    re.compile(r"\b(?:crash|data loss|corruption|deadlock|race condition)\b"),
)

_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "this",
        "that",
        "with",
        "from",
        "are",
        "was",
        "were",
        "been",
        "have",
        "has",
        "had",
        "will",
        "would",
        "could",
        "should",
        "can",
        "may",
        "not",
        "but",
        "its",
        "you",
        "your",
        "our",
        "their",
        "does",
        "did",
        "doing",
        "about",
        "into",
        "over",
        "than",
        "also",
        "just",
        "very",
        "some",
        "any",
        "all",
        "more",
        "most",
    }
)

NEEDS_HUMAN_REVIEW_CONFIDENCE_THRESHOLD = 0.5
SHORT_TEXT_THRESHOLD = 10
KEYWORD_OVERLAP_THRESHOLD = 0.2

_STEMMING_EXCEPTIONS = frozenset(
    {
        "analysis",
        "basis",
        "class",
        "crisis",
        "diagnosis",
        "emphasis",
        "hypothesis",
        "oasis",
        "parenthesis",
        "status",
        "thesis",
        "this",
        "bus",
        "gas",
        "has",
        "was",
        "yes",
        "access",
        "process",
        "address",
        "compress",
        "express",
        "dismiss",
        "excess",
        "success",
        "witness",
        "assess",
        "business",
        "discuss",
        "response",
        "release",
        "cause",
        "because",
        "pause",
        "clause",
        "issue",
        "tissue",
        "across",
        "focus",
        "virus",
        "bonus",
        "census",
        "consensus",
        "corpus",
        "stimulus",
        "radius",
        "genus",
        "nexus",
        "plus",
        "thus",
    }
)


def normalize_review_events(
    repo: str,
    pr_number: int,
    events: list[dict],
    head_sha: str | None = None,
    *,
    enable_semantic: bool = True,
) -> dict:
    must_fix: list[dict[str, Any]] = []
    should_fix: list[dict[str, Any]] = []
    ignore: list[dict[str, Any]] = []
    needs_human_review_items: list[dict[str, Any]] = []
    seen_actionable: set[tuple[str, str | None, int | None, str]] = set()
    seen_ignore: set[tuple[str, str | None, int | None, str]] = set()

    all_actionable_items: list[dict[str, Any]] = []

    for event in events:
        if not isinstance(event, Mapping):
            logger.debug("Skip non-mapping review event: %r", event)
            continue

        event_type_raw = event.get("event_type")
        payload_raw = event.get("payload")
        if not isinstance(event_type_raw, str) or not isinstance(payload_raw, Mapping):
            logger.debug("Skip invalid event envelope: %r", event)
            continue

        event_type = event_type_raw.strip().lower()
        payload: Mapping[str, Any] = payload_raw

        candidate = _extract_candidate(event_type, payload)
        if candidate is None:
            logger.debug("Skip unsupported or incomplete event type=%s", event_type)
            continue

        normalized_text = _normalize_text_for_dedupe(candidate["text"])
        dedupe_key = (
            candidate["source"],
            candidate["path"],
            candidate["line"],
            normalized_text,
        )

        if _is_ignorable_text(normalized_text):
            if dedupe_key in seen_ignore:
                continue
            seen_ignore.add(dedupe_key)

            if enable_semantic:
                semantic_type, confidence = _classify_semantic_type(
                    candidate["text"], candidate["source"], candidate["is_must_fix"]
                )
            else:
                semantic_type = SEMANTIC_INFORMATIONAL
                confidence = 1.0

            item = _build_item(candidate, semantic_type, confidence, "P3", False, None)
            ignore.append(item)
            continue

        if dedupe_key in seen_actionable:
            continue
        seen_actionable.add(dedupe_key)

        base_severity = classify_severity(candidate["text"])

        if enable_semantic:
            semantic_type, confidence = _classify_semantic_type(
                candidate["text"], candidate["source"], candidate["is_must_fix"]
            )
            severity = _enhance_severity(base_severity, semantic_type, confidence)
            human_review = _needs_human_review(
                candidate["text"], semantic_type, confidence
            )
        else:
            semantic_type = (
                SEMANTIC_BLOCKING_DEFECT
                if candidate["is_must_fix"]
                else SEMANTIC_NON_BLOCKING_SUGGESTION
            )
            confidence = 1.0
            severity = base_severity
            human_review = False

        item = _build_item(
            candidate, semantic_type, confidence, severity, human_review, None
        )

        if semantic_type == SEMANTIC_INFORMATIONAL:
            ignore.append(item)
        elif semantic_type in (
            SEMANTIC_NON_BLOCKING_SUGGESTION,
            SEMANTIC_CLARIFICATION,
        ):
            should_fix.append(item)
        elif semantic_type == SEMANTIC_NEEDS_HUMAN_DECISION:
            should_fix.append(item)
            needs_human_review_items.append(item)
        else:
            must_fix.append(item)

        all_actionable_items.append(item)

    semantic_groups: list[dict[str, Any]] = []
    if enable_semantic and all_actionable_items:
        semantic_groups, group_assignments = _detect_semantic_groups(
            all_actionable_items
        )
        for item in all_actionable_items:
            gid = group_assignments.get(id(item))
            if gid is not None:
                item["group_id"] = gid

    summary = (
        f"{len(must_fix)} blocking issues, "
        f"{len(should_fix)} suggestions, "
        f"{len(ignore)} ignored"
    )
    if enable_semantic and needs_human_review_items:
        summary += f", {len(needs_human_review_items)} need human review"

    result: dict[str, Any] = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "must_fix": must_fix,
        "should_fix": should_fix,
        "ignore": ignore,
        "summary": summary,
    }

    if enable_semantic:
        result["needs_human_review_count"] = len(needs_human_review_items)
        result["semantic_groups"] = semantic_groups

    return result


def classify_severity(text: str) -> str:
    normalized = text.lower()
    if _contains_any(normalized, _SEVERITY_P0_TERMS):
        return "P0"
    if _contains_any(normalized, _SEVERITY_P1_TERMS):
        return "P1"
    if _contains_any(normalized, _SEVERITY_P2_TERMS):
        return "P2"
    return "P3"


def _classify_semantic_type(
    text: str,
    source: str,
    is_must_fix: bool,
) -> tuple[str, float]:
    normalized = text.lower().strip()
    if not normalized:
        return SEMANTIC_INFORMATIONAL, 1.0

    if any(p.search(normalized) for p in _INFORMATIONAL_MARKERS):
        return SEMANTIC_INFORMATIONAL, 0.9

    if any(p.search(normalized) for p in _QUESTION_MARKERS):
        return SEMANTIC_CLARIFICATION, 0.8

    has_blocking_terms = _contains_any(normalized, _SEVERITY_BLOCKING_TERMS)
    has_explicit_blocking = any(p.search(normalized) for p in _BLOCKING_MARKERS)

    suggestion_count = sum(1 for p in _SUGGESTION_MARKERS if p.search(normalized))
    has_suggestion = suggestion_count > 0

    if has_suggestion and (has_blocking_terms or has_explicit_blocking):
        return SEMANTIC_NEEDS_HUMAN_DECISION, 0.5

    if has_suggestion:
        confidence = 0.7 + min(suggestion_count * 0.1, 0.2)
        return SEMANTIC_NON_BLOCKING_SUGGESTION, confidence

    if has_explicit_blocking:
        return SEMANTIC_BLOCKING_DEFECT, 0.8

    if has_blocking_terms:
        return SEMANTIC_BLOCKING_DEFECT, 0.7

    if any(p.search(normalized) for p in _NEEDS_HUMAN_MARKERS):
        return SEMANTIC_NEEDS_HUMAN_DECISION, 0.6

    if is_must_fix:
        return SEMANTIC_BLOCKING_DEFECT, 0.5

    return SEMANTIC_NON_BLOCKING_SUGGESTION, 0.5


def _enhance_severity(
    base_severity: str,
    semantic_type: str,
    confidence: float,
) -> str:
    if semantic_type == SEMANTIC_INFORMATIONAL:
        return "P3"

    if semantic_type == SEMANTIC_BLOCKING_DEFECT:
        if base_severity in ("P0", "P1"):
            return base_severity
        if confidence > 0.7:
            return "P1"
        return "P2"

    if semantic_type == SEMANTIC_CLARIFICATION:
        return "P3"

    if semantic_type == SEMANTIC_NEEDS_HUMAN_DECISION:
        return base_severity

    return base_severity


def _needs_human_review(
    text: str,
    semantic_type: str,
    confidence: float,
) -> bool:
    if confidence < NEEDS_HUMAN_REVIEW_CONFIDENCE_THRESHOLD:
        return True
    if semantic_type == SEMANTIC_NEEDS_HUMAN_DECISION:
        return True
    stripped = text.strip()
    if (
        len(stripped) < SHORT_TEXT_THRESHOLD
        and stripped
        and semantic_type != SEMANTIC_INFORMATIONAL
    ):
        return True
    return False


def _detect_semantic_groups(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    summary_items = [
        i
        for i in items
        if i["source"] == "pull_request_review"
        and i.get("semantic_type") != SEMANTIC_INFORMATIONAL
    ]
    inline_items = [i for i in items if i["source"] == "pull_request_review_comment"]

    if not summary_items or not inline_items:
        return [], {}

    groups: list[dict[str, Any]] = []
    group_assignments: dict[int, str] = {}
    assigned_inline_ids: set[int] = set()

    for summary_idx, summary in enumerate(summary_items):
        summary_keywords = _extract_keywords(summary["text"])
        if not summary_keywords:
            continue

        group: dict[str, Any] = {
            "type": "summary_inline",
            "summary_index": summary_idx,
            "inline_indices": [],
            "overlap_keywords": [],
            "overlap_score": 0.0,
        }

        for inline_idx, inline in enumerate(inline_items):
            if inline_idx in assigned_inline_ids:
                continue
            inline_keywords = _extract_keywords(inline["text"])
            if not inline_keywords:
                continue

            overlap = summary_keywords & inline_keywords
            if overlap:
                combined_size = max(len(summary_keywords), len(inline_keywords), 1)
                score = len(overlap) / combined_size
                if score >= KEYWORD_OVERLAP_THRESHOLD:
                    group["inline_indices"].append(inline_idx)
                    group["overlap_keywords"].extend(sorted(overlap))
                    assigned_inline_ids.add(inline_idx)
                    group["overlap_score"] = max(group["overlap_score"], score)

        if group["inline_indices"]:
            group_id = f"sg_{summary_idx}"
            group["group_id"] = group_id
            group_assignments[id(summary)] = group_id
            for idx in group["inline_indices"]:
                group_assignments[id(inline_items[idx])] = group_id
            groups.append(group)

    return groups, group_assignments


def _extract_keywords(text: str) -> set[str]:
    normalized = text.lower().strip()
    words = re.findall(r"\b[a-z]{3,}\b", normalized)
    stemmed: set[str] = set()
    for w in words:
        if w in _STOP_WORDS:
            continue
        if (
            w.endswith("s")
            and not w.endswith("ss")
            and len(w) > 3
            and w not in _STEMMING_EXCEPTIONS
        ):
            w = w[:-1]
        stemmed.add(w)
    return stemmed


def _build_item(
    candidate: dict[str, Any],
    semantic_type: str,
    confidence: float,
    severity: str,
    needs_human_review: bool,
    group_id: str | None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source": candidate["source"],
        "path": candidate["path"],
        "line": candidate["line"],
        "text": candidate["text"],
        "severity": severity,
    }
    if semantic_type != SEMANTIC_NON_BLOCKING_SUGGESTION or confidence != 1.0:
        item["semantic_type"] = semantic_type
    if confidence != 1.0:
        item["confidence"] = confidence
    if needs_human_review:
        item["needs_human_review"] = True
    if group_id is not None:
        item["group_id"] = group_id
    return item


def _extract_candidate(
    event_type: str, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    if event_type == "pull_request_review":
        review = payload.get("review")
        if not isinstance(review, Mapping):
            return None

        body = _as_text(review.get("body"))
        state = _as_text(review.get("state"))
        return {
            "source": "pull_request_review",
            "path": None,
            "line": None,
            "text": body,
            "is_must_fix": (state or "").lower() == "changes_requested",
        }

    if event_type == "pull_request_review_comment":
        comment = payload.get("comment")
        if not isinstance(comment, Mapping):
            return None

        body = _as_text(comment.get("body"))
        path = _as_text(comment.get("path"))
        line = _as_int(comment.get("line"))
        if line is None:
            line = _as_int(comment.get("start_line"))
        return {
            "source": "pull_request_review_comment",
            "path": path,
            "line": line,
            "text": body,
            "is_must_fix": True,
        }

    if event_type == "issue_comment":
        issue = payload.get("issue")
        if not isinstance(issue, Mapping):
            return None
        if not isinstance(issue.get("pull_request"), Mapping):
            return None

        comment = payload.get("comment")
        if not isinstance(comment, Mapping):
            return None

        body = _as_text(comment.get("body"))
        return {
            "source": "issue_comment",
            "path": None,
            "line": None,
            "text": body,
            "is_must_fix": False,
        }

    return None


def _normalize_text_for_dedupe(text: str) -> str:
    lowered = text.lower().strip()
    return re.sub(r"\s+", " ", lowered)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        if value < 0 or value > sys.maxsize:
            return None
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        if parsed > sys.maxsize:
            return None
        return parsed
    return None


def _is_ignorable_text(normalized_text: str) -> bool:
    return not normalized_text or normalized_text in _IGNORE_TEXTS
