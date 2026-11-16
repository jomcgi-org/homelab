package store_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func majorWriter(t *testing.T, st *store.Store, holder string) *store.Writer {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		w, err := st.TryAcquireWriter(context.Background(), holder)
		if err != nil {
			t.Fatal(err)
		}
		if w != nil {
			t.Cleanup(w.Release)
			return w
		}
		if time.Now().After(deadline) {
			t.Fatal("writer takeover timed out")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestRenovateReservationsKeepCapAndTombstonesAcrossTakeover(t *testing.T) {
	st, ctx := open(t), context.Background()
	w := majorWriter(t, st, "first")
	const repo = "jomcgi-org/homelab"
	admit := func(number int, want bool) {
		t.Helper()
		var got bool
		err := w.Tx(ctx, func(tx *sql.Tx) error {
			var err error
			got, err = st.AdmitRenovateMajor(ctx, tx, repo, number)
			return err
		})
		if err != nil || got != want {
			t.Fatalf("admit PR %d = %v, %v; want %v", number, got, err, want)
		}
	}
	admit(1, true)
	admit(1, false)
	admit(2, true)
	admit(3, false)
	rows, err := st.RenovateMajors(ctx, repo)
	if err != nil || len(rows) != 2 {
		t.Fatalf("reservations = %+v, %v", rows, err)
	}
	id := rows[0].ID
	if err := w.Tx(ctx, func(tx *sql.Tx) error {
		if err := st.AttachRenovateIssue(ctx, tx, id, 1001, "HOME-1001"); err != nil {
			return err
		}
		return st.StopRenovateMajor(ctx, tx, id)
	}); err != nil {
		t.Fatal(err)
	}
	old := w
	old.Release()
	w = majorWriter(t, st, "replacement")
	if err := old.Tx(ctx, func(tx *sql.Tx) error {
		return st.CloseRenovateMajor(ctx, tx, id)
	}); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("old writer changed a reservation: %v", err)
	}
	m, err := st.RenovateMajorForIssue(ctx, 1001)
	if err != nil || !m.Stopped || m.Closed || m.IssueKey != "HOME-1001" {
		t.Fatalf("takeover lost the issue or its stop: %+v, %v", m, err)
	}
	if n, err := st.RenovateSourcePR(ctx, repo, 1001); err != nil || n != 1 {
		t.Fatalf("source = %d, %v", n, err)
	}
	admit(3, false) // A stopped issue still occupies its slot.
	if err := w.Tx(ctx, func(tx *sql.Tx) error {
		return st.CloseRenovateMajor(ctx, tx, id)
	}); err != nil {
		t.Fatal(err)
	}
	admit(1, false) // Closing releases capacity, never the source identity.
	admit(3, true)
	admit(4, false)
}

func TestRenovateReleaseRequiresAnUnsentUnownedReservation(t *testing.T) {
	for _, state := range []string{"unprepared", "ready", "sent", "refused", "stopped", "closed", "attached"} {
		t.Run(state, func(t *testing.T) {
			st, ctx := open(t), context.Background()
			w := majorWriter(t, st, state)
			const repo, message = "jomcgi-org/homelab", "renovate-major/jomcgi-org/homelab/1/create"
			if err := w.Tx(ctx, func(tx *sql.Tx) error {
				_, err := st.AdmitRenovateMajor(ctx, tx, repo, 1)
				return err
			}); err != nil {
				t.Fatal(err)
			}
			rows, err := st.RenovateMajors(ctx, repo)
			if err != nil || len(rows) != 1 {
				t.Fatalf("reservations = %+v, %v", rows, err)
			}
			id := rows[0].ID
			if err := w.Tx(ctx, func(tx *sql.Tx) error {
				if state != "unprepared" {
					if _, err := st.PrepareRenovateAttempt(ctx, tx, message, w.Epoch, "original brief"); err != nil {
						return err
					}
				}
				switch state {
				case "sent", "refused":
					if err := st.SendRenovateAttempt(ctx, tx, message, w.Epoch); err != nil {
						return err
					}
					if state == "refused" {
						return st.SetRenovateAttempt(ctx, tx, message, "ready", "definitively refused")
					}
				case "stopped":
					return st.StopRenovateMajor(ctx, tx, id)
				case "closed":
					return st.CloseRenovateMajor(ctx, tx, id)
				case "attached":
					return st.AttachRenovateIssue(ctx, tx, id, 1001, "HOME-1001")
				}
				return nil
			}); err != nil {
				t.Fatal(err)
			}
			if err := w.Tx(ctx, func(tx *sql.Tx) error {
				return st.ReleaseUnsentRenovateMajor(ctx, tx, id, message)
			}); err != nil {
				t.Fatal(err)
			}
			rows, err = st.RenovateMajors(ctx, repo)
			released := state == "unprepared" || state == "ready"
			if err != nil || (len(rows) == 0) != released {
				t.Fatalf("release=%v, reservations=%+v, %v", released, rows, err)
			}
			var attemptRemains bool
			if err := st.DB.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM renovate_attempt WHERE message_id=$1)`, message).Scan(&attemptRemains); err != nil {
				t.Fatal(err)
			}
			if attemptRemains == released {
				t.Fatal("reservation and attempt were not retained or released together")
			}
			if released {
				if err := w.Tx(ctx, func(tx *sql.Tx) error {
					admitted, err := st.AdmitRenovateMajor(ctx, tx, repo, 1)
					if err == nil && !admitted {
						t.Error("a released source could not be admitted again")
					}
					return err
				}); err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}

func TestRenovateAttemptPayloadAndSendEvidenceSurviveTakeover(t *testing.T) {
	st, ctx := open(t), context.Background()
	w := majorWriter(t, st, "original")
	const message = "renovate-major/jomcgi-org/homelab/1/create"
	prepare := func(payload, state string, tries int) {
		t.Helper()
		var a *store.RenovateAttempt
		err := w.Tx(ctx, func(tx *sql.Tx) error {
			var err error
			a, err = st.PrepareRenovateAttempt(ctx, tx, message, w.Epoch, payload)
			return err
		})
		if err != nil || a.State != state || a.Tries != tries {
			t.Fatalf("attempt = %+v, %v; want %s with %d tries", a, err, state, tries)
		}
		var persisted string
		if err := json.Unmarshal(a.Payload, &persisted); err != nil || persisted != "original brief" {
			t.Fatalf("prepared payload changed: %s, %v", a.Payload, err)
		}
	}
	prepare("original brief", "ready", 0)
	rollback := errors.New("interrupted transaction")
	if err := w.Tx(ctx, func(tx *sql.Tx) error {
		if err := st.SendRenovateAttempt(ctx, tx, message, w.Epoch); err != nil {
			return err
		}
		return rollback
	}); !errors.Is(err, rollback) {
		t.Fatal(err)
	}
	prepare("changed before sending", "ready", 0)
	if err := w.Tx(ctx, func(tx *sql.Tx) error {
		return st.SendRenovateAttempt(ctx, tx, message, w.Epoch)
	}); err != nil {
		t.Fatal(err)
	}
	w.Release()
	w = majorWriter(t, st, "replacement")
	prepare("changed after takeover", "sent", 1)
	if err := w.Tx(ctx, func(tx *sql.Tx) error {
		return st.SetRenovateAttempt(ctx, tx, message, "ready", "definitively refused")
	}); err != nil {
		t.Fatal(err)
	}
	prepare("changed after refusal", "ready", 1)
	if err := w.Tx(ctx, func(tx *sql.Tx) error {
		return st.SendRenovateAttempt(ctx, tx, message, w.Epoch)
	}); err != nil {
		t.Fatal(err)
	}
	var epoch int64
	if err := st.DB.QueryRowContext(ctx, `SELECT writer_epoch FROM renovate_attempt WHERE message_id=$1`, message).Scan(&epoch); err != nil || epoch != w.Epoch {
		t.Fatalf("send epoch = %d, %v; want %d", epoch, err, w.Epoch)
	}
	if err := w.Tx(ctx, func(tx *sql.Tx) error {
		return st.SetRenovateAttempt(ctx, tx, message, "done", "")
	}); err != nil {
		t.Fatal(err)
	}
	prepare("changed after acknowledgement", "done", 2)
}
