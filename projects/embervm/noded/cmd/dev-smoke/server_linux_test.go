package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func testPool(count int) *scanPool {
	p := &scanPool{}
	for range count {
		p.slots = append(p.slots, &scanSlot{state: "ready", jobs: make(chan scanJob, 1)})
	}
	return p
}

func scanRequest(body string) *http.Request {
	r := httptest.NewRequest(http.MethodPost, "/scan", strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	return r
}

func nextJob(t *testing.T, slot *scanSlot) scanJob {
	t.Helper()
	select {
	case job := <-slot.jobs:
		return job
	case <-time.After(2 * time.Second):
		t.Fatal("scan was not dispatched")
		return scanJob{}
	}
}

func TestPoolRejectsOverflowAndBecomesNotReady(t *testing.T) {
	p := testPool(4)
	h := p.handler()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	results := make(chan int, 4)
	for range 4 {
		go func() {
			w := httptest.NewRecorder()
			h.ServeHTTP(w, scanRequest(`{}`).WithContext(ctx))
			results <- w.Code
		}()
	}
	var jobs []scanJob
	for _, slot := range p.slots {
		job := nextJob(t, slot)
		jobs = append(jobs, job)
	}
	for _, req := range []*http.Request{
		scanRequest(`{}`), httptest.NewRequest(http.MethodGet, "/readyz", nil),
	} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, req)
		if w.Code != 503 {
			t.Fatalf("%s: got %d, want 503", req.URL.Path, w.Code)
		}
	}
	p.mu.Lock()
	s := p.statusLocked()
	p.mu.Unlock()
	if s.Accepted != 4 || s.Rejected != 1 || s.Active != 4 || s.Ready != 0 || s.Peak != 4 {
		t.Fatalf("unexpected status: %+v", s)
	}
	for _, job := range jobs {
		job.done <- scanOutcome{}
	}
	for range 4 {
		if code := <-results; code != 200 {
			t.Fatalf("accepted scan got HTTP %d", code)
		}
	}
}

func TestInvalidRequestsAndDrainDoNotClaimVM(t *testing.T) {
	p := testPool(1)
	h := p.handler()
	w := httptest.NewRecorder()
	h.ServeHTTP(w, scanRequest(`{"files":99999}`))
	if w.Code != 400 || len(p.slots[0].jobs) != 0 {
		t.Fatal("invalid workload was admitted")
	}
	p.totals.Draining = true
	for _, req := range []*http.Request{scanRequest(`{}`), httptest.NewRequest(http.MethodGet, "/readyz", nil)} {
		w = httptest.NewRecorder()
		h.ServeHTTP(w, req)
		if w.Code != 503 || len(p.slots[0].jobs) != 0 {
			t.Fatal("draining pool admitted work or remained ready")
		}
	}
}

func TestDisconnectCancelsDispatchedScan(t *testing.T) {
	p := testPool(1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		p.handler().ServeHTTP(httptest.NewRecorder(), scanRequest(`{}`).WithContext(ctx))
	}()
	job := nextJob(t, p.slots[0])
	cancel()
	select {
	case <-job.ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("guest request did not inherit client cancellation")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("handler did not return after disconnect")
	}
}
