## 1. OpenSpec Adoption

- [x] 1.1 Keep the OpenSpec repository scaffolding and usage notes in version control.
- [x] 1.2 Use this change as the source of truth for the missing standalone issue -> PR requirement.

## 2. Issue-Backed Run Model

- [x] 2.1 Add explicit issue-backed run fields instead of overloading `pr_number` with issue IDs.
- [x] 2.2 Update queueing, idempotency, and state handling so issue-backed runs can exist before a PR is created.

## 3. Issue -> Branch -> PR Execution

- [x] 3.1 Resolve standalone issue context before queueing the run.
- [x] 3.2 Create a working branch from a base branch for issue-backed runs.
- [x] 3.3 Push the working branch and create a new pull request automatically on success.
- [x] 3.4 Persist the created PR metadata and comment back on the source issue.

## 4. Validation

- [x] 4.1 Add automated coverage for standalone issue intake, branch creation, push, and PR creation.
- [x] 4.2 Add failure-path coverage for missing issue context, branch creation errors, and PR creation errors.
