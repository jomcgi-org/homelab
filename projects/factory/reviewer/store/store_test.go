package store

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/reviewer/migrations"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

func openTestStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(pgtest.NewDB(t, migrations.Apply))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.DB.Close() })
	return s
}

func acquire(t *testing.T, s *Store) *Writer {
	t.Helper()
	w, err := s.TryAcquireWriter(context.Background(), t.Name())
	if err != nil || w == nil {
		t.Fatalf("acquire writer: %v", err)
	}
	t.Cleanup(w.Release)
	return w
}

func TestSnapshotSurvivesWriterTakeover(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	if err := migrations.Apply(ctx, s.DB); err != nil {
		t.Fatal(err)
	}
	w := acquire(t, s)
	if data, err := w.Load(ctx); err != nil || data != nil {
		t.Fatalf("empty state = %s, %v", data, err)
	}
	if other, err := s.TryAcquireWriter(ctx, "other"); err != nil || other != nil {
		t.Fatalf("second writer = %v, %v", other, err)
	}
	if err := w.Save(ctx, []byte(`{"imported": true}`)); err != nil {
		t.Fatal(err)
	}
	w.Release()
	if err := w.Save(ctx, []byte(`{}`)); !errors.Is(err, ErrNotWriter) {
		t.Fatalf("released write: %v", err)
	}
	next := acquire(t, s)
	if next.Epoch <= w.Epoch {
		t.Fatal("epoch did not advance")
	}
	data, err := next.Load(ctx)
	if err != nil || string(data) != `{"imported": true}` {
		t.Fatalf("state = %s, %v", data, err)
	}
}

func TestLostLockAndEpochStopWrites(t *testing.T) {
	for _, loss := range []string{"unlock", "epoch", "connection"} {
		t.Run(loss, func(t *testing.T) {
			s := openTestStore(t)
			w := acquire(t, s)
			ctx := context.Background()
			var err error
			switch loss {
			case "unlock":
				_, err = w.conn.ExecContext(ctx, `SELECT pg_advisory_unlock_all()`)
			case "epoch":
				_, err = s.DB.ExecContext(ctx, `UPDATE writer SET epoch = epoch + 1 WHERE id = 1`)
			case "connection":
				var pid int
				if err = w.conn.QueryRowContext(ctx, `SELECT pg_backend_pid()`).Scan(&pid); err == nil {
					_, err = s.DB.ExecContext(ctx, `SELECT pg_terminate_backend($1, 5000)`, pid)
				}
			}
			if err != nil {
				t.Fatal(err)
			}
			if err := w.Check(ctx); !errors.Is(err, ErrNotWriter) {
				t.Fatalf("check after loss: %v", err)
			}
			if err := w.Save(ctx, []byte(`{}`)); !errors.Is(err, ErrNotWriter) {
				t.Fatalf("write after loss: %v", err)
			}
			var count int
			if err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM reviewer_state`).Scan(&count); err != nil {
				t.Fatal(err)
			}
			if count != 0 {
				t.Fatal("fenced writer committed state")
			}
		})
	}
}
