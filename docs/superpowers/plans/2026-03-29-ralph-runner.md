# Ralph Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ralph` as a first-class runner mode with settings, feature flags, execution fallback, tests, and docs.

**Architecture:** Extend the existing agent-mode pipeline instead of introducing a separate Ralph subsystem. Reuse the shared feature-flag model, `/settings` form handling, and generic subprocess runner so Ralph participates in the same fallback and logging flow as OpenHands and Claude Agent SDK.

**Tech Stack:** FastAPI, Jinja templates, SQLite-backed feature flags, pytest

---

### Task 1: Lock the behavior with failing tests

**Files:**
- Modify: `tests/test_feature_flags.py`
- Modify: `tests/test_web_settings.py`
- Modify: `tests/test_agent_runner.py`

- [ ] Step 1: Add failing feature-flag expectations for `ralph`
- [ ] Step 2: Run targeted feature-flag tests and confirm failure
- [ ] Step 3: Add failing settings page/save expectations for `ralph`
- [ ] Step 4: Run targeted web settings tests and confirm failure
- [ ] Step 5: Add failing runner execution tests for `ralph`
- [ ] Step 6: Run targeted runner tests and confirm failure

### Task 2: Implement Ralph feature flags and settings

**Files:**
- Modify: `app/services/feature_flags.py`
- Modify: `app/routes/web.py`
- Modify: `app/templates/settings.html`

- [ ] Step 1: Extend the feature-flag data model and normalization helpers for Ralph
- [ ] Step 2: Persist and expose Ralph fields in settings context
- [ ] Step 3: Parse and save Ralph settings from `/settings`
- [ ] Step 4: Re-run feature-flag and settings tests until green

### Task 3: Implement Ralph runner execution

**Files:**
- Modify: `app/services/agent_runner.py`
- Modify: `tests/test_agent_runner.py`

- [ ] Step 1: Add Ralph mode constants, failure code, and command argv builder
- [ ] Step 2: Add `_run_ralph_agent()` via the shared subprocess path
- [ ] Step 3: Add Ralph to `_execute_agent_sdks()` fallback ordering
- [ ] Step 4: Re-run targeted runner tests until green

### Task 4: Update docs and validate end state

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] Step 1: Document Ralph as a supported runner mode and installation prerequisite
- [ ] Step 2: Run the full relevant pytest subset
- [ ] Step 3: Inspect diff for scope correctness
- [ ] Step 4: Commit, push, and open a draft PR
