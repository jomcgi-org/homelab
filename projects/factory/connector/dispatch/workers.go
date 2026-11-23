package dispatch

import (
	"context"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// capacityMaxAge is how old a worker count may be and still decide. The
// reaper writes each 15 s, so three missed passes make a reading unknown.
const capacityMaxAge = 45 * time.Second

// workerHolds is the in-process state of the worker wait. Both maps are keyed
// by attempt ID and pruned with the runs that are no longer open.
type workerHolds struct {
	mu sync.Mutex
	// waiting holds when each held send first found no worker.
	waiting map[int64]hold
	// released holds each new-session send let through, with the pool it
	// takes a worker from. The reaper's row does not count it until its next
	// pass, so the next decision subtracts it.
	released map[int64]release
}

type hold struct {
	run   int64
	first time.Time
}

type release struct {
	run  int64
	pool string // namespace/agent
	at   time.Time
}

// prune drops the entries of runs that are no longer open.
func (w *workerHolds) prune(open map[int64]bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for id, h := range w.waiting {
		if !open[h.run] {
			delete(w.waiting, id)
		}
	}
	for id, r := range w.released {
		if !open[r.run] {
			delete(w.released, id)
		}
	}
}

// awaitWorker reports whether to hold a send because its WorkerPool has no
// worker for it. A held attempt has not been issued: it stays `sending`, so
// the pool's pending demand counts it once the connector publishes that
// series (#8591), which wakes an idle pool, and no ambiguous outcome is
// recorded. kagent answers a resume with no free worker ("no worker has
// room") in a way the connector must treat as ambiguous (HOME-131), so the
// wait prevents the send, not the failure.
//
// A new session (an initial send) needs a free worker: one that is ready and
// hosts no actor, less the sends this connector let through since the reaper
// wrote its row, because that row does not count them yet. A continuation
// needs a ready worker, because an idle session's actor may already hold its
// worker.
//
// The wait is bounded by Config.WorkerWait from the first held pass. It never
// holds on a missing, stale or failed reading, nor after the bound: those send
// as before. Zero WorkerWait turns the wait off.
//
// Known limit: the reservation counts only this process's sends, and the
// reaper's pass is 15 s apart. A second connector, or an actor placed by
// another caller (the reviewer, Bosun) in the same window, can still take the
// worker. Such a send fails as before.
func (e *Engine) awaitWorker(ctx context.Context, a *store.Attempt) bool {
	if e.cfg.WorkerWait <= 0 {
		return false
	}
	pool := a.KagentNamespace + "/" + a.KagentAgent
	c, found, err := e.st.WorkerPoolCapacity(ctx, a.KagentNamespace, a.KagentAgent)
	if err != nil {
		e.log.WarnContext(ctx, "worker pool capacity unreadable; sending", "attempt", a.ID, "error", err)
	}
	w := &e.workers
	now := e.cfg.Now()
	w.mu.Lock()
	defer w.mu.Unlock()
	if err != nil || !found || c.Age > capacityMaxAge {
		delete(w.waiting, a.ID)
		e.m.WorkerWaits.WithLabelValues("unknown").Inc()
		return false
	}
	have := c.Ready
	if a.Kind == "initial" {
		have = c.Ready - c.Busy
		// Sends released after the reaper wrote the row, not counting this one.
		since := now.Add(-c.Age)
		for id, r := range w.released {
			if id != a.ID && r.pool == pool && !r.at.Before(since) {
				have--
			}
		}
	}
	if have >= 1 {
		delete(w.waiting, a.ID)
		if a.Kind == "initial" {
			if _, ok := w.released[a.ID]; !ok {
				if w.released == nil {
					w.released = map[int64]release{}
				}
				w.released[a.ID] = release{run: a.RunID, pool: pool, at: now}
			}
		}
		e.m.WorkerWaits.WithLabelValues("ready").Inc()
		return false
	}
	h, held := w.waiting[a.ID]
	if !held {
		if w.waiting == nil {
			w.waiting = map[int64]hold{}
		}
		h = hold{run: a.RunID, first: now}
		w.waiting[a.ID] = h
	}
	if now.Sub(h.first) >= e.cfg.WorkerWait {
		delete(w.waiting, a.ID)
		e.m.WorkerWaits.WithLabelValues("timeout").Inc()
		return false
	}
	e.m.WorkerWaits.WithLabelValues("waiting").Inc()
	return true
}
