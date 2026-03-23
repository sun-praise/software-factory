# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-03-23

### Added

- Claude Agent SDK mode with settings-driven flags and updated agent execution paths
- OpenSpec propose, explore, apply, and archive commands for spec-driven workflow
- Repo cache workspaces, background start scripts, run operator hints, and richer run list controls
- Open source project metadata, templates, Docker publish workflow, and multilingual README variants

### Changed

- Expanded autofix context with PR metadata, CI status propagation, mergeability gating, and sturdier workspace preparation
- Refreshed docs, settings pages, and tests to cover new runtime and release workflows

### Fixed

- GitHub metadata fallback and PR head resolution when `gh` data is incomplete
- PR conflict detection and base-branch sync before autofix execution
- Background runtime defaults, manual issue context resolution, and agent image GitHub tooling gaps

## [0.1.0] - 2026-03-12

### Added

#### M1: Minimal Skeleton
- FastAPI service with health check endpoint
- SQLite database initialization
- Basic web pages (home, runs list, run detail)
- Hook events endpoint (`/hook-events`)
- GitHub webhook endpoint (`/github/webhook`)
- CI workflow with pytest

#### M2: Event Persistence
- Hook event parsing and storage
- GitHub webhook event parsing
- Event deduplication with `event_key`
- Session and PR association
- Error handling and status tracking

#### M3: GitHub Webhook Integration
- Webhook signature verification
- Pull request review event handling
- Pull request review comment handling
- Issue comment (for PR) handling
- Debounce mechanism for rapid events

#### M4: Review Normalizer
- Normalize review events into structured format
- Classify severity (P0-P3)
- Deduplicate similar feedback
- Filter ignorable comments (lgtm, +1, etc.)
- Generate actionable fix items

#### M5: Agent Runner Pipeline
- Autofix task queue with SQLite
- Worker script (`run_worker.py`)
- Git operations (checkout, commit, push)
- PR comment posting
- Check command execution (pytest, ruff, mypy)
- Log file generation with sensitive data redaction

#### M6: Strategy and Stability
- **#27**: Task-level idempotency with `idempotency_key`
- **#28**: Per-PR autofix limit (default: 3)
- **#29**: Bot/noise filtering (bot_logins, noise patterns)
- **#30**: Concurrency control with PR-level locking
- **#31**: Error recovery with exponential backoff retry
- **#32**: Log archiving with retention policy

#### M7: Documentation and Acceptance
- **#36**: Architecture documentation
- **#33**: End-to-end integration tests (5 scenarios)
- **#35**: Bot loop prevention tests (5 cases)
- **#34**: Stress test scripts with Locust
- **#37**: MVP scope documentation

### Configuration

Environment variables:
- `APP_ENV` - Environment (development/production)
- `HOST` - Server host (default: 127.0.0.1)
- `PORT` - Server port (default: 8000)
- `DB_PATH` - SQLite database path
- `GITHUB_WEBHOOK_SECRET` - Webhook signature secret
- `GITHUB_WEBHOOK_DEBOUNCE_SECONDS` - Debounce window (default: 60)
- `MAX_AUTOFIX_PER_PR` - Max fixes per PR (default: 3)
- `MAX_CONCURRENT_RUNS` - Max concurrent runs (default: 3)
- `PR_LOCK_TTL_SECONDS` - PR lock TTL (default: 900)
- `MAX_RETRY_ATTEMPTS` - Max retry attempts (default: 3)
- `RETRY_BACKOFF_BASE_SECONDS` - Retry backoff base (default: 30)
- `RETRY_BACKOFF_MAX_SECONDS` - Retry backoff max (default: 1800)
- `BOT_LOGINS` - Bot account list (comma-separated)
- `NOISE_COMMENT_PATTERNS` - Noise patterns (regex, comma-separated)
- `MANAGED_REPO_PREFIXES` - Managed repo prefixes (comma-separated)
- `AUTOFIX_COMMENT_AUTHOR` - Bot comment author name
- `LOG_DIR` - Log directory (default: logs)
- `LOG_RETENTION_DAYS` - Log retention days (default: 7)
- `WORKER_ID` - Worker identifier

### Documentation

- `docs/architecture.md` - System architecture
- `docs/troubleshooting.md` - Troubleshooting guide
- `docs/mvp-scope.md` - MVP scope and roadmap
- `docs/stress_test.md` - Stress test guide
- `docs/hook-samples.md` - Hook configuration samples

### Tests

- 101 pytest test cases
- 5 E2E integration tests
- 5 bot loop prevention tests
- Stress test with Locust

### Known Limitations

- Single worker only
- SQLite only (no PostgreSQL)
- GitHub only (no GitLab/Gitea)
- Python/JavaScript/TypeScript/Go/Rust repositories only
- No webhook retry on failure
- No metrics/monitoring integration

### Contributors

@Svtter

[Unreleased]: https://github.com/sun-praise/software-factory/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sun-praise/software-factory/compare/v0.1.1...v0.2.0
[0.1.0]: https://github.com/sun-praise/software-factory/releases/tag/v0.1.0
