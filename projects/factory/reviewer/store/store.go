// Package store owns the reviewer's durable gate state and single-writer epoch.
package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"sync"

	_ "github.com/lib/pq" // registers the postgres driver
)

// ErrNotWriter means this process no longer holds the writer lock at its
// epoch. Every external side effect checks for it first.
var ErrNotWriter = errors.New("store: not the current writer")

// writerLockKey is the session advisory lock that admits one writer.
const writerLockKey = 0x726576696577 // "review"

const writerFence = `SELECT epoch FROM writer WHERE id = 1 AND EXISTS (
	SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()
	AND classid::bigint = ($1::bigint >> 32)
	AND objid::bigint = ($1::bigint & 4294967295) AND objsubid = 1 AND granted
)`

// Store wraps the reviewer database.
type Store struct {
	DB *sql.DB
}

// Open connects to Postgres with the lib/pq driver.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	return &Store{DB: db}, nil
}

// Writer is a held writer lock. The lock lives on conn's session, so the
// epoch check runs there: a lost connection fails the check.
type Writer struct {
	mu    sync.Mutex
	conn  *sql.Conn
	Epoch int64
}

// TryAcquireWriter takes the advisory lock without waiting and, when it gets
// it, increments the writer epoch. It returns nil when another process holds
// the lock.
func (s *Store) TryAcquireWriter(ctx context.Context, holder string) (*Writer, error) {
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return nil, err
	}
	var ok bool
	if err := conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock($1)`, writerLockKey).Scan(&ok); err != nil {
		conn.Close()
		return nil, err
	}
	if !ok {
		conn.Close()
		return nil, nil
	}
	var epoch int64
	err = conn.QueryRowContext(ctx, `
		INSERT INTO writer (id, epoch, holder) VALUES (1, 1, $1)
		ON CONFLICT (id) DO UPDATE SET epoch = writer.epoch + 1, holder = $1, acquired_at = now()
		RETURNING epoch`, holder).Scan(&epoch)
	if err != nil {
		_ = conn.Raw(func(any) error { return driver.ErrBadConn })
		conn.Close()
		return nil, err
	}
	return &Writer{conn: conn, Epoch: epoch}, nil
}

// Load returns the last committed snapshot, including failure intents saved
// before import completes. Nil means no snapshot has committed yet. Reads
// use the writer session as well as writes.
func (w *Writer) Load(ctx context.Context) ([]byte, error) {
	var data []byte
	err := w.Tx(ctx, func(tx *sql.Tx) error {
		err := tx.QueryRowContext(ctx, `SELECT state FROM reviewer_state WHERE id = 1`).Scan(&data)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		return err
	})
	return data, err
}

// Save atomically commits all gate observations and projection intents. The
// single row makes import and cross-PR shared-head observations indivisible.
func (w *Writer) Save(ctx context.Context, data []byte) error {
	return w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `INSERT INTO reviewer_state (id, state) VALUES (1, $1::jsonb)
			ON CONFLICT (id) DO UPDATE SET state = excluded.state`, string(data))
		return err
	})
}

// Check confirms this writer still holds the lock at its epoch.
func (w *Writer) Check(ctx context.Context) error {
	if w == nil {
		return ErrNotWriter
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn == nil {
		return ErrNotWriter
	}
	var epoch int64
	if err := w.conn.QueryRowContext(ctx, writerFence, writerLockKey).Scan(&epoch); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("%w: %v", ErrNotWriter, err)
	}
	if epoch != w.Epoch {
		return ErrNotWriter
	}
	return nil
}

// Tx commits authority-sensitive changes on the advisory-lock connection. The
// writer row lock prevents an epoch change until this transaction has finished.
// The mutex serializes use of that one PostgreSQL session. Callbacks must not
// call Writer.Check, Writer.Tx, or Writer.Release while holding it.
func (w *Writer) Tx(ctx context.Context, fn func(*sql.Tx) error) error {
	if w == nil {
		return ErrNotWriter
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn == nil {
		return ErrNotWriter
	}
	lost := func(err error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("%w: %v", ErrNotWriter, err)
	}
	tx, err := w.conn.BeginTx(ctx, nil)
	if err != nil {
		return lost(err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after commit
	var epoch int64
	if err := tx.QueryRowContext(ctx, writerFence+` FOR UPDATE`, writerLockKey).Scan(&epoch); err != nil {
		return lost(err)
	}
	if epoch != w.Epoch {
		return ErrNotWriter
	}
	if err := fn(tx); err != nil {
		var state interface{ SQLState() string }
		if errors.Is(err, driver.ErrBadConn) || errors.Is(err, sql.ErrConnDone) || errors.Is(err, sql.ErrTxDone) ||
			(errors.As(err, &state) && (state.SQLState() == "57P01" || len(state.SQLState()) >= 2 && state.SQLState()[:2] == "08")) {
			return lost(err)
		}
		return err
	}
	if err := tx.Commit(); err != nil {
		return lost(err)
	}
	return nil
}

// Release drops the lock by closing its session: driver.ErrBadConn makes
// database/sql discard the connection instead of pooling it.
func (w *Writer) Release() {
	if w == nil {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn != nil {
		_ = w.conn.Raw(func(any) error { return driver.ErrBadConn })
		w.conn.Close()
		w.conn = nil
	}
}
