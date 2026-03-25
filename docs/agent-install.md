# Agent Install Guide

Use this guide when an AI agent needs to install and verify `software-factory` locally.

## Scope

- Work from the repository root.
- Follow this file and [docs/local-runtime.md](local-runtime.md) exactly.
- If `docs/local-runtime.md` is more specific about `DB_PATH`, treat it as the source of truth.

## Repository bootstrap

If the current workspace is not already this repository:

```bash
git clone https://github.com/sun-praise/software-factory.git
cd software-factory
```

## Required outcome

- Install dependencies in a Python virtual environment
- Initialize the SQLite database
- Start the web service on port `8001`
- If starting the worker, use the exact same `DB_PATH` as the web service
- Verify the setup with `curl -i http://127.0.0.1:8001/healthz`

## Steps

1. Create a virtual environment and install dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

2. Create local environment config.

```bash
cp example.env .env
```

3. Choose one writable `DB_PATH` and use that exact same value for every local process.

```bash
export DB_PATH="$(pwd)/data/software_factory.db"
```

4. Initialize the database.

```bash
python scripts/init_db.py
```

5. Start the web service on port `8001`.

```bash
env DB_PATH="$DB_PATH" uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

6. Optionally start the worker with the same `DB_PATH`.

```bash
env DB_PATH="$DB_PATH" python scripts/run_worker.py --loop --workspace-dir "$(pwd)"
```

7. Verify the web service.

```bash
curl -i http://127.0.0.1:8001/healthz
```

8. If both web and worker are running, verify that both processes expose the same `DB_PATH`.

See [docs/local-runtime.md](local-runtime.md) for the exact process-level checks and the failure mode.

## Guardrails

- Do not modify application code just to make local setup pass.
- Only change local env or config when needed.
- If something fails, report the exact failing command, the root cause, and the smallest fix.
