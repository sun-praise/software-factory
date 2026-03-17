## Context

The current implementation is PR-centric end to end. Manual issue submission can
capture issue text, comments, or review fragments, but the run pipeline still
assumes there is already a pull request number, pull request head branch, and a
target PR for final comments. For a standalone issue, that means the system can
ingest context but cannot complete the promised issue -> code -> PR workflow.

OpenSpec is being introduced now so this requirement is tracked as a durable
contract instead of a conversational assumption.

## Goals / Non-Goals

**Goals:**
- Make OpenSpec the repository workflow for recording product intent and missed
  review requirements.
- Add an explicit issue-backed run mode instead of overloading `pr_number` with
  issue numbers.
- Allow a standalone GitHub issue to create a working branch from a base branch,
  run the autofix pipeline, push code, and open a pull request automatically.
- Preserve traceability between the original issue, the created branch, the run,
  and the created pull request.
- Provide clear failure reporting when GitHub context, branch creation, push, or
  PR creation fails.

**Non-Goals:**
- Replacing the existing PR-review autofix flow.
- Building a generic issue triage or project planning system.
- Automatically resolving ambiguous product scope without explicit issue text.

## Decisions

### 1. Keep issue-backed runs distinct from PR-backed runs

Issue-backed runs must carry their own source identity instead of reusing
`pr_number` as a placeholder. The run model should be able to represent:

- source kind: `pull_request` or `issue`
- original `issue_number` when the source is an issue
- created `pr_number` only after a new PR exists
- base branch and working branch used for the implementation

This avoids quota, idempotency, locking, and UI confusion caused by mixing issue
 numbers and PR numbers in the same field.

### 2. Resolve issue context before the agent starts

The service layer should fetch issue body, issue comments, or linked review
content before the run is queued. The agent should receive structured issue
requirements, not just a raw GitHub URL.

### 3. Create a branch from a base branch for issue-backed runs

Issue-backed runs should clone the repository from the default branch (or an
explicitly configured base branch), create a dedicated working branch, and then
run the existing agent/check pipeline in that branch.

Suggested branch naming format:

- `autofix/issue-<issue-number>-<slug>`

### 4. Create the pull request after a successful push

For issue-backed runs, PR creation should happen only after the code is pushed
successfully. The system should create the PR with:

- a deterministic title derived from the issue title
- a body that links back to the issue and summarizes the autofix run
- the created PR number persisted back to the run record

### 5. Report back to the original issue

When the run succeeds, the system should comment on the original issue with the
created PR URL and run summary. When the run fails before PR creation, it should
comment with a precise failure reason when safe to do so.

## Risks / Trade-offs

- Introducing a new issue-backed run mode requires schema and queue changes,
  which increases migration complexity.
- GitHub authentication becomes more critical because issue intake, branch push,
  and PR creation all depend on valid credentials.
- Some issues will still be underspecified; the system must fail clearly instead
  of guessing product scope.
- Separate issue and PR flows increase branching in the runner, so tests must
  cover both paths explicitly.
