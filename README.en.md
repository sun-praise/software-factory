# software-factory

[English](./README.en.md) | [中文](./README.md)

`software-factory` is a lightweight FastAPI-based issue/PR-driven autonomous development system.

It focuses on a narrow but practical workflow:

- accept manual issues and operator-provided issue-like inputs
- collect signals from local hooks and GitHub webhooks
- normalize review feedback into structured work items
- run agent workers to apply fixes, validate changes, and push updates
- expose recent runs and failures through a thin web UI

This project is intentionally not a full CI/CD platform or a multi-tenant DevOps control plane.

## Positioning

The simplest way to describe the project is:

- issue/PR-driven autonomous development system
- AI-native GitHub issue and PR orchestrator

It sits somewhere between agent executors such as `OpenHands` / `SWE-agent` and event-driven review infrastructure such as `Prow` / `Zuul`.

## Architecture

```text
Hook (Claude Code lifecycle)
  -> Local Orchestrator API
    -> State Store / Queue
      -> GitHub Webhook Adapter
      -> Review Normalizer
      -> Agent Worker
      -> Thin Web
```

Main responsibilities:

- Hook: reports local managed-session events without making semantic decisions
- Webhook adapter: ingests GitHub events, validates signatures, and deduplicates raw events
- Normalizer: converts review/comments into structured autofix inputs
- Agent worker: checks out code, applies fixes, runs validation, and writes results back
- Thin web: shows runs, statuses, and error summaries

## Current Scope

Already implemented:

- FastAPI service, health check, and SSR pages
- GitHub webhook ingestion with signature verification
- review/comment normalization and filtering
- autofix queue, retry logic, PR locking, and concurrency controls
- agent execution pipeline with git checkout / commit / push
- local runtime scripts for web + worker

In progress:

- documentation hardening
- end-to-end coverage
- load and stress testing

## Quick Start

1. Create a virtual environment and install dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

2. Configure environment variables.

```bash
cp example.env .env
```

3. Initialize the SQLite database.

```bash
python scripts/init_db.py
```

4. Start the web service.

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

5. Or start both `web` and `worker` in the background.

```bash
chmod +x scripts/start_system_bg.sh
./scripts/start_system_bg.sh start
```

Important local runtime note:

- keep `web` and `worker` on the same `DB_PATH`
- see [docs/local-runtime.md](docs/local-runtime.md) for the required shared database path

## Useful Pages

- `http://127.0.0.1:8001/`
- `http://127.0.0.1:8001/runs`
- `http://127.0.0.1:8001/runs/demo-run`

## Repository Structure

```text
app/        FastAPI app, routes, services, templates, static files
scripts/    local runtime and maintenance scripts
tests/      test suite
docs/       architecture, troubleshooting, and hook examples
openspec/   requirement tracking and change specs
```

## Documentation

- [Architecture](docs/architecture.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Hook Samples](docs/hook-samples.md)
- [OpenSpec workflow](openspec/README.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

## Docker

Build the primary service image:

```bash
docker build -t svtter/software-factory:latest .
```

Run the web app:

```bash
docker run --rm -p 8000:8000 \
  -e PORT=8000 \
  -e DB_PATH=/app/data/software_factory.db \
  svtter/software-factory:latest
```

Run the worker with the same image by overriding the command:

```bash
docker run --rm \
  -e DB_PATH=/app/data/software_factory.db \
  svtter/software-factory:latest \
  python scripts/run_worker.py --loop --workspace-dir /app
```

## License

This project is licensed under [Apache License 2.0](./LICENSE).
