// Package reconcile is the fc-agentd control loop (ADR 022, Phase 3). Each pass
// drives the live microVMs toward the desired state recorded in the Postgres
// registry: create PENDING threads, restore IDLE threads with a pending wake,
// reclaim COMPLETED threads, GC idle-expired threads, and re-adopt RUNNING
// threads orphaned by a daemon restart. Durable state lives in Postgres, so the
// loop is stateless across restarts (it rebuilds live handles from the table).
package reconcile

import (
	"context"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/control"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/store"
	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

var tracer = otel.Tracer("fc-agentd/reconcile")

// Registry is the store surface the loop reads and writes.
type Registry interface {
	ListThreadsForNode(ctx context.Context, node string) ([]store.Thread, error)
	ListByStateForNode(ctx context.Context, node string, state substrate.State) ([]store.Thread, error)
	ListWakeRequestedForNode(ctx context.Context, node string) ([]store.Thread, error)
	ListIdleExpiredForNode(ctx context.Context, node string) ([]store.Thread, error)
	SetState(ctx context.Context, threadID string, state substrate.State) error
	SetThreadSnapshot(ctx context.Context, threadID, ref string, sizeBytes int64) error
	ClearWake(ctx context.Context, threadID string) error
	Delete(ctx context.Context, threadID string) error
}

// Executor is the microVM lifecycle surface (the FC driver satisfies it).
type Executor interface {
	Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error)
	Restore(ctx context.Context, ref substrate.SnapshotRef) (substrate.Handle, error)
	Release(ctx context.Context, h substrate.Handle) error
}

// Reclaimer deletes a thread's on-disk snapshot bundle (the FC driver satisfies
// it). Kept separate from Executor because it is not part of the Substrate seam.
type Reclaimer interface {
	RemoveBundle(threadID string) error
}

// Loop drives one reconcile pass per tick.
type Loop struct {
	Registry  Registry
	Executor  Executor
	Reclaimer Reclaimer
	Node      string
	Interval  time.Duration
	Logger    *slog.Logger

	// ControlUDS resolves a thread's vsock control UDS path so the loop can serve
	// the per-thread control channel (task delivery + idle/done). nil disables
	// control serving (dry-run / tests with no vsock).
	ControlUDS func(threadID string) string

	live    map[string]substrate.Handle   // threadID -> live microVM handle
	control map[string]context.CancelFunc // threadID -> cancel its control server
	idle    chan idleEvent                // guest idle boundaries, handled on the loop goroutine
}

// idleEvent is a guest's quiescent idle-boundary signal, delivered from a
// control-server goroutine to the loop goroutine so the snapshot runs without
// racing the loop's live/control maps.
type idleEvent struct {
	threadID string
	wake     vsockproto.WakeCondition
}

// Run ticks until ctx is cancelled. It returns nil on graceful shutdown.
func (l *Loop) Run(ctx context.Context) error {
	log := l.Logger
	if log == nil {
		log = slog.Default()
	}
	if l.live == nil {
		l.live = make(map[string]substrate.Handle)
	}
	if l.control == nil {
		l.control = make(map[string]context.CancelFunc)
	}
	if l.idle == nil {
		l.idle = make(chan idleEvent, 32)
	}
	interval := l.Interval
	if interval <= 0 {
		interval = 5 * time.Second
	}

	log.Info("reconcile loop starting", "node", l.Node, "interval", interval.String())
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	l.reconcileOnce(ctx, log)
	for {
		select {
		case <-ctx.Done():
			log.Info("reconcile loop stopping")
			return nil
		case <-ticker.C:
			l.reconcileOnce(ctx, log)
		case ev := <-l.idle:
			l.snapshotIdle(ctx, log, ev)
		}
	}
}

// snapshotIdle captures a thread's microVM at a quiescent boundary, records the
// snapshot as the thread's durable IDLE form, and releases the live VM so an
// idle thread costs nothing but disk. It runs on the loop goroutine, so live and
// control are safe to touch. Restore-on-wake rebuilds the VM from the snapshot.
func (l *Loop) snapshotIdle(ctx context.Context, log *slog.Logger, ev idleEvent) {
	h, ok := l.live[ev.threadID]
	if !ok {
		return // already gone (reclaimed, or a duplicate idle signal)
	}
	snapshotter, ok := l.Executor.(substrate.Snapshotable)
	if !ok {
		log.Warn("reconcile: executor cannot snapshot; leaving thread running", "thread", ev.threadID)
		return
	}
	ref, err := snapshotter.Snapshot(ctx, h)
	if err != nil {
		log.Error("reconcile: snapshot on idle", "thread", ev.threadID, "err", err)
		return
	}
	if err := l.Registry.SetThreadSnapshot(ctx, ev.threadID, ref.ID, ref.SizeBytes); err != nil {
		log.Error("reconcile: record idle snapshot", "thread", ev.threadID, "err", err)
		return
	}
	if err := l.Executor.Release(ctx, h); err != nil {
		log.Error("reconcile: release after snapshot", "thread", ev.threadID, "err", err)
	}
	delete(l.live, ev.threadID)
	if cancel, ok := l.control[ev.threadID]; ok {
		cancel()
		delete(l.control, ev.threadID)
	}
	log.Info("reconcile: thread idled and snapshotted", "thread", ev.threadID, "snapshot", ref.ID, "size", ref.SizeBytes)
}

func (l *Loop) reconcileOnce(ctx context.Context, log *slog.Logger) {
	ctx, span := tracer.Start(ctx, "reconcileOnce")
	defer span.End()

	if l.Registry == nil || l.Executor == nil {
		return // dry-run (no registry/executor configured)
	}

	l.createPending(ctx, log)
	l.restoreWakeRequested(ctx, log)
	l.adoptOrphanedRunning(ctx, log)
	l.reclaimCompleted(ctx, log)
	l.gcIdleExpired(ctx, log)
}

// createPending boots a microVM for each PENDING thread and marks it RUNNING.
func (l *Loop) createPending(ctx context.Context, log *slog.Logger) {
	threads, err := l.Registry.ListByStateForNode(ctx, l.Node, substrate.StatePending)
	if err != nil {
		log.Error("reconcile: list pending", "err", err)
		return
	}
	for _, t := range threads {
		if _, ok := l.live[t.ThreadID]; ok {
			continue
		}
		spec := substrate.ClaimSpec{ThreadID: t.ThreadID, Repo: t.Repo, Branch: t.Branch, Arch: t.Arch}
		if t.BaseSnapshotRef != "" {
			spec.BaseSnapshotRef = substrate.SnapshotRef{ID: t.BaseSnapshotRef, Node: t.Node, Arch: t.Arch, Base: true}
		}
		h, err := l.Executor.Claim(ctx, spec)
		if err != nil {
			log.Error("reconcile: claim pending thread", "thread", t.ThreadID, "err", err)
			_ = l.Registry.SetState(ctx, t.ThreadID, substrate.StateFailed)
			continue
		}
		l.live[t.ThreadID] = h
		if err := l.Registry.SetState(ctx, t.ThreadID, substrate.StateRunning); err != nil {
			log.Error("reconcile: mark running", "thread", t.ThreadID, "err", err)
		}
		l.startControl(ctx, log, t)
	}
}

// startControl serves the per-thread vsock control channel: it hands the guest
// its task (Assign) and reacts to the guest's idle/done signals. The server
// runs until the guest disconnects or the thread is reclaimed (which cancels
// its context). It is a no-op when ControlUDS is unset (dry-run / tests).
func (l *Loop) startControl(ctx context.Context, log *slog.Logger, t store.Thread) {
	if l.ControlUDS == nil {
		return
	}
	if _, ok := l.control[t.ThreadID]; ok {
		return
	}
	cctx, cancel := context.WithCancel(ctx)
	l.control[t.ThreadID] = cancel
	uds := l.ControlUDS(t.ThreadID)
	assign := control.Assignment{ThreadID: t.ThreadID, Recipe: t.Recipe, Task: t.Task}
	handlers := control.Handlers{
		OnIdle: func(threadID string, wake vsockproto.WakeCondition) {
			log.Info("reconcile: thread reached idle boundary", "thread", threadID, "wake", string(wake))
			// Hand off to the loop goroutine so the snapshot does not race live/control.
			select {
			case l.idle <- idleEvent{threadID: threadID, wake: wake}:
			case <-cctx.Done():
			}
		},
		OnDone: func(threadID, status string) {
			log.Info("reconcile: thread harness done", "thread", threadID, "status", status)
			if err := l.Registry.SetState(context.WithoutCancel(cctx), threadID, substrate.StateCompleted); err != nil {
				log.Error("reconcile: mark completed on done", "thread", threadID, "err", err)
			}
		},
	}
	go func() {
		if err := control.Serve(cctx, log, uds, assign, handlers); err != nil && cctx.Err() == nil {
			log.Warn("reconcile: control server exited", "thread", t.ThreadID, "err", err)
		}
	}()
}

// restoreWakeRequested restores IDLE threads that have a pending wake request.
func (l *Loop) restoreWakeRequested(ctx context.Context, log *slog.Logger) {
	threads, err := l.Registry.ListWakeRequestedForNode(ctx, l.Node)
	if err != nil {
		log.Error("reconcile: list wake-requested", "err", err)
		return
	}
	for _, t := range threads {
		if _, ok := l.live[t.ThreadID]; ok {
			_ = l.Registry.ClearWake(ctx, t.ThreadID)
			continue
		}
		h, err := l.Executor.Restore(ctx, refFor(t))
		if err != nil {
			log.Error("reconcile: restore on wake", "thread", t.ThreadID, "err", err)
			continue
		}
		l.live[t.ThreadID] = h
		if err := l.Registry.ClearWake(ctx, t.ThreadID); err != nil {
			log.Error("reconcile: clear wake", "thread", t.ThreadID, "err", err)
		}
	}
}

// adoptOrphanedRunning rebuilds live handles for RUNNING threads the loop does
// not track (e.g. after a daemon restart). With a snapshot it restores;
// otherwise it re-inits via PENDING (snapshots are never load-bearing).
func (l *Loop) adoptOrphanedRunning(ctx context.Context, log *slog.Logger) {
	threads, err := l.Registry.ListByStateForNode(ctx, l.Node, substrate.StateRunning)
	if err != nil {
		log.Error("reconcile: list running", "err", err)
		return
	}
	for _, t := range threads {
		if _, ok := l.live[t.ThreadID]; ok {
			continue
		}
		if t.ThreadSnapshotRef == "" {
			log.Warn("reconcile: orphaned RUNNING with no snapshot; re-initialising", "thread", t.ThreadID)
			_ = l.Registry.SetState(ctx, t.ThreadID, substrate.StatePending)
			continue
		}
		h, err := l.Executor.Restore(ctx, refFor(t))
		if err != nil {
			log.Error("reconcile: re-adopt running", "thread", t.ThreadID, "err", err)
			continue
		}
		l.live[t.ThreadID] = h
	}
}

// reclaimCompleted releases the microVM and deletes the bundle + row for
// COMPLETED threads.
func (l *Loop) reclaimCompleted(ctx context.Context, log *slog.Logger) {
	threads, err := l.Registry.ListByStateForNode(ctx, l.Node, substrate.StateCompleted)
	if err != nil {
		log.Error("reconcile: list completed", "err", err)
		return
	}
	for _, t := range threads {
		if h, ok := l.live[t.ThreadID]; ok {
			if err := l.Executor.Release(ctx, h); err != nil {
				log.Error("reconcile: release completed", "thread", t.ThreadID, "err", err)
			}
			delete(l.live, t.ThreadID)
		}
		l.reclaimBundleAndRow(ctx, log, t.ThreadID)
	}
}

// gcIdleExpired deletes the bundle + row for IDLE threads past their TTL.
func (l *Loop) gcIdleExpired(ctx context.Context, log *slog.Logger) {
	threads, err := l.Registry.ListIdleExpiredForNode(ctx, l.Node)
	if err != nil {
		log.Error("reconcile: list idle-expired", "err", err)
		return
	}
	for _, t := range threads {
		log.Info("reconcile: GC idle-expired thread", "thread", t.ThreadID, "ttl_secs", t.TTLSeconds)
		l.reclaimBundleAndRow(ctx, log, t.ThreadID)
	}
}

func (l *Loop) reclaimBundleAndRow(ctx context.Context, log *slog.Logger, threadID string) {
	if l.Reclaimer != nil {
		if err := l.Reclaimer.RemoveBundle(threadID); err != nil {
			log.Error("reconcile: remove bundle", "thread", threadID, "err", err)
		}
	}
	if err := l.Registry.Delete(ctx, threadID); err != nil {
		log.Error("reconcile: delete row", "thread", threadID, "err", err)
	}
	if cancel, ok := l.control[threadID]; ok {
		cancel()
		delete(l.control, threadID)
	}
	delete(l.live, threadID)
}

func refFor(t store.Thread) substrate.SnapshotRef {
	return substrate.SnapshotRef{
		ID:        t.ThreadSnapshotRef,
		ThreadID:  t.ThreadID,
		Node:      t.Node,
		Arch:      t.Arch,
		SizeBytes: t.SizeBytes,
	}
}

// LiveThreads reports the thread IDs the loop currently tracks (test/observability).
func (l *Loop) LiveThreads() int { return len(l.live) }
