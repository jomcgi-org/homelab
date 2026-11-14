// Package migrations holds the connector's Postgres schema as ordered SQL
// files, named <UTC timestamp>_<name>.sql like the monolith's Atlas
// migrations, and applies the ones a database has not seen.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"sort"
	"strings"
)

//go:embed *.sql
var files embed.FS

// lockKey serializes concurrent Apply calls across processes.
const lockKey = 0x66616374 // "fact"

// Versions lists the embedded migration versions in apply order.
func Versions() ([]string, error) {
	entries, err := fs.Glob(files, "*.sql")
	if err != nil {
		return nil, err
	}
	sort.Strings(entries)
	versions := make([]string, 0, len(entries))
	for _, e := range entries {
		versions = append(versions, strings.TrimSuffix(e, ".sql"))
	}
	return versions, nil
}

// Apply runs every migration the database has not recorded, each in its own
// transaction under a transaction-scoped advisory lock, which also covers
// creating the bookkeeping table.
func Apply(ctx context.Context, db *sql.DB) error {
	versions, err := Versions()
	if err != nil {
		return err
	}
	for _, v := range versions {
		if err := applyOne(ctx, db, v); err != nil {
			return err
		}
	}
	return nil
}

func applyOne(ctx context.Context, db *sql.DB, version string) error {
	body, err := files.ReadFile(version + ".sql")
	if err != nil {
		return err
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback() //nolint:errcheck // no-op after commit
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, lockKey); err != nil {
		return fmt.Errorf("lock migrations: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
		version text PRIMARY KEY,
		applied_at timestamptz NOT NULL DEFAULT now()
	)`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}
	var seen bool
	if err := tx.QueryRowContext(ctx,
		`SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)`, version,
	).Scan(&seen); err != nil {
		return err
	}
	if seen {
		return nil
	}
	if _, err := tx.ExecContext(ctx, string(body)); err != nil {
		return fmt.Errorf("apply %s: %w", version, err)
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO schema_migrations (version) VALUES ($1)`, version,
	); err != nil {
		return err
	}
	return tx.Commit()
}
