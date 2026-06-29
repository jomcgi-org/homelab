// Package driver is the FC-direct Snapshotable executor (ADR 022, Phase 1). It
// drives Firecracker processes and their snapshot API directly to boot a
// microVM, pause -> CreateSnapshot -> resume, and LoadSnapshot + resume a fresh
// microVM that continues exactly where the snapshot was taken.
//
// Storage follows the E2B bundle layout: a directory per thread under the
// snapshot root, holding the FC API socket plus the snapfile + memfile that make
// up a full snapshot. Snapshots are node/arch-bound (FC validates CPU on
// restore), so the driver stamps every handle and ref with its node + arch.
package driver

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/fcclient"
	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
	"go.opentelemetry.io/otel"
)

// tracer spans the cold-boot phases (rootfs provision, firecracker boot) so the
// cold-start cost is visible per phase in SigNoz (ADR 026 measurement).
var tracer = otel.Tracer("fc-agentd/driver")

// Config holds the node-4 substrate paths and microVM sizing.
type Config struct {
	// KernelImagePath is the guest kernel (kata vmlinux.container on node-4).
	KernelImagePath string
	// KernelBootArgs are appended to the kernel command line on cold boot.
	KernelBootArgs string
	// RootfsPath is the host path to a shared/static rootfs, used only when no
	// BaseRootfsPath is set (e.g. the kata rootfs for a heartbeat smoke test).
	RootfsPath string
	// BaseRootfsPath is the read-only base rootfs image (the flattened harness
	// image). When set, each thread gets its own writable rootfs derived from it.
	BaseRootfsPath string
	// Provisioner selects the per-thread rootfs strategy (ADR 026): "copy" (the
	// default CopyProvisioner full file copy) or "devmapper" (DevmapperProvisioner
	// copy-on-write thin-snapshot). An empty value means "copy".
	Provisioner string
	// ThinPool is the devmapper thin-pool name the DevmapperProvisioner snapshots
	// into (e.g. "devpool"); ignored by the copy provisioner.
	ThinPool string
	// HarnessInit, when set, is appended to the kernel command line as
	// init=<path> so the guest boots straight into fc-agent-init (raw FC boot
	// ignores the OCI entrypoint).
	HarnessInit string
	// VCPUs and MemMib size the guest.
	VCPUs  int
	MemMib int
	// SnapshotRoot is the directory holding per-thread bundles (/disks/nvme-02).
	SnapshotRoot string
	// Node and Arch pin where snapshots may be restored.
	Node string
	Arch string
}

func (c Config) withDefaults() Config {
	if c.VCPUs == 0 {
		c.VCPUs = 1
	}
	if c.MemMib == 0 {
		c.MemMib = 1024
	}
	if c.KernelBootArgs == "" {
		c.KernelBootArgs = "console=ttyS0 reboot=k panic=1 pci=off"
	}
	return c
}

// Process is a running Firecracker process.
type Process interface {
	Kill() error
	Wait() error
}

// Launcher starts a Firecracker process whose API socket is at socketPath and
// returns once the socket accepts connections.
type Launcher interface {
	Launch(ctx context.Context, vmID, socketPath string) (Process, error)
}

// fcAPI is the subset of the Firecracker client the driver uses, kept as an
// interface so tests can supply a fake. *fcclient.Client satisfies it.
type fcAPI interface {
	PutMachineConfig(ctx context.Context, m fcclient.MachineConfig) error
	PutBootSource(ctx context.Context, b fcclient.BootSource) error
	PutDrive(ctx context.Context, d fcclient.Drive) error
	PutVsock(ctx context.Context, v fcclient.Vsock) error
	Start(ctx context.Context) error
	Pause(ctx context.Context) error
	Resume(ctx context.Context) error
	CreateSnapshot(ctx context.Context, s fcclient.SnapshotCreate) error
	LoadSnapshot(ctx context.Context, s fcclient.SnapshotLoad) error
}

// Driver implements substrate.Substrate and substrate.Snapshotable for FC-direct.
type Driver struct {
	cfg         Config
	launcher    Launcher
	newClient   func(socketPath string) fcAPI
	provisioner RootfsProvisioner // nil => use cfg.RootfsPath (shared/static)

	mu   sync.Mutex
	live map[string]*instance // handle ID -> instance
}

type instance struct {
	handle substrate.Handle
	proc   Process
	client fcAPI
	dir    string
	sock   string
}

var (
	_ substrate.Substrate    = (*Driver)(nil)
	_ substrate.Snapshotable = (*Driver)(nil)
)

// New builds a Driver. launcher must not be nil. If newClient is nil the real
// fcclient is used.
func New(cfg Config, launcher Launcher, newClient func(socketPath string) fcAPI) *Driver {
	if newClient == nil {
		newClient = func(sock string) fcAPI { return fcclient.New(sock) }
	}
	d := &Driver{
		cfg:       cfg.withDefaults(),
		launcher:  launcher,
		newClient: newClient,
		live:      make(map[string]*instance),
	}
	if cfg.BaseRootfsPath != "" {
		if cfg.Provisioner == "devmapper" {
			d.provisioner = &DevmapperProvisioner{
				Pool:     cfg.ThinPool,
				Base:     cfg.BaseRootfsPath,
				StateDir: cfg.SnapshotRoot,
			}
		} else {
			d.provisioner = &CopyProvisioner{Base: cfg.BaseRootfsPath}
		}
	}
	return d
}

// SetProvisioner overrides the rootfs provisioner (tests inject a fake).
func (d *Driver) SetProvisioner(p RootfsProvisioner) { d.provisioner = p }

func newID(prefix string) string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return prefix + "-" + hex.EncodeToString(b[:])
}

// threadDir is the bundle directory for a thread.
func (d *Driver) threadDir(threadID string) string {
	return filepath.Join(d.cfg.SnapshotRoot, threadID)
}

func (d *Driver) snapfilePath(threadID string) string {
	return filepath.Join(d.threadDir(threadID), "snapfile")
}

func (d *Driver) memfilePath(threadID string) string {
	return filepath.Join(d.threadDir(threadID), "memfile")
}

// guestCID is the vsock context id assigned to every microVM. CIDs 0-2 are
// reserved (2 is the host), so guests start at 3. The controller reaches a guest
// by listening on the per-thread UDS, not by CID, so a fixed value is fine.
const guestCID = 3

// VsockUDSPath is the host unix-domain socket backing a thread's vsock device.
// Firecracker multiplexes guest connections onto "<uds>_<port>", so the control
// server listens on VsockUDSPath(threadID)+"_"+ControlPort. The path is
// deterministic from the bundle dir, so the reconcile loop can reach a live
// microVM's control channel without the driver handing back extra state.
func (d *Driver) VsockUDSPath(threadID string) string {
	return filepath.Join(d.threadDir(threadID), "vsock.sock")
}

// removeStaleVsockUDS unlinks any leftover vsock unix-domain socket before a
// (re)launch. Firecracker *binds* the vsock UDS on PUT /vsock, and bind() fails
// with EADDRINUSE when the path already exists. A thread's bundle dir lives on
// the persistent snapshot disk, so a vsock.sock from a previous incarnation
// survives a daemon/pod restart and makes orphan recovery loop on a 400 until it
// exhausts retries and marks the thread FAILED. The launcher already clears the
// API socket; the vsock UDS and its per-port children (<uds>_<port>, created once
// the guest connects) are not, so clear them here.
func (d *Driver) removeStaleVsockUDS(threadID string) {
	vsock := d.VsockUDSPath(threadID)
	_ = os.Remove(vsock)
	matches, err := filepath.Glob(vsock + "_*")
	if err != nil {
		return
	}
	for _, m := range matches {
		_ = os.Remove(m)
	}
}

// bootArgs is the kernel command line. When HarnessInit is set it appends
// init=<path> so the guest boots straight into fc-agent-init (raw FC boot does
// not honour the OCI image entrypoint).
func (d *Driver) bootArgs() string {
	args := d.cfg.KernelBootArgs
	if d.cfg.HarnessInit != "" {
		args += " init=" + d.cfg.HarnessInit
	}
	return args
}

// baseDir is the bundle directory for a warm base, keyed by an opaque base key
// (one per repo env-image version). Bases live under bases/ so they are never
// confused with per-thread bundles and survive thread GC.
func (d *Driver) baseDir(key string) string {
	return filepath.Join(d.cfg.SnapshotRoot, "bases", key)
}

func (d *Driver) baseSnapfile(key string) string { return filepath.Join(d.baseDir(key), "snapfile") }
func (d *Driver) baseMemfile(key string) string  { return filepath.Join(d.baseDir(key), "memfile") }

// loadInto launches a fresh Firecracker process for threadID and restores it
// from the given snapfile + memfile (File backend, resume). Used by both
// thread-snapshot restore and warm-base restore.
func (d *Driver) loadInto(ctx context.Context, threadID, snapPath, memPath, sockName string) (substrate.Handle, error) {
	dir := d.threadDir(threadID)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: mkdir bundle: %w", err)
	}
	sock := filepath.Join(dir, sockName)
	_ = os.Remove(sock)
	// A restored snapshot re-binds the vsock UDS from its embedded config; clear
	// any stale socket first so the resume does not fail on EADDRINUSE.
	d.removeStaleVsockUDS(threadID)
	vmID := newID("vm")

	proc, err := d.launcher.Launch(ctx, vmID, sock)
	if err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: launch firecracker for restore: %w", err)
	}
	client := d.newClient(sock)
	if err := client.LoadSnapshot(ctx, fcclient.SnapshotLoad{
		SnapshotPath: snapPath,
		MemBackend:   &fcclient.MemBackend{BackendType: "File", BackendPath: memPath},
		ResumeVM:     true,
	}); err != nil {
		return d.abort(proc, err)
	}
	h := substrate.Handle{ThreadID: threadID, ID: vmID, Node: d.cfg.Node}
	d.track(&instance{handle: h, proc: proc, client: client, dir: dir, sock: sock})
	return h, nil
}

// Claim boots a microVM. With a BaseSnapshotRef it restores from that warm base
// for an instant ready start; otherwise it cold-boots from the kernel + rootfs.
func (d *Driver) Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error) {
	if spec.Arch != "" && d.cfg.Arch != "" && spec.Arch != d.cfg.Arch {
		return substrate.Handle{}, fmt.Errorf("driver: arch mismatch: spec %q != node %q (snapshots non-portable)", spec.Arch, d.cfg.Arch)
	}
	threadID := spec.ThreadID
	if threadID == "" {
		threadID = newID("thread")
	}

	// Warm-base start: restore the new thread from a base bundle for an instant
	// ready start, skipping boot + harness init.
	if spec.BaseSnapshotRef.ID != "" {
		ref := spec.BaseSnapshotRef
		if ref.Arch != "" && d.cfg.Arch != "" && ref.Arch != d.cfg.Arch {
			return substrate.Handle{}, fmt.Errorf("driver: base arch mismatch: ref %q != node %q", ref.Arch, d.cfg.Arch)
		}
		snap := d.baseSnapfile(ref.ID)
		if _, err := os.Stat(snap); err != nil {
			return substrate.Handle{}, fmt.Errorf("driver: base bundle missing for %q: %w", ref.ID, err)
		}
		return d.loadInto(ctx, threadID, snap, d.baseMemfile(ref.ID), "api.sock")
	}

	dir := d.threadDir(threadID)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: mkdir bundle: %w", err)
	}
	// Clear a stale vsock UDS left by a prior incarnation so PUT /vsock can bind
	// (see removeStaleVsockUDS): the bundle dir persists across daemon restarts.
	d.removeStaleVsockUDS(threadID)
	sock := filepath.Join(dir, "api.sock")
	vmID := newID("vm")

	proc, err := d.launcher.Launch(ctx, vmID, sock)
	if err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: launch firecracker: %w", err)
	}
	client := d.newClient(sock)

	// Each thread gets its own writable rootfs (from the base image) so threads
	// never share or corrupt one disk. With no provisioner, fall back to the
	// shared/static rootfs (e.g. a kata rootfs smoke test).
	rootfsPath := d.cfg.RootfsPath
	if d.provisioner != nil {
		// provision_rootfs is the cold-start cost ADR 026 targets (the full-copy
		// CopyProvisioner today; a CoW provisioner later). Its own span makes the
		// before/after directly visible in SigNoz.
		pctx, pspan := tracer.Start(ctx, "provision_rootfs")
		rootfsPath, err = d.provisioner.Provision(pctx, threadID, dir)
		pspan.End()
		if err != nil {
			return d.abort(proc, err)
		}
	}

	// firecracker_boot: configure the microVM and Start it (the kernel boot + the
	// guest fc-agent-init handshake complete asynchronously after Start returns,
	// so they are not in this span).
	_, bspan := tracer.Start(ctx, "firecracker_boot")
	bootErr := func() error {
		if err := client.PutMachineConfig(ctx, fcclient.MachineConfig{VCPUCount: d.cfg.VCPUs, MemSizeMib: d.cfg.MemMib}); err != nil {
			return err
		}
		if err := client.PutBootSource(ctx, fcclient.BootSource{KernelImagePath: d.cfg.KernelImagePath, BootArgs: d.bootArgs()}); err != nil {
			return err
		}
		if err := client.PutDrive(ctx, fcclient.Drive{DriveID: "rootfs", PathOnHost: rootfsPath, IsRootDevice: true}); err != nil {
			return err
		}
		// The vsock device is the guest's only channel to the controller (task
		// delivery, idle signal, egress proxy). It must be configured before Start.
		if err := client.PutVsock(ctx, fcclient.Vsock{GuestCID: guestCID, UDSPath: d.VsockUDSPath(threadID)}); err != nil {
			return err
		}
		return client.Start(ctx)
	}()
	bspan.End()
	if bootErr != nil {
		return d.abort(proc, bootErr)
	}

	h := substrate.Handle{ThreadID: threadID, ID: vmID, Node: d.cfg.Node}
	d.track(&instance{handle: h, proc: proc, client: client, dir: dir, sock: sock})
	return h, nil
}

// Snapshot pauses the microVM, writes a full snapshot bundle, and resumes it so
// the handle stays usable. Snapshot create is off the user-facing hot path.
func (d *Driver) Snapshot(ctx context.Context, h substrate.Handle) (substrate.SnapshotRef, error) {
	inst := d.get(h.ID)
	if inst == nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: snapshot of unknown handle %q", h.ID)
	}
	snapPath := d.snapfilePath(h.ThreadID)
	memPath := d.memfilePath(h.ThreadID)

	if err := inst.client.Pause(ctx); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: pause: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapPath, MemFilePath: memPath}); err != nil {
		// Best-effort resume so a snapshot failure does not strand the VM paused.
		_ = inst.client.Resume(ctx)
		return substrate.SnapshotRef{}, fmt.Errorf("driver: create snapshot: %w", err)
	}
	if err := inst.client.Resume(ctx); err != nil {
		// Resume failed: the VM is stranded paused. Tear it down so a dead handle
		// is not leaked into `live`; the thread re-inits from Postgres on the next
		// reconcile (snapshots are never load-bearing).
		_ = d.Release(ctx, h)
		return substrate.SnapshotRef{}, fmt.Errorf("driver: resume after snapshot: %w", err)
	}

	ref := substrate.SnapshotRef{
		ID:        newID("snap"),
		ThreadID:  h.ThreadID,
		Node:      d.cfg.Node,
		Arch:      d.cfg.Arch,
		SizeBytes: bundleSize(snapPath, memPath),
	}
	return ref, nil
}

// Restore launches a fresh Firecracker process and loads the snapshot bundle for
// the ref's thread, resuming it. The microVM continues exactly where it paused;
// the new handle keeps the stable ThreadID but gets a fresh microVM id.
func (d *Driver) Restore(ctx context.Context, ref substrate.SnapshotRef) (substrate.Handle, error) {
	if ref.Arch != "" && d.cfg.Arch != "" && ref.Arch != d.cfg.Arch {
		return substrate.Handle{}, fmt.Errorf("driver: arch mismatch on restore: ref %q != node %q", ref.Arch, d.cfg.Arch)
	}
	if ref.Node != "" && d.cfg.Node != "" && ref.Node != d.cfg.Node {
		return substrate.Handle{}, fmt.Errorf("driver: node mismatch on restore: ref %q != node %q", ref.Node, d.cfg.Node)
	}
	threadID := ref.ThreadID
	if threadID == "" {
		return substrate.Handle{}, errors.New("driver: restore requires a ThreadID on the ref")
	}
	snapPath := d.snapfilePath(threadID)
	if _, err := os.Stat(snapPath); err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: snapshot bundle missing for thread %q: %w", threadID, err)
	}
	return d.loadInto(ctx, threadID, snapPath, d.memfilePath(threadID), "restore.sock")
}

// SnapshotBase captures a warmed microVM into a shared base bundle keyed by
// baseKey (one per repo env-image version). New threads restore from it for an
// instant ready start. Like Snapshot it pauses, writes, and resumes.
func (d *Driver) SnapshotBase(ctx context.Context, h substrate.Handle, baseKey string) (substrate.SnapshotRef, error) {
	inst := d.get(h.ID)
	if inst == nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: snapshot-base of unknown handle %q", h.ID)
	}
	if err := os.MkdirAll(d.baseDir(baseKey), 0o750); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: mkdir base bundle: %w", err)
	}
	snapPath := d.baseSnapfile(baseKey)
	memPath := d.baseMemfile(baseKey)

	if err := inst.client.Pause(ctx); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: pause: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapPath, MemFilePath: memPath}); err != nil {
		_ = inst.client.Resume(ctx)
		return substrate.SnapshotRef{}, fmt.Errorf("driver: create base snapshot: %w", err)
	}
	if err := inst.client.Resume(ctx); err != nil {
		_ = d.Release(ctx, h)
		return substrate.SnapshotRef{}, fmt.Errorf("driver: resume after base snapshot: %w", err)
	}
	return substrate.SnapshotRef{
		ID:        baseKey,
		Node:      d.cfg.Node,
		Arch:      d.cfg.Arch,
		Base:      true,
		SizeBytes: bundleSize(snapPath, memPath),
	}, nil
}

// RemoveBaseBundle deletes a warm base's on-disk bundle (when a base is
// superseded by a newer env-image version).
func (d *Driver) RemoveBaseBundle(baseKey string) error {
	if baseKey == "" {
		return fmt.Errorf("driver: RemoveBaseBundle requires a baseKey")
	}
	if err := os.RemoveAll(d.baseDir(baseKey)); err != nil {
		return fmt.Errorf("driver: remove base bundle: %w", err)
	}
	return nil
}

// Exec is provided by the in-VM harness over the wrapper channel (Phase 2); the
// FC-direct driver does not run processes in the guest from the host.
func (d *Driver) Exec(_ context.Context, _ substrate.Handle, _ substrate.Request) (substrate.Stream, error) {
	return nil, errors.New("driver: Exec is handled by the in-VM harness (Phase 2), not the host driver")
}

// Release kills the microVM process and removes its API socket. The snapshot
// bundle is left in place (Release returns/destroys the live env, not its
// snapshots; GC reclaims bundles separately).
func (d *Driver) Release(_ context.Context, h substrate.Handle) error {
	d.mu.Lock()
	inst, ok := d.live[h.ID]
	if ok {
		delete(d.live, h.ID)
	}
	d.mu.Unlock()
	if !ok {
		return fmt.Errorf("driver: release of unknown handle %q", h.ID)
	}
	killErr := inst.proc.Kill()
	_ = os.Remove(inst.sock)
	if killErr != nil {
		return fmt.Errorf("driver: kill firecracker: %w", killErr)
	}
	return nil
}

func (d *Driver) abort(proc Process, cause error) (substrate.Handle, error) {
	_ = proc.Kill()
	return substrate.Handle{}, cause
}

func (d *Driver) track(inst *instance) {
	d.mu.Lock()
	d.live[inst.handle.ID] = inst
	d.mu.Unlock()
}

func (d *Driver) get(id string) *instance {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.live[id]
}

// RemoveBundle deletes a thread's on-disk snapshot bundle directory (GC/reclaim).
// It is a no-op if the directory is already gone.
func (d *Driver) RemoveBundle(threadID string) error {
	if threadID == "" {
		return fmt.Errorf("driver: RemoveBundle requires a threadID")
	}
	// Release the provisioner's out-of-dir resources first (a CoW thin device +
	// its pool allocation); the COW data lives in the pool, not the bundle dir, so
	// RemoveAll alone would leak the dm device and its thin id. The VM process is
	// already killed (Release ran before reclaim), so the device is free.
	if d.provisioner != nil {
		ctx, cancel := context.WithTimeout(context.Background(), teardownTimeout)
		defer cancel()
		if err := d.provisioner.Teardown(ctx, threadID); err != nil {
			return fmt.Errorf("driver: teardown rootfs for %q: %w", threadID, err)
		}
	}
	dir := d.threadDir(threadID)
	if err := os.RemoveAll(dir); err != nil {
		return fmt.Errorf("driver: remove bundle %q: %w", dir, err)
	}
	return nil
}

// LiveCount reports how many microVMs the driver is currently supervising.
func (d *Driver) LiveCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.live)
}

func bundleSize(paths ...string) int64 {
	var total int64
	for _, p := range paths {
		if fi, err := os.Stat(p); err == nil {
			total += fi.Size()
		}
	}
	return total
}
