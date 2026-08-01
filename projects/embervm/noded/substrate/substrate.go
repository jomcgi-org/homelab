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
	// NIC, when non-nil, requests a tap network interface on the microVM (serving
	// class, R3). It is set ONLY by the serving cold-boot path; task and session
	// claims leave it nil and their boot path is byte-unchanged (vsock-only, no NIC).
	// Firecracker cannot hot-attach a NIC to a resumed snapshot, so a NIC is only
	// meaningful on a COLD boot (BaseSnapshotRef unset); the serving relight path
	// restores a snapshot that already captured its NIC and never sets this.
	NIC *NICSpec
}

// NICSpec describes the tap network interface a serving-class microVM cold-boots
// with. The daemon owns the host tap and its IP; the guest receives the static IP
// via kernel boot-args (ip=<vmip>::<gwip>:<mask>::<iface>:off), configured pre-Start.
type NICSpec struct {
	// HostDevName is the host tap device the daemon already created and attached to
	// the serving bridge.
	HostDevName string
	// GuestMAC is the deterministic MAC for the guest eth0 (optional; empty lets FC
	// assign one).
	GuestMAC string
	// IP, GatewayIP, and PrefixLen configure the guest's static address via boot-args.
	IP        string
	GatewayIP string
	PrefixLen int
	// IfaceName is the in-guest interface name the boot-args ip= directive targets
	// (eth0 by convention).
	IfaceName string
	// ServingPort is the guest TCP port the serving shim binds on the tap NIC
	// (spec.serving.port). The driver appends it to boot-args as
	// `ember.serving_port=<port>` so guest-init flips the python shim from vsock
	// to TCP (D-R3.11.1). It is the SAME port the daemon health-probes and
	// publishes, single-sourced from the StartServing request. Zero means "no
	// serving-port directive" (the boot stays on the vsock path); it is only
	// meaningful on the serving cold-boot path where a NICSpec exists at all.
	ServingPort uint32
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
	// Vendor pins the CPUID vendor ("amd", "intel") this snapshot was captured
	// on (R7, standing decisions 1 and 11): Firecracker restore never crosses
	// the AMD/Intel boundary, so a restore whose ref.Vendor mismatches the
	// node's own vendor fails closed exactly like an Arch mismatch does. A ref
	// with an empty Vendor is a legacy (pre-R7) snapshot; the daemon treats it
	// as vendor "amd" (the node-4 alias) so an existing on-disk base or bundle
	// never re-exports or false-mismatches merely for predating vendor keying.
	Vendor string
	// Template names the Firecracker CPU template this snapshot was captured
	// under (PR-E, ADR embervm/012), the other half of cpu_sku alongside
	// Vendor. A ref with an EMPTY Template is an UNSTAMPED legacy artifact
	// under the grandfather rule: never refused for the missing stamp,
	// restorable exactly where it was cut. A NON-EMPTY Template that differs
	// from the node's own is a hard mismatch, refused fail-closed exactly like
	// Vendor. Unlike Vendor there is no legacy alias for Template: an empty
	// Template reads as "unstamped", never aliased to a guessed value, because
	// guessing a template (unlike guessing node-4's vendor, a real historical
	// fact) has no safe default.
	Template string
	// SizeBytes is the on-disk bundle size, used for capacity reporting.
	SizeBytes int64
	// Base reports whether this is a warm base template rather than a per-thread
	// idle snapshot.
	Base bool
}

// ServingHandlerArtifact is one discovered cold-boot handler artifact on disk (a
// serving base's handler.zip plus its runtime-ref sidecar), returned by the driver's
// startup rescan so the server re-seeds its serving-images inventory after a restart
// (D-R3.11.2). It sits in substrate because both the driver (which owns the on-disk
// base-bundle layout it globs) and the server (which consumes the rescan through the
// servingDriver seam) name it, and neither may import the other.
type ServingHandlerArtifact struct {
	// BaseKey is the serving base key (== the serving image ref the control plane
	// places on), recovered from the bundle dir name.
	BaseKey string
	// Path is the host path of the handler.zip artifact (the cold-boot drive 2).
	Path string
	// RuntimeImageRef is the runtime image whose rootfs is drive 1, read from the
	// runtime.ref sidecar the write records so the rescan needs no control-plane call.
	RuntimeImageRef string
	// SizeBytes is the exact zip length (the EOCD-padding defence), read from the
	// artifact file so the guest reads only the payload, not the block padding.
	SizeBytes int64
}

// StatefulBundleInfo is one discovered banked stateful bundle on disk (R4),
// returned by the driver's startup rescan (ScanStatefulBundles) so the server
// re-seeds its stateful-bundle inventory after a restart, mirroring how
// ServingHandlerArtifact serves the serving-images rescan. workload is not
// carried: the bundle dir name is the opaque snapshot_ref, not the workload, so
// the server recovers a binding from its own prior state if any, else reports
// it with an empty workload for the control plane to rebind by adoption.
type StatefulBundleInfo struct {
	// SnapshotRef is the opaque bundle identity (the bundle dir name).
	SnapshotRef string
	// Generation is the volume generation this bundle was banked at (read from
	// the gen sidecar), the pair key a relight compares against the volume's
	// CURRENT generation. Zero when the sidecar is missing or malformed, which
	// is a deliberately unusable value (no real volume generation is ever 0
	// after its first FRESH attach), so an unreadable stamp never falsely matches.
	Generation uint64
	// SizeBytes is the bundle's on-disk size (snapfile + memfile).
	SizeBytes int64
	// CreatedAtUnixMs is the bundle's on-disk modification time, for reporting.
	CreatedAtUnixMs int64
}

// GroupNetworkRecord is one discovered on-disk group-network record (R5),
// returned by the driver's startup rescan (ScanGroupNetworks) so the server
// re-seeds its group-network inventory after a restart. The record is the DURABLE
// truth for a group network: the bridge itself lives in noded's pod netns and
// dies with the pod (D-R3.11.4), so the on-disk config.json under
// group_networks/<group_instance_id>/ is what a restarted daemon (and, via
// NodeStatus, the control plane) reconciles from. CreateGroupNetwork is idempotent
// precisely so the control plane can re-issue it to rebuild the bridge a rescanned
// record names.
type GroupNetworkRecord struct {
	// GroupInstanceID is the control-plane group-instance identity (the record dir
	// name and the bridge idempotency key).
	GroupInstanceID string `json:"groupInstanceId"`
	// BridgeName is the daemon-derived bridge device name (deterministic from the
	// group_instance_id), recorded so a rescan reports it without re-deriving.
	BridgeName string `json:"bridgeName"`
	// SubnetCIDR is the group's /24 within the composite supernet.
	SubnetCIDR string `json:"subnetCidr"`
	// GatewayIP is the bridge's .1 address on the /24 (the members' default route).
	GatewayIP string `json:"gatewayIp"`
	// CreatedAtUnixMs is when the group network was first created, for reporting.
	CreatedAtUnixMs int64 `json:"createdAtUnixMs"`
}

// GroupBundleMemberInfo is one member's banked snapshot discovered within a group
// bundle set on disk (R5), returned by the driver's startup rescan
// (ScanGroupBundleSets). The member subdir name is the member_name; the ref is the
// opaque per-member bundle handle (group/<set_id>/<member_name>). New bundles
// carry member.json with the exact pinned IP and guest health port held at bank
// time. Older bundles have empty metadata and remain control-plane relightable,
// but are not eligible for node-local group activation.
type GroupBundleMemberInfo struct {
	// MemberName is the member subdir name within the set dir.
	MemberName string
	// SnapshotRef is the opaque per-member bundle handle (group/<set_id>/<member>).
	SnapshotRef string
	// SizeBytes is the member bundle's on-disk size (snapfile + memfile).
	SizeBytes int64
	// PinnedIP is the exact group-subnet IP baked into the member snapshot.
	// Empty when member.json is absent or unreadable.
	PinnedIP string
	// Port is the guest TCP health port held by the member at bank time.
	// Zero when member.json is absent or unreadable.
	Port uint32
}

// GroupBundleMemberMetadata is the minimal per-member bank sidecar used by a
// restarted daemon to reconstruct the snapshot's pinned network world.
type GroupBundleMemberMetadata struct {
	PinnedIP string `json:"pinnedIp"`
	Port     uint32 `json:"port"`
}

const GroupBundleMemberMetadataFile = "member.json"

// GroupBundleSetInfo is the per-member banked snapshots the driver's startup rescan
// (ScanGroupBundleSets) found grouped under one set directory (group/<set_id>/), so
// the server re-seeds its banked-group inventory after a restart and reports it in
// NodeStatus.group_bundle_sets. The daemon reports refs GROUPED BY the set dir it
// wrote them under; it makes NO completeness judgment (whether the set has every
// member it needs to relight is the control plane's to decide, exactly as the proto
// contract states).
type GroupBundleSetInfo struct {
	// SetID is the opaque set directory name (group/<set_id>/).
	SetID string
	// Members are the per-member bundles found under the set dir.
	Members []GroupBundleMemberInfo
	// CreatedAtUnixMs is the set dir's on-disk modification time, for reporting.
	CreatedAtUnixMs int64
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
