# Test Speed Cache Reset Design

## Context

While preparing a clean worktree for issue `#164`, running `pytest -q` on `origin/main` exposed two separate problems:

- the suite has pre-existing functional failures on current `origin/main`
- the wall-clock runtime is dominated by a cross-test pollution problem that makes a normally fast end-to-end retry-path test take more than ten minutes when run after other tests

The immediate task here is not to solve all current failing tests. The task is to remove the avoidable runtime blow-up caused by cached environment-derived settings leaking across tests.

## Observed Evidence

- `tests/test_e2e.py::TestE2ERetryPath::test_failure_schedules_retry` runs in about `0.56s` when executed alone
- the same test takes about `11m` when run after other tests in the full suite or after `tests/test_web_settings.py`
- `app/services/feature_flags.py` caches `get_agent_feature_flag_env_overrides()` with `@lru_cache`
- test helpers such as `tests/fixtures/e2e_fixtures.py::setup_e2e_env()` mutate environment variables but do not clear that feature-flag cache
- direct inspection confirms the cached feature flag values remain stale across test setups

This shows the slow test is not intrinsically slow. It is running against stale cached env-derived agent settings from earlier tests.

## Goal

Make the affected tests deterministic and fast by ensuring test fixtures clear the feature-flag env cache whenever they change env vars that feed agent configuration.

## Non-Goals

- Fix all existing functional failures in `origin/main`
- Remove caching from production code entirely
- Redesign test architecture across the whole suite
- Optimize unrelated slow-but-valid test paths

## Recommended Approach

Use targeted cache invalidation inside test setup helpers.

### What changes

- update test setup helpers that mutate agent/runtime env vars to clear `get_agent_feature_flag_env_overrides.cache_clear()` before initializing state
- keep production caching behavior unchanged
- add a regression test proving that a later fixture sees fresh env-derived agent command settings even if a previous test populated the cache with different values

### Why this is the right scope

- it addresses the proven root cause directly
- it avoids changing production request/runtime behavior just to satisfy tests
- it keeps the fix small and easy to reason about
- it is compatible with broader future cleanup if the project later wants a centralized test-state reset helper

## Alternatives Considered

### 1. Remove the feature-flag env cache entirely

Pros:

- eliminates this class of cache pollution completely

Cons:

- changes production behavior and cost profile
- broader than needed for the current root cause

### 2. Build a new global test reset helper for every settings cache

Pros:

- cleaner long-term test API

Cons:

- larger refactor
- unnecessary for proving and fixing the current slowdown

### 3. Targeted cache clear in env-mutating fixtures (recommended)

Pros:

- smallest effective fix
- preserves current production behavior
- easy to cover with a regression test

Cons:

- leaves some future cleanup opportunity for broader fixture consolidation

## Affected Areas

- `tests/fixtures/e2e_fixtures.py`
- any other test helper that mutates `AGENT_SDKS`, `OPENHANDS_*`, `CLAUDE_AGENT_*`, or legacy `CLAUDE_AGENT_SDK_*` env vars and expects fresh resolution
- regression coverage in a test file already exercising env-based agent resolution

## Testing Strategy

Implementation will follow TDD.

### Required regression coverage

- prove that cached feature-flag env overrides do not leak from one setup context into another
- prove the E2E retry-path test remains fast when run after env-mutating settings tests

### Verification commands

- targeted: run the new regression test and the previously interacting tests together
- profiling: run `pytest --durations=25 -q` and confirm the retry-path E2E test no longer dominates runtime abnormally

## Risks

- another cache may also be involved, and this fix may reveal a second layer of pollution
- only fixing one helper may miss another env-mutating fixture elsewhere in the suite

## Mitigation

- verify with a regression test that intentionally primes the cache before switching env
- inspect related helpers for the same env/cache pattern during implementation
- use duration profiling again after the change to confirm the runtime improvement is real

## Expected Outcome

- the retry-path E2E test remains sub-second or low-second regardless of test order
- the suite runtime drops materially because the pathological slow path disappears
- production code behavior remains unchanged outside of test isolation
