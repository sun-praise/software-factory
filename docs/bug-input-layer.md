# Bug Input Compatibility Layer

## Overview

The bug input compatibility layer decouples the core autofix pipeline from GitHub PR semantics. It provides a unified abstraction for ingesting bug/fix tasks from multiple sources so that downstream components (runner, planner, reviewer) remain agnostic to the origin of the task.

## Architecture

```text
+------------------+   +------------------+   +------------------+
|  Plain Text Bug  |   |  GitHub Issue    |   |  Structured Bug  |
|  Description     |   |  / PR Link       |   |  (files/errors)  |
+--------+---------+   +--------+---------+   +--------+---------+
         |                      |                      |
         v                      v                      v
+--------------------------------------------------------------+
|                    BugInputProvider Protocol                   |
|  (PlaintextBugProvider, GitHubPRBugProvider, ...)             |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                   Normalized Review Dict                       |
|  (same shape consumed by runner, planner, reviewer)           |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                    Task Queue (autofix_runs)                  |
+--------------------------------------------------------------+
```

## Supported Providers

| Provider | Kind | Description |
|----------|------|-------------|
| `PlaintextBugProvider` | `plaintext` | Free-form text bug description |
| `GitHubPRBugProvider` | `github_pr` | GitHub PR link or reference |
| `GitHubIssueBugProvider` | `github_issue` | GitHub issue link or reference |
| `StructuredBugProvider` | `structured` | Structured input with files, errors, stack traces |
| `LogStacktraceBugProvider` | `log_stacktrace` | Raw log output or error traces |

## API Endpoints

### Submit a bug

```
POST /api/bugs
```

Request body:

```json
{
  "provider": "plaintext",
  "title": "Fix login crash on mobile",
  "description": "App crashes when user taps login button",
  "repo": "acme/mobile-app",
  "source_url": "https://jira.acme.com/BUG-42",
  "context": {
    "files": ["src/auth.py"],
    "error_messages": ["NullPointerError"],
    "stack_traces": [],
    "logs": [],
    "metadata": {}
  },
  "dry_run": false
}
```

Response:

```json
{
  "ok": true,
  "message": "Bug submission accepted.",
  "repo": "acme/mobile-app",
  "queue_status": "queued",
  "queued_run_id": 42,
  "idempotency_key": "abc123...",
  "remaining_quota": 3,
  "head_sha": null
}
```

### List providers

```
GET /api/bugs/providers
```

Returns all registered bug input providers:

```json
{
  "ok": true,
  "providers": [
    {"kind": "plaintext"},
    {"kind": "github_pr"},
    {"kind": "github_issue"},
    {"kind": "structured"},
    {"kind": "log_stacktrace"}
  ]
}
```

## Extending with a New Provider

To add support for a new bug input source (e.g., Jira, Sentry, GitLab):

### 1. Add a new `BugProviderKind` enum value

In `app/schemas/bug_input.py`, add a new member to `BugProviderKind`:

```python
class BugProviderKind(str, Enum):
    # ... existing values ...
    JIRA = "jira"
```

### 2. Implement the provider class

Create a class in `app/services/bug_input.py` (or a new module):

```python
from app.schemas.bug_input import BugInput, BugProviderKind
from app.services.bug_input import BugInputProvider, register_provider

class JiraBugProvider:
    provider_kind = "jira"

    def supports(self, bug_input: BugInput) -> bool:
        return bug_input.provider == BugProviderKind.JIRA

    def to_normalized_review(
        self, bug_input: BugInput, *, repo: str, synthetic_pr_number: int
    ) -> dict[str, Any]:
        # Translate Jira-specific fields into the canonical normalized review dict
        title = bug_input.title
        description = bug_input.description
        # ... Jira-specific parsing logic ...

        return {
            "repo": repo,
            "pr_number": synthetic_pr_number,
            "head_sha": None,
            "must_fix": [{
                "source": "bug_input_jira",
                "path": None,
                "line": None,
                "text": f"Title: {title}\n\n{description}",
                "severity": "P1",
            }],
            "should_fix": [],
            "ignore": [],
            "summary": "1 blocking issues, 0 suggestions, 0 ignored",
            "project_type": "python",
            "source_kind": "bug_input",
            "bug_provider": self.provider_kind,
            "bug_title": title,
            "bug_source_url": bug_input.source_url,
        }
```

### 3. Register the provider

At module level or in an init function:

```python
register_provider(JiraBugProvider())
```

### 4. (Optional) Add tests

See `tests/test_bug_input.py` for examples of testing providers.

## How It Works

1. A `BugSubmissionRequest` is received via the `/api/bugs` endpoint.
2. The request is converted to a `BugInput` model.
3. `resolve_provider()` finds the matching `BugInputProvider` based on the `provider` field.
4. The provider's `to_normalized_review()` method converts the `BugInput` into the canonical normalized review dict.
5. The normalized review is enqueued into the task queue (`autofix_runs` table) with `trigger_source = "bug_input"`.
6. The existing agent worker processes the run identically to PR-sourced runs.

## Relationship to Existing PR Flow

The existing GitHub webhook and issue submission flows remain unchanged. The bug input layer provides an alternative entry point that does not require a GitHub PR number or URL. Both flows produce the same normalized review dict consumed by the core pipeline.

| Entry Point | Endpoint | Trigger Source |
|-------------|----------|----------------|
| GitHub Webhook | `POST /github/webhook` | `github_webhook` |
| Issue Submission | `POST /api/issues` | `manual_issue` |
| **Bug Input (new)** | `POST /api/bugs` | `bug_input` |
