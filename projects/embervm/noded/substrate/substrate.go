// Package substrate is embervm-noded's forked, trimmed copy of the FC executor
// seam types the driver depends on. It is a DELIBERATE FORK of the fc-invoke
// substrate seam (ADR embervm/001 mandates fork-not-extend: embervm-noded shares
// no Go packages with projects/firecracker), reduced to only the types the forked
// fcvm driver and its gueststats use.
//
// Dropped on the fork relative to fc-invoke's seam: the HTTP-shaped NodeExecutor
// (the daemon's northbound face is gRPC now, not an in-process HTTP interface),
// the Workload JSON catalog (concurrency is the control plane's; the daemon reads
// per-build resources off the gRPC BuildBase request, not a node-side catalog),
// and the Suspendable/Persistent/State surface the task-class lifecycle never uses.
package substrate

import (
	"context"
	"io"
	"time"
)

// ClaimSpec describes the isolated microVM a caller wants. A claim is satisfied
// cold (boot fresh from the rootfs) or restored (from a base snapshot ref); which
// path the driver takes is its own concern, keyed off BaseSnapshotRef.
type ClaimSpec struct {
	// ThreadID is the per-claim bundle identity that names the on-disk bundle dir
	// and the vsock socket path. Empty means "assign a fresh one".
	ThreadID string
	// Repo and Branch scoped an agent workspace in the fc-invoke lineage. The
	// task class has no workspace concept, so the forked driver ignores them; they
	// are carried only so the inherited driver package (copied verbatim, including
	// its tests) compiles unchanged. Do not read them in noded code.
	Repo   string
	Branch string
	// BaseSnapshotRef, when set, requests a restore from a warmed base snapshot
	// for an instant ready start instead of a cold boot.
	BaseSnapshotRef SnapshotRef
	// Arch pins CPU architecture; Firecracker snapshots are non-portable and a
	// mismatched restore fails closed.
	Arch string
}

// Handle is an opaque reference to a claimed, live microVM. It is only valid
// between Claim/Restore and Release.
type Handle struct {
	// ThreadID is the stable bundle identity backing this live microVM.
	ThreadID string
	// ID is the per-claim microVM instance identity, which changes across
	// snapshot/restore even though ThreadID does not.
	ID string
	// Node is where the microVM is running; snapshots are node-affine.
	Node string
}

// GuestStats is a host-side resource sample for one guest's firecracker process,
// read from /proc just before the VM is torn down. Because each invocation is a
// fresh single-use process, these are whole-invocation totals:
//   - CPUMillis is total user+system CPU across every vCPU + VMM thread since
//     Launch.
//   - PeakRSSMib is the kernel's peak resident set (VmHWM) for the process, an
//     upper bound on the VM's host memory footprint.
type GuestStats struct {
	CPUMillis  int64
	PeakRSSMib int64
}

// Request is a unit of work to run inside a claimed environment. It exists only
// to satisfy the Substrate interface's Exec signature; the FC-direct driver runs
// no host-launched processes in the guest (work arrives over vsock HTTP).
type Request struct {
	Argv    []string
	Env     map[string]string
	Timeout time.Duration
}

// Stream carries the output of an Exec. Callers must Close it.
type Stream interface {
	io.ReadCloser
}

// SnapshotRef identifies a stored snapshot bundle (snapfile + memfile). It is a
// value, not a handle: it survives the microVM and is restorable later.
type SnapshotRef struct {
	// ID is the snapshot identity; for a base it keys the bases/<ID> bundle dir
	// under the snapshot root, and for a per-thread snapshot it is a fresh id.
	ID string
	// ThreadID is the thread this snapshot belongs to (empty for a base).
	ThreadID string
	// Node and Arch pin where the snapshot can be restored (non-portable).
	Node string
	Arch string
	// SizeBytes is the on-disk bundle size, used for capacity reporting.
	SizeBytes int64
	// Base reports whether this is a warm base template rather than a per-thread
	// idle snapshot.
	Base bool
}

// Substrate is the core the driver satisfies: acquire an isolated env, run work
// in it, return/destroy it. Exec is unused by the FC-direct driver (work arrives
// over vsock HTTP), but the interface keeps the driver honest about the seam.
type Substrate interface {
	Claim(ctx context.Context, spec ClaimSpec) (Handle, error)
	Exec(ctx context.Context, h Handle, req Request) (Stream, error)
	Release(ctx context.Context, h Handle) error
}

// Snapshotable is the optional capability the FC-direct driver implements:
// capture an environment to a restorable bundle, and restore a new live
// environment from one.
type Snapshotable interface {
	Snapshot(ctx context.Context, h Handle) (SnapshotRef, error)
	Restore(ctx context.Context, ref SnapshotRef) (Handle, error)
}
