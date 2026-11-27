// Package config holds process-level configuration shared by web and worker.
//
// The Go rewrite keeps the Python version's ownership rules: env vars seed
// defaults, and runtime settings stored in SQLite override env (see
// docs/runtime-config.md). For now only bootstrap values live here; the full
// runtime-settings layer arrives with milestone M5.
package config

import (
	"os"
	"path/filepath"
)

// DefaultDBPath mirrors app/db.py DEFAULT_DB_PATH.
const DefaultDBPath = "data/software_factory.db"

// DBPath returns the SQLite database file path.
func DBPath() string {
	if p := os.Getenv("DB_PATH"); p != "" {
		return p
	}
	return DefaultDBPath
}

// DefaultWebPort mirrors app/config.py Settings.port (8000); example.env uses
// PORT to override.
const DefaultWebPort = "8000"

// WebPort returns the web server listen port.
func WebPort() string {
	if p := os.Getenv("PORT"); p != "" {
		return p
	}
	return DefaultWebPort
}

// EnsureDBDir creates the parent directory of DBPath when missing.
func EnsureDBDir() error {
	dir := filepath.Dir(DBPath())
	if dir == "." {
		return nil
	}
	return os.MkdirAll(dir, 0o755)
}
