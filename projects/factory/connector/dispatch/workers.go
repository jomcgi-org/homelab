package dispatch

import (
	"context"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// capacityMaxAge is how old a worker count may be and still decide. The
// reaper writes each 15 s, so three missed passes make a reading unknown.
const capacityMaxAge = 45 * time.Second

// awaitWorker reports whether to hold a send because its WorkerPool has no
// worker for it. A held attempt has not been issued: it stays `sending`, so it
// counts in the pool's pending demand (which wakes an idle pool) and no
// ambiguous outcome is recorded. kagent answers a resume with no free worker
// ("no worker has room") in a way the connector must treat as ambiguous
// (HOME-131), so the wait prevents the send, not the failure.
//
// A new session (an initial send) needs a free worker: one that is ready and
// hosts no actor. A continuation needs only a ready worker, because an idle
// session's actor may already hold its worker.
//
// The wait is bounded by Config.WorkerWait from the first held pass. It never
// holds on a missing, stale or failed reading, nor after the bound: those send
// as before. Zero WorkerWait turns the wait off.
func (e *Engine) awaitWorker(ctx context.Context, a *store.Attempt) bool {
	if e.cfg.WorkerWait <= 0 {
		return false
	}
	c, found, err := e.st.WorkerPoolCapacity(ctx, a.KagentNamespace, a.KagentAgent)
	if err != nil {
		e.log.WarnContext(ctx, "worker pool capacity unreadable; sending", "attempt", a.ID, "error", err)
	}
	if err != nil || !found || c.Age > capacityMaxAge {
		e.workerWaits.Delete(a.ID)
		e.m.WorkerWaits.WithLabelValues("unknown").Inc()
		return false
	}
	have := c.Ready
	if a.Kind == "initial" {
		have = c.Ready - c.Busy
	}
	if have >= 1 {
		e.workerWaits.Delete(a.ID)
		e.m.WorkerWaits.WithLabelValues("ready").Inc()
		return false
	}
	now := e.cfg.Now()
	first, _ := e.workerWaits.LoadOrStore(a.ID, now)
	if now.Sub(first.(time.Time)) >= e.cfg.WorkerWait {
		e.workerWaits.Delete(a.ID)
		e.m.WorkerWaits.WithLabelValues("timeout").Inc()
		return false
	}
	e.m.WorkerWaits.WithLabelValues("waiting").Inc()
	return true
}
