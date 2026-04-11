# Runtime Configuration Ownership

Issue `#161` uses a hybrid runtime configuration model.

## Resolution Order

For mutable, non-secret runtime settings, the effective value is resolved in this order:

1. environment variable or `.env`
2. SQLite value in `app_feature_flags`
3. code default

This preserves emergency env overrides while allowing web and worker to share the same persisted runtime defaults.

## DB-Backed Mutable Settings

These settings are safe to inspect in the product and safe to update at runtime through `/settings`:

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

These values are visible in:

- the `/settings` page under `Effective Runtime Config`
- `GET /api/settings/runtime`

Writes are audit-trailed in `app_config_audit_log`.

## Operator-Only Interfaces

`/settings` (both the HTML page and the `POST` handler) and `GET /api/settings/runtime` are **operator-only** interfaces. They expose and mutate internal runtime configuration and must not be accessible to end-users or untrusted clients.

### Deployment Boundary

In production, access to these endpoints is enforced at the reverse-proxy layer:

- `sf.sun-praise.com/settings` and `sf.sun-praise.com/api/settings/runtime` are protected behind **nginx basic auth**.
- No application-level authentication middleware exists for these routes; the nginx layer is the sole gatekeeper.
- If nginx basic auth is removed or misconfigured, these endpoints become publicly writable.

### Operational Guidance

- When deploying behind a new reverse proxy, replicate the basic-auth location blocks from the production nginx config for both `/settings` and `/api/settings/runtime`.
- Audit log entries (`app_config_audit_log`) record `changed_by` and `change_source` — cross-reference these when investigating unexpected configuration changes.
- Future sensitive settings (e.g. if secret values are ever added to `app_feature_flags`) must be marked `sensitive: True` in their `RuntimeSettingSpec`. Sensitive values are excluded from the inspect API responses and redacted in audit log entries (see **Audit Log Sensitive Value Redaction** below).

## Env-Only Settings

These settings must stay outside SQLite because they are bootstrap, deployment-specific, or secret:

- `DB_PATH`
- `GITHUB_WEBHOOK_SECRET`
- `GITEE_WEBHOOK_SECRET`
- `FORGE_PROVIDER`
- `TASK_SOURCE_PROVIDER`
- `WEBHOOK_PROVIDER`
- `GIT_REMOTE_PROVIDER`
- provider API keys and auth tokens
- host / port
- other deployment-only values that should not be changed from the product

Secrets are intentionally omitted from the runtime inspect API.

## Local Rollout

- keep `DB_PATH` in env
- start `web` and `worker` with the same `DB_PATH`
- move mutable non-secret knobs into `/settings` when you want a shared persisted source
- if an env var and DB value disagree, env still wins until the env var is removed

## Dev / Prod Rollout

1. confirm secrets and bootstrap values still come from env or a secret manager
2. copy mutable non-secret runtime values into `/settings`
3. use `GET /api/settings/runtime` to confirm each effective winner and source
4. remove redundant env overrides only after the DB-backed value is correct

## Operator Checks

- if the UI and worker behave differently, check `DB_PATH` first
- if a saved DB value does not take effect, check whether the inspect API reports `source: env`
- if a runtime value changed unexpectedly, inspect `app_config_audit_log`

## Audit Log Sensitive Value Redaction

When a `RuntimeSettingSpec` is marked `sensitive: True`, the `save_runtime_setting_values()` function redacts both `old_value` and `new_value` in the audit log row. The stored audit row will contain the literal string `***REDACTED***` instead of the plaintext value. The actual value is still written to `app_feature_flags` as normal — only the audit trail is masked.

Currently no runtime settings are marked sensitive. This mechanism exists as a defence-in-depth measure for future settings that may carry secrets (e.g. webhook tokens stored in the DB).

## Audit Log Retention

`app_config_audit_log` is an append-only table with no automatic cleanup. Retention strategy:

| Aspect | Policy |
|---|---|
| Default retention | Unlimited (no TTL) |
| Recommended operational ceiling | 90 days |
| Cleanup method | Manual `DELETE FROM app_config_audit_log WHERE created_at < datetime('now', '-90 days')` |
| Archive before purge | Optional: `INSERT INTO app_config_audit_log_archive SELECT * FROM app_config_audit_log WHERE created_at < …` then delete |

In production, add a periodic cron job or scheduled task to enforce the 90-day retention window if audit volume grows beyond operational needs.

## SQLite Concurrency Constraints

The application uses a single SQLite file shared between the `web` and `worker` processes. SQLite handles concurrent writes via file-level locking:

- **Write contention**: SQLite returns `SQLITE_BUSY` when a writer cannot acquire the lock. The default busy-timeout is 5 seconds (`PRAGMA busy_timeout = 5000`).
- **Read during write**: WAL mode is not enabled; readers may see `SQLITE_BUSY` if a write transaction is in progress.
- **Mitigation**: Keep write transactions short. The `save_runtime_setting_values()` function performs a single `executemany` + `commit` and should complete well within the busy-timeout window.
- **Practical constraint**: The current workload (operator-initiated settings saves, webhook processing) generates very low write concurrency. If concurrent write throughput increases (e.g. high-frequency webhook bursts), consider enabling WAL mode or migrating to a client-server database.
