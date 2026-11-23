package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"google.golang.org/grpc/codes"
)

type reply struct {
	status int
	body   string
	took   time.Duration
}

// post sends one scan request to the gate's handler over a real connection.
func post(t *testing.T, e *env, body string, header map[string]string) reply {
	t.Helper()
	srv := httptest.NewServer(e.gate.handler())
	defer srv.Close()
	return postTo(t, srv.URL+"/scan", body, header)
}

func postTo(t *testing.T, url, body string, header map[string]string) reply {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range header {
		req.Header.Set(k, v)
	}
	began := time.Now()
	resp, err := (&http.Client{Transport: &http.Transport{DisableKeepAlives: true}}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return reply{resp.StatusCode, string(data), time.Since(began)}
}

func outcomeCount(e *env, outcome string) float64 {
	return testutil.ToFloat64(e.m.requests.WithLabelValues(outcome))
}

func TestScanReturnsTheEngineReplyUnchanged(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	// The engine writes a progress dot before the JSON. The gate does not parse
	// the reply: the bytes go back as they are.
	e.rt.reply = []byte(".\n{\"results\":[{\"check_id\":\"aws-key\",\"start\":{\"offset\":4},\"end\":{\"offset\":24}}]}\nnot even json after")
	r := post(t, e, "host\nAKIAIOSFODNN7EXAMPLE", nil)
	if r.status != 200 || r.body != string(e.rt.reply) {
		t.Fatalf("got %d %q", r.status, r.body)
	}
	if outcomeCount(e, outcomeOK) != 1 {
		t.Fatal("outcome ok not counted")
	}
	sent := e.rt.sent
	if len(sent) != 1 || string(sent[0].Body) != "host\nAKIAIOSFODNN7EXAMPLE" {
		t.Fatalf("the router got %+v", sent)
	}
}

func TestPoolHitIsFastAndReplenishesAfterwards(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	e.api.createDelay.Store(int64(300 * time.Millisecond)) // the replacement is slow
	r := post(t, e, "x", nil)
	if r.status != 200 || r.took > 100*time.Millisecond {
		t.Fatalf("pool hit took %v (status %d): it must not wait for a create", r.took, r.status)
	}
	if testutil.ToFloat64(e.m.poolMiss) != 0 {
		t.Fatal("a pool hit counted as a miss")
	}
	target := e.rt.targets()[0]
	if !strings.HasPrefix(target, "egress-scan-engine/eg-gate-0-") {
		t.Fatalf("target %q", target)
	}
	e.api.exit(strings.TrimPrefix(target, "egress-scan-engine/"))
	e.eventually("replenished", func() bool { return e.waitingCount() == 2 })
}

func TestNoChildServesTwoRequests(t *testing.T) {
	e := newEnv(t, func(c *config) { c.TargetWaiting = 4; c.CreateConcurrency = 4 })
	e.start()
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		// The child serves once and the control plane deletes it.
		e.api.exit(child)
		return sendResult{Body: cleanReply}, nil
	}
	srv := httptest.NewServer(e.gate.handler())
	defer srv.Close()
	var wg sync.WaitGroup
	for i := 0; i < 24; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			postTo(t, srv.URL+"/scan", "x", nil)
		}()
	}
	wg.Wait()
	seen := map[string]int{}
	for _, tg := range e.rt.targets() {
		seen[tg]++
	}
	for tg, n := range seen {
		if n != 1 {
			t.Fatalf("%s took %d requests", tg, n)
		}
	}
}

func TestCallerCannotNameAnActor(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	srv := httptest.NewServer(e.gate.handler())
	defer srv.Close()
	for _, h := range []map[string]string{
		{"ate-target-actor": "other-atespace/victim"},
		{"Ate-Target-Actor": "egress-scan-engine/seed-0123456789abcdef"},
		{"X-Ate-Target-Port": "22", "Host": "victim"},
		{"X-Forwarded-Host": "victim", "X-Actor": "victim"},
	} {
		if r := postTo(t, srv.URL+"/scan?actor=victim&atespace=other", "x", h); r.status != 200 {
			t.Fatalf("status %d", r.status)
		}
	}
	for _, tg := range e.rt.targets() {
		if !strings.HasPrefix(tg, "egress-scan-engine/eg-gate-0-") || strings.Contains(tg, "victim") || strings.Contains(tg, "seed") {
			t.Fatalf("a request reached %q", tg)
		}
	}
	// Only POST /scan exists.
	for _, p := range []struct{ method, path string }{{"GET", "/scan"}, {"POST", "/"}, {"POST", "/scan/x"}, {"PUT", "/scan"}} {
		req, _ := http.NewRequest(p.method, srv.URL+p.path, strings.NewReader("x"))
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != 404 && resp.StatusCode != 405 {
			t.Fatalf("%s %s = %d", p.method, p.path, resp.StatusCode)
		}
	}
}

func TestBodyLimits(t *testing.T) {
	e := newEnv(t, func(c *config) { c.MaxBody = 1024 })
	e.start()
	if r := post(t, e, strings.Repeat("a", 1025), nil); r.status != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversize = %d", r.status)
	}
	if r := post(t, e, strings.Repeat("a", 1024), nil); r.status != 200 {
		t.Fatalf("limit = %d", r.status)
	}
	if r := post(t, e, "", nil); r.status != http.StatusBadRequest {
		t.Fatalf("empty = %d", r.status)
	}
	if outcomeCount(e, outcomeOversize) != 1 || outcomeCount(e, outcomeBadRequest) != 1 {
		t.Fatal("outcomes not counted")
	}
}

func TestDeadlineHeaderReachesTheRouter(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	var got time.Duration
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		dl, ok := ctx.Deadline()
		if !ok {
			t.Error("no deadline on the router call")
		}
		got = time.Until(dl)
		<-ctx.Done()
		return sendResult{}, &sendError{Kind: sendTimeout}
	}
	r := post(t, e, "x", map[string]string{"X-Deadline-Ms": "150"})
	if r.status != http.StatusGatewayTimeout {
		t.Fatalf("status %d", r.status)
	}
	if r.took < 120*time.Millisecond || r.took > 400*time.Millisecond {
		t.Fatalf("answered after %v for a 150 ms deadline", r.took)
	}
	if got <= 0 || got > 150*time.Millisecond {
		t.Fatalf("router deadline %v", got)
	}
	if outcomeCount(e, outcomeDeadline) != 1 {
		t.Fatal("deadline outcome not counted")
	}
}

func TestDeadlineDefaultCapAndBadValues(t *testing.T) {
	e := newEnv(t, func(c *config) { c.DefaultDeadline = 200 * time.Millisecond; c.MaxDeadline = 400 * time.Millisecond })
	e.start()
	var d time.Duration
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		dl, _ := ctx.Deadline()
		d = time.Until(dl)
		return sendResult{Body: cleanReply}, nil
	}
	post(t, e, "x", nil)
	if d > 200*time.Millisecond || d < 100*time.Millisecond {
		t.Fatalf("default deadline %v", d)
	}
	post(t, e, "x", map[string]string{"X-Deadline-Ms": "999999"})
	if d > 400*time.Millisecond || d < 300*time.Millisecond {
		t.Fatalf("capped deadline %v", d)
	}
	for _, v := range []string{"0", "-5", "abc", "1.5"} {
		if r := post(t, e, "x", map[string]string{"X-Deadline-Ms": v}); r.status != http.StatusBadRequest {
			t.Fatalf("deadline %q = %d", v, r.status)
		}
	}
}

func TestScannerLeavingEndsTheCallAndSpendsTheChild(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	entered := make(chan struct{})
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		close(entered)
		<-ctx.Done()
		return sendResult{}, &sendError{Kind: sendTimeout}
	}
	srv := httptest.NewServer(e.gate.handler())
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, srv.URL+"/scan", strings.NewReader("x"))
	go func() { <-entered; cancel() }()
	if _, err := http.DefaultClient.Do(req); err == nil {
		t.Fatal("expected the cancelled call to fail")
	}
	e.eventually("client_gone counted", func() bool { return outcomeCount(e, outcomeClientGone) == 1 })
	// The body was sent, so the child is spent and not put back.
	e.eventually("replacement", func() bool { return e.waitingCount() == 2 })
}

func TestShedWhenFull(t *testing.T) {
	e := newEnv(t, func(c *config) { c.MaxInFlight = 2; c.TargetWaiting = 4; c.CreateConcurrency = 4; c.MaxFallback = 0 })
	e.start()
	release := make(chan struct{})
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		<-release
		return sendResult{Body: cleanReply}, nil
	}
	srv := httptest.NewServer(e.gate.handler())
	defer srv.Close()
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); postTo(t, srv.URL+"/scan", "x", nil) }()
	}
	e.eventually("two in flight", func() bool { return len(e.gate.slots) == 2 })
	r := postTo(t, srv.URL+"/scan", "x", nil)
	if r.status != http.StatusServiceUnavailable || r.took < 20*time.Millisecond || r.took > 200*time.Millisecond {
		t.Fatalf("third request: %d after %v", r.status, r.took)
	}
	if outcomeCount(e, outcomeShed) != 1 {
		t.Fatal("shed not counted")
	}
	close(release)
	wg.Wait()
	// A queued request takes a freed slot.
	if r := postTo(t, srv.URL+"/scan", "x", nil); r.status != 200 {
		t.Fatalf("after the burst: %d", r.status)
	}
}

func TestEmptyPoolFallsBackToCreateOnDemand(t *testing.T) {
	e := newEnv(t, nil)
	// The loop is not started: nothing waits.
	e.pool.started = true
	e.api.resumeDelay.Store(int64(20 * time.Millisecond))
	r := post(t, e, "x", nil)
	if r.status != 200 {
		t.Fatalf("status %d %q", r.status, r.body)
	}
	if testutil.ToFloat64(e.m.poolMiss) != 1 || testutil.ToFloat64(e.m.fallbacks.WithLabelValues("ok")) != 1 {
		t.Fatal("the miss and the fallback must be counted")
	}
	tg := e.rt.targets()[0]
	if !strings.HasPrefix(tg, "egress-scan-engine/eg-gate-0-") || e.api.creates.Load() != 1 || e.api.resumes.Load() != 1 {
		t.Fatalf("target %q, creates %d, resumes %d", tg, e.api.creates.Load(), e.api.resumes.Load())
	}
}

func TestFallbackIsBoundedAndNeedsTime(t *testing.T) {
	e := newEnv(t, func(c *config) { c.MaxFallback = 1 })
	e.pool.started = true
	e.api.resumeDelay.Store(int64(300 * time.Millisecond))
	srv := httptest.NewServer(e.gate.handler())
	defer srv.Close()
	first := make(chan reply, 1)
	go func() { first <- postTo(t, srv.URL+"/scan", "x", nil) }()
	e.eventually("first fallback running", func() bool { return e.api.creates.Load() == 1 })
	// One fallback at a time: the second request is refused, not queued behind it.
	r := postTo(t, srv.URL+"/scan", "x", nil)
	if r.status != http.StatusServiceUnavailable || r.took > 100*time.Millisecond {
		t.Fatalf("second request: %d after %v", r.status, r.took)
	}
	if f := <-first; f.status != 200 {
		t.Fatalf("first request: %d", f.status)
	}
	// With less than FallbackMinRemaining left, path A is not tried.
	creates := e.api.creates.Load()
	r = postTo(t, srv.URL+"/scan", "x", map[string]string{"X-Deadline-Ms": "100"})
	if r.status != http.StatusServiceUnavailable || e.api.creates.Load() != creates {
		t.Fatalf("short deadline: %d, creates %d -> %d", r.status, creates, e.api.creates.Load())
	}
	if outcomeCount(e, outcomePoolEmpty) != 2 {
		t.Fatalf("pool_empty = %v", outcomeCount(e, outcomePoolEmpty))
	}
}

func TestAPIDownRefusesFastAndRecovers(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	// Drain the pool, then take the API away.
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		e.api.exit(child)
		return sendResult{Body: cleanReply}, nil
	}
	e.api.down.Store(true)
	// The children that wait still serve: the fake router needs no API.
	for i := 0; i < 2; i++ {
		if r := post(t, e, "x", nil); r.status != 200 {
			t.Fatalf("waiting child %d = %d", i, r.status)
		}
	}
	// The first request after the pool is drained tries path A and learns that it fails.
	var last reply
	for i := 0; i < 3; i++ {
		last = post(t, e, "x", nil)
		if last.status != http.StatusServiceUnavailable {
			t.Fatalf("request %d = %d", i, last.status)
		}
	}
	// A failed create within FailFast is refused at once, with no API call.
	creates := e.api.creates.Load()
	if r := post(t, e, "x", nil); r.status != http.StatusServiceUnavailable || r.took > 50*time.Millisecond || e.api.creates.Load() != creates {
		t.Fatalf("refusal: %d after %v", r.status, r.took)
	}
	if outcomeCount(e, outcomeNoWorker) < 1 {
		t.Fatal("no_worker not counted")
	}
	if e.pool.ready() {
		e.clock.advance(time.Minute)
		if e.pool.ready() {
			t.Fatal("ready with a dead API")
		}
	}
	e.api.down.Store(false)
	e.eventually("recovers", func() bool { return e.waitingCount() == 2 && e.pool.ready() })
	if r := post(t, e, "x", nil); r.status != 200 {
		t.Fatalf("after recovery: %d", r.status)
	}
}

func TestRouterErrorKeepsTheChild(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		return sendResult{}, &sendError{Kind: sendUnsent}
	}
	r := post(t, e, "x", nil)
	if r.status != http.StatusServiceUnavailable || outcomeCount(e, outcomeRouterDown) != 1 {
		t.Fatalf("status %d", r.status)
	}
	// The body never left, so no child is lost and none is deleted.
	if e.waitingCount() != 2 || len(e.api.deletedNames()) != 0 {
		t.Fatalf("waiting %d, deleted %v", e.waitingCount(), e.api.deletedNames())
	}
}

func TestChildCrashMidRequestRetriesOnceAndReplenishes(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	var first string
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		if n == 1 {
			first = child
			return sendResult{}, &sendError{Kind: sendNoReply}
		}
		e.api.exit(child)
		return sendResult{Body: cleanReply}, nil
	}
	r := post(t, e, "secret-body", nil)
	if r.status != 200 {
		t.Fatalf("status %d", r.status)
	}
	targets := e.rt.targets()
	if len(targets) != 2 || targets[0] == targets[1] {
		t.Fatalf("targets %v", targets)
	}
	if string(e.rt.sent[0].Body) != "secret-body" || string(e.rt.sent[1].Body) != "secret-body" {
		t.Fatal("the retry sent another body")
	}
	e.eventually("dead child deleted", func() bool { return len(e.api.names(first)) == 0 })
	e.eventually("replenished", func() bool { return e.waitingCount() == 2 })
	if testutil.ToFloat64(e.m.retries) != 1 || testutil.ToFloat64(e.m.childErrs) != 1 {
		t.Fatal("retry and child error must be counted")
	}
}

func TestChildCrashWithNoTimeLeftIsReported(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		return sendResult{}, &sendError{Kind: sendNoReply}
	}
	// 80 ms is under RetryMinRemaining: no second child.
	r := post(t, e, "x", map[string]string{"X-Deadline-Ms": "80"})
	if r.status != http.StatusBadGateway || len(e.rt.targets()) != 1 {
		t.Fatalf("status %d after %d sends", r.status, len(e.rt.targets()))
	}
	// Both children crash: reported, not retried a third time.
	r = post(t, e, "x", nil)
	if r.status != http.StatusBadGateway || len(e.rt.targets()) != 3 {
		t.Fatalf("status %d after %d sends", r.status, len(e.rt.targets()))
	}
	if outcomeCount(e, outcomeChildError) != 2 {
		t.Fatal("child_error not counted")
	}
	e.eventually("replenished", func() bool { return e.waitingCount() == 2 })
}

func TestEngineFailuresAreReportedWithoutDetail(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	for _, tc := range []struct {
		err     *sendError
		status  int
		outcome string
	}{
		{&sendError{Kind: sendStatus, Status: 500}, 502, outcomeChildError},
		{&sendError{Kind: sendStatus, Status: 400}, 502, outcomeChildError},
		{&sendError{Kind: sendTooLarge}, 502, outcomeBadReply},
	} {
		e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) { return sendResult{}, tc.err }
		r := post(t, e, "x", nil)
		if r.status != tc.status || strings.TrimSpace(r.body) != tc.outcome {
			t.Fatalf("%+v: %d %q", tc.err, r.status, r.body)
		}
		e.eventually("replenished", func() bool { return e.waitingCount() == 2 })
	}
}

func TestLogsHoldNoBody(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	const marker = "AKIA-MARKER-NOT-FOR-LOGS"
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		return sendResult{}, &sendError{Kind: sendStatus, Status: 502}
	}
	post(t, e, "host\n"+marker, nil)
	e.api.failCreate.Store(uint32(codes.Internal))
	post(t, e, "host\n"+marker, nil)
	time.Sleep(50 * time.Millisecond)
	logs := e.logs.String()
	if strings.Contains(logs, marker) {
		t.Fatal("a log line holds the request body")
	}
	if !strings.Contains(logs, "scan failed") {
		t.Fatalf("expected a failure line, got %q", logs)
	}
}

func TestFailureLogsAreRateLimited(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		return sendResult{}, &sendError{Kind: sendStatus, Status: 400}
	}
	for i := 0; i < 20; i++ {
		post(t, e, "x", nil)
		e.eventually("replenished", func() bool { return e.waitingCount() == 2 })
	}
	if n := strings.Count(e.logs.String(), "scan failed"); n != 1 {
		t.Fatalf("%d failure lines for 20 failures", n)
	}
}

func TestProbes(t *testing.T) {
	e := newEnv(t, nil)
	h := adminHandler(e.pool, e.m, http.NotFoundHandler())
	get := func(path string) int {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		return rec.Code
	}
	if get("/healthz") != 200 {
		t.Fatal("healthz")
	}
	if get("/readyz") != 503 {
		t.Fatal("readyz before the pool is full")
	}
	e.start()
	if get("/readyz") != 200 {
		t.Fatal("readyz with a full pool")
	}
	// After the first fill, an empty pool is still ready: path A serves.
	e.pool.mu.Lock()
	saved := e.pool.waiting
	e.pool.waiting = nil
	e.pool.mu.Unlock()
	if get("/readyz") != 200 {
		t.Fatal("readyz with an empty pool after the first fill")
	}
	e.pool.drain()
	if get("/readyz") != 503 || get("/healthz") != 200 {
		t.Fatal("probes while draining")
	}
	e.pool.mu.Lock()
	e.pool.waiting = saved
	e.pool.mu.Unlock()
}

func TestDeadlineHeaderCannotOverflow(t *testing.T) {
	e := newEnv(t, func(c *config) { c.DefaultDeadline = 200 * time.Millisecond; c.MaxDeadline = 400 * time.Millisecond })
	e.start()
	var d time.Duration
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		dl, _ := ctx.Deadline()
		d = time.Until(dl)
		return sendResult{Body: cleanReply}, nil
	}
	// 9223372036855 ms times 1e6 wraps negative in a time.Duration.
	for _, v := range []string{"9223372036855", "9223372036854775807"} {
		if r := post(t, e, "x", map[string]string{"X-Deadline-Ms": v}); r.status != 200 {
			t.Fatalf("deadline %s = %d", v, r.status)
		}
		if d < 300*time.Millisecond || d > 400*time.Millisecond {
			t.Fatalf("deadline %s gave %v, want the cap", v, d)
		}
	}
	// Beyond int64 is a bad value.
	if r := post(t, e, "x", map[string]string{"X-Deadline-Ms": "99999999999999999999"}); r.status != http.StatusBadRequest {
		t.Fatalf("status %d", r.status)
	}
}

func TestBodyLimitDefaultAndEarlyRejection(t *testing.T) {
	if got := testConfig(t).MaxBody; got != 1258291 {
		t.Fatalf("default max body %d", got)
	}
	e := newEnv(t, func(c *config) { c.MaxInFlight = 1; c.MaxFallback = 0; c.MaxBody = 1024 })
	e.start()
	release := make(chan struct{})
	e.rt.fn = func(ctx context.Context, child string, n int) (sendResult, error) {
		<-release
		return sendResult{Body: cleanReply}, nil
	}
	srv := httptest.NewServer(e.gate.handler())
	defer srv.Close()
	done := make(chan reply, 1)
	go func() { done <- postTo(t, srv.URL+"/scan", "x", nil) }()
	e.eventually("slot taken", func() bool { return len(e.gate.slots) == 1 })
	// With every slot busy, a body over the limit is a 413 at once, not a shed.
	r := postTo(t, srv.URL+"/scan", strings.Repeat("a", 2048), nil)
	if r.status != http.StatusRequestEntityTooLarge || r.took > 15*time.Millisecond {
		t.Fatalf("oversize with a full gate: %d after %v", r.status, r.took)
	}
	close(release)
	<-done
}
