# Local runtime notes

See also: `docs/runtime-config.md` for the DB-vs-env ownership rules and the dev/prod rollout guidance for mutable runtime settings.

## Token safety: environment isolation

**The `web` service must never receive AI tokens.** Only the `worker` needs them.

AI-token env vars include: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, `ZHIPU_API_KEY`, and any other provider keys.

If `web` inherits these from the shell (e.g. via `.env` or an interactive shell), it creates a security surface and can cause confusion when debugging configuration issues.

Use the dedicated startup scripts which enforce this isolation:

```bash
./scripts/start_web.sh           # strips all AI tokens before starting uvicorn
./scripts/start_worker.sh        # loads tokens from a controlled source
./scripts/start_system_bg.sh     # orchestrates both with correct isolation
```

Or use `scripts/start_system_bg.sh` to manage both processes together.

## Single source of truth for runtime state

When running the local `web` service and the local `worker`, both processes must use the same database file.

Do not let `web` fall back to its default `./data/software_factory.db` while `worker` uses a different `DB_PATH`.

Known-good local database path:

```bash
${HOME}/data/software_factory.db
```

## Required startup rule

Always start `web` and `worker` with the same `DB_PATH`.

`DB_PATH` is bootstrap configuration and stays env-only. Do not move it into SQLite or the `/settings` form.

Example:

```bash
export DB_PATH=${HOME}/data/software_factory.db
```

Start `web` (recommended — uses the isolation script):

```bash
./scripts/start_web.sh
```

Start `web` (manual, ensure no AI tokens leak):

```bash
env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL \
  -u ANTHROPIC_MODEL -u ANTHROPIC_SMALL_FAST_MODEL \
  -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_MODEL \
  -u ZHIPU_API_KEY -u ZHIPU_AUTH_TOKEN -u API_TIMEOUT_MS \
  -u DEEPSEEK_API_KEY -u ENABLE_TOOL_SEARCH \
  -u CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC \
  DB_PATH=${HOME}/data/software_factory.db \
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Start `worker` (recommended — uses the isolation script):

```bash
./scripts/start_worker.sh
```

Start `worker` with DeepSeek backend:

```bash
./scripts/run_worker_deepseek.sh
```

Start `worker` (manual):

```bash
env DB_PATH=${HOME}/data/software_factory.db \
  python3 scripts/run_worker.py --loop --workspace-dir ${HOME}/project/software-factory
```

## Quick verification

Check `worker` database:

```bash
tr '\0' '\n' </proc/<worker-pid>/environ | rg '^DB_PATH='
```

Check `web` database:

```bash
tr '\0' '\n' </proc/<web-pid>/environ | rg '^DB_PATH='
```

Both commands must print the same `DB_PATH`.

## Failure mode

If this is wrong, the UI and the worker will read different SQLite files:

- the UI may show missing or stale runs
- new queued runs may never be claimed by the visible worker
- deleting a run in the UI may appear to do nothing

If the UI state and worker behavior disagree, check `DB_PATH` first.

In local development, runtime knobs like retry limits or bot filters may live in DB, but `DB_PATH` must still come from env so both processes point at the same SQLite file.
