# Issue 161 Runtime Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add audited DB-backed runtime config inspection and ownership documentation so issue `#161` is complete end to end.

**Architecture:** Extend the existing `runtime_settings` service instead of replacing it. Keep runtime resolution at `env/.env > DB > code default`, add a registry plus inspect metadata, persist config-change audit rows in SQLite, then expose the effective non-secret config through the web layer and docs.

**Tech Stack:** Python, FastAPI, SQLite, Jinja2, pytest

---

## File Map

- Modify: `app/services/runtime_settings.py` - add registry metadata, inspect helpers, audited save flow
- Modify: `app/db.py` - add migration for config audit table and index
- Modify: `app/models.py` - declare audit table schema and index in `SCHEMA_SQL`
- Modify: `app/routes/web.py` - add `GET /api/settings/runtime`, include inspect data in `/settings`, write audit metadata on save
- Modify: `app/templates/settings.html` - render read-only effective runtime config section
- Modify: `tests/test_runtime_settings.py` - cover registry, inspect source resolution, audit writes, env-only protections
- Modify: `tests/test_web_settings.py` - cover runtime inspect API, settings-page render, and audit log writes
- Create: `docs/runtime-config.md` - ownership, security boundary, rollout, local/dev/prod migration guidance
- Modify: `docs/local-runtime.md` - cross-link the runtime config ownership doc while preserving `DB_PATH` rules

### Task 1: Add runtime config registry and audit persistence

**Files:**
- Modify: `app/services/runtime_settings.py`
- Modify: `app/db.py`
- Modify: `app/models.py`
- Test: `tests/test_runtime_settings.py`

- [ ] **Step 1: Write the failing registry and inspect tests**

```python
def test_describe_runtime_settings_reports_sources_and_ownership(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_MAX_RETRY_ATTEMPTS_KEY, "7"),
    )
    conn.commit()
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "4")

    described = describe_runtime_settings(conn)
    described_by_key = {item.key: item for item in described}

    assert described_by_key[RUNTIME_MAX_AUTOFIX_PER_PR_KEY].source == "env"
    assert described_by_key[RUNTIME_MAX_AUTOFIX_PER_PR_KEY].ownership == "db"
    assert described_by_key[RUNTIME_MAX_RETRY_ATTEMPTS_KEY].source == "db"
    assert described_by_key[RUNTIME_DB_PATH_KEY].ownership == "env_only"


def test_save_runtime_settings_records_audit_rows_only_for_changed_values(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_settings(
        conn,
        github_webhook_debounce_seconds=45,
        max_autofix_per_pr=7,
        max_concurrent_runs=5,
        stale_run_timeout_seconds=321,
        pr_lock_ttl_seconds=654,
        max_retry_attempts=4,
        retry_backoff_base_seconds=12,
        retry_backoff_max_seconds=900,
        bot_logins=["ci-helper"],
        noise_comment_patterns=[r"^/retest\\b"],
        managed_repo_prefixes=["acme/"],
        autofix_comment_author="autofix-bot",
        changed_by="settings_ui",
        change_source="web.settings",
    )
    save_runtime_settings(
        conn,
        github_webhook_debounce_seconds=45,
        max_autofix_per_pr=7,
        max_concurrent_runs=5,
        stale_run_timeout_seconds=321,
        pr_lock_ttl_seconds=654,
        max_retry_attempts=4,
        retry_backoff_base_seconds=12,
        retry_backoff_max_seconds=900,
        bot_logins=["ci-helper"],
        noise_comment_patterns=[r"^/retest\\b"],
        managed_repo_prefixes=["acme/"],
        autofix_comment_author="autofix-bot",
        changed_by="settings_ui",
        change_source="web.settings",
    )

    rows = conn.execute(
        "SELECT key, old_value, new_value, changed_by, change_source FROM app_config_audit_log ORDER BY id"
    ).fetchall()

    assert len(rows) == 12
    assert rows[0]["changed_by"] == "settings_ui"
    assert rows[0]["change_source"] == "web.settings"
```

- [ ] **Step 2: Run the targeted test file to verify it fails**

Run: `pytest tests/test_runtime_settings.py -q`
Expected: FAIL with missing `describe_runtime_settings`, missing `RUNTIME_DB_PATH_KEY`, missing audit table or missing new save parameters.

- [ ] **Step 3: Add the schema and service implementation**

```python
APP_CONFIG_AUDIT_LOG_TABLE = TableDef(
    name="app_config_audit_log",
    create_sql="""
CREATE TABLE IF NOT EXISTS app_config_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT NOT NULL,
    change_source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""".strip(),
)


@dataclass(frozen=True)
class RuntimeSettingDescription:
    key: str
    label: str
    ownership: str
    sensitive: bool
    env_var: str
    effective: Any
    source: str
    updated_at: str | None = None


def save_runtime_settings(..., changed_by: str, change_source: str) -> None:
    existing_rows = load_runtime_setting_rows(conn)
    changed_rows = [
        (key, existing_rows.get(key), value)
        for key, value in values
        if existing_rows.get(key) != value
    ]
    conn.executemany(..., values)
    conn.executemany(
        """
        INSERT INTO app_config_audit_log (key, old_value, new_value, changed_by, change_source)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (key, old_value, new_value, changed_by, change_source)
            for key, old_value, new_value in changed_rows
        ],
    )
    conn.commit()
```

- [ ] **Step 4: Run the targeted test file to verify it passes**

Run: `pytest tests/test_runtime_settings.py -q`
Expected: PASS with all runtime-settings assertions green.

- [ ] **Step 5: Commit Task 1**

```bash
git add app/models.py app/db.py app/services/runtime_settings.py tests/test_runtime_settings.py
git commit -m "feat: audit runtime setting changes"
```

### Task 2: Expose effective runtime config through web routes and settings UI

**Files:**
- Modify: `app/routes/web.py`
- Modify: `app/templates/settings.html`
- Modify: `tests/test_web_settings.py`

- [ ] **Step 1: Write the failing route tests**

```python
def test_runtime_settings_api_reports_effective_values_and_sources(tmp_path: Path) -> None:
    _setup_db(tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/settings/runtime")

    assert response.status_code == 200
    payload = response.json()
    max_retry = next(
        item for item in payload["settings"] if item["key"] == "runtime.max_retry_attempts"
    )
    db_path = next(
        item for item in payload["env_only"] if item["env_var"] == "DB_PATH"
    )

    assert max_retry["ownership"] == "db"
    assert max_retry["source"] == "default"
    assert db_path["ownership"] == "env_only"
    assert db_path["source"] == "env"


def test_save_settings_writes_runtime_audit_log(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data={
                "agent_claude_agent_enabled": "on",
                "github_webhook_debounce_seconds": "45",
                "max_autofix_per_pr": "7",
                "max_concurrent_runs": "5",
                "stale_run_timeout_seconds": "321",
                "pr_lock_ttl_seconds": "654",
                "max_retry_attempts": "4",
                "retry_backoff_base_seconds": "12",
                "retry_backoff_max_seconds": "900",
                "bot_logins_text": "ci-helper",
                "noise_comment_patterns_text": "^/retest\\b",
                "managed_repo_prefixes_text": "acme/",
                "autofix_comment_author": "autofix-bot",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT key, changed_by, change_source FROM app_config_audit_log ORDER BY id"
        ).fetchall()

    assert rows
    assert rows[0]["changed_by"] == "settings_ui"
    assert rows[0]["change_source"] == "web.settings"
```

- [ ] **Step 2: Run the targeted route test file to verify it fails**

Run: `pytest tests/test_web_settings.py -q`
Expected: FAIL because `/api/settings/runtime` does not exist and `POST /settings` does not yet pass audit metadata through the save flow.

- [ ] **Step 3: Implement the route and template changes**

```python
@router.get("/api/settings/runtime")
async def runtime_settings_api() -> JSONResponse:
    with connect_db() as conn:
        described = describe_runtime_settings(conn)
    return JSONResponse(
        {
            "settings": [item for item in _serialize_runtime_settings(described) if item["ownership"] == "db"],
            "env_only": [item for item in _serialize_runtime_settings(described) if item["ownership"] == "env_only"],
        }
    )


save_runtime_settings(
    conn,
    ...,
    changed_by="settings_ui",
    change_source="web.settings",
)
```

```html
<fieldset>
  <legend>Effective Runtime Config</legend>
  <table>
    <thead>
      <tr>
        <th>Setting</th>
        <th>Effective value</th>
        <th>Source</th>
      </tr>
    </thead>
    <tbody>
      {% for item in runtime_settings_descriptions %}
      <tr>
        <td>{{ item.label }}</td>
        <td><code>{{ item.display_value }}</code></td>
        <td>{{ item.source }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</fieldset>
```

- [ ] **Step 4: Run the targeted route test file to verify it passes**

Run: `pytest tests/test_web_settings.py -q`
Expected: PASS with settings page, inspect API, and audit assertions all green.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/routes/web.py app/templates/settings.html tests/test_web_settings.py
git commit -m "feat: expose effective runtime config"
```

### Task 3: Document ownership and rollout rules for operators

**Files:**
- Create: `docs/runtime-config.md`
- Modify: `docs/local-runtime.md`
- Test: manual doc verification against issue `#161`

- [ ] **Step 1: Write the documentation files**

```md
# Runtime Configuration Ownership

## DB-backed mutable settings

- `runtime.github_webhook_debounce_seconds`
- `runtime.max_autofix_per_pr`
- `runtime.max_concurrent_runs`
- `runtime.stale_run_timeout_seconds`
- `runtime.pr_lock_ttl_seconds`
- `runtime.max_retry_attempts`
- `runtime.retry_backoff_base_seconds`
- `runtime.retry_backoff_max_seconds`
- `runtime.bot_logins`
- `runtime.noise_comment_patterns`
- `runtime.managed_repo_prefixes`
- `runtime.autofix_comment_author`

## Env-only settings

- `DB_PATH`
- `GITHUB_WEBHOOK_SECRET`
- provider API keys and tokens
- host / port and similar bootstrap values

## Resolution order

1. env or `.env`
2. DB
3. code default
```

- [ ] **Step 2: Verify the docs cover local, dev, and prod rollout**

Run: `python3 - <<'PY'
from pathlib import Path
for path in [Path('docs/runtime-config.md'), Path('docs/local-runtime.md')]:
    text = path.read_text(encoding='utf-8')
    print(path.name, 'DB_PATH' in text, 'env' in text.lower(), 'prod' in text.lower())
PY`
Expected: output shows both files mention `DB_PATH`, env handling, and production/dev guidance.

- [ ] **Step 3: Commit Task 3**

```bash
git add docs/runtime-config.md docs/local-runtime.md
git commit -m "docs: describe runtime config ownership"
```

### Task 4: Full verification and PR preparation

**Files:**
- Modify: any files touched during review follow-ups

- [ ] **Step 1: Run the focused regression tests**

Run: `pytest tests/test_runtime_settings.py tests/test_web_settings.py -q`
Expected: PASS with all new runtime config and web settings tests green.

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`
Expected: PASS with zero failures.

- [ ] **Step 3: Review the diff against `main`**

Run: `git diff --stat main...HEAD && git diff main...HEAD`
Expected: diff only includes runtime config audit, inspect, and documentation changes required by issue `#161`.

- [ ] **Step 4: Commit any final review fixes**

```bash
git add app/services/runtime_settings.py app/routes/web.py app/templates/settings.html app/db.py app/models.py tests/test_runtime_settings.py tests/test_web_settings.py docs/runtime-config.md docs/local-runtime.md
git commit -m "feat: complete issue 161 runtime config rollout"
```

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin feat/issue-161-config-audit
gh pr create --title "feat: complete issue 161 runtime config rollout" --body "$(cat <<'EOF'
## Summary
- add audited runtime setting persistence and effective config inspection for issue #161
- expose effective non-secret runtime config in the settings UI and API while keeping env-only boundaries intact
- document DB-vs-env ownership and rollout guidance for local, dev, and prod

## Testing
- pytest tests/test_runtime_settings.py tests/test_web_settings.py -q
- pytest -q
EOF
)"
```

## Self-Review

- Spec coverage: registry/ownership, inspect API, audit persistence, UI inspection, and rollout docs are all mapped to a task.
- Placeholder scan: no `TODO`, `TBD`, or vague “handle later” steps remain.
- Type consistency: plan consistently uses `describe_runtime_settings`, `RuntimeSettingDescription`, `app_config_audit_log`, `changed_by`, and `change_source` across service and route tasks.
