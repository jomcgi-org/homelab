package guestmem

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/guestagent"
)

type fetchFunc func(context.Context, string, string) (guestagent.MemoryStatus, error)

func (f fetchFunc) Fetch(c context.Context, u, n string) (guestagent.MemoryStatus, error) {
	return f(c, u, n)
}

func TestActivationStateMachine(t *testing.T) {
	now := time.Unix(100, 0)
	kills, calls := uint64(8), 0
	var fetchErr error
	var events []Event
	p := New(Options{Enabled: true, Interval: time.Second, Now: func() time.Time { return now }, Event: func(e Event) { events = append(events, e) }, Client: fetchFunc(func(_ context.Context, uds, nonce string) (guestagent.MemoryStatus, error) {
		calls++
		if uds != "host-uds" {
			t.Fatal("wrong UDS")
		}
		return goodSample(nonce, kills), fetchErr
	})})
	target := Target{VM: "bundle", Workload: "host-workload", Activation: "first", UDS: "host-uds"}
	p.Track(target)
	assertState := func(state State, oom uint64) {
		t.Helper()
		a := p.Snapshot()
		if a.Counts[string(state)] != 1 || a.GuestOOM != oom || len(a.Counts) != 5 {
			t.Fatalf("aggregates=%+v want=%s oom=%d", a, state, oom)
		}
	}
	assertState(Pending, 0)
	p.Poll(context.Background())
	assertState(OK, 0) // snapshot-carried counter is a baseline, not an event
	kills = 10
	p.Poll(context.Background())
	p.Poll(context.Background())
	assertState(OK, 2)
	if len(events) != 1 || events[0].Kind != "guest_oom" || events[0].Delta != 2 || events[0].Target != target {
		t.Fatalf("events=%+v", events)
	}
	fetchErr = errors.New("timeout")
	now = now.Add(time.Second)
	p.Poll(context.Background())
	assertState(Error, 2)
	now = now.Add(2 * time.Second)
	assertState(Stale, 2)
	p.Poll(context.Background())
	p.Poll(context.Background())
	if len(events) != 2 || events[1].Kind != "stale" {
		t.Fatalf("repeated stale events: %+v", events)
	}
	fetchErr = nil
	p.Poll(context.Background())
	assertState(OK, 2)
	p.Forget(target.VM, target.Activation)
	target.Activation = "restored"
	p.Track(target)
	assertState(Pending, 2)
	kills = 999
	p.Poll(context.Background())
	assertState(OK, 2)           // new activation resets the counter baseline
	p.Forget(target.VM, "first") // late old teardown must not erase restore
	assertState(OK, 2)
	fetchErr = ErrUnsupported
	p.Poll(context.Background())
	assertState(Unsupported, 2)
	before := calls
	for i := 0; i < 10; i++ {
		p.Poll(context.Background())
		now = now.Add(time.Hour)
	}
	assertState(Unsupported, 2)
	if calls != before || len(events) != 3 {
		t.Fatal("unsupported was repeatedly probed or logged")
	}
}

func TestDisabledNeverDials(t *testing.T) {
	p := New(Options{Client: Client{Dial: func(context.Context, string) (conn net.Conn, err error) { t.Fatal("disabled dialed"); return }}})
	p.Track(Target{VM: "vm", Activation: "a", UDS: "uds"})
	p.Poll(context.Background())
	p.Run(context.Background())
	if p.Snapshot().Counts["pending"] != 0 {
		t.Fatal("disabled tracked VM")
	}
}

func TestInvalidEvidenceNeverOKOrOOM(t *testing.T) {
	for _, kind := range []string{"error", "malformed", "wrong nonce", "counter backwards"} {
		t.Run(kind, func(t *testing.T) {
			bad := false
			p := New(Options{Enabled: true, Client: fetchFunc(func(_ context.Context, _, n string) (guestagent.MemoryStatus, error) {
				m := goodSample(n, 5)
				if bad {
					switch kind {
					case "error":
						return m, errors.New("failed")
					case "malformed":
						m.OOMSupported = nil
					case "wrong nonce":
						m.Nonce = "other"
					case "counter backwards":
						m = goodSample(n, 4)
					}
				}
				return m, nil
			})})
			p.Track(Target{VM: "vm", Activation: "a"})
			p.Poll(context.Background())
			bad = true
			p.Poll(context.Background())
			a := p.Snapshot()
			if a.Counts["error"] != 1 || a.GuestOOM != 0 {
				t.Fatalf("invalid evidence=%+v", a)
			}
		})
	}
}

func TestRestoreRejectsInFlightOldReport(t *testing.T) {
	started, finish := make(chan struct{}), make(chan struct{})
	p := New(Options{Enabled: true, Client: fetchFunc(func(ctx context.Context, _, n string) (guestagent.MemoryStatus, error) {
		close(started)
		<-finish
		if ctx.Err() == nil {
			t.Error("restore did not cancel request")
		}
		return goodSample(n, 999), nil
	})})
	p.Track(Target{VM: "vm", Activation: "old"})
	done := make(chan struct{})
	go func() { p.Poll(context.Background()); close(done) }()
	<-started
	p.Track(Target{VM: "vm", Activation: "new"})
	close(finish)
	<-done
	a := p.Snapshot()
	if a.Counts["pending"] != 1 || a.GuestOOM != 0 {
		t.Fatalf("old report contaminated restore: %+v", a)
	}
}
