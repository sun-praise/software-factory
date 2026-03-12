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
    head_sha TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    trigger_source TEXT NOT NULL DEFAULT 'github_webhook',
    normalized_review_json TEXT NOT NULL DEFAULT '{}',
    logs_path TEXT,
    commit_sha TEXT,
    error_summary TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
""".strip(),
)


SCHEMA_STATEMENTS = [
    SESSIONS_TABLE.create_sql,
    PULL_REQUESTS_TABLE.create_sql,
    REVIEW_EVENTS_TABLE.create_sql,
    AUTOFIX_RUNS_TABLE.create_sql,
    "CREATE INDEX IF NOT EXISTS idx_sessions_repo_branch ON sessions(repo, branch);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pull_requests_repo_pr_number ON pull_requests(repo, pr_number);",
    "CREATE INDEX IF NOT EXISTS idx_review_events_repo_pr_number ON review_events(repo, pr_number);",
    "CREATE INDEX IF NOT EXISTS idx_autofix_runs_repo_pr_number ON autofix_runs(repo, pr_number);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_event_key ON review_events(event_key);",
]


SCHEMA_SQL = "\n\n".join(SCHEMA_STATEMENTS)
