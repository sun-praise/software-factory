# Issue 161 Runtime Config Completion Design

## Context

Issue `#161` asks for mutable, non-secret application configuration to move from process environment into a shared persisted source while keeping bootstrap and secret material outside the database. The repository already partially implements this through `app_feature_flags`, `app/services/runtime_settings.py`, and the `/settings` page. This design completes the missing pieces needed to satisfy the issue acceptance criteria.

The current gap is not basic DB-backed storage. The missing gap is operational completeness:

- operators cannot inspect a structured effective runtime config through an API
- configuration writes are not audit-trailed as historical events
- ownership boundaries between DB-backed settings and env-only settings are not explicitly modeled or documented
- rollout guidance is implicit rather than documented for local, dev, and prod

## Goals

- Keep mutable, non-secret runtime settings in the database as the shared persisted source for web and worker processes.
- Preserve `env/.env > DB > code default` resolution for supported runtime settings.
- Add durable audit history for DB-backed config writes.
- Expose a non-secret inspect surface for effective runtime configuration and its source.
- Document which settings belong in DB versus env, and how to roll the model out safely.

## Non-Goals

- Moving secrets, tokens, webhook secrets, or API keys into SQLite.
- Moving bootstrap or deployment-specific settings like `DB_PATH`, host, or port into DB.
- Replacing the existing `app_feature_flags` storage model with typed tables in this iteration.
- Adding live push-based config subscriptions for workers.

## Ownership Rules

Configuration is split into two ownership classes.

### DB-Backed Mutable Runtime Settings

These stay inspectable and writable through the product because they are non-secret and safe to change at runtime:

- GitHub webhook debounce window
- max autofix per PR
- max concurrent runs
- stale run timeout
- PR lock TTL
- retry attempt and backoff knobs
- bot login filters
- noise comment regex filters
- managed repo prefixes
- autofix comment author

### Env-Only Settings

These remain outside the database because they are secret, bootstrap, or deployment-specific:

- `DB_PATH`
- API keys and auth tokens
- GitHub webhook secret
- host / port
- deployment-specific external base URLs when they vary by environment rather than by product behavior

## Proposed Architecture

### 1. Runtime Setting Registry

`app/services/runtime_settings.py` will gain an explicit registry describing each supported setting. Each registry entry defines:

- DB key
- env var name
- value type
- default value
- ownership class: `db` or `env_only`
- sensitive flag
- optional validation metadata

This registry becomes the single place that explains what the system supports and how each setting is resolved.

### 2. Effective Resolution Metadata

The existing runtime resolver will continue to return the concrete settings used by web and worker code, but it will also support a structured inspect path that includes per-setting metadata:

- effective value
- source: `env`, `db`, or `default`
- ownership class
- sensitive flag
- env var name
- DB `updated_at` timestamp when applicable

This keeps behavior unchanged for callers that only need typed runtime settings while enabling API and UI inspection without duplicating resolution logic.

### 3. Audit Log Table

Add a new SQLite table named `app_config_audit_log` with columns:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `key TEXT NOT NULL`
- `old_value TEXT`
- `new_value TEXT`
- `changed_by TEXT NOT NULL`
- `change_source TEXT NOT NULL`
- `created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP`

Indexes:

- index on `(key, created_at DESC)` for per-setting history lookup

The initial writer identity will be system-level rather than user-level because the current product has no operator auth context. The first implementation will use a fixed actor like `settings_ui` with source `web.settings`. This preserves auditability now and leaves room to swap in real operator identity later.

### 4. Save Flow

`POST /settings` will continue to be the primary write path. During save:

1. parse and validate submitted DB-backed settings
2. load current stored values for all runtime setting keys
3. compute changed keys only
4. write updated values to `app_feature_flags`
5. append one audit row per changed key
6. commit in a single transaction

If a submitted value normalizes to the same serialized DB value already stored, no audit row is written.

## Product Surfaces

### Settings Page

Keep `/settings` as the only write UI. Extend it with a read-only effective config section showing non-secret runtime settings, their effective value, and source. This allows operators to confirm whether a value is currently coming from env, DB, or code default.

The page will not offer write controls for env-only settings. They may be documented or listed in inspection output, but they remain non-editable from the product.

### Inspect API

Add `GET /api/settings/runtime`.

Response shape:

```json
{
  "settings": [
    {
      "key": "runtime.max_retry_attempts",
      "label": "Max retry attempts",
      "ownership": "db",
      "sensitive": false,
      "env_var": "MAX_RETRY_ATTEMPTS",
      "effective": 4,
      "source": "db",
      "updated_at": "2026-03-28 08:00:00"
    }
  ],
  "env_only": [
    {
      "key": "bootstrap.db_path",
      "ownership": "env_only",
      "sensitive": false,
      "env_var": "DB_PATH",
      "effective": "/path/to/db",
      "source": "env"
    }
  ]
}
```

Rules:

- secrets are omitted entirely from the response
- env-only non-secret settings may appear for inspection, but never as DB-writable entries
- DB-backed settings always report their winning source and current `updated_at` if a DB value exists

### Audit API and UI Scope

This issue only requires durable audit writes, not a new history browser. The first PR will persist audit history and cover it in tests. A future change can expose audit history in the UI or API if needed.

## Resolution Order

The effective precedence remains:

1. env or `.env` override
2. DB-backed stored value
3. code default

Why this stays unchanged:

- preserves emergency override capability
- keeps compatibility with existing deployments
- enables gradual migration from env-backed mutable settings into DB-backed runtime settings

The inspect API must explicitly show when env wins over DB so operators can detect why a saved DB value is not currently effective.

## Web and Worker Consistency

Both web and worker already resolve runtime settings from the same SQLite database plus env overrides. This design keeps that behavior by extending the shared resolver rather than introducing separate config readers.

Worker refresh semantics remain unchanged in this issue:

- values are resolved when current code paths call into the runtime resolver
- no new polling or subscription mechanism is added

This is acceptable because issue `#161` only requires shared persisted config, not hot reload guarantees.

## Validation and Error Handling

- numeric DB-backed fields continue using the current positive or non-negative validation rules
- invalid stored DB values continue to fall back to defaults and emit warnings
- env-only keys must be rejected from DB save helpers if accidentally passed through future callers
- audit writes happen only after values are successfully normalized
- save operations are transactional so DB values and audit rows cannot diverge

## Migration and Rollout

This is a no-break backfill migration.

### Existing Deployments

- existing env-based mutable settings continue to work immediately
- DB values can be introduced gradually
- if both env and DB are present, env continues to win

### Local / Dev / Prod Guidance

- local: keep `DB_PATH` in env exactly as documented in `docs/local-runtime.md`
- dev and prod: move mutable non-secret knobs into DB over time through `/settings`
- all environments: keep secrets and bootstrap settings in env or secret manager

### Backfill Strategy

No automatic import job is required. Operators can copy current mutable env values into DB through the settings page, then remove redundant env vars when ready. The inspect API helps validate the final winner for each setting.

## Testing Strategy

Implementation will follow TDD.

### Service Tests

- registry reports correct ownership and metadata
- inspect resolution reports correct source for env, DB, and default cases
- env-only settings are not writable through DB save helpers
- audit rows are recorded only when DB-backed values actually change
- unchanged saves do not create audit noise

### Route Tests

- `GET /api/settings/runtime` returns structured effective config
- `POST /settings` writes DB-backed settings and matching audit rows
- inspect output shows env overriding DB when both are present

### Documentation Verification

- docs clearly list DB-backed versus env-only settings
- docs state that `DB_PATH` remains env-only and that web and worker must share it

## Risks and Mitigations

- env overrides may surprise operators who expect saved DB values to win; mitigation: show `source` explicitly in the inspect API and settings page
- audit actor identity is coarse without auth; mitigation: store system-level actor/source now and leave schema flexible for future user identity
- expanding the inspect surface could accidentally expose secrets; mitigation: drive output from the registry and omit `sensitive` settings entirely

## Implementation Plan Boundary

This design intentionally targets the smallest change set that closes issue `#161` end to end:

- no typed config table migration
- no secret storage changes
- no hot reload redesign
- no config history UI browser

The implementation plan should therefore focus on:

1. runtime setting registry and inspect metadata
2. audit table migration and save-path auditing
3. inspect API and settings-page effective config display
4. ownership and rollout documentation
