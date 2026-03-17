## Why

The current system reliably autofixes feedback on existing pull requests, but it
does not yet fulfill the original product requirement to start from a standalone
GitHub issue, write code, and open a new pull request automatically. That gap
already caused review feedback and verbal expectations to drift apart, so we
need a spec-backed contract before more implementation work lands.

## What Changes

- Introduce OpenSpec as the repository-level workflow for capturing product
  requirements, design decisions, and implementation tasks.
- Define a standalone issue-to-PR autofix flow that is distinct from the
  existing PR-review autofix flow.
- Require issue-backed runs to preserve issue identity, create a working branch
  from a base branch, push code, and open a new pull request automatically.
- Require clear failure states when issue context, branch creation, push, or PR
  creation cannot complete.

## Capabilities

### New Capabilities
- `issue-to-pr-autofix`: Accept a standalone GitHub issue as the task source,
  generate code changes on a fresh branch, and open a new pull request.

### Modified Capabilities

## Impact

- `app/routes/web.py` issue submission and task creation flow
- `app/models.py`, queueing, and run state handling for issue-backed runs
- `app/services/agent_runner.py` workspace, branch, and completion flow
- `app/services/git_ops.py` for first-push and PR creation support
- operator workflow, docs, and review validation process
