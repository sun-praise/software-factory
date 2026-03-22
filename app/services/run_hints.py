from __future__ import annotations


RUN_HINT_EDITABLE_STATUSES = frozenset(
    {"queued", "running", "cancel_requested", "retry_scheduled"}
)
OPERATOR_HINT_SEPARATOR = "\n\n---\n"
OPERATOR_HINT_APPEND_MAX_CHARS = 1_000
OPERATOR_HINTS_MAX_CHARS = 4_000
OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT = 2_000
