package db

import (
	"path/filepath"
	"testing"
)

func TestOpenMigratesSchema(t *testing.T) {
	t.Setenv("DB_PATH", filepath.Join(t.TempDir(), "sf.db"))

	handle, err := Open()
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer handle.Close()

	for _, table := range Tables {
		var name string
		err := handle.QueryRow(
			"SELECT name FROM sqlite_master WHERE type='table' AND name=?", table.Name,
		).Scan(&name)
		if err != nil {
			t.Fatalf("table %s missing: %v", table.Name, err)
		}
	}

	// Unique indexes must reject duplicate event keys (dedup contract).
	if _, err := handle.Exec(
		"INSERT INTO review_events(repo, pr_number, event_type, event_key, raw_payload_json) VALUES('o/r', 1, 'pull_request_review', 'k1', '{}')",
	); err != nil {
		t.Fatalf("insert review event: %v", err)
	}
	if _, err := handle.Exec(
		"INSERT INTO review_events(repo, pr_number, event_type, event_key, raw_payload_json) VALUES('o/r', 1, 'pull_request_review', 'k1', '{}')",
	); err == nil {
		t.Fatal("duplicate event_key accepted; dedup index missing")
	}
}

func TestOpenIsIdempotent(t *testing.T) {
	t.Setenv("DB_PATH", filepath.Join(t.TempDir(), "sf.db"))

	first, err := Open()
	if err != nil {
		t.Fatalf("first Open: %v", err)
	}
	first.Close()

	second, err := Open()
	if err != nil {
		t.Fatalf("second Open: %v", err)
	}
	defer second.Close()

	if err := second.Ping(); err != nil {
		t.Fatalf("ping: %v", err)
	}
	if err := Migrate(second); err != nil {
		t.Fatalf("re-migrate: %v", err)
	}
}
