# Local runtime notes

See also: `docs/runtime-config.md` for the DB-vs-env ownership rules and the dev/prod rollout guidance for mutable runtime settings.

## Single source of truth for runtime state

When running the local `web` service and the local `worker`, both processes must use the same database file.

Do not let `web` fall back to its default `./data/software_factory.db` while `worker` uses a different `DB_PATH`.

Known-good local database path:

```bash
/home/svtter/work/project/software-factory-homepage/data/software_factory.db
```

## Required startup rule

Always start `web` and `worker` with the same `DB_PATH`.

`DB_PATH` is bootstrap configuration and stays env-only. Do not move it into SQLite or the `/settings` form.

Example:

```bash
export DB_PATH=/home/svtter/work/project/software-factory-homepage/data/software_factory.db
```

Start `web`:

```bash
env DB_PATH=/home/svtter/work/project/software-factory-homepage/data/software_factory.db \
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Start `worker`:

```bash
env DB_PATH=/home/svtter/work/project/software-factory-homepage/data/software_factory.db \
ANTHROPIC_API_KEY="$DEEPSEEK_API_KEY" \
ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" \
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
ANTHROPIC_MODEL=deepseek-chat \
ANTHROPIC_SMALL_FAST_MODEL=deepseek-chat \
ENABLE_TOOL_SEARCH=false \
python3 scripts/run_worker.py --loop --workspace-dir /home/svtter/work/project/software-factory-aider
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
