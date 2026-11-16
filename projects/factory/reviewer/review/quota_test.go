package review

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"

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
