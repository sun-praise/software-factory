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

## Env-Only Settings

These settings must stay outside SQLite because they are bootstrap, deployment-specific, or secret:

- `DB_PATH`
- `GITHUB_WEBHOOK_SECRET`
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
