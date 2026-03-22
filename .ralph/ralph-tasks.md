# Ralph Tasks

## Issue #112: Manual issue verification must not pollute main run list
- [x] Add dry_run/validate_only parameter to issue submission endpoint
- [x] When dry_run=True, validate the issue URL and context but do NOT create a run in the database
- [x] Return validation result without persisting anything

## Issue #114: Reuse existing active runs for same source link
- [x] Add function to find existing active runs by manual_issue_source_url
- [x] Modify _enqueue_issue_fix to check for existing active runs before creating new ones
- [x] If existing active run found, return its run link instead of creating new run
- [x] If previous run stopped abnormally (failed/cancelled), allow creating fresh run

## Issue #115: CSV upload batch entrypoint for run creation
- [x] Add /api/issues/batch endpoint for CSV upload
- [x] Parse CSV rows and create runs for each
- [x] Return summary of created/reused/rejected runs

## Issue #116: Run terminal state semantics for preexisting check failures
- [x] When preexisting checks fail, ensure status and error_summary are consistent
- [x] For successful runs with preexisting check failures, set error_summary to None
- [x] Update agent_runner.py to handle preexisting check failures correctly

## Tests and Verification
- [x] Add tests for dry_run mode (issue #112)
- [x] Add tests for run reuse (issue #114)
- [x] Add tests for CSV batch endpoint (issue #115)
- [x] Add tests for terminal state semantics (issue #116)
- [x] Run full test suite to verify no regressions