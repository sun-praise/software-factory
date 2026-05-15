"""Verify bugs found during code review (#213 - #216).

All tests in this file are expected to FAIL until the corresponding bugs are fixed.
Each test asserts the correct (desired) behaviour, exposing the current buggy behaviour.
"""
from __future__ import annotations

import json
import logging
import shlex
import sqlite3
from unittest.mock import patch

import pytest

from app.models import SCHEMA_SQL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_runs(conn: sqlite3.Connection, runs: list[dict]) -> None:
    for r in runs:
        conn.execute(
            """
            INSERT INTO autofix_runs
                (repo, pr_number, status, trigger_source, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                r["repo"],
                r["pr_number"],
                r.get("status", "queued"),
                r.get("trigger_source", "github_webhook"),
                json.dumps(r.get("review", {})),
            ),
        )
    conn.commit()


# ===================================================================
# Bug #213  –  LIKE wildcards not escaped in _fetch_runs search
# ===================================================================

class TestLikeWildcardNotEscaped:
    """Business scenario: an operator searches for a repo name that contains
    SQL LIKE special characters (``%`` or ``_``).  Because ``_fetch_runs``
    does not escape these characters, the search returns more rows than
    expected.

    The underscore ``_`` in SQL LIKE matches any single character, so
    searching for ``api_service`` would also match ``api-service``.
    """

    @pytest.fixture()
    def _seeded_conn(self):
        conn = _make_conn()
        _insert_runs(conn, [
            {"repo": "acme/api_service", "pr_number": 1},
            {"repo": "acme/api-service", "pr_number": 2},
            {"repo": "acme/x_service",   "pr_number": 3},
        ])
        return conn

    @patch("app.routes.web.connect_db")
    @patch("app.routes.web.resolve_runtime_settings")
    @patch("app.routes.web.get_runtime_form_int_field_specs", return_value={})
    def test_underscore_not_escaped(self, _mock_specs, _mock_rt, mock_db, _seeded_conn):
        mock_db.return_value.__enter__ = lambda s: _seeded_conn
        mock_db.return_value.__exit__ = lambda s, *a: None

        from app.routes.web import _fetch_runs

        result = _fetch_runs(page=1, page_size=20, query="api_service")

        # Desired: only "acme/api_service" matches.
        # Bug:     "_" acts as LIKE wildcard, also matching "api-service".
        matched_repos = [r["repo"] for r in result["items"]]
        assert matched_repos == ["acme/api_service"], (
            f"Bug #213: LIKE wildcard '_' not escaped – "
            f"expected only acme/api_service but got {matched_repos}"
        )

    @patch("app.routes.web.connect_db")
    @patch("app.routes.web.resolve_runtime_settings")
    @patch("app.routes.web.get_runtime_form_int_field_specs", return_value={})
    def test_percent_not_escaped(self, _mock_specs, _mock_rt, mock_db):
        conn = _make_conn()
        _insert_runs(conn, [
            {"repo": "acme/service-v2",  "pr_number": 1},
            {"repo": "acme/service-v2a", "pr_number": 2},
            {"repo": "acme/service-v2b", "pr_number": 3},
        ])

        mock_db.return_value.__enter__ = lambda s: conn
        mock_db.return_value.__exit__ = lambda s, *a: None

        from app.routes.web import _fetch_runs

        result = _fetch_runs(page=1, page_size=20, query="service-v2%")

        # Desired: no repo literally named "service-v2%", so nothing matches.
        # Bug:     "%" acts as LIKE wildcard, matching all three repos.
        matched_repos = [r["repo"] for r in result["items"]]
        assert matched_repos == [], (
            f"Bug #213: LIKE wildcard '%' not escaped – "
            f"expected empty results but got {matched_repos}"
        )


# ===================================================================
# Bug #214  –  _read_metadata silently swallows JSON errors
# ===================================================================

class TestReadMetadataSilentSwallow:
    """Business scenario: session metadata_json in the DB is corrupted.
    When the system processes hooks, _read_metadata returns an empty dict
    without logging any warning, making it impossible to diagnose why
    session data is being silently discarded.
    """

    def test_invalid_json_emits_no_warning(self, caplog):
        from app.services.hooks import _read_metadata

        with caplog.at_level(logging.WARNING, logger="app.services.hooks"):
            result = _read_metadata('{"broken": ')

        assert result == {}

        # Desired: at least one warning log should be emitted.
        # Bug:     no log at all – the error is silently swallowed.
        assert len(caplog.records) > 0, (
            "Bug #214: _read_metadata silently swallows JSON parse errors "
            "without emitting any log, making corruption impossible to debug"
        )

    def test_non_dict_json_emits_no_warning(self, caplog):
        from app.services.hooks import _read_metadata

        with caplog.at_level(logging.WARNING, logger="app.services.hooks"):
            result = _read_metadata("[1, 2, 3]")

        assert result == {}

        # Desired: at least one warning log should be emitted for non-dict JSON.
        # Bug:     silently returns {} with no diagnostic.
        assert len(caplog.records) > 0, (
            "Bug #214: _read_metadata returns {} for non-dict JSON without logging"
        )


# ===================================================================
# Bug #215  –  _DISALLOWED_COMMAND_TOKENS skips inner_argv[0]
# ===================================================================

class TestDisallowedCommandTokensSkipsFirst:
    """Business scenario: an admin configures an agent command.  The security
    check is meant to reject any shell control operators, but it only checks
    inner_argv[1:], skipping the first token.

    While shlex.split normally won't produce a shell operator as the first
    token, the check itself is logically incomplete – it should validate
    ALL tokens, not just from index 1 onward.
    """

    def test_check_scope_covers_all_tokens(self):
        from app.services.agent_runner import _DISALLOWED_COMMAND_TOKENS

        # Simulate what the code does: check only inner_argv[1:]
        tokens_checked = ["echo", "&&", "rm", "-rf", "/"]
        checked_tail = any(t in _DISALLOWED_COMMAND_TOKENS for t in tokens_checked[1:])
        checked_all  = any(t in _DISALLOWED_COMMAND_TOKENS for t in tokens_checked)

        # Both should detect the "&&" in position 1
        assert checked_tail is True
        assert checked_all is True

        # Now construct an artificial case: operator in position 0
        tokens_first = [";", "evil_command"]
        checked_tail_first = any(t in _DISALLOWED_COMMAND_TOKENS for t in tokens_first[1:])
        checked_all_first  = any(t in _DISALLOWED_COMMAND_TOKENS for t in tokens_first)

        # Desired: both should detect the ";" in position 0.
        # Bug: checked_tail_first is False (skips index 0).
        assert checked_tail_first is True, (
            "Bug #215: _DISALLOWED_COMMAND_TOKENS check skips inner_argv[0] – "
            "a shell operator in the first token position would not be caught"
        )

    def test_check_is_applied_to_full_argv(self):
        """Verify the actual code path uses inner_argv (not inner_argv[1:])."""
        from app.services.agent_runner import _DISALLOWED_COMMAND_TOKENS
        import re

        # Read the actual source to verify the check scope
        from app.services import agent_runner
        import inspect

        source = inspect.getsource(agent_runner)

        # The pattern we're looking for: the check should use inner_argv, not inner_argv[1:]
        pattern = r"for\s+token\s+in\s+inner_argv\[1:\]"
        match = re.search(pattern, source)

        assert match is None, (
            "Bug #215: security check uses inner_argv[1:] instead of inner_argv, "
            "skipping validation of the first command token"
        )


# ===================================================================
# Bug #216  –  Pagination shows "page 1 of 1" for empty results
# ===================================================================

class TestPaginationEmptyResults:
    """Business scenario: a freshly deployed instance with zero runs.  The
    operator opens the web UI and sees pagination reading "page 1 of 1"
    even though there are no items.  This is confusing.
    """

    @patch("app.routes.web.connect_db")
    @patch("app.routes.web.resolve_runtime_settings")
    @patch("app.routes.web.get_runtime_form_int_field_specs", return_value={})
    def test_total_pages_zero_when_no_results(self, _mock_specs, _mock_rt, mock_db):
        conn = _make_conn()
        # No rows inserted – database is empty.

        mock_db.return_value.__enter__ = lambda s: conn
        mock_db.return_value.__exit__ = lambda s, *a: None

        from app.routes.web import _fetch_runs

        result = _fetch_runs(page=1, page_size=10, query="")

        assert result["total_count"] == 0

        # Desired: total_pages should be 0 when there are no items.
        # Bug:     max(1, ...) forces total_pages to 1.
        assert result["total_pages"] == 0, (
            f"Bug #216: total_pages is {result['total_pages']} "
            f"when total_count is 0 – should be 0"
        )

        # Additionally, has_prev and has_next should both be False
        assert result["has_prev"] is False
        assert result["has_next"] is False
