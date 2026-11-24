package detect

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type fake struct {
	name   string
	detect func(ctx context.Context, in Input) ([]Finding, error)
}

func (f *fake) Name() string { return f.name }
func (f *fake) Detect(ctx context.Context, in Input) ([]Finding, error) {
	return f.detect(ctx, in)
}

func cfg(mode Mode, fm FailureMode) Config {
	return Config{Name: "fake", Mode: mode, FailureMode: fm, Limits: Limits{MaxInputBytes: 100, Deadline: 50 * time.Millisecond}}
}

func in(s string) Input { return Input{Bytes: []byte(s), Source: "body"} }

func TestVerdicts(t *testing.T) {
	block := []Finding{{RuleID: "r"}} // an empty class defaults to block
	report := []Finding{{RuleID: "r", Class: ClassReport}}
	cases := []struct {
		name    string
		det     func(context.Context, Input) ([]Finding, error)
		cfg     Config
		outcome Outcome
		failure Failure
		deny    bool
	}{
		{"clean", func(context.Context, Input) ([]Finding, error) { return nil, nil }, cfg(ModeEnforce, FailDeny), OutcomeClean, "", false},
		{"block finding denies", func(context.Context, Input) ([]Finding, error) { return block, nil }, cfg(ModeEnforce, FailDeny), OutcomeBlocked, "", true},
		{"block finding in observe mode", func(context.Context, Input) ([]Finding, error) { return block, nil }, cfg(ModeObserve, FailDeny), OutcomeBlocked, "", false},
		{"report finding never denies", func(context.Context, Input) ([]Finding, error) { return report, nil }, cfg(ModeEnforce, FailDeny), OutcomeClean, "", false},
		{"budget denies", func(context.Context, Input) ([]Finding, error) { return nil, ErrBudget }, cfg(ModeEnforce, FailDeny), OutcomeFailed, FailureBudget, true},
		{"error denies", func(context.Context, Input) ([]Finding, error) { return nil, errors.New("x") }, cfg(ModeEnforce, FailDeny), OutcomeFailed, FailureError, true},
		{"error allowed by failure mode", func(context.Context, Input) ([]Finding, error) { return nil, errors.New("x") }, cfg(ModeEnforce, FailAllow), OutcomeFailed, FailureError, false},
		{"error in observe mode", func(context.Context, Input) ([]Finding, error) { return nil, errors.New("x") }, cfg(ModeObserve, FailDeny), OutcomeFailed, FailureError, false},
		{"panic is a failure", func(context.Context, Input) ([]Finding, error) { panic("boom") }, cfg(ModeEnforce, FailDeny), OutcomeFailed, FailurePanic, true},
		{"a detector that ignores its context times out", func(ctx context.Context, _ Input) ([]Finding, error) {
			time.Sleep(time.Second)
			return nil, nil
		}, cfg(ModeEnforce, FailDeny), OutcomeFailed, FailureTimeout, true},
		{"a context error is a timeout", func(ctx context.Context, _ Input) ([]Finding, error) {
			<-ctx.Done()
			return nil, ctx.Err()
		}, cfg(ModeEnforce, FailDeny), OutcomeFailed, FailureTimeout, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			start := time.Now()
			v := NewHost(&fake{name: "fake", detect: tc.det}, tc.cfg).Scan(context.Background(), in("x"))
			if v.Outcome != tc.outcome || v.Failure != tc.failure || v.Deny != tc.deny {
				t.Errorf("got %s, want %s failure=%q deny=%t", v, tc.outcome, tc.failure, tc.deny)
			}
			if time.Since(start) > 500*time.Millisecond {
				t.Errorf("the host held the call for %s", time.Since(start))
			}
			for _, f := range v.Findings {
				if f.Detector != "fake" || f.Class == "" {
					t.Errorf("finding not completed by the host: %+v", f)
				}
			}
		})
	}
}

func TestOversizeIsAFailureNotASkip(t *testing.T) {
	var calls atomic.Int32
	h := NewHost(&fake{name: "fake", detect: func(context.Context, Input) ([]Finding, error) {
		calls.Add(1)
		return nil, nil
	}}, cfg(ModeEnforce, FailDeny))
	v := h.Scan(context.Background(), in("ok"), in(strings.Repeat("a", 101)))
	if v.Outcome != OutcomeFailed || v.Failure != FailureOversize || !v.Deny {
		t.Fatalf("oversize: %s", v)
	}
	if calls.Load() != 1 {
		t.Errorf("the detector saw %d inputs, want the first only", calls.Load())
	}
}

func TestScanStopsAtTheFirstBlockAndSkipsEmptyInput(t *testing.T) {
	var seen []string
	h := NewHost(&fake{name: "fake", detect: func(_ context.Context, in Input) ([]Finding, error) {
		seen = append(seen, in.Source)
		if in.Source == "b" {
			return []Finding{{RuleID: "r"}}, nil
		}
		return nil, nil
	}}, cfg(ModeEnforce, FailDeny))
	v := h.Scan(context.Background(), Input{Bytes: []byte("a"), Source: "a"}, Input{Source: "empty"}, Input{Bytes: []byte("b"), Source: "b"}, Input{Bytes: []byte("c"), Source: "c"})
	if !v.Deny || strings.Join(seen, ",") != "a,b" {
		t.Fatalf("verdict %s after inputs %v", v, seen)
	}
}

func TestCallerCancellationIsNoVerdict(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	v := NewHost(&fake{name: "fake", detect: func(ctx context.Context, _ Input) ([]Finding, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}}, cfg(ModeEnforce, FailDeny)).Scan(ctx, in("x"))
	if v.Canceled == nil || v.Deny {
		t.Fatalf("caller gone: %s canceled=%v", v, v.Canceled)
	}
}

type privateFake struct {
	fake
	loaded, reloaded int
	health           error
}

func (p *privateFake) Load(path string) error { p.loaded++; return nil }
func (p *privateFake) Reload() error          { p.reloaded++; return nil }
func (p *privateFake) Health() error          { return p.health }

func TestRegistryAndPrivateData(t *testing.T) {
	p := &privateFake{fake: fake{name: "priv", detect: func(context.Context, Input) ([]Finding, error) { return nil, nil }}}
	Register("test-private", func(Config) (Detector, error) { return p, nil })
	Register("test-plain", func(Config) (Detector, error) {
		return &fake{name: "test-plain", detect: func(context.Context, Input) ([]Finding, error) { return nil, nil }}, nil
	})
	names := strings.Join(Names(), ",")
	if !strings.Contains(names, "test-plain") || !strings.Contains(names, "test-private") {
		t.Fatalf("registered names: %s", names)
	}
	good := Config{Name: "test-private", Mode: ModeEnforce, FailureMode: FailDeny, DataPath: "/data"}
	h, err := New(good)
	if err != nil || p.loaded != 1 {
		t.Fatalf("private detector: loaded=%d err=%v", p.loaded, err)
	}
	p.health = errors.New("stale")
	if h.Health() == nil {
		t.Error("the host hides a data health error")
	}
	if err := h.Reload(); err != nil || p.reloaded != 1 {
		t.Errorf("reload: %v (%d)", err, p.reloaded)
	}
	// A detector without private data has nothing to load or to report.
	plain, err := New(Config{Name: "test-plain", Mode: ModeEnforce, FailureMode: FailDeny})
	if err != nil || plain.Health() != nil || plain.Reload() != nil {
		t.Errorf("plain detector: %v", err)
	}
	// A private detector without a data path must not start.
	good.DataPath = ""
	if _, err := New(good); err == nil {
		t.Error("a detector with private data started without a data path")
	}
}

func TestConfigErrors(t *testing.T) {
	ok := Config{Name: "test-plain", Mode: ModeEnforce, FailureMode: FailDeny}
	for name, c := range map[string]Config{
		"no name":      {Mode: ModeEnforce, FailureMode: FailDeny},
		"bad mode":     {Name: "x", Mode: "audit", FailureMode: FailDeny},
		"bad failure":  {Name: "x", Mode: ModeEnforce, FailureMode: "ignore"},
		"negative cap": {Name: "x", Mode: ModeEnforce, FailureMode: FailDeny, Limits: Limits{MaxInputBytes: -1}},
		"unknown":      {Name: "no-such-detector", Mode: ModeEnforce, FailureMode: FailDeny},
	} {
		if _, err := New(c); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := New(ok); err != nil {
		t.Errorf("good config: %v", err)
	}
	defer func() {
		if recover() == nil {
			t.Error("a repeated name was accepted")
		}
	}()
	Register("test-plain", func(Config) (Detector, error) { return nil, nil })
}

func TestScanWithinUsesWhatIsLeftOfTheDeadline(t *testing.T) {
	slow := func(ctx context.Context, _ Input) ([]Finding, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	h := NewHost(&fake{name: "fake", detect: slow}, cfg(ModeEnforce, FailDeny))
	// 50ms is the configured deadline; 5ms is what the request has left.
	start := time.Now()
	v := h.ScanWithin(context.Background(), 5*time.Millisecond, in("x"))
	if v.Failure != FailureTimeout || !v.Deny || time.Since(start) > 250*time.Millisecond {
		t.Errorf("short budget: %s after %s", v, time.Since(start))
	}
	// Nothing left is a timeout before any call.
	called := false
	h = NewHost(&fake{name: "fake", detect: func(context.Context, Input) ([]Finding, error) {
		called = true
		return nil, nil
	}}, cfg(ModeEnforce, FailDeny))
	if v := h.ScanWithin(context.Background(), 0, in("x")); v.Failure != FailureTimeout || called {
		t.Errorf("no budget: %s (called %t)", v, called)
	}
	// A longer budget never lifts the configured deadline.
	start = time.Now()
	v = NewHost(&fake{name: "fake", detect: slow}, cfg(ModeEnforce, FailDeny)).ScanWithin(context.Background(), time.Hour, in("x"))
	if v.Failure != FailureTimeout || time.Since(start) > 400*time.Millisecond {
		t.Errorf("long budget: %s after %s", v, time.Since(start))
	}
}

func TestAbandonedCallsAreBounded(t *testing.T) {
	release := make(chan struct{})
	c := cfg(ModeEnforce, FailDeny)
	c.Limits.Deadline = 10 * time.Millisecond
	c.Limits.MaxConcurrent = 2
	h := NewHost(&fake{name: "fake", detect: func(context.Context, Input) ([]Finding, error) {
		<-release // ignores its context: the call outlives its deadline
		return nil, nil
	}}, c)
	for i := range 2 {
		if v := h.Scan(context.Background(), in("x")); v.Failure != FailureTimeout {
			t.Fatalf("call %d: %s", i, v)
		}
	}
	// Both slots are held by abandoned calls: the next call fails at once.
	start := time.Now()
	if v := h.Scan(context.Background(), in("x")); v.Failure != FailureBudget || !v.Deny || time.Since(start) > 50*time.Millisecond {
		t.Errorf("over the bound: %s", v)
	}
	close(release)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if v := h.Scan(context.Background(), in("x")); v.Failure == FailureNone {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Error("the slots were not released after the calls returned")
}

// A report finding before an error is not a verdict: the call has no verdict.
func TestReportFindingBeforeAnErrorIsAFailure(t *testing.T) {
	h := NewHost(&fake{name: "fake", detect: func(context.Context, Input) ([]Finding, error) {
		return []Finding{{RuleID: "r", Class: ClassReport}}, ErrBudget
	}}, cfg(ModeEnforce, FailDeny))
	v := h.Scan(context.Background(), in("x"))
	if v.Outcome != OutcomeFailed || v.Failure != FailureBudget || !v.Deny || len(v.Findings) != 1 {
		t.Errorf("got %s", v)
	}
	h = NewHost(&fake{name: "fake", detect: func(context.Context, Input) ([]Finding, error) {
		return []Finding{{RuleID: "r"}}, ErrBudget
	}}, cfg(ModeEnforce, FailDeny))
	if v := h.Scan(context.Background(), in("x")); v.Outcome != OutcomeBlocked || !v.Deny {
		t.Errorf("a block finding before an error is a verdict: %s", v)
	}
}
