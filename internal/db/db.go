// Package db opens the SQLite state store and applies the schema.
//
// The schema is ported verbatim from app/models.py so an existing Python
// deployment's database file can be reused as-is during the switch-over.
package db

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"

	"github.com/sun-praise/software-factory/internal/config"
)

// TableDef mirrors app/models.py TableDef.
type TableDef struct {
	Name      string
	CreateSQL string
}

const sessionsTable = `
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    cwd TEXT,
    source TEXT NOT NULL DEFAULT 'claude_code',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
)`

const pullRequestsTable = `
CREATE TABLE IF NOT EXISTS pull_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT,
    branch TEXT,
    state TEXT NOT NULL DEFAULT 'IDLE',
    linked_session_id INTEGER,
    autofix_count INTEGER NOT NULL DEFAULT 0,
    seen_shas TEXT NOT NULL DEFAULT '',
    lock_owner TEXT,
    lock_run_id INTEGER,
    lock_acquired_at TEXT,
    lock_expires_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (linked_session_id) REFERENCES sessions(id) ON DELETE SET NULL
)`

const reviewEventsTable = `
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_key TEXT NOT NULL,
    actor TEXT,
    head_sha TEXT,
    raw_payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)`

const autofixRunsTable = `
CREATE TABLE IF NOT EXISTS autofix_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    opened_pr_number INTEGER,
    opened_pr_url TEXT,
    head_sha TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    trigger_source TEXT NOT NULL DEFAULT 'github_webhook',
    idempotency_key TEXT,
    normalized_review_json TEXT NOT NULL DEFAULT '{}',
    worker_id TEXT,
    claimed_at TEXT,
    started_at TEXT,
    logs_path TEXT,
    commit_sha TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    retryable INTEGER NOT NULL DEFAULT 1,
    retry_after TEXT,
    last_error_code TEXT,
    last_error_at TEXT,
    error_summary TEXT,
    operator_hints TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
)`

const appFeatureFlagsTable = `
CREATE TABLE IF NOT EXISTS app_feature_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)`

const appConfigAuditLogTable = `
CREATE TABLE IF NOT EXISTS app_config_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT NOT NULL,
    change_source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)`

const userAPIKeysTable = `
CREATE TABLE IF NOT EXISTS user_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL UNIQUE,
    encrypted_key TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)`

// Tables mirrors app/models.py SCHEMA_STATEMENTS.
var Tables = []TableDef{
	{"sessions", sessionsTable},
	{"pull_requests", pullRequestsTable},
	{"review_events", reviewEventsTable},
	{"autofix_runs", autofixRunsTable},
	{"app_feature_flags", appFeatureFlagsTable},
	{"app_config_audit_log", appConfigAuditLogTable},
	{"user_api_keys", userAPIKeysTable},
}

// Indexes mirrors the index statements in app/models.py SCHEMA_STATEMENTS.
var Indexes = []string{
	"CREATE INDEX IF NOT EXISTS idx_sessions_repo_branch ON sessions(repo, branch);",
	"CREATE UNIQUE INDEX IF NOT EXISTS idx_pull_requests_repo_pr_number ON pull_requests(repo, pr_number);",
	"CREATE INDEX IF NOT EXISTS idx_review_events_repo_pr_number ON review_events(repo, pr_number);",
	"CREATE INDEX IF NOT EXISTS idx_autofix_runs_repo_pr_number ON autofix_runs(repo, pr_number);",
	"CREATE INDEX IF NOT EXISTS idx_pull_requests_lock_owner ON pull_requests(lock_owner);",
	"CREATE INDEX IF NOT EXISTS idx_autofix_runs_status_retry_after ON autofix_runs(status, retry_after);",
	"CREATE UNIQUE INDEX IF NOT EXISTS idx_autofix_runs_idempotency_key ON autofix_runs(idempotency_key);",
	"CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_event_key ON review_events(event_key);",
	"CREATE INDEX IF NOT EXISTS idx_app_config_audit_log_key_created_at ON app_config_audit_log(key, created_at DESC);",
	"CREATE INDEX IF NOT EXISTS idx_user_api_keys_provider ON user_api_keys(provider);",
}

// Open opens (creating when necessary) the SQLite database and applies the
// schema. Busy timeout and WAL keep the web/worker pair usable concurrently,
// matching the Python deployment's behavior.
func Open() (*sql.DB, error) {
	if err := config.EnsureDBDir(); err != nil {
		return nil, fmt.Errorf("create db dir: %w", err)
	}
	dsn := "file:" + config.DBPath() + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)"
	handle, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// SQLite serializes writes; a small pool avoids SQLITE_BUSY contention.
	handle.SetMaxOpenConns(1)
	if err := Migrate(handle); err != nil {
		handle.Close()
		return nil, err
	}
	return handle, nil
}

// Migrate applies schema statements idempotently.
func Migrate(handle *sql.DB) error {
	for _, t := range Tables {
		if _, err := handle.Exec(t.CreateSQL); err != nil {
			return fmt.Errorf("create table %s: %w", t.Name, err)
		}
	}
	for _, stmt := range Indexes {
		if _, err := handle.Exec(stmt); err != nil {
			return fmt.Errorf("create index: %w", err)
		}
	}
	return nil
}
