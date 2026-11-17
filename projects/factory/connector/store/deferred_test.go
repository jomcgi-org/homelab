package store_test

import (
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestEnqueueDeferredOnlyRecoversRollbackAbandonment(t *testing.T) {
	for _, tc := range []struct {
		name, kind, reason string
		delivered, recover bool
	}{
		{"rollback", store.OutboxDeferredStart, "unknown kind deferred_start", false, true},
		{"stopped", store.OutboxDeferredStart, "deferred issue stopped", false, false},
		{"binding changed", store.OutboxDeferredStart, "deferred binding changed", false, false},
		{"ineligible", store.OutboxDeferredStart, "itsaplan: conflict", false, false},
		{"retries exhausted", store.OutboxDeferredStart, "connection refused", false, false},
		{"delivered", store.OutboxDeferredStart, "unknown kind deferred_start", true, false},
		{"other kind", store.OutboxComment, "unknown kind deferred_start", false, false},
		{"pending", store.OutboxDeferredStart, "", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			st, ctx := open(t), t.Context()
			now := time.Date(2026, 11, 16, 0, 0, 0, 0, time.UTC)
			st.Now = func() time.Time { return now }
			const key = "deferred/118/2026-11-16"
			if err := st.Enqueue(ctx, st.DB, tc.kind, key, 0, "", map[string]string{"delegate": "original"}); err != nil {
				t.Fatal(err)
			}
			var id int64
			if err := st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id); err != nil {
				t.Fatal(err)
			}
			if err := st.OutboxSendStarted(ctx, id); err != nil {
				t.Fatal(err)
			}
			if err := st.OutboxFailed(ctx, id, "waiting", now.Add(time.Hour)); err != nil {
				t.Fatal(err)
			}
			if tc.reason != "" {
				if err := st.OutboxAbandon(ctx, st.DB, id, tc.reason); err != nil {
					t.Fatal(err)
				}
			}
			if tc.delivered {
				if err := st.OutboxDelivered(ctx, st.DB, id); err != nil {
					t.Fatal(err)
				}
			}
			for i := 0; i < 2; i++ {
				if err := st.EnqueueDeferred(ctx, st.DB, key, 118, map[string]string{"delegate": "replacement"}); err != nil {
					t.Fatal(err)
				}
			}
			pending, err := st.PendingOutbox(ctx, now, 10)
			if err != nil || (len(pending) == 1) != tc.recover {
				t.Fatalf("pending = %v, err = %v", pending, err)
			}
			if tc.recover && (pending[0].ID != id || pending[0].Tries != 1 || !pending[0].SendStarted) {
				t.Fatalf("recovery changed the receipt: %+v", pending[0])
			}
			var count int
			if err := st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE dedupe_key = $1 AND payload->>'delegate' = 'original'`, key).Scan(&count); err != nil || count != 1 {
				t.Fatalf("original requests = %d, err = %v", count, err)
			}
		})
	}
}

func TestEnqueueDeferredPreservesStopAcrossRollback(t *testing.T) {
	f := newRecoveryFixture(t)
	const key = "deferred/101/2026-11-15"
	f.must(f.st.EnqueueDeferred(f.ctx, f.st.DB, key, f.work.IssueID, map[string]string{"delegate": "coder"}))
	var id int64
	f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id))
	f.must(f.st.OutboxAbandon(f.ctx, f.st.DB, id, "unknown kind deferred_start"))
	_, err := f.st.StopWork(f.ctx, f.work.ID, "operator stop", "operator")
	f.must(err)
	// Neither the existing row nor a fresh date may bypass the stored stop.
	for _, date := range []string{"2026-11-15", "2026-11-16"} {
		f.must(f.st.EnqueueDeferred(f.ctx, f.st.DB, "deferred/101/"+date, f.work.IssueID, nil))
	}
	var count int
	f.must(f.st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE kind = 'deferred_start' AND abandoned_at IS NULL`).Scan(&count))
	if count != 0 {
		t.Fatalf("stopped starts = %d", count)
	}
}

func TestEnqueueDeferredPreservesUnlinkedStopAcrossRollback(t *testing.T) {
	for _, state := range []string{store.RunWaiting, store.RunClosed} {
		t.Run(state, func(t *testing.T) {
			st, ctx := open(t), t.Context()
			const key = "deferred/118/2026-11-16"
			if err := st.EnqueueDeferred(ctx, st.DB, key, 118, nil); err != nil {
				t.Fatal(err)
			}
			var id int64
			if err := st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id); err != nil {
				t.Fatal(err)
			}
			if err := st.OutboxAbandon(ctx, st.DB, id, "unknown kind deferred_start"); err != nil {
				t.Fatal(err)
			}
			if _, _, err := st.RecordClaim(ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", IssueID: 118, IssueKey: "HL-118", Attempt: 1}); err != nil {
				t.Fatal(err)
			}
			if stopped, err := st.StopIssue(ctx, "HL-118", "operator stop", "operator"); err != nil || stopped != (store.IssueStop{HeldRuns: 1}) {
				t.Fatalf("cancel = %+v, %v", stopped, err)
			}
			if err := st.SetRunState(ctx, st.DB, 1, state, "operator stop"); err != nil {
				t.Fatal(err)
			}
			// Neither rollback recovery nor a different date clears this stop.
			for _, date := range []string{"2026-11-16", "2026-11-17"} {
				if err := st.EnqueueDeferred(ctx, st.DB, "deferred/118/"+date, 118, nil); err != nil {
					t.Fatal(err)
				}
			}
			var pending int
			if err := st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE kind = 'deferred_start' AND abandoned_at IS NULL`).Scan(&pending); err != nil || pending != 0 {
				t.Fatalf("stopped starts = %d, %v", pending, err)
			}
			if stopped, err := st.DeferredStopped(ctx, 118); err != nil || !stopped {
				t.Fatalf("unlinked stop = %v, %v", stopped, err)
			}
			// The stop belongs to this issue; other deferred work still starts.
			if err := st.EnqueueDeferred(ctx, st.DB, "deferred/119/2026-11-17", 119, nil); err != nil {
				t.Fatal(err)
			}
			if err := st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE dedupe_key = 'deferred/119/2026-11-17' AND abandoned_at IS NULL`).Scan(&pending); err != nil || pending != 1 {
				t.Fatalf("unrelated start = %d, %v", pending, err)
			}
			if stopped, err := st.DeferredStopped(ctx, 119); err != nil || stopped {
				t.Fatalf("unrelated issue stopped = %v, %v", stopped, err)
			}
		})
	}
}
