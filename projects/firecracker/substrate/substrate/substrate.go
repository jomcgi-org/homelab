// Package substrate defines the thin executor interface from ADR 019: a minimal
// core (Claim/Exec/Release) plus optional capability interfaces that an executor
// advertises and consumers type-assert. ADR 022's Firecracker snapshot/restore
// controller is the Snapshotable implementation behind this seam.
//
// The interface is deliberately small. It exists so consumers (AgentWorkflow,
// job-mcp) never couple to a single executor: a cold-on-demand pod, a warm pool,
// and a Firecracker microVM all satisfy the same core, while snapshot/restore is
// a capability the core never requires.
package substrate

import (
	"context"
	"io"
	"time"
)

// State is the lifecycle state of an AgentThread, mirrored in the Postgres
// registry (ADR 022, decision 5). The durable unit is the thread, keyed by a
// stable ID that outlives every microVM.
type State string

const (
	StatePending   State = "PENDING"
	StateRunning   State = "RUNNING"
	StateIdle      State = "IDLE"
	StateCompleted State = "COMPLETED"
	StateFailed    State = "FAILED"
)

// ClaimSpec describes the isolated environment a consumer wants. A claim may be
// satisfied cold (boot fresh), warm (from a pool), or restored (from a snapshot
// ref) - which path is taken is the executor's concern, not the consumer's.
type ClaimSpec struct {
	// ThreadID is the stable identity that correlates the Discord thread, the
	// Postgres task state, and the snapshot refs. Empty means "assign a new one".
	ThreadID string
	// Repo and Branch scope the workspace the harness operates on.
	Repo   string
	Branch string
	// BaseSnapshotRef, when set, requests a restore from a warmed base template
	// for an instant ready start instead of a cold boot + harness init.
	BaseSnapshotRef SnapshotRef
	// Arch pins CPU architecture; Firecracker snapshots are non-portable and a
	// mismatched restore fails closed (ADR 022 security note).
	Arch string
}

// Handle is an opaque reference to a claimed, live environment. It is only valid
// between Claim/Restore and Release.
type Handle struct {
	// ThreadID is the stable thread identity backing this live environment.
	ThreadID string
	// ID is the per-claim instance identity (e.g. the microVM id), which changes
	// across snapshot/restore even though ThreadID does not.
	ID string
	// Node is where the environment is running; snapshots are node-affine.
	Node string
}

// GuestStats is a host-side resource sample for one guest's firecracker
// process, read from /proc just before the VM is torn down. Because each
// invocation is a fresh single-use process (its CPU counter starts at zero at
// Launch and its RSS high-water-mark accumulates over its whole life), these are
// whole-invocation totals, not a windowed delta:
//   - CPUMillis is the total user+system CPU consumed across all the process's
//     threads (every vCPU plus VMM housekeeping) since Launch. CPUMillis/wall
//     approximates the average cores used, so a value well above the wall time
//     means the guest ran multi-core and a value near it means single-core.
//   - PeakRSSMib is the kernel's peak resident set (VmHWM) for the process, an
//     upper bound on the VM's host memory footprint (it blends per-VM dirtied
//     pages with copy-on-write pages faulted in from the shared base snapshot).
type GuestStats struct {
	CPUMillis  int64
	PeakRSSMib int64
}

// Request is a unit of work to run inside a claimed environment. Exec runs an
// opaque process and streams its output; the harness (Claude CLI, Goose recipe)
// is a property of the workload image, not the platform (ADR 019).
type Request struct {
	// Argv is the command to run inside the environment.
	Argv []string
	// Env are extra environment variables for the process.
	Env map[string]string
	// Timeout bounds the execution; zero means the executor default.
	Timeout time.Duration
}

// Stream carries the output of an Exec. Callers must Close it.
type Stream interface {
	io.ReadCloser
}

// SnapshotRef identifies a stored snapshot bundle (snapfile + memfile + rootfs).
// It is a value, not a handle: it survives the microVM and is restorable later.
type SnapshotRef struct {
	// ID is the snapshot identity; for FC-direct this keys the dir-per-thread
	// bundle on /disks/nvme-02.
	ID string
	// ThreadID is the thread this snapshot belongs to (empty for a base).
	ThreadID string
	// Node and Arch pin where the snapshot can be restored (non-portable).
	Node string
	Arch string
	// SizeBytes is the on-disk bundle size, used for GC budgeting.
	SizeBytes int64
	// Base reports whether this is a warm base template (baseSnapshotRef) rather
	// than a per-thread idle snapshot (threadSnapshotRef).
	Base bool
}

// Substrate is the core every executor satisfies: acquire an isolated env, run
// work in it, return/destroy it.
type Substrate interface {
	Claim(ctx context.Context, spec ClaimSpec) (Handle, error)
	Exec(ctx context.Context, h Handle, req Request) (Stream, error)
	Release(ctx context.Context, h Handle) error
}

// Suspendable is an optional capability: pause/resume a live environment without
// destroying it.
type Suspendable interface {
	Suspend(ctx context.Context, h Handle) error
	Resume(ctx context.Context, h Handle) error
}

// Snapshotable is an optional capability: capture an environment's full state to
// a restorable bundle, and restore a new live environment from one. This is the
// capability ADR 022's Firecracker controller implements.
type Snapshotable interface {
	Snapshot(ctx context.Context, h Handle) (SnapshotRef, error)
	Restore(ctx context.Context, ref SnapshotRef) (Handle, error)
}

// Persistent is an optional capability: durable volumes survive Release.
type Persistent interface {
	// Volume returns the durable volume identity associated with a handle.
	Volume(ctx context.Context, h Handle) (string, error)
}
