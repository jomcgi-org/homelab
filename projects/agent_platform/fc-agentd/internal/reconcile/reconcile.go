// Package reconcile holds the fc-agentd control loop. In Phase 0 it is a no-op
// loop that proves the daemon idles cleanly: it ticks on an interval, reads the
// threads pinned to this node from the registry, and does nothing with them yet.
// Phases 1-3 fill in the create/restore/snapshot/reclaim transitions.
package reconcile

import (
	"context"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/store"
)

var tracer = otel.Tracer("fc-agentd/reconcile")

// Registry is the subset of the store the loop depends on. Keeping it an
// interface lets the loop be tested with a fake (no Postgres).
type Registry interface {
	ListThreadsForNode(ctx context.Context, node string) ([]store.Thread, error)
}

// Loop drives one reconcile pass per tick.
type Loop struct {
	Registry Registry
	Node     string
	Interval time.Duration
	Logger   *slog.Logger
}

// Run ticks until ctx is cancelled. It returns nil on graceful shutdown.
func (l *Loop) Run(ctx context.Context) error {
	log := l.Logger
	if log == nil {
		log = slog.Default()
	}
	interval := l.Interval
	if interval <= 0 {
		interval = 5 * time.Second
	}

	log.Info("reconcile loop starting", "node", l.Node, "interval", interval.String())
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	// Run one pass immediately so startup is observable without waiting a tick.
	l.reconcileOnce(ctx, log)

	for {
		select {
		case <-ctx.Done():
			log.Info("reconcile loop stopping")
			return nil
		case <-ticker.C:
			l.reconcileOnce(ctx, log)
		}
	}
}

// reconcileOnce performs a single desired-vs-actual pass. Phase 0: read-only.
func (l *Loop) reconcileOnce(ctx context.Context, log *slog.Logger) {
	ctx, span := tracer.Start(ctx, "reconcileOnce")
	defer span.End()

	if l.Registry == nil {
		// Dry-run mode (no Postgres configured): nothing to reconcile.
		return
	}

	threads, err := l.Registry.ListThreadsForNode(ctx, l.Node)
	if err != nil {
		log.Error("reconcile: list threads failed", "err", err)
		span.RecordError(err)
		return
	}
	// Phase 0 is a no-op beyond observability; later phases act on transitions.
	log.Debug("reconcile pass", "node", l.Node, "threads", len(threads))
}
