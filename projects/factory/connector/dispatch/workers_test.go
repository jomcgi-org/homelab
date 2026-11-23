package dispatch_test

import (
	"testing"
	"time"
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
// stays `sending`, which the pool's pending demand counts. It goes once a
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
