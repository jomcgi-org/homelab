package dispatch

import (
	"context"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// HandlePlannerClaim hands a claimed itsaplan run to planner admission.
func (e *Engine) HandlePlannerClaim(ctx context.Context, run *itsaplan.Run) error {
	return e.handlePlannerClaim(ctx, run)
}

// AwaitWorker exposes the worker wait to the package's tests.
func (e *Engine) AwaitWorker(ctx context.Context, a *store.Attempt) bool {
	return e.awaitWorker(ctx, a)
}

// PruneWorkerHolds exposes the wait's pruning to the package's tests.
func (e *Engine) PruneWorkerHolds(open map[int64]bool) { e.workers.prune(open) }

// WorkerHolds counts the entries the worker wait keeps: held sends and sends
// let through.
func (e *Engine) WorkerHolds() (waiting, released int) {
	e.workers.mu.Lock()
	defer e.workers.mu.Unlock()
	return len(e.workers.waiting), len(e.workers.released)
}
