package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc/codes"
)

func observeConfig() semgrepConfig {
	cfg := testConfig()
	cfg.Mode = modeObserve
	return cfg
}

func hasFamily(t *testing.T, registry *prometheus.Registry, name string) bool {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, family := range families {
		if family.GetName() == name {
			return true
		}
	}
	return false
}

func observed(t *testing.T, registry *prometheus.Registry, result string) float64 {
	t.Helper()
	return counter(t, registry, "factory_egress_scan_semgrep_observe_total", map[string]string{"result": result})
}

func requireAllowed(t *testing.T, reply *authv3.CheckResponse, err error) {
	t.Helper()
	if err != nil || reply.GetStatus().GetCode() != int32(codes.OK) || reply.GetDeniedResponse() != nil {
		t.Fatalf("want allow, got %v %v", reply, err)
	}
}

func TestObserveModeValidation(t *testing.T) {
	cfg := testConfig()
	cfg.Mode = "audit"
	if _, err := newSemgrepTier(cfg, engineFunc(nil)); err == nil {
		t.Fatal("an unknown mode was accepted")
	}
	// An empty mode is enforce, so a config without the field is unchanged.
	cfg.Mode = ""
	tier, err := newSemgrepTier(cfg, engineFunc(nil))
	if err != nil || tier.observing() || tier.cfg.Mode != modeEnforce {
		t.Fatalf("empty mode: %v %+v", err, tier)
	}
	cfg.Mode = modeObserve
	if tier, err = newSemgrepTier(cfg, engineFunc(nil)); err != nil || !tier.observing() {
		t.Fatalf("observe: %v", err)
	}
}

func TestObserveFindingIsAllowedAndCounted(t *testing.T) {
	logs := captureLog(t)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		return Verdict{Findings: []Finding{{"aws-key-0", 3, 9}}}, nil
	})
	s, registry := newTierScanner(t, observeConfig(), engine)
	reply, err := s.Check(context.Background(), cleanRequest("engine-should-flag"))
	requireAllowed(t, reply, err)

	waitFor(t, func() bool { return observed(t, registry, observeWouldDeny) == 1 })
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "found"}); got != 1 {
		t.Errorf("found = %v", got)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_agreement_total", map[string]string{"gitleaks": "clean", "semgrep": "found"}); got != 1 {
		t.Errorf("agreement = %v", got)
	}
	for _, result := range []string{observeWouldFail, observeNoChange} {
		if got := observed(t, registry, result); got != 0 {
			t.Errorf("%s = %v", result, got)
		}
	}
	out := logs.String()
	if !strings.Contains(out, "observed a finding") || !strings.Contains(out, "aws-key-0@3-9") || strings.Contains(out, "engine-should-flag") {
		t.Fatalf("log must hold rule and offsets, not text: %q", out)
	}
}

func TestObserveCleanIsNoChange(t *testing.T) {
	s, registry := newTierScanner(t, observeConfig(), engineFunc(func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, nil }))
	reply, err := s.Check(context.Background(), cleanRequest("fine"))
	requireAllowed(t, reply, err)
	waitFor(t, func() bool { return observed(t, registry, observeNoChange) == 1 })
	if got := counter(t, registry, "factory_egress_scan_semgrep_agreement_total", map[string]string{"gitleaks": "clean", "semgrep": "clean"}); got != 1 {
		t.Errorf("agreement = %v", got)
	}
}

// The engine adds no latency to the reply. The reply comes back in Gitleaks
// time while the engine call is still running.
func TestObserveSlowEngineAddsNoLatency(t *testing.T) {
	var finished atomic.Bool
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		select {
		case <-time.After(250 * time.Millisecond):
		case <-ctx.Done():
		}
		finished.Store(true)
		return Verdict{Findings: []Finding{{"slow-rule", 1, 2}}}, nil
	})
	cfg := observeConfig()
	cfg.Deadline = 2 * time.Second
	s, registry := newTierScanner(t, cfg, engine)

	began := time.Now()
	reply, err := s.Check(context.Background(), cleanRequest("fine"))
	elapsed := time.Since(began)
	requireAllowed(t, reply, err)
	if elapsed > 120*time.Millisecond {
		t.Fatalf("the reply waited for the engine: %v", elapsed)
	}
	if finished.Load() {
		t.Fatal("the engine finished before the reply: the test proves nothing")
	}
	// The call goes on in the background and is counted when it ends.
	waitFor(t, func() bool { return observed(t, registry, observeWouldDeny) == 1 })
}

func TestObserveTimeoutIsAllowedAndCounted(t *testing.T) {
	logs := captureLog(t)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		<-ctx.Done()
		return Verdict{}, ctx.Err()
	})
	cfg := observeConfig()
	cfg.Deadline = 50 * time.Millisecond
	cfg.FailureMode = failureDeny // observe never applies it
	s, registry := newTierScanner(t, cfg, engine)

	began := time.Now()
	reply, err := s.Check(context.Background(), cleanRequest("fine"))
	requireAllowed(t, reply, err)
	if time.Since(began) > 40*time.Millisecond {
		t.Fatalf("the reply waited for the deadline: %v", time.Since(began))
	}
	waitFor(t, func() bool { return observed(t, registry, observeWouldFail) == 1 })
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "timeout"}); got != 1 {
		t.Errorf("timeout = %v", got)
	}
	if hasFamily(t, registry, "factory_egress_scan_semgrep_failure_mode_total") {
		t.Error("observe mode must not expose the failure mode series")
	}
	if !strings.Contains(logs.String(), "observed timeout, request not blocked") {
		t.Fatalf("no failure log: %q", logs.String())
	}
}

func TestObserveShedIsAllowedAndCounted(t *testing.T) {
	hold := make(chan struct{})
	entered := make(chan struct{}, 4)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		entered <- struct{}{}
		select {
		case <-hold:
		case <-ctx.Done():
		}
		return Verdict{}, nil
	})
	cfg := observeConfig()
	cfg.Concurrency = 1
	cfg.Deadline = 5 * time.Second
	s, registry := newTierScanner(t, cfg, engine)
	defer close(hold)

	reply, err := s.Check(context.Background(), cleanRequest("first"))
	requireAllowed(t, reply, err)
	<-entered
	began := time.Now()
	reply, err = s.Check(context.Background(), cleanRequest("second"))
	requireAllowed(t, reply, err)
	if time.Since(began) > 120*time.Millisecond {
		t.Fatal("a shed request waited")
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "shed"}); got != 1 {
		t.Errorf("shed = %v", got)
	}
	if got := observed(t, registry, observeWouldFail); got != 1 {
		t.Errorf("would_fail = %v", got)
	}
}

func TestObserveBreakerOpenIsAllowedAndCounted(t *testing.T) {
	var calls atomic.Int32
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		calls.Add(1)
		return Verdict{}, errors.New("engine down")
	})
	cfg := observeConfig()
	cfg.BreakerFailures = 2
	cfg.BreakerCooldown = time.Hour
	s, registry := newTierScanner(t, cfg, engine)
	for i := 0; i < 2; i++ {
		reply, err := s.Check(context.Background(), cleanRequest("fine"))
		requireAllowed(t, reply, err)
		want := float64(i + 1)
		waitFor(t, func() bool {
			return counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "error"}) == want
		})
	}
	reply, err := s.Check(context.Background(), cleanRequest("fine"))
	requireAllowed(t, reply, err)
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "breaker_open"}); got != 1 {
		t.Errorf("breaker_open = %v", got)
	}
	if calls.Load() != 2 {
		t.Errorf("engine calls = %d, an open breaker must not call it", calls.Load())
	}
	if got := observed(t, registry, observeWouldFail); got != 3 {
		t.Errorf("would_fail = %v", got)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_breaker_open", nil); got != 1 {
		t.Errorf("breaker gauge = %v", got)
	}
}

func TestObservePanicIsRecoveredAndAllowed(t *testing.T) {
	logs := captureLog(t)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		panic("boom " + string(body))
	})
	s, registry := newTierScanner(t, observeConfig(), engine)
	reply, err := s.Check(context.Background(), cleanRequest("fine SENSITIVE-BODY-TEXT"))
	requireAllowed(t, reply, err)
	waitFor(t, func() bool { return observed(t, registry, observeWouldFail) == 1 })
	if got := counter(t, registry, "factory_egress_scan_semgrep_inflight", nil); got != 0 {
		t.Errorf("inflight = %v after a panic", got)
	}
	if !strings.Contains(logs.String(), "engine panicked") || strings.Contains(logs.String(), "SENSITIVE-BODY-TEXT") {
		t.Fatalf("log must name the panic and hold no body: %q", logs.String())
	}
}

// A panic outside Engine.Scan, in the tier itself, is also recovered and
// frees the slot.
func TestObservePanicOutsideTheEngineFreesTheSlot(t *testing.T) {
	cfg := observeConfig()
	cfg.Concurrency = 1
	s, registry := newTierScanner(t, cfg, engineFunc(func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, nil }))
	s.semgrep.metrics.inflight = nil // makes exec panic before the engine call
	reply, err := s.Check(context.Background(), cleanRequest("fine"))
	requireAllowed(t, reply, err)
	waitFor(t, func() bool { return len(s.semgrep.slots) == 0 })
	waitFor(t, func() bool { return observed(t, registry, observeWouldFail) == 1 })
}

func TestObserveGitleaksDenyStillDenies(t *testing.T) {
	var calls atomic.Int32
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		calls.Add(1)
		return Verdict{}, nil
	})
	s, registry := newTierScanner(t, observeConfig(), engine)
	reply, err := s.Check(context.Background(), cleanRequest(secretFixture))
	if err != nil {
		t.Fatal(err)
	}
	if reply.GetDeniedResponse().GetStatus().GetCode() != typev3.StatusCode_Forbidden ||
		reply.GetDeniedResponse().GetBody() != "request contains a credential pattern" {
		t.Fatalf("Gitleaks deny was lost: %v", reply)
	}
	// The engine still sees the request, so that the agreement is measured.
	waitFor(t, func() bool {
		return counter(t, registry, "factory_egress_scan_semgrep_agreement_total", map[string]string{"gitleaks": "found", "semgrep": "clean"}) == 1
	})
	if calls.Load() != 1 {
		t.Errorf("engine calls = %d", calls.Load())
	}
	// Gitleaks denied, so the engine changes nothing.
	waitFor(t, func() bool { return observed(t, registry, observeNoChange) == 1 })
	if got := observed(t, registry, observeWouldDeny); got != 0 {
		t.Errorf("would_deny = %v", got)
	}
}

// A work-limit result of Gitleaks gives no verdict to compare.
func TestObserveGitleaksWorkLimitCountsNoAgreement(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, nil })
	s, registry := newTierScanner(t, observeConfig(), engine)
	obs := s.semgrep.beginObserve("host\n", []byte("fine"), "")
	obs.setGitleaks(glAborted)
	waitFor(t, func() bool {
		obs.mu.Lock()
		defer obs.mu.Unlock()
		return obs.engine != nil
	})
	if got := observed(t, registry, observeNoChange) + observed(t, registry, observeWouldDeny) + observed(t, registry, observeWouldFail); got != 0 {
		t.Errorf("an aborted request was counted: %v", got)
	}
}

// A hostile engine cannot change a reply: whatever it returns, the reply is the
// Gitleaks reply.
func TestObserveNeverChangesTheReply(t *testing.T) {
	garbage := []engineFunc{
		func(ctx context.Context, body []byte) (Verdict, error) {
			return Verdict{Findings: []Finding{{"x", -5, 1 << 40}}}, nil
		},
		func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, context.Canceled },
		func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, errEngineGarbage },
	}
	for i, e := range garbage {
		s, _ := newTierScanner(t, observeConfig(), e)
		reply, err := s.Check(context.Background(), cleanRequest("fine"))
		if err != nil {
			t.Fatalf("case %d: %v", i, err)
		}
		requireAllowed(t, reply, nil)
	}
}

func TestObserveCancelledRequestDoesNotCancelTheEngineCall(t *testing.T) {
	cancelled := make(chan struct{}, 1)
	release := make(chan struct{})
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		select {
		case <-release:
			return Verdict{}, nil
		case <-ctx.Done():
			cancelled <- struct{}{}
			return Verdict{}, ctx.Err()
		}
	})
	cfg := observeConfig()
	cfg.Deadline = 5 * time.Second
	s, registry := newTierScanner(t, cfg, engine)
	ctx, cancel := context.WithCancel(context.Background())
	reply, err := s.Check(ctx, cleanRequest("fine"))
	requireAllowed(t, reply, err)
	cancel()
	select {
	case <-cancelled:
		t.Fatal("the end of the request cancelled the engine call")
	case <-time.After(50 * time.Millisecond):
	}
	close(release)
	waitFor(t, func() bool { return observed(t, registry, observeNoChange) == 1 })
}

// Enforce mode and the tier-off scanner expose no observe series, and observe
// mode exposes no failure mode series.
func TestObserveSeriesExistOnlyInObserveMode(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, nil })
	names := func(registry *prometheus.Registry) string {
		families, err := registry.Gather()
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, f := range families {
			out = append(out, f.GetName())
		}
		return fmt.Sprint(out)
	}
	_, enforce := newTierScanner(t, testConfig(), engine)
	if got := names(enforce); strings.Contains(got, "observe") {
		t.Errorf("enforce mode exposes observe series: %s", got)
	}
	if !hasFamily(t, enforce, "factory_egress_scan_semgrep_failure_mode_total") {
		t.Error("enforce mode lost the failure mode series")
	}
	_, observing := newTierScanner(t, observeConfig(), engine)
	if got := names(observing); strings.Contains(got, "failure_mode") {
		t.Errorf("observe mode exposes the failure mode series: %s", got)
	}
	for _, name := range []string{"factory_egress_scan_semgrep_observe_total", "factory_egress_scan_semgrep_observe_mode"} {
		if !hasFamily(t, observing, name) {
			t.Errorf("observe mode lacks %s", name)
		}
	}
	// The check histogram keeps its old buckets: the engine is off the path.
	families, _ := observing.Gather()
	for _, f := range families {
		if f.GetName() != "factory_egress_scan_check_duration_seconds" {
			continue
		}
		for _, m := range f.GetMetric() {
			if n := len(m.GetHistogram().GetBucket()); n != 10 {
				t.Errorf("observe mode changed the check buckets: %d", n)
			}
		}
	}
	// The tier off creates none of the tier series.
	off := prometheus.NewRegistry()
	newMeteredScanner(newTestScanner(t), off)
	if got := names(off); strings.Contains(got, "semgrep") || strings.Contains(got, "observe") {
		t.Errorf("the tier off exposes tier series: %s", got)
	}
}

// The structural bound is the in-flight limit. With a hung engine and a burst
// of requests, the scanner runs at most `concurrency` engine calls and at most
// that many extra goroutines, and every goroutine ends at the deadline.
func TestObserveBoundsGoroutinesWithAHungEngine(t *testing.T) {
	var running, peak atomic.Int32
	hold := make(chan struct{})
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		n := running.Add(1)
		defer running.Add(-1)
		for {
			p := peak.Load()
			if n <= p || peak.CompareAndSwap(p, n) {
				break
			}
		}
		select {
		case <-hold:
		case <-ctx.Done():
		}
		return Verdict{}, ctx.Err()
	})
	cfg := observeConfig()
	cfg.Concurrency = 8
	cfg.Deadline = 400 * time.Millisecond
	cfg.BreakerFailures = 0
	s, registry := newTierScanner(t, cfg, engine)
	s.slots = make(chan struct{}, 2048) // the Gitleaks slots must not limit the burst
	defer close(hold)

	baseline := runtime.NumGoroutine()
	const burst = 2000
	var wg sync.WaitGroup
	var denied atomic.Int32
	for i := 0; i < burst; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			reply, err := s.Check(context.Background(), cleanRequest("fine"))
			if err != nil || reply.GetStatus().GetCode() != int32(codes.OK) {
				if reply.GetDeniedResponse().GetBody() == engineUnavailableReason {
					denied.Add(1)
				}
			}
		}()
	}
	wg.Wait()
	if denied.Load() != 0 {
		t.Fatalf("%d requests were denied for the engine", denied.Load())
	}
	// All requests have returned. Only observe goroutines can remain.
	if extra := runtime.NumGoroutine() - baseline; extra > cfg.Concurrency+2 {
		t.Fatalf("%d goroutines remain after the burst, bound is %d", extra, cfg.Concurrency)
	}
	if peak.Load() > int32(cfg.Concurrency) {
		t.Fatalf("%d engine calls at once, limit %d", peak.Load(), cfg.Concurrency)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_inflight", nil); got > float64(cfg.Concurrency) {
		t.Fatalf("inflight %v", got)
	}
	// Every goroutine ends at the deadline, and the slots are free again.
	waitFor(t, func() bool { return runtime.NumGoroutine() <= baseline+1 && len(s.semgrep.slots) == 0 })
	shed := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "shed"})
	timeouts := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "timeout"})
	if shed+timeouts != burst {
		t.Errorf("shed %v + timeout %v != %d", shed, timeouts, burst)
	}
}

// The same burst against a real hung HTTP engine: connections and goroutines
// of the client also end at the deadline.
func TestObserveBoundsGoroutinesWithAHungHTTPEngine(t *testing.T) {
	stop := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-stop:
		case <-r.Context().Done():
		}
	}))
	engine, err := newHTTPEngine(srv.URL+"/", 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	cfg := observeConfig()
	cfg.Concurrency = 8
	cfg.Deadline = 150 * time.Millisecond
	s, registry := newTierScanner(t, cfg, engine)
	s.slots = make(chan struct{}, 2048)

	baseline := runtime.NumGoroutine()
	var wg sync.WaitGroup
	var bad atomic.Int32
	for i := 0; i < 2000; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			reply, err := s.Check(context.Background(), cleanRequest("fine"))
			if err != nil || reply.GetDeniedResponse().GetBody() == engineUnavailableReason {
				bad.Add(1)
			}
		}()
	}
	wg.Wait()
	if bad.Load() != 0 {
		t.Fatalf("%d requests failed", bad.Load())
	}
	waitFor(t, func() bool {
		return counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "timeout"}) >= 1 && len(s.semgrep.slots) == 0
	})
	close(stop)
	srv.Close()
	waitFor(t, func() bool { return runtime.NumGoroutine() <= baseline+2 })
}

// Enforce mode keeps waiting for the engine: observe is the only mode that
// returns before it.
func TestEnforceModeStillWaitsForTheEngine(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		time.Sleep(100 * time.Millisecond)
		return Verdict{Findings: []Finding{{"r", 1, 2}}}, nil
	})
	cfg := testConfig()
	cfg.Mode = modeEnforce
	s, _ := newTierScanner(t, cfg, engine)
	began := time.Now()
	reply, err := s.Check(context.Background(), cleanRequest("fine"))
	if err != nil || reply.GetDeniedResponse().GetStatus().GetCode() != typev3.StatusCode_Forbidden {
		t.Fatalf("enforce must deny on a finding: %v %v", reply, err)
	}
	if time.Since(began) < 100*time.Millisecond {
		t.Fatal("enforce mode did not wait for the engine")
	}
}

// The slot release is a method, so that taking and freeing a slot allocates
// nothing: the enforce path allocates as it did before observe mode.
func TestAdmitAndReleaseAllocateNothing(t *testing.T) {
	tier, err := newSemgrepTier(testConfig(), engineFunc(nil))
	if err != nil {
		t.Fatal(err)
	}
	allocs := testing.AllocsPerRun(1000, func() {
		if tier.admit(10, 10) != "" {
			t.Fatal("not admitted")
		}
		tier.releaseSlot()
	})
	if allocs != 0 {
		t.Fatalf("admit and releaseSlot allocate %v times", allocs)
	}
}

// The background call holds its own copy of the text. A change of the request
// body after the handler returns must not reach the engine.
func TestObserveEngineGetsACopyOfTheBody(t *testing.T) {
	release := make(chan struct{})
	got := make(chan string, 1)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		<-release
		got <- string(body)
		return Verdict{}, nil
	})
	cfg := observeConfig()
	cfg.Deadline = 5 * time.Second
	s, _ := newTierScanner(t, cfg, engine)
	req := cleanRequest("original-body-text")
	reply, err := s.Check(context.Background(), req)
	requireAllowed(t, reply, err)
	raw := req.GetAttributes().GetRequest().GetHttp().GetRawBody()
	for i := range raw {
		raw[i] = 'X' // the codec may reuse this buffer after the handler returns
	}
	close(release)
	if text := <-got; !strings.Contains(text, "original-body-text") || strings.Contains(text, "XXXX") {
		t.Fatalf("the engine saw a changed body: %q", text)
	}
}

func histogramFor(t *testing.T, registry *prometheus.Registry, name string) map[string][2]float64 {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][2]float64{}
	for _, family := range families {
		if family.GetName() != name {
			continue
		}
		for _, m := range family.GetMetric() {
			if len(m.GetLabel()) != 1 || m.GetLabel()[0].GetName() != "kind" {
				t.Fatalf("unexpected labels: %v", m.GetLabel())
			}
			out[m.GetLabel()[0].GetValue()] = [2]float64{float64(m.GetHistogram().GetSampleCount()), m.GetHistogram().GetSampleSum()}
		}
	}
	return out
}

func TestObserveRecordsBodySizes(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, nil })
	cfg := observeConfig()
	cfg.MaxBody = 1000 // a larger body is oversize, and still counted in the sizes
	s, registry := newTierScanner(t, cfg, engine)
	for _, n := range []int{10, 2000} {
		reply, err := s.Check(context.Background(), cleanRequest(strings.Repeat("a", n)))
		requireAllowed(t, reply, err)
	}
	sizes := histogramFor(t, registry, "factory_egress_scan_semgrep_observe_size_bytes")
	if len(sizes) != 2 {
		t.Fatalf("kinds: %v", sizes)
	}
	if raw := sizes[sizeRawBody]; raw[0] != 2 || raw[1] != 2010 {
		t.Errorf("raw_body count and sum = %v", raw)
	}
	// The text holds the address and header lines and two separators in addition.
	if text := sizes[sizeEngineText]; text[0] != 2 || text[1] <= 2010 {
		t.Errorf("engine_text count and sum = %v", text)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "oversize"}); got != 1 {
		t.Errorf("oversize = %v", got)
	}
}

func TestSizeHistogramExistsOnlyInObserveMode(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) { return Verdict{}, nil })
	_, enforce := newTierScanner(t, testConfig(), engine)
	if hasFamily(t, enforce, "factory_egress_scan_semgrep_observe_size_bytes") {
		t.Error("enforce mode exposes the size histogram")
	}
	_, observing := newTierScanner(t, observeConfig(), engine)
	if !hasFamily(t, observing, "factory_egress_scan_semgrep_observe_size_bytes") {
		t.Error("observe mode lacks the size histogram")
	}
}
