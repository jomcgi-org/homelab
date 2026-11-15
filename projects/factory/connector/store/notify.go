package store

import (
	"context"
	"database/sql"
	"errors"
	"regexp"
)

var notifyKey = regexp.MustCompile(`^factory/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9a-f]{64}$`)

// RecordNotifyAttempt fences and records every outbound notification request.
// It stores no credential, URL or notification text, only a hashed daily key.
func (s *Store) RecordNotifyAttempt(ctx context.Context, w *Writer, key, phase string) error {
	if !notifyKey.MatchString(key) {
		return errors.New("invalid notification key")
	}
	switch phase {
	case "token", "initialize", "notifications/initialized", "tools/call":
	default:
		return errors.New("invalid notification phase")
	}
	return w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO notify_attempt (dedupe_key, phase, writer_epoch) VALUES ($1, $2, $3)
			ON CONFLICT (dedupe_key, phase) DO UPDATE SET
			writer_epoch = EXCLUDED.writer_epoch, attempts = notify_attempt.attempts + 1,
			last_attempt_at = now()`, key, phase, w.Epoch)
		return err
	})
}
