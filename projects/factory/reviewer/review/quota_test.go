package review

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

type fakeQuota struct {
	mu      sync.Mutex
	reading github.QuotaReading
	err     error
}

func (q *fakeQuota) Quota(context.Context) (github.QuotaReading, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.reading, q.err
}

func (q *fakeQuota) set(reading github.QuotaReading, err error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.reading, q.err = reading, err
}

func waitForQuotaCheck(t *testing.T, h *harness) recorded {
	t.Helper()
	eventually(t, "quota wait and released worker", func() bool {
		c := h.gh.check(shaA)
		return c != nil && c.Title == "Waiting for Codex quota" && testutil.ToFloat64(h.m.InFlight) == 0
	})
	c := h.gh.check(shaA)
	rec := RecordedTask(c.Summary)
	if c.Status != "in_progress" || c.Conclusion != "" || rec.QuotaSince.IsZero() || rec.ID != "" {
		t.Fatalf("quota wait = %+v, recorded = %+v", c, rec)
	}
	return rec
}

// Even repeated refusals on the last attempt keep the check open, survive
// restart and give each resumed send a new identity without using a retry.
func TestQuotaFailuresSpendNoAttempt(t *testing.T) {
	h := newHarness(t)
	addPR7(h)
	q := &fakeQuota{}
	h.cfg.Quota = q
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	for attempt := 2; attempt <= MaxAttempts; attempt++ {
		h.k.finish(task, kagent.StateFailed, "ordinary task failure")
		eventually(t, "ordinary retry", func() bool {
			task = h.taskOn(7, shaA, attempt)
			return task != ""
		})
	}
	for n, status := range []string{
		"Codex execution failed: exceeded retry limit, last status: 429 Too Many Requests",
		"usage_limit_reached for account 1234", "insufficient_quota", "rate_limit_exceeded",
	} {
		h.k.finish(task, kagent.StateFailed, status)
		rec := waitForQuotaCheck(t, h)
		if summary := h.gh.check(shaA).Summary; strings.Contains(summary, status) || strings.Contains(summary, "account 1234") {
			t.Fatal("provider status text reached the public quota check")
		}
		if rec.Attempt != MaxAttempts || rec.QuotaRetry != n+1 {
			t.Fatalf("refusal spent an attempt or lost its identity: %+v", rec)
		}
		if got := testutil.ToFloat64(h.m.Retries.WithLabelValues("homelab")); got != MaxAttempts-1 {
			t.Fatalf("retries = %v", got)
		}
		stop()
		e, ctx, stop = h.start()
		sends := len(h.k.sent())
		for _, reading := range []github.QuotaReading{
			{},
			{ReceivedAt: rec.QuotaSince.Add(-time.Second)},
			{ReceivedAt: rec.QuotaSince},
			{Exhausted: true, ReceivedAt: rec.QuotaSince.Add(time.Second)},
		} {
			q.set(reading, nil)
			e.Tick(ctx)
			if len(h.k.sent()) != sends || testutil.ToFloat64(h.m.InFlight) != 0 {
				t.Fatal("blocked review dispatched or held a worker after restart")
			}
		}
		q.set(github.QuotaReading{ReceivedAt: rec.QuotaSince.Add(time.Second)}, nil)
		e.Tick(ctx)
		id := h.msgID(7, shaA, MaxAttempts) + fmt.Sprintf("/quota-%d", n+1)
		eventually(t, "resumed quota task", func() bool {
			task = h.k.taskFor(id)
			return task != "" && RecordedTask(h.gh.check(shaA).Summary).ID == task
		})
		sent := h.k.sent()
		if sent[len(sent)-1].ContextID != "" {
			t.Fatal("quota resume reused a failed session")
		}
	}
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "review passes on the same check", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	if h.gh.checkCount() != 1 || h.k.rejections() != 0 {
		t.Fatal("quota resumes created another check or reused a message identity")
	}
}

func TestExhaustedBrokerPreventsSendUntilUsable(t *testing.T) {
	h := newHarness(t)
	addPR7(h)
	reset := time.Now().Add(24 * time.Hour).UTC().Truncate(time.Second)
	q := &fakeQuota{reading: github.QuotaReading{Exhausted: true, ResetsAt: reset, ReceivedAt: time.Now()}}
	h.cfg.Quota = q
	h.cfg.Concurrency = 1
	e, ctx, stop := h.start()
	e.Tick(ctx)
	rec := waitForQuotaCheck(t, h)
	if rec.Attempt != 1 || rec.QuotaRetry != 0 || len(h.k.sent()) != 0 {
		t.Fatalf("exhausted grant sent work: %+v, sends %d", rec, len(h.k.sent()))
	}
	if !strings.Contains(h.gh.check(shaA).Summary, reset.Format(time.RFC3339)) {
		t.Fatal("check omits the broker's reset time")
	}
	started := testutil.ToFloat64(h.m.ReviewsStarted.WithLabelValues("homelab", "reattach"))
	for range 3 {
		e.Tick(ctx)
	}
	if len(h.k.sent()) != 0 || testutil.ToFloat64(h.m.ReviewsStarted.WithLabelValues("homelab", "reattach")) != started {
		t.Fatal("polling a waiting check acquired a review worker")
	}
	stop()
	e, ctx, _ = h.start()
	q.set(github.QuotaReading{ReceivedAt: rec.QuotaSince.Add(time.Second)}, nil)
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "usable grant completes review", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	if testutil.ToFloat64(h.m.Retries.WithLabelValues("homelab")) != 0 || h.gh.checkCount() != 1 {
		t.Fatal("preflight wait spent an attempt or replaced the check")
	}
}

func TestQuotaReadFailureWaitsWithoutSending(t *testing.T) {
	h := newHarness(t)
	addPR7(h)
	q := &fakeQuota{err: errors.New("broker unavailable")}
	h.cfg.Quota = q
	e, ctx, _ := h.start()
	e.Tick(ctx)
	rec := waitForQuotaCheck(t, h)
	e.Tick(ctx)
	if len(h.k.sent()) != 0 {
		t.Fatal("unreadable quota allowed a send")
	}
	q.set(github.QuotaReading{ReceivedAt: rec.QuotaSince.Add(time.Second)}, nil)
	runReview(t, h, e, ctx, 7, shaA)
}

// The quota changes after preflight while the first task is running.
// Terminal reports must still complete, even when new dispatch is blocked.
func TestExhaustedQuotaDoesNotBlockCompletedReport(t *testing.T) {
	h := newHarness(t)
	addPR7(h)
	q := &fakeQuota{}
	h.cfg.Quota = q
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	q.set(github.QuotaReading{Exhausted: true, ReceivedAt: time.Now()}, nil)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "completed report despite exhausted quota", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	if len(h.k.sent()) != 1 {
		t.Fatal("completed report sent another task")
	}
}

// Split requests need no model call, including for checks an older process
// left waiting before it checked the diff budget.
func TestOversizedDiffSplitsWhileQuotaExhausted(t *testing.T) {
	for _, reattach := range []bool{false, true} {
		t.Run(fmt.Sprintf("reattach=%t", reattach), func(t *testing.T) {
			h := newHarness(t)
			h.gh.addPR(&fakePR{Number: 7, Head: shaA, Files: []github.PullRequestFile{
				{Path: "a.go", Status: "modified", Additions: 400},
				{Path: "b.go", Status: "added", Additions: 101},
				{Path: "go.sum", Status: "modified", Additions: 9000},
			}})
			h.cfg.Quota = &fakeQuota{reading: github.QuotaReading{Exhausted: true, ReceivedAt: time.Now()}}
			var checkID int64
			if reattach {
				// Leave a durable waiting check, then restore the normal
				// budget before a new process attaches to that same head.
				h.cfg.Bindings[0].MaxDiffLines = 1000
				e, ctx, stop := h.start()
				e.Tick(ctx)
				waitForQuotaCheck(t, h)
				checkID = h.gh.check(shaA).ID
				stop()
				h.cfg.Bindings[0].MaxDiffLines = 500
			}
			e, ctx, _ := h.start()
			e.Tick(ctx)
			eventually(t, "split request while quota is exhausted", func() bool {
				c := h.gh.check(shaA)
				return c != nil && c.Status == "completed"
			})
			c := h.gh.check(shaA)
			if c.Conclusion != ConclusionFailure || c.Title != "Split this PR" ||
				!strings.Contains(c.Summary, "501 changed lines in 2 files, not counting 1 generated files") ||
				!strings.Contains(c.Summary, splitOnlyMarker) {
				t.Fatalf("split check = %+v", c)
			}
			if reattach && c.ID != checkID {
				t.Fatal("reattachment replaced the waiting check")
			}
			if len(h.k.sent()) != 0 || testutil.ToFloat64(h.m.Retries.WithLabelValues("homelab")) != 0 ||
				testutil.ToFloat64(h.m.SplitRequests.WithLabelValues("homelab")) != 1 {
				t.Fatal("split request sent a task, spent an attempt or was not counted")
			}
			if reattach && testutil.ToFloat64(h.m.ReviewsStarted.WithLabelValues("homelab", "reattach")) != 0 {
				t.Fatal("splitting a waiting check acquired a review worker")
			}
		})
	}
}

func TestQuotaWaitReleasesWorkerWhenDiffUnreadable(t *testing.T) {
	h := newHarness(t)
	addPR7(h)
	h.cfg.Quota = &fakeQuota{reading: github.QuotaReading{Exhausted: true}}
	h.gh.failWith("GET /repos/"+testRepo+"/pulls/7/files", 502)
	e, ctx, _ := h.start()
	e.Tick(ctx)
	rec := waitForQuotaCheck(t, h)
	e.Tick(ctx)
	if rec.Attempt != 1 || len(h.k.sent()) != 0 || testutil.ToFloat64(h.m.InFlight) != 0 ||
		testutil.ToFloat64(h.m.Retries.WithLabelValues("homelab")) != 0 {
		t.Fatal("unreadable diff spent an attempt or held a worker during quota exhaustion")
	}
}

// A quota pause keeps the review's root span open. The resumed worker
// continues it, so the review is one root span with its verdict and the
// usage of both tasks.
func TestQuotaPauseKeepsOneReviewRoot(t *testing.T) {
	exporter := captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	q := &fakeQuota{}
	h.cfg.Quota = q
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.stamp(task, map[string]any{"inputTokens": float64(5), "outputTokens": float64(1)})
	h.k.finish(task, kagent.StateFailed, "usage_limit_reached for account 1234")
	rec := waitForQuotaCheck(t, h)
	q.set(github.QuotaReading{ReceivedAt: rec.QuotaSince.Add(time.Second)}, nil)
	e.Tick(ctx)
	id := h.msgID(7, shaA, 1) + "/quota-1"
	eventually(t, "resumed quota task", func() bool {
		task = h.k.taskFor(id)
		return task != ""
	})
	h.k.stamp(task, map[string]any{"inputTokens": float64(7), "outputTokens": float64(2)})
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "review passes", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	awaitReviewRoot(t, exporter)
	stop()
	var roots []map[string]string
	var events []string
	for _, span := range exporter.GetSpans() {
		if span.Name != "review" || span.Parent.IsValid() {
			continue
		}
		attrs := map[string]string{}
		for _, a := range span.Attributes {
			attrs[string(a.Key)] = a.Value.Emit()
		}
		roots = append(roots, attrs)
		for _, ev := range span.Events {
			events = append(events, ev.Name)
		}
	}
	if len(roots) != 1 {
		t.Fatalf("got %d review roots, want 1: %v", len(roots), roots)
	}
	for k, v := range map[string]string{"review.verdict": ConclusionSuccess, "tokens.input": "12", "tokens.output": "3", "pr.head_sha": shaA} {
		if roots[0][k] != v {
			t.Errorf("%s = %q, want %q", k, roots[0][k], v)
		}
	}
	if !slices.Contains(events, "review paused") || !slices.Contains(events, "review resumed") {
		t.Errorf("root events = %v", events)
	}
}

// awaitReviewRoot waits for the worker to end the review root. The check's
// conclusion is visible before the worker ends the root, and a stop in that
// gap cancels the worker's check write, which leaves the root to the next
// process.
func awaitReviewRoot(t *testing.T, exporter *tracetest.InMemoryExporter) {
	t.Helper()
	eventually(t, "the review root", func() bool { return len(reviewSpans(exporter)) > 0 })
}

// reviewSpans returns the attributes and events of each exported review
// span, with whether it is a root.
func reviewSpans(exporter *tracetest.InMemoryExporter) (spans []map[string]string) {
	for _, span := range exporter.GetSpans() {
		if span.Name != "review" {
			continue
		}
		attrs := map[string]string{"root": fmt.Sprint(!span.Parent.IsValid()), "id": span.SpanContext.SpanID().String()}
		for _, a := range span.Attributes {
			attrs[string(a.Key)] = a.Value.Emit()
		}
		for _, ev := range span.Events {
			attrs["event."+ev.Name] = "true"
		}
		spans = append(spans, attrs)
	}
	return spans
}

// A process that stops while its review waits for quota exports no root.
// The next process continues the same root span with the usage recorded
// before the stop.
func TestRestartedReviewKeepsOneRoot(t *testing.T) {
	exporter := captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	q := &fakeQuota{}
	h.cfg.Quota = q
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.stamp(task, map[string]any{"inputTokens": float64(5), "outputTokens": float64(1)})
	h.k.finish(task, kagent.StateFailed, "insufficient_quota")
	rec := waitForQuotaCheck(t, h)
	stop()
	if spans := reviewSpans(exporter); len(spans) != 0 {
		t.Fatalf("stopped process exported the paused root: %v", spans)
	}
	e, ctx, stop = h.start()
	q.set(github.QuotaReading{ReceivedAt: rec.QuotaSince.Add(time.Second)}, nil)
	e.Tick(ctx)
	id := h.msgID(7, shaA, 1) + "/quota-1"
	eventually(t, "resumed quota task", func() bool {
		task = h.k.taskFor(id)
		return task != ""
	})
	h.k.stamp(task, map[string]any{"inputTokens": float64(7), "outputTokens": float64(2)})
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "review passes", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	awaitReviewRoot(t, exporter)
	stop()
	spans := reviewSpans(exporter)
	if len(spans) != 1 {
		t.Fatalf("review spans = %v, want one root", spans)
	}
	for k, v := range map[string]string{"root": "true", "review.verdict": ConclusionSuccess, "tokens.input": "12", "tokens.output": "3", "pr.number": "7", "pr.head_sha": shaA, "event.review resumed": "true"} {
		if spans[0][k] != v {
			t.Errorf("%s = %q, want %q", k, spans[0][k], v)
		}
	}
}

// A quota wait on the current head past the parked-root limit exports no
// root. The resumed worker continues the same root, so the review exports
// it once.
func TestLongQuotaWaitKeepsOneRoot(t *testing.T) {
	exporter := captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	q := &fakeQuota{}
	h.cfg.Quota = q
	var clockMu sync.Mutex
	now := time.Now()
	h.cfg.Now = func() time.Time { clockMu.Lock(); defer clockMu.Unlock(); return now }
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.stamp(task, map[string]any{"inputTokens": float64(5), "outputTokens": float64(1)})
	h.k.finish(task, kagent.StateFailed, "insufficient_quota")
	rec := waitForQuotaCheck(t, h)
	clockMu.Lock()
	now = now.Add(parkedLimit + time.Hour)
	clockMu.Unlock()
	e.Tick(ctx)
	e.wg.Wait()
	if spans := reviewSpans(exporter); len(spans) != 0 {
		t.Fatalf("expiry exported a root that its check still holds: %v", spans)
	}
	q.set(github.QuotaReading{ReceivedAt: rec.QuotaSince.Add(time.Second)}, nil)
	e.Tick(ctx)
	id := h.msgID(7, shaA, 1) + "/quota-1"
	eventually(t, "resumed quota task", func() bool {
		task = h.k.taskFor(id)
		return task != ""
	})
	h.k.stamp(task, map[string]any{"inputTokens": float64(7), "outputTokens": float64(2)})
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "review passes", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	e.wg.Wait()
	spans := reviewSpans(exporter)
	if len(spans) != 1 || spans[0]["root"] != "true" || spans[0]["review.verdict"] != ConclusionSuccess || spans[0]["tokens.input"] != "12" {
		t.Fatalf("review spans = %v, want one root with the verdict and both tasks", spans)
	}
}

// A task whose usage was recorded before a failed PR read and a restart is
// counted once on the continued root.
func TestRestartAfterFailedRevalidationCountsTaskOnce(t *testing.T) {
	exporter := captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.gh.mu.Lock()
	h.gh.failGQL = map[string]bool{"pull_request": true}
	h.gh.mu.Unlock()
	h.k.stamp(task, map[string]any{"inputTokens": float64(5), "outputTokens": float64(1)})
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "failed revalidation", func() bool {
		return h.gh.graphqlCalls("pull_request") > 0 && testutil.ToFloat64(h.m.InFlight) == 0
	})
	stop()
	h.gh.mu.Lock()
	h.gh.failGQL = nil
	h.gh.mu.Unlock()
	e, ctx, stop = h.start()
	e.Tick(ctx)
	eventually(t, "review passes", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	awaitReviewRoot(t, exporter)
	stop()
	spans := reviewSpans(exporter)
	if len(spans) != 1 || spans[0]["root"] != "true" || spans[0]["tokens.input"] != "5" || spans[0]["review.verdict"] != ConclusionSuccess {
		t.Fatalf("review spans = %v, want one root with the task counted once", spans)
	}
}

// A task that completes before a failed PR read reaches the code span that
// the next poll resumes, and the root counts it once.
func TestFailedRevalidationKeepsCodeSpanUsage(t *testing.T) {
	exporter := captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.gh.mu.Lock()
	h.gh.failGQL = map[string]bool{"pull_request": true}
	h.gh.mu.Unlock()
	h.k.stamp(task, map[string]any{"inputTokens": float64(5), "outputTokens": float64(1)})
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "failed revalidation", func() bool {
		return h.gh.graphqlCalls("pull_request") > 0 && testutil.ToFloat64(h.m.InFlight) == 0
	})
	h.gh.mu.Lock()
	h.gh.failGQL = nil
	h.gh.mu.Unlock()
	e.Tick(ctx)
	eventually(t, "review passes", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	awaitReviewRoot(t, exporter)
	stop()
	var roots, codes []map[string]string
	for _, span := range exporter.GetSpans() {
		attrs := map[string]string{}
		for _, a := range span.Attributes {
			attrs[string(a.Key)] = a.Value.Emit()
		}
		switch {
		case span.Name == "review" && !span.Parent.IsValid():
			roots = append(roots, attrs)
		case span.Name == "code review":
			codes = append(codes, attrs)
		}
	}
	if len(roots) != 1 || roots[0]["tokens.input"] != "5" || len(codes) == 0 {
		t.Fatalf("roots = %v, code spans = %v", roots, codes)
	}
	if last := codes[len(codes)-1]; last["tokens.input"] != "5" || last["tokens.output"] != "1" {
		t.Fatalf("resumed code span usage = %v", last)
	}
	// The span that the failed read ended has the task's report.
	if len(codes) != 2 {
		t.Fatalf("code spans = %v, want the failed read's span and the resumed one", codes)
	}
	first, last := codes[0], codes[1]
	for _, k := range []string{"review.verdict", "review.findings.high", "review.findings.medium", "review.round", "tokens.input"} {
		if first[k] == "" || first[k] != last[k] {
			t.Errorf("%s = %q on the failed read's span, %q on the resumed span", k, first[k], last[k])
		}
	}
	if first["review.outcome"] != "revalidation_failed" {
		t.Errorf("failed read's span outcome = %q", first["review.outcome"])
	}
}

// A root restored from its check state keeps the review round that the
// state records.
func TestRestoredRootKeepsRound(t *testing.T) {
	exporter := captureReviewSpans(t)
	ts := &TraceState{Parent: "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01", Start: time.Now(), PR: 7, Round: 3}
	_, root := restoreRoot(context.Background(), ts, github.PullRequest{Number: 7, HeadSHA: shaA})
	root.End()
	spans := reviewSpans(exporter)
	if len(spans) != 1 || spans[0]["review.round"] != "3" || spans[0]["id"] != "0123456789abcdef" {
		t.Fatalf("restored root = %v", spans)
	}
}

// The check state saves the round with the root, so a restart restores it.
func TestCheckStateSavesReviewRound(t *testing.T) {
	captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	h.cfg.Quota = &fakeQuota{}
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateFailed, "insufficient_quota")
	waitForQuotaCheck(t, h)
	st, _ := LoadState(h.gh.check(shaA).Text)
	if st.Trace == nil || st.Trace.Round != 1 || st.Trace.PR != 7 {
		t.Fatalf("trace state = %+v", st.Trace)
	}
}

// A superseded head's review that waits for quota ends once: its check is
// completed as cancelled, which ends the parked root. A failed check write
// keeps the root parked, and the next poll ends it.
func TestSupersededParkedRootEndsOnce(t *testing.T) {
	exporter := captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	h.cfg.Quota = &fakeQuota{}
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateFailed, "insufficient_quota")
	waitForQuotaCheck(t, h)
	shaB := sha40('b')
	h.gh.push(7, shaB)
	patchOld := fmt.Sprintf("PATCH /repos/%s/check-runs/%d", testRepo, h.gh.check(shaA).ID)
	h.gh.failWith(patchOld, http.StatusBadGateway)
	e.Tick(ctx)
	var newer string
	eventually(t, "the new head's task", func() bool { newer = h.taskOn(7, shaB, 1); return newer != "" })
	h.k.finish(newer, kagent.StateFailed, "insufficient_quota")
	eventually(t, "the new head's quota wait", func() bool {
		c := h.gh.check(shaB)
		return c != nil && c.Title == "Waiting for Codex quota" && testutil.ToFloat64(h.m.InFlight) == 0
	})
	old := func() (spans []map[string]string) {
		for _, span := range reviewSpans(exporter) {
			if span["pr.head_sha"] == shaA {
				spans = append(spans, span)
			}
		}
		return spans
	}
	if c := h.gh.check(shaA); c.Status != "in_progress" || len(old()) != 0 {
		t.Fatalf("failed write: check %s, old head's spans %v", c.Status, old())
	}
	h.gh.failWith(patchOld, 0)
	e.Tick(ctx)
	e.wg.Wait()
	e.Tick(ctx)
	e.wg.Wait()
	stop()
	if c := h.gh.check(shaA); c.Conclusion != ConclusionCancelled {
		t.Fatalf("old head's check = %s %s", c.Status, c.Conclusion)
	}
	if spans := old(); len(spans) != 1 || spans[0]["root"] != "true" || spans[0]["review.verdict"] != ConclusionCancelled {
		t.Fatalf("old head's review spans = %v, want one cancelled root", spans)
	}
}

// A process stops while a head waits for quota, and a push replaces that
// head before the next process starts. The new head's review completes the
// old check as cancelled and ends the old head's recorded root once.
func TestRestartThenPushEndsOldRootOnce(t *testing.T) {
	for _, tc := range []struct {
		name string
		// pushes are the heads pushed while the reviewer is offline.
		pushes []string
		// failOnce makes GitHub refuse the first cancellation of head A.
		failOnce bool
	}{
		{name: "one push", pushes: []string{sha40('b')}},
		{name: "two pushes", pushes: []string{sha40('b'), sha40('c')}},
		{name: "cancellation refused once", pushes: []string{sha40('b')}, failOnce: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			exporter := captureReviewSpans(t)
			h := newHarness(t)
			addPR7(h)
			q := &fakeQuota{}
			h.cfg.Quota = q
			e, ctx, stop := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(task, kagent.StateFailed, "insufficient_quota")
			waitForQuotaCheck(t, h)
			stop()
			for _, sha := range tc.pushes {
				h.gh.push(7, sha)
			}
			head := tc.pushes[len(tc.pushes)-1]
			update := fmt.Sprintf("PATCH /repos/%s/check-runs/%d", testRepo, h.gh.check(shaA).ID)
			if tc.failOnce {
				h.gh.failWith(update, 502)
			}
			e, ctx, stop = h.start()
			e.Tick(ctx)
			eventually(t, "the new head's task", func() bool { return h.taskOn(7, head, 1) != "" })
			if tc.failOnce {
				if c := h.gh.check(shaA); c.Status == "completed" {
					t.Fatal("old head's check completed despite the refused write")
				}
				h.gh.failWith(update, 0)
			}
			e.Tick(ctx)
			stop()
			if c := h.gh.check(shaA); c.Status != "completed" || c.Conclusion != ConclusionCancelled {
				t.Fatalf("old head's check = %s %s", c.Status, c.Conclusion)
			}
			var old []map[string]string
			for _, span := range reviewSpans(exporter) {
				if span["pr.head_sha"] == shaA {
					old = append(old, span)
				}
			}
			if len(old) != 1 || old[0]["root"] != "true" || old[0]["review.verdict"] != ConclusionCancelled {
				t.Fatalf("old head's review spans = %v, want one cancelled root", old)
			}
		})
	}
}

func TestRestartAfterCloseEndsPausedRootOnce(t *testing.T) {
	exporter := captureReviewSpans(t)
	h := newHarness(t)
	addPR7(h)
	h.cfg.Quota = &fakeQuota{}
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateFailed, "insufficient_quota")
	waitForQuotaCheck(t, h)
	state, _ := LoadState(h.gh.check(shaA).Text)
	if state.Trace == nil {
		t.Fatal("paused check lost its root")
	}
	recorded, ok := state.Trace.spanContext()
	if !ok {
		t.Fatal("invalid persisted trace context")
	}
	stop()
	for n := range maxPreviousLookups {
		h.gh.push(7, fmt.Sprintf("%040x", n+1))
	}
	h.gh.withLock(func() { h.gh.prs[7].Closed = true })
	update := fmt.Sprintf("PATCH /repos/%s/check-runs/%d", testRepo, h.gh.check(shaA).ID)
	h.gh.failWith(update, http.StatusBadGateway)
	e, ctx, stop = h.start()
	writes := h.gh.countCalls(update)
	e.Tick(ctx) // Newest page.
	if h.gh.countCalls(update) != writes {
		t.Fatal("cancelled an older page before following the cursor")
	}
	e.Tick(ctx) // Refused cancellation on the older page.
	if h.gh.countCalls(update) != writes+1 || h.gh.check(shaA).Status != "in_progress" || len(reviewSpans(exporter)) != 0 {
		t.Fatal("refused cancellation retired the review")
	}
	h.gh.failWith(update, 0)
	e.Tick(ctx)
	e.Tick(ctx)
	stop()
	c := h.gh.check(shaA)
	spans := reviewSpans(exporter)
	if c.Status != "completed" || c.Conclusion != ConclusionCancelled || len(spans) != 1 ||
		spans[0]["id"] != recorded.SpanID().String() || spans[0]["root"] != "true" || spans[0]["pr.head_sha"] != shaA || spans[0]["review.verdict"] != ConclusionCancelled {
		t.Fatalf("check %s %s, review spans %v", c.Status, c.Conclusion, spans)
	}
}
