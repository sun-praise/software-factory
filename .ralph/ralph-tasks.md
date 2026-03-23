# Ralph Tasks

## Completed Issue Work
- [x] Add dry_run/validate_only parameter to issue submission endpoint
- [x] When dry_run=True, validate the issue URL and context but do NOT create a run in the database
- [x] Return validation result without persisting anything
- [x] Add function to find existing active runs by manual_issue_source_url
- [x] Modify _enqueue_issue_fix to check for existing active runs before creating new ones
- [x] If existing active run found, return its run link instead of creating new run
- [x] If previous run stopped abnormally (failed/cancelled), allow creating fresh run
- [x] Add /api/issues/batch endpoint for CSV upload
- [x] Parse CSV rows and create runs for each
- [x] Return summary of created/reused/rejected runs
- [x] When preexisting checks fail, ensure status and error_summary are consistent
- [x] For successful runs with preexisting check failures, set error_summary to None
- [x] Update agent_runner.py to handle preexisting check failures correctly
- [x] Add tests for dry_run mode (issue #112)
- [x] Add tests for run reuse (issue #114)
- [x] Add tests for CSV batch endpoint (issue #115)
- [x] Add tests for terminal state semantics (issue #116)
- [x] Run full test suite to verify no regressions

## PR Acceptance Gate For PR #117
- [x] Inspect PR #117 current mergeability, CI status, and AI review comments using GitHub CLI
- [x] Triage each material issue from AI review comment https://github.com/sun-praise/software-factory/pull/117#issuecomment-4107225038
  - Fixed: LIKE wildcards escaping, import positions, bool parsing helper
  - Documented acceptable: race condition (low-risk), style suggestions
- [ ] Implement fixes or write concrete evidence for any item intentionally left unchanged
- [ ] Run the relevant local validation for any new code changes
- [ ] Commit and push any follow-up changes to the same PR branch
- [ ] Wait for CI and AI review to settle after the latest push
- [ ] Re-check PR #117 and confirm: mergeable, CI passing, and AI review feedback positive overall or every negative item explicitly documented

## Stop Rule
- [ ] Only output <promise>COMPLETE</promise> after every PR Acceptance Gate item is marked complete
