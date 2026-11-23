package dispatch_test

import (
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// setCapacity writes the worker reaper's reading of the binding's pool, as of
// now by the database's clock.
func (v *env) setCapacity(ready, busy int, age time.Duration) {
	v.t.Helper()
	if _, err := v.st.DB.Exec(`INSERT INTO worker_pool_capacity (namespace, pool, ready, busy, observed_at)
		VALUES ('factory-homelab', 'coder', $1, $2, now() - $3 * interval '1 second')
		ON CONFLICT (namespace, pool) DO UPDATE SET ready = $1, busy = $2, observed_at = excluded.observed_at`,
		ready, busy, age.Seconds()); err != nil {
		v.t.Fatal(err)
	}
}

func (v *env) attemptState(run int64) string {
	v.t.Helper()
	var state string
	if err := v.st.DB.QueryRow(`SELECT state FROM delivery_attempt WHERE run_id = $1 ORDER BY seq DESC LIMIT 1`, run).Scan(&state); err != nil {
		v.t.Fatal(err)
	}
	return state
}

// A send for a new session waits while the pool has no free worker. It is held
// before it is issued: no kagent call, no ambiguous attempt, and the attempt
// stays `sending`, which the pool's pending demand counts once the connector
// publishes it (#8591). It goes once a
// worker is free.
func TestSendWaitsForAFreeWorkerAndThenGoes(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.WorkerWait = 5 * time.Minute
	v.e = v.engine("connector-a")
	v.its.add(1, "coder", 101, "HL-7", "Fix the flaky test.")

	// Scaled to zero: no ready worker.
	v.setCapacity(0, 0, 0)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 0 {
		t.Fatalf("sends = %d with no worker, want the send held", n)
	}
	if got := v.attemptState(1); got != "sending" {
		t.Fatalf("held attempt = %q, want sending (never issued)", got)
	}

	// One worker is ready and busy: still no free worker for a new session.
	v.setCapacity(1, 1, 0)
	v.clk.Advance(30 * time.Second)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 0 {
		t.Fatalf("sends = %d with every worker busy, want the send held", n)
	}

	v.setCapacity(2, 1, 0)
	v.clk.Advance(30 * time.Second)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 1 {
		t.Fatalf("sends = %d with a free worker, want 1", n)
	}
}

// A wait ends at the bound, however long the pool stays empty: the send then
// goes as it did before the wait existed.
func TestSendWaitIsBounded(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.WorkerWait = 5 * time.Minute
	v.e = v.engine("connector-a")
	v.its.add(1, "coder", 101, "HL-7", "Fix the flaky test.")

	v.setCapacity(0, 0, 0)
	v.tick(v.e)
	v.clk.Advance(4 * time.Minute)
	v.setCapacity(0, 0, 0)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 0 {
		t.Fatalf("sends = %d inside the bound, want the send held", n)
	}
	v.clk.Advance(time.Minute)
	v.setCapacity(0, 0, 0)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 1 {
		t.Fatalf("sends = %d past the bound, want 1", n)
	}
}

// With no reading, or one the reaper wrote too long ago, the pool's state is
// unknown and the send goes at once.
func TestSendDoesNotWaitOnAMissingOrStaleReading(t *testing.T) {
	for name, setup := range map[string]func(*env){
		"no reading":    func(*env) {},
		"stale reading": func(v *env) { v.setCapacity(0, 0, 2*time.Minute) },
	} {
		t.Run(name, func(t *testing.T) {
			v := newEnv(t)
			v.e.Release()
			v.cfg.WorkerWait = 5 * time.Minute
			v.e = v.engine("connector-a")
			setup(v)
			v.its.add(1, "coder", 101, "HL-7", "Fix the flaky test.")
			v.tick(v.e)
			if n := len(v.kg.Sends()); n != 1 {
				t.Fatalf("sends = %d, want 1", n)
			}
		})
	}
}

// Without WorkerWait the connector reads no capacity: behavior is unchanged.
func TestSendIgnoresCapacityWhenTheWaitIsOff(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-7", "Fix the flaky test.")
	v.setCapacity(0, 0, 0)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 1 {
		t.Fatalf("sends = %d with the wait off, want 1", n)
	}
}

// Two new sessions in one reaper interval share one free worker: the first
// takes it, and the reaper's row does not show it until its next pass, so the
// connector subtracts the sends it let through itself.
func TestSendsLetThroughAreReservedUntilTheReaperCountsThem(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.WorkerWait = 5 * time.Minute
	v.e = v.engine("connector-a")
	v.its.add(1, "coder", 101, "HL-7", "Fix the flaky test.")
	v.its.add(2, "coder", 102, "HL-8", "Fix another test.")

	v.setCapacity(1, 0, 0) // one ready worker, no actor yet
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 1 {
		t.Fatalf("sends = %d with one free worker for two runs, want 1", n)
	}
	// The same reading, now 5 s old: the first send already took the worker.
	v.clk.Advance(5 * time.Second)
	v.setCapacity(1, 0, 5*time.Second)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 1 {
		t.Fatalf("sends = %d before the reaper counts the first, want 1", n)
	}
	// The reaper's next pass counts it, and a second worker is ready.
	v.clk.Advance(10 * time.Second)
	v.setCapacity(2, 1, 0)
	v.tick(v.e)
	if n := len(v.kg.Sends()); n != 2 {
		t.Fatalf("sends = %d once a second worker is free, want 2", n)
	}
}

// A new session needs a worker with no actor. A continuation needs only a
// ready worker, because its actor may already hold one.
func TestWorkerWaitByAttemptKind(t *testing.T) {
	for _, c := range []struct {
		name        string
		kind        string
		ready, busy int
		hold        bool
	}{
		{"initial, none ready", "initial", 0, 0, true},
		{"initial, all busy", "initial", 2, 2, true},
		{"initial, one free", "initial", 2, 1, false},
		{"continuation, none ready", "continuation", 0, 0, true},
		{"continuation, all busy", "continuation", 2, 2, false},
		{"continuation, one free", "continuation", 2, 1, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			v := newEnv(t)
			v.e.Release()
			v.cfg.WorkerWait = 5 * time.Minute
			v.e = v.engine("connector-a")
			v.setCapacity(c.ready, c.busy, 0)
			a := &store.Attempt{ID: 1, RunID: 1, Kind: c.kind, KagentNamespace: "factory-homelab", KagentAgent: "coder"}
			if got := v.e.AwaitWorker(v.ctx, a); got != c.hold {
				t.Fatalf("hold = %v, want %v", got, c.hold)
			}
		})
	}
}

// The wait keeps nothing for a run that is no longer open.
func TestWorkerWaitStatePrunesWithItsRun(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.WorkerWait = 5 * time.Minute
	v.e = v.engine("connector-a")
	v.setCapacity(0, 0, 0)
	held := &store.Attempt{ID: 1, RunID: 10, Kind: "initial", KagentNamespace: "factory-homelab", KagentAgent: "coder"}
	if !v.e.AwaitWorker(v.ctx, held) {
		t.Fatal("no hold on an empty pool")
	}
	v.setCapacity(3, 0, 0)
	through := &store.Attempt{ID: 2, RunID: 20, Kind: "initial", KagentNamespace: "factory-homelab", KagentAgent: "coder"}
	if v.e.AwaitWorker(v.ctx, through) {
		t.Fatal("held with free workers")
	}
	if w, r := v.e.WorkerHolds(); w != 1 || r != 1 {
		t.Fatalf("holds = %d waiting, %d released; want 1 and 1", w, r)
	}
	v.e.PruneWorkerHolds(map[int64]bool{20: true})
	if w, r := v.e.WorkerHolds(); w != 0 || r != 1 {
		t.Fatalf("after run 10 closed: %d waiting, %d released; want 0 and 1", w, r)
	}
	v.e.PruneWorkerHolds(map[int64]bool{})
	if w, r := v.e.WorkerHolds(); w != 0 || r != 0 {
		t.Fatalf("after every run closed: %d waiting, %d released; want none", w, r)
	}
}
