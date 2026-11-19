package reaper

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// leaseKey serializes reapers across processes ("reap").
const leaseKey = 0x72656170

// ErrNotLeader means this process no longer holds the reaper lease.
var ErrNotLeader = errors.New("reaper: lease lost")

// Lease is the reaper's single-writer lock. It lives on one PostgreSQL
// session, so a lost connection loses the lease, and each acquisition
// raises the epoch that deletion journal rows carry.
type Lease struct {
	conn  *sql.Conn
	Epoch int64
}

// TryLease takes the reaper lease without waiting. It returns nil when
// another reaper holds it.
func TryLease(ctx context.Context, db *sql.DB) (*Lease, error) {
	conn, err := db.Conn(ctx)
	if err != nil {
		return nil, err
	}
	var ok bool
	if err := conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock($1)`, leaseKey).Scan(&ok); err != nil || !ok {
		conn.Close()
		return nil, err
	}
	var epoch int64
	if err := conn.QueryRowContext(ctx, `INSERT INTO reaper_writer (id, epoch) VALUES (1, 1)
		ON CONFLICT (id) DO UPDATE SET epoch = reaper_writer.epoch + 1, acquired_at = now()
		RETURNING epoch`).Scan(&epoch); err != nil {
		conn.Close()
		return nil, err
	}
	return &Lease{conn: conn, Epoch: epoch}, nil
}

// Close releases the lease.
func (l *Lease) Close() error {
	if l == nil || l.conn == nil {
		return nil
	}
	return l.conn.Close()
}

// begin journals a deletion under its stable operation ID while the lease's
// epoch is current. It reports false when the operation already ran or is
// running.
func (l *Lease) begin(ctx context.Context, op string, p placement, actorVersion int64) (bool, error) {
	if l == nil || l.conn == nil {
		return false, ErrNotLeader
	}
	tx, err := l.conn.BeginTx(ctx, nil)
	if err != nil {
		return false, fmt.Errorf("%w: %v", ErrNotLeader, err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after commit
	var epoch int64
	if err := tx.QueryRowContext(ctx, `SELECT epoch FROM reaper_writer WHERE id = 1 FOR UPDATE`).Scan(&epoch); err != nil {
		return false, fmt.Errorf("%w: %v", ErrNotLeader, err)
	}
	if epoch != l.Epoch {
		return false, ErrNotLeader
	}
	res, err := tx.ExecContext(ctx, `INSERT INTO worker_deletion
		(operation_id, writer_epoch, namespace, pod, uid, actor_version)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (operation_id) DO UPDATE SET writer_epoch = excluded.writer_epoch,
			attempted_at = now(), outcome = NULL, finished_at = NULL
		-- Retry a failed call, one a reaper that lost its lease left open, or
		-- one whose outcome was never saved (a scan deadline).
		WHERE worker_deletion.outcome LIKE 'error:%'
			OR (worker_deletion.finished_at IS NULL AND (worker_deletion.writer_epoch < excluded.writer_epoch
				OR worker_deletion.attempted_at < now() - interval '2 minutes'))`,
		op, l.Epoch, p.namespace, p.pod, p.uid, actorVersion)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, err
	}
	return n == 1, tx.Commit()
}

// finish records a journaled deletion's outcome. It does not use the scan's
// context, so a scan deadline after the call cannot strand the row.
func (l *Lease) finish(op, outcome string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := l.conn.ExecContext(ctx, `UPDATE worker_deletion SET outcome = $2, finished_at = now()
		WHERE operation_id = $1 AND finished_at IS NULL`, op, outcome)
	return err
}
