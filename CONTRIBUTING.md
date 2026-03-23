# Contributing

Thanks for contributing to `software-factory`.

## Before You Start

- Search existing issues and pull requests before opening a new one.
- Keep changes scoped. Small, reviewable PRs are preferred.
- For behavioral changes, include the problem statement and expected outcome.

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python scripts/init_db.py
```

Start the web app:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

Start the worker:

```bash
python scripts/run_worker.py --loop --workspace-dir .
```

When running `web` and `worker` locally, keep both processes on the same `DB_PATH`.
See [docs/local-runtime.md](docs/local-runtime.md).

## Validation

Run the checks relevant to your change:

```bash
python -m pytest -q
python -m ruff check .
python -m mypy .
```

If your change touches Docker packaging:

```bash
docker build -t software-factory:test .
```

## Pull Requests

- Explain what changed and why.
- Link related issues.
- Mention any follow-up work that is intentionally left out.
- Keep PR descriptions concrete. Avoid “misc cleanup” summaries for behavior changes.

## Issues

Bug reports should include:

- expected behavior
- actual behavior
- reproduction steps
- relevant logs or screenshots
- environment details when they matter

Feature requests should describe the workflow gap, not only the proposed implementation.
