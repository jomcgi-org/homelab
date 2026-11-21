package dispatch

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestFormerWriterCannotQueueOpenAttemptStop(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer st.DB.Close()
	first, err := st.TryAcquireWriter(ctx, "former")
	if err != nil || first == nil {
		t.Fatalf("acquire former writer: %v", err)
	}
	defer first.Release()
	run, _, err := st.RecordWriterClaim(ctx, first, store.ClaimedRun{
		ID: 1, AgentKey: "coder", IssueID: 101, Attempt: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = st.DB.ExecContext(ctx, `
		INSERT INTO delivery_attempt (run_id,seq,run_attempt,work_item_epoch,
			decision_id,kind,message_id,kagent_namespace,kagent_agent,state,writer_epoch)
		VALUES ($1,1,1,1,'test','initial','open-attempt','ns','coder','accepted',$2)`, run.ID, first.Epoch)
	if err != nil {
		t.Fatal(err)
	}

	first.Release()
	next, err := st.TryAcquireWriter(ctx, "successor")
	if err != nil || next == nil {
		t.Fatalf("acquire successor: %v", err)
	}
	defer next.Release()
	former := &Engine{st: st, w: first}
	if err := former.stopRun(ctx, run, "stopped"); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("former stop = %v, want ErrNotWriter", err)
	}
	pending, err := st.PendingCancellations(ctx)
	if err != nil || len(pending) != 0 {
		t.Fatalf("former writer queued cancellation: %+v, %v", pending, err)
	}
	successor := &Engine{st: st, w: next}
	if err := successor.stopRun(ctx, run, "stopped"); err != nil {
		t.Fatal(err)
	}
	pending, err = st.PendingCancellations(ctx)
	if err != nil || len(pending) != 1 || pending[0].Reason != store.CancelRoundStopped {
		t.Fatalf("successor cancellation = %+v, %v", pending, err)
	}
}

func TestFormerWriterCannotClearQuotaWait(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer st.DB.Close()
	first, err := st.TryAcquireWriter(ctx, "former")
	if err != nil || first == nil {
		t.Fatalf("acquire former writer: %v", err)
	}
	defer first.Release()
	const scope = "kagent:ns/coder"
	if _, err := st.DB.ExecContext(ctx, `
		INSERT INTO grant_block (scope,class,reason,created_at)
		VALUES ($1,'quota','exhausted',now()-interval '1 minute')`, scope); err != nil {
		t.Fatal(err)
	}
	first.Release()
	next, err := st.TryAcquireWriter(ctx, "successor")
	if err != nil || next == nil {
		t.Fatalf("acquire successor: %v", err)
	}
	defer next.Release()
	before := time.Now()
	if _, err := st.ClearQuotaBlocks(ctx, first, before); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("former clear = %v, want ErrNotWriter", err)
	}
	blocked, _, err := st.GrantBlocked(ctx, scope)
	if err != nil || !blocked {
		t.Fatalf("grant blocked = %v, %v", blocked, err)
	}
	cleared, err := st.ClearQuotaBlocks(ctx, next, before)
	if err != nil || len(cleared) != 1 || cleared[0] != scope {
		t.Fatalf("successor cleared = %v, %v", cleared, err)
	}
}

type delayedResultFailure struct {
	Itsaplan
	started chan struct{}
	release chan struct{}
}

func (f *delayedResultFailure) Result(ctx context.Context, _ string, _ int64, _ itsaplan.Result) error {
	close(f.started)
	select {
	case <-f.release:
		return errors.New("result transport failed")
	case <-ctx.Done():
		return ctx.Err()
	}
}

func TestFormerWriterCannotAbandonFinishingResult(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	st, err := store.Open(pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer st.DB.Close()
	first, err := st.TryAcquireWriter(ctx, "former")
	if err != nil || first == nil {
		t.Fatalf("acquire former writer: %v", err)
	}
	defer first.Release()
	run, _, err := st.RecordWriterClaim(ctx, first, store.ClaimedRun{
		ID: 1, AgentKey: "coder", IssueID: 101, Attempt: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	failure := &delayedResultFailure{started: make(chan struct{}), release: make(chan struct{})}
	former := New(Config{}, st, failure, nil, nil, nil, nil, nil)
	former.w = first
	if err := former.finishRun(ctx, st.DB, run, itsaplan.Result{Status: "success"}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB.ExecContext(ctx, `UPDATE outbox SET tries = $1 WHERE run_id = $2`, outboxMaxTries-1, run.ID); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- former.flushOutbox(ctx) }()
	select {
	case <-failure.started:
	case <-ctx.Done():
		t.Fatal("result delivery did not start")
	}
	first.Release()
	next, err := st.TryAcquireWriter(ctx, "successor")
	if err != nil || next == nil {
		t.Fatalf("acquire successor: %v", err)
	}
	defer next.Release()
	close(failure.release)
	select {
	case err := <-done:
		if !errors.Is(err, store.ErrNotWriter) {
			t.Fatalf("former flush = %v, want ErrNotWriter", err)
		}
	case <-ctx.Done():
		t.Fatal("former flush did not return")
	}
	current := &Engine{st: st, w: next}
	if err := current.checkFinishing(ctx, run); err != nil {
		t.Fatal(err)
	}
	phases, err := current.RunPhases(ctx, run.IssueID)
	if err != nil || len(phases) != 1 || phases[0].Phase != store.RunFinishing {
		t.Fatalf("phases after late failure = %+v, %v", phases, err)
	}
	queued, err := st.PendingOutbox(ctx, time.Now(), 1)
	if err != nil || len(queued) != 1 || queued[0].Tries != outboxMaxTries-1 {
		t.Fatalf("successor result queue = %+v, %v", queued, err)
	}
}
