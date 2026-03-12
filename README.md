# software-factory

Minimal SSR foundation for M1 with FastAPI + Jinja2 templates and simple static CSS.

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 2. Configure

Copy environment template:

```bash
cp example.env .env
```

Then edit `.env` as needed (host, port, database path, webhook secret).

## 3. Initialize Database

This milestone uses SQLite. Initialize the schema:

```bash
python scripts/init_db.py
```

This creates 4 core tables: `sessions`, `pull_requests`, `review_events`, `autofix_runs`.

## 4. Start Service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open in browser:

- Home: `http://127.0.0.1:8000/`
- Run detail example: `http://127.0.0.1:8000/runs/demo-run`

## 5. Local Debug API Examples

Use curl for quick debugging while developing endpoints:

```bash
curl -i http://127.0.0.1:8000/healthz
curl -i -X POST http://127.0.0.1:8000/hook-events -H 'content-type: application/json' -d '{"event":"UserPromptSubmit","session_id":"sess-1","repo":"sun-praise/software-factory","branch":"feat/m2-hooks-schema","cwd":"/workspace/software-factory","timestamp":"2026-03-12T10:00:00Z","metadata":{"client":"opencode"},"payload":{"prompt":"hello"}}'
curl -i -X POST http://127.0.0.1:8000/github/webhook -H 'x-github-event: pull_request_review' -H 'content-type: application/json' -d '{"action":"submitted"}'
```

`/hook-events` validates payload schema for `UserPromptSubmit`, `PostToolUse`, and `PostToolUseFailure`. Invalid payloads return HTTP `422`. `timestamp` must be ISO-8601 with timezone (for example `Z`).

Web pages:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/runs`
- `http://127.0.0.1:8000/runs/demo-run`

Most APIs are placeholders in M1 and return a unified structure (`ok`, `message`, `event_type`, `received`).
