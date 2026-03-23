## 1. OpenSpec Adoption

- [ ] 1.1 Keep the OpenSpec repository scaffolding and usage notes in version control.
- [ ] 1.2 Use this change as the source of truth for the missing standalone issue -> PR requirement.

## 2. Issue-Backed Run Model

- [ ] 2.1 Add explicit issue-backed run fields instead of overloading `pr_number` with issue IDs.
- [ ] 2.2 Update queueing, idempotency, and state handling so issue-backed runs can exist before a PR is created.

## 3. Issue -> Branch -> PR Execution

- [ ] 3.1 Resolve standalone issue context before queueing the run.
- [ ] 3.2 Create a working branch from a base branch for issue-backed runs.
- [ ] 3.3 Push the working branch and create a new pull request automatically on success.
- [ ] 3.4 Persist the created PR metadata and comment back on the source issue.

## 4. Validation

- [ ] 4.1 Add automated coverage for standalone issue intake, branch creation, push, and PR creation.
- [ ] 4.2 Add failure-path coverage for missing issue context, branch creation errors, and PR creation errors.
