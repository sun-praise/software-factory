# OpenSpec Workflow

This repository uses OpenSpec to keep product requirements, implementation scope,
and review follow-ups in sync.

Core workflow:

1. Create a change under `openspec/changes/<change-id>/`
2. Write `proposal.md` to capture the problem and acceptance scope
3. Write `specs/` to define testable requirements
4. Write `design.md` and `tasks.md` before implementation
5. Implement from a dedicated git worktree and keep tasks current during review

Recommended commands:

- `openspec list`
- `openspec show <change-id>`
- `openspec status --change <change-id>`
- `openspec validate <change-id>`

For this project, OpenSpec is the source of truth for feature expectations that
must survive AI-assisted implementation, review, and follow-up fixes.
