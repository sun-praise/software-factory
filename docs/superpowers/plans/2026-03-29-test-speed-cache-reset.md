# Test Speed Cache Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the cross-test cache pollution that makes the retry-path E2E test explode from sub-second runtime to multi-minute runtime.

**Architecture:** Keep production caching intact and fix test isolation at the fixture boundary. Clear the cached feature-flag env overrides in env-mutating test setup helpers, then add a regression test that proves later env changes are visible after earlier tests have already primed the cache.

**Tech Stack:** Python, pytest, FastAPI, sqlite3, functools.lru_cache

---

## File Map

- Modify: `tests/fixtures/e2e_fixtures.py` - clear cached feature-flag env overrides when E2E test env is rewritten
- Modify: `tests/test_web_settings.py` - add regression coverage for feature-flag cache invalidation across env-changing test setups
- Verify: `tests/test_e2e.py` - confirm retry-path test runtime no longer blows up after env-mutating tests

### Task 1: Add a failing regression test for cache pollution

**Files:**
- Modify: `tests/test_web_settings.py`
- Test: `tests/test_web_settings.py`

- [ ] **Step 1: Write the failing regression test**

```python
def test_setup_db_then_e2e_env_sees_fresh_agent_commands(tmp_path: Path) -> None:
    from tests.fixtures.e2e_fixtures import setup_e2e_env

    _setup_db(tmp_path / "web")
    setup_e2e_env(tmp_path / "e2e")

    overrides = get_agent_feature_flag_env_overrides()

    assert overrides.openhands_command == "true"
    assert overrides.claude_agent_command == "true"
```

- [ ] **Step 2: Run the targeted regression test and watch it fail**

Run: `pytest tests/test_web_settings.py::test_setup_db_then_e2e_env_sees_fresh_agent_commands -q`
Expected: FAIL because the cached env overrides still show the stale commands from the earlier setup.

- [ ] **Step 3: Implement the minimal test-fixture fix**

```python
def setup_e2e_env(tmp_path: Path, secret: str = "test-secret") -> Path:
    get_settings.cache_clear()
    get_agent_feature_flag_env_overrides.cache_clear()
    _get_debounce_backend.cache_clear()
    ...
```

- [ ] **Step 4: Run the targeted regression test again**

Run: `pytest tests/test_web_settings.py::test_setup_db_then_e2e_env_sees_fresh_agent_commands -q`
Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add tests/fixtures/e2e_fixtures.py tests/test_web_settings.py
git commit -m "fix: clear cached feature flag env overrides in tests"
```

### Task 2: Verify the pathological slow interaction is gone

**Files:**
- Verify only

- [ ] **Step 1: Run the previously interacting tests together**

Run: `pytest tests/test_web_settings.py tests/test_e2e.py::TestE2ERetryPath::test_failure_schedules_retry -q`
Expected: no cache-pollution slowdown; the E2E retry-path test completes in low seconds instead of many minutes.

- [ ] **Step 2: Run duration profiling for confirmation**

Run: `pytest tests/test_web_settings.py tests/test_e2e.py --durations=10 -q`
Expected: `TestE2ERetryPath::test_failure_schedules_retry` no longer dominates runtime abnormally.

- [ ] **Step 3: Capture the current full-suite status without claiming unrelated fixes**

Run: `pytest -q`
Expected: report current suite status honestly. Existing unrelated failures may remain, but the pathological slow path should be removed.

- [ ] **Step 4: Commit any final test-only cleanup if needed**

```bash
git add tests/fixtures/e2e_fixtures.py tests/test_web_settings.py
git commit -m "test: harden env cache isolation"
```

## Self-Review

- Spec coverage: targeted cache invalidation and regression proof are both represented.
- Placeholder scan: no TODO/TBD steps remain.
- Type consistency: the plan consistently uses `get_agent_feature_flag_env_overrides.cache_clear()` and `setup_e2e_env()` as the isolation point.
