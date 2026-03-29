from dataclasses import dataclass


@dataclass(frozen=True)
class TableDef:
    name: str
    create_sql: str


SESSIONS_TABLE = TableDef(
    name="sessions",
    create_sql="""
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    cwd TEXT,
    source TEXT NOT NULL DEFAULT 'claude_code',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
""".strip(),
)

PULL_REQUESTS_TABLE = TableDef(
    name="pull_requests",
    create_sql="""
CREATE TABLE IF NOT EXISTS pull_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT,
    branch TEXT,
    state TEXT NOT NULL DEFAULT 'IDLE',
    linked_session_id INTEGER,
    autofix_count INTEGER NOT NULL DEFAULT 0,
    lock_owner TEXT,
    lock_run_id INTEGER,
    lock_acquired_at TEXT,
    lock_expires_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (linked_session_id) REFERENCES sessions(id) ON DELETE SET NULL
);
""".strip(),
)

REVIEW_EVENTS_TABLE = TableDef(
    name="review_events",
    create_sql="""
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
);
""".strip(),
)

AUTOFIX_RUNS_TABLE = TableDef(
    name="autofix_runs",
    create_sql="""
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
    finished_at TEXT,
    source_url TEXT
);
""".strip(),
)


APP_FEATURE_FLAGS_TABLE = TableDef(
    name="app_feature_flags",
    create_sql="""
CREATE TABLE IF NOT EXISTS app_feature_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""".strip(),
)


APP_CONFIG_AUDIT_LOG_TABLE = TableDef(
    name="app_config_audit_log",
    create_sql="""
CREATE TABLE IF NOT EXISTS app_config_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT NOT NULL,
    change_source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""".strip(),
)


SCHEMA_STATEMENTS = [
    SESSIONS_TABLE.create_sql,
    PULL_REQUESTS_TABLE.create_sql,
    REVIEW_EVENTS_TABLE.create_sql,
    AUTOFIX_RUNS_TABLE.create_sql,
    APP_FEATURE_FLAGS_TABLE.create_sql,
    APP_CONFIG_AUDIT_LOG_TABLE.create_sql,
    "CREATE INDEX IF NOT EXISTS idx_sessions_repo_branch ON sessions(repo, branch);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pull_requests_repo_pr_number ON pull_requests(repo, pr_number);",
    "CREATE INDEX IF NOT EXISTS idx_review_events_repo_pr_number ON review_events(repo, pr_number);",
    "CREATE INDEX IF NOT EXISTS idx_autofix_runs_repo_pr_number ON autofix_runs(repo, pr_number);",
    "CREATE INDEX IF NOT EXISTS idx_pull_requests_lock_owner ON pull_requests(lock_owner);",
    "CREATE INDEX IF NOT EXISTS idx_autofix_runs_status_retry_after ON autofix_runs(status, retry_after);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_autofix_runs_idempotency_key ON autofix_runs(idempotency_key);",
    "CREATE INDEX IF NOT EXISTS idx_autofix_runs_source_url ON autofix_runs(source_url);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_event_key ON review_events(event_key);",
    "CREATE INDEX IF NOT EXISTS idx_app_config_audit_log_key_created_at ON app_config_audit_log(key, created_at DESC);",
]


SCHEMA_SQL = "\n\n".join(SCHEMA_STATEMENTS)
