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
	"net"
	"os"
	"path/filepath"
	"sync"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	"go.opentelemetry.io/otel"
)

// tracer spans the cold-boot phases (rootfs provision, firecracker boot) so the
// cold-start cost is visible per phase in SigNoz (ADR 026 measurement).
var tracer = otel.Tracer("embervm-noded/driver")

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
	// RootfsReadOnly attaches the rootfs drive read-only on cold boot. Used by the
	// warm-base scanner pattern: when every mutable path in the guest is a tmpfs
	// (RAM, captured in the snapshot memfile), the rootfs is never written, so one
	// shared read-only rootfs file can back every microVM restored from a single
	// warm base without per-thread provisioning or corruption.
	RootfsReadOnly bool
	// CanonicalVsockDir, when set, is the fixed directory whose vsock.sock the base
	// snapshot embeds. Cold boot binds the guest's vsock there (so the snapshot
	// captures that path), and the launcher bind-mounts each VM's bundle dir over it
	// per instance, so concurrent restores from one base each get their own
	// host-reachable vsock socket. Pairs with ExecLauncher.VsockBindTarget. Empty
	// keeps the per-thread vsock path (the legacy per-thread, no-warm-base behaviour).
	CanonicalVsockDir string
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
	// Pid returns the OS process id of the firecracker process, or 0 if it has
	// not started. Used to read the process's /proc resource counters for
	// per-invocation stats before teardown.
	Pid() int
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
	PutNetworkInterface(ctx context.Context, n fcclient.NetworkInterface) error
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

// bootVsockPath is the vsock UDS path firecracker binds at cold boot. With a
// CanonicalVsockDir it is that fixed dir's vsock.sock (so the base snapshot embeds
// a stable path the launcher can bind-mount per instance); otherwise it is the
// per-thread host path. The launcher's per-instance bind-mount makes the canonical
// path resolve to VsockUDSPath(threadID) on the host either way.
func (d *Driver) bootVsockPath(threadID string) string {
	if d.cfg.CanonicalVsockDir != "" {
		return filepath.Join(d.cfg.CanonicalVsockDir, "vsock.sock")
	}
	return d.VsockUDSPath(threadID)
}

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
	return d.bootArgsFor(nil)
}

// nicIfaceID is the Firecracker network-interface id for a serving VM's single tap.
const nicIfaceID = "eth0"

// bootArgsFor is bootArgs plus, for a serving-class cold boot (nic != nil), the
// kernel `ip=` directive that statically configures the guest's interface at boot:
// ip=<vmip>::<gwip>:<mask>::<iface>:off (autoconf off; the daemon owns the address).
// Task/session boots pass nil and get exactly the previous boot args (byte-unchanged).
// The IP is baked at THIS cold boot; a later snapshot resume keeps it (a resume does
// not re-run kernel init), which is why the serving IP is pinned across bank/relight
// (D-R3.4.1).
func (d *Driver) bootArgsFor(nic *substrate.NICSpec) string {
	args := d.cfg.KernelBootArgs
	if d.cfg.HarnessInit != "" {
		args += " init=" + d.cfg.HarnessInit
	}
	if nic != nil {
		iface := nic.IfaceName
		if iface == "" {
			iface = nicIfaceID
		}
		mask := prefixLenToMask(nic.PrefixLen)
		// Linux kernel ip= directive: client-ip::gw-ip:netmask:hostname:device:autoconf
		args += fmt.Sprintf(" ip=%s::%s:%s::%s:off", nic.IP, nic.GatewayIP, mask, iface)
	}
	return args
}

// prefixLenToMask renders an IPv4 prefix length as a dotted-decimal netmask for the
// kernel ip= boot directive (which wants a netmask, not a prefix length).
func prefixLenToMask(prefixLen int) string {
	if prefixLen < 0 || prefixLen > 32 {
		prefixLen = 24
	}
	mask := net.CIDRMask(prefixLen, 32)
	return fmt.Sprintf("%d.%d.%d.%d", mask[0], mask[1], mask[2], mask[3])
}

// baseDir is the bundle directory for a warm base, keyed by an opaque base key
// (one per repo env-image version). Bases live under bases/ so they are never
// confused with per-thread bundles and survive thread GC.
func (d *Driver) baseDir(key string) string {
	return filepath.Join(d.cfg.SnapshotRoot, "bases", key)
}

func (d *Driver) baseSnapfile(key string) string { return filepath.Join(d.baseDir(key), "snapfile") }
func (d *Driver) baseMemfile(key string) string  { return filepath.Join(d.baseDir(key), "memfile") }

// SessionsDir is the parent directory holding all banked SESSION snapshot bundles
// (one bundle dir per session snapshot_ref). It is a sibling of bases/ under the
// snapshot root so a session bundle is never confused with a base or a per-thread
// bundle, and so the daemon can rescan exactly this dir on start to report its
// banked-session inventory. Callers own its 0700 permission and daemon-ownership.
func (d *Driver) SessionsDir() string { return filepath.Join(d.cfg.SnapshotRoot, "sessions") }

// sessionDir is the bundle directory for one banked session snapshot, keyed by an
// opaque session snapshot_ref. It sits under sessions/ so it is never confused
// with a base (bases/) or a per-thread (SnapshotRoot/<threadID>) bundle.
func (d *Driver) sessionDir(ref string) string { return filepath.Join(d.SessionsDir(), ref) }

func (d *Driver) sessionSnapfile(ref string) string {
	return filepath.Join(d.sessionDir(ref), "snapfile")
}

func (d *Driver) sessionMemfile(ref string) string {
	return filepath.Join(d.sessionDir(ref), "memfile")
}

// ServingDir is the parent directory holding all banked SERVING snapshot bundles,
// under the serving/ prefix of the snapshot root (a sibling of bases/ and sessions/).
// The daemon rescans exactly this dir on start to report its banked-serving
// inventory. Callers own its 0700 permission and daemon-ownership.
func (d *Driver) ServingDir() string { return filepath.Join(d.cfg.SnapshotRoot, "serving") }

// servingDir is the bundle directory for one banked serving snapshot, keyed by an
// opaque serving snapshot_ref. It sits under serving/ so it is never confused with a
// base, a session, or a per-thread bundle.
func (d *Driver) servingDir(ref string) string { return filepath.Join(d.ServingDir(), ref) }

func (d *Driver) servingSnapfile(ref string) string {
	return filepath.Join(d.servingDir(ref), "snapfile")
}

func (d *Driver) servingMemfile(ref string) string {
	return filepath.Join(d.servingDir(ref), "memfile")
}

// servingMetafile is the sidecar recording the pinned tap IP a serving snapshot was
// banked with (D-R3.4.1). A relight reads it to re-acquire the SAME host IP, because
// the guest's eth0 keeps the IP baked at fresh boot and a snapshot resume never
// re-runs kernel init. It sits inside the bundle dir so eviction removes it with the
// bundle and a startup rescan recovers the pin.
func (d *Driver) servingMetafile(ref string) string {
	return filepath.Join(d.servingDir(ref), "ip")
}

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

	return d.coldBoot(ctx, threadID, coldBootSpec{
		rootfsPath: d.cfg.RootfsPath,
		vcpus:      d.cfg.VCPUs,
		memMib:     d.cfg.MemMib,
		nic:        spec.NIC,
	})
}

// coldBootSpec parameterises a cold boot. The task cold-boot path (Claim) fills it
// from driver config; the serving cold-boot path (ClaimServing) fills it per call
// (per-workload rootfs/sizing) and sets a NIC. Keeping the boot sequence in one helper
// means the task and serving cold boots are byte-identical except for the fields here,
// so the serving addition cannot drift the task boot.
type coldBootSpec struct {
	rootfsPath string
	vcpus      int
	memMib     int
	nic        *substrate.NICSpec
}

// coldBoot launches a fresh Firecracker process, provisions a per-thread rootfs (or
// falls back to the given static rootfs), configures the machine + boot source +
// rootfs drive (+ tap NIC when nic != nil) + vsock, and Starts it. The NIC branch is
// the ONLY serving-specific step and is skipped entirely when nic is nil, so the
// task/session cold boot is byte-unchanged.
func (d *Driver) coldBoot(ctx context.Context, threadID string, cb coldBootSpec) (substrate.Handle, error) {
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
	rootfsPath := cb.rootfsPath
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
		if err := client.PutMachineConfig(ctx, fcclient.MachineConfig{VCPUCount: cb.vcpus, MemSizeMib: cb.memMib}); err != nil {
			return err
		}
		if err := client.PutBootSource(ctx, fcclient.BootSource{KernelImagePath: d.cfg.KernelImagePath, BootArgs: d.bootArgsFor(cb.nic)}); err != nil {
			return err
		}
		if err := client.PutDrive(ctx, fcclient.Drive{DriveID: "rootfs", PathOnHost: rootfsPath, IsRootDevice: true, IsReadOnly: d.cfg.RootfsReadOnly}); err != nil {
			return err
		}
		// Serving class (R3): attach the tap NIC pre-Start. Task/session claims leave
		// nic nil and never reach this, so their boot path is byte-unchanged
		// (vsock-only, no NIC). Firecracker cannot hot-attach a NIC to a resumed
		// snapshot, so a NIC is only configured on this COLD-boot path; the serving
		// relight path restores a snapshot that already captured its NIC.
		if cb.nic != nil {
			if err := client.PutNetworkInterface(ctx, fcclient.NetworkInterface{
				IfaceID:     nicIfaceID,
				HostDevName: cb.nic.HostDevName,
				GuestMAC:    cb.nic.GuestMAC,
			}); err != nil {
				return err
			}
		}
		// The zip lane no longer attaches the archive as a block device: it is
		// hydrated over vsock (POST /shim/hydrate) after boot, so the build guest
		// has only the rootfs drive and the snapshot carries no archive backing
		// file (self-contained + portable). See noded/server buildBaseZip.
		// The vsock device is the guest's only channel to the controller (task
		// delivery, idle signal, egress proxy). It must be configured before Start.
		if err := client.PutVsock(ctx, fcclient.Vsock{GuestCID: guestCID, UDSPath: d.bootVsockPath(threadID)}); err != nil {
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

// ClaimServing cold-boots a serving-class microVM from a per-workload rootfs WITH a
// tap NIC configured pre-Start and its static IP baked via boot-args (D-R3.4.2: a
// serving VM must be cold-booted because a resumed snapshot cannot gain a NIC). The
// VM lands in THIS driver's live map, so it counts against LiveCount exactly like a
// task or session VM, and its later bank (SnapshotServing) / destroy (Release) run
// against the same driver. rootfsPath/vcpus/memMib come from the serving workload's
// image identity (resolved by the server from its image table), NOT driver config, so
// one shared driver serves every serving workload.
func (d *Driver) ClaimServing(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec) (substrate.Handle, error) {
	if nic.HostDevName == "" {
		return substrate.Handle{}, fmt.Errorf("driver: ClaimServing requires a host tap device")
	}
	// harnessInit currently rides driver-global cfg.HarnessInit (the serving rootfs
	// boots the same shim init as every other guest); the parameter is carried so a
	// per-image init override can be threaded later without a signature change.
	_ = harnessInit
	vcpus = orDefault(vcpus, d.cfg.VCPUs)
	memMib = orDefault(memMib, d.cfg.MemMib)
	nicCopy := nic
	return d.coldBoot(ctx, newID("serv"), coldBootSpec{
		rootfsPath: rootfsPath,
		vcpus:      vcpus,
		memMib:     memMib,
		nic:        &nicCopy,
	})
}

// orDefault returns v when positive, else def.
func orDefault(v, def int) int {
	if v > 0 {
		return v
	}
	return def
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
	// Write to temp paths and rename into place. A rebuild must NOT overwrite the
	// live snapfile/memfile in place: another thread may be restoring from them
	// (the File mem-backend mmaps the memfile), and overwriting a mapped file is a
	// SIGBUS foot-gun. rename(2) swaps the directory entry to a new inode while any
	// in-flight restore keeps the old (now-unlinked) one.
	snapTmp := snapPath + ".tmp"
	memTmp := memPath + ".tmp"

	if err := inst.client.Pause(ctx); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: pause: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapTmp, MemFilePath: memTmp}); err != nil {
		_ = inst.client.Resume(ctx)
		return substrate.SnapshotRef{}, fmt.Errorf("driver: create base snapshot: %w", err)
	}
	if err := inst.client.Resume(ctx); err != nil {
		_ = d.Release(ctx, h)
		return substrate.SnapshotRef{}, fmt.Errorf("driver: resume after base snapshot: %w", err)
	}
	// Publish memfile before snapfile: a restore reads the snapfile to locate the
	// memfile, so the memfile must already be in place when the snapfile appears.
	if err := os.Rename(memTmp, memPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish base memfile: %w", err)
	}
	if err := os.Rename(snapTmp, snapPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish base snapfile: %w", err)
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

// SnapshotSession captures a LIVE session microVM into a self-contained session
// bundle keyed by the opaque snapshot_ref, under sessions/<ref>. It is the R2
// session-bank mechanic and REUSES the base-bundle format exactly (a full memfile
// + snapfile, no archive backing file), so a session snapshot is as portable and
// restorable as a base: the memory image IS the session state.
//
// Unlike SnapshotBase, it does NOT resume the guest: a Bank pauses, snapshots, and
// then the caller destroys the VM (the session releases its live capacity and
// holds only disk). Pausing before the snapshot is required so the memfile is a
// consistent point-in-time image; leaving the VM paused is fine because the caller
// Releases the handle immediately after. On a snapshot failure the VM is torn down
// (a stranded paused VM would squat capacity), matching SnapshotBase's failure
// posture.
//
// The bundle is written to temp paths and renamed into place (memfile before
// snapfile, so a concurrent restore reading the snapfile always finds its memfile),
// the same publish discipline the base path uses. The sessions dir is created 0700
// so a banked bundle (a principal's memory image) is never world-readable.
func (d *Driver) SnapshotSession(ctx context.Context, h substrate.Handle, snapshotRef string) (substrate.SnapshotRef, error) {
	if snapshotRef == "" {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: SnapshotSession requires a snapshot_ref")
	}
	inst := d.get(h.ID)
	if inst == nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: snapshot-session of unknown handle %q", h.ID)
	}
	// A bank is destructive: the VM is destined for teardown regardless of outcome.
	// Any failure AFTER the handle is confirmed tears the VM down here (it may be
	// running, or stranded paused mid-snapshot), so a failed bank NEVER leaves a
	// live or paused handle behind for the server to misreport as session capacity.
	// On success the VM is left paused; the caller (server Bank) destroys it.
	banked := false
	defer func() {
		if !banked {
			_ = d.Release(ctx, h)
		}
	}()
	if err := os.MkdirAll(d.sessionDir(snapshotRef), 0o700); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: mkdir session bundle: %w", err)
	}
	snapPath := d.sessionSnapfile(snapshotRef)
	memPath := d.sessionMemfile(snapshotRef)
	snapTmp := snapPath + ".tmp"
	memTmp := memPath + ".tmp"

	if err := inst.client.Pause(ctx); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: pause session: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapTmp, MemFilePath: memTmp}); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: create session snapshot: %w", err)
	}
	// Publish memfile before snapfile: a restore reads the snapfile to locate the
	// memfile, so the memfile must already be in place when the snapfile appears.
	if err := os.Rename(memTmp, memPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish session memfile: %w", err)
	}
	if err := os.Rename(snapTmp, snapPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish session snapfile: %w", err)
	}
	banked = true
	return substrate.SnapshotRef{
		ID:        snapshotRef,
		Node:      d.cfg.Node,
		Arch:      d.cfg.Arch,
		SizeBytes: bundleSize(snapPath, memPath),
	}, nil
}

// RestoreSession launches a fresh Firecracker process and loads a banked SESSION
// bundle (sessions/<snapshotRef>), resuming it so the guest continues exactly where
// it was banked. It is the R2 relight mechanic and REUSES loadInto (the same path
// warm-base restore uses); the restored handle gets a fresh microVM id and a fresh
// thread id (the bundle carries its own embedded vsock config, so the thread id is
// just the new host bundle dir). A missing bundle is an error the caller maps to
// FAILED_PRECONDITION (the control plane then decides; the snapshot is never
// deleted on a failed restore).
func (d *Driver) RestoreSession(ctx context.Context, snapshotRef string) (substrate.Handle, error) {
	if snapshotRef == "" {
		return substrate.Handle{}, fmt.Errorf("driver: RestoreSession requires a snapshot_ref")
	}
	snapPath := d.sessionSnapfile(snapshotRef)
	if _, err := os.Stat(snapPath); err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: session bundle missing for %q: %w", snapshotRef, err)
	}
	// A restored session gets its own fresh thread (host bundle dir + vsock socket);
	// the bundle's embedded config is re-bound into it by loadInto.
	threadID := newID("sess")
	return d.loadInto(ctx, threadID, snapPath, d.sessionMemfile(snapshotRef), "restore.sock")
}

// RemoveSessionBundle deletes a banked session snapshot's on-disk bundle
// (sessions/<snapshotRef>). It is the R2 EvictSnapshot mechanic. Idempotent: a
// missing bundle is not an error (RemoveAll on an absent path succeeds).
func (d *Driver) RemoveSessionBundle(snapshotRef string) error {
	if snapshotRef == "" {
		return fmt.Errorf("driver: RemoveSessionBundle requires a snapshot_ref")
	}
	if err := os.RemoveAll(d.sessionDir(snapshotRef)); err != nil {
		return fmt.Errorf("driver: remove session bundle: %w", err)
	}
	return nil
}

// ---- serving snapshot mechanics (R3) ---------------------------------------
//
// These mirror the R2 session bank/relight/evict mechanics exactly, under the
// serving/ prefix instead of sessions/, with ONE addition: the pinned tap IP is
// written to an "ip" sidecar in the bundle at bank and returned by rescan, because a
// serving guest's eth0 IP is baked at fresh boot and a resume cannot change it, so a
// relight must re-acquire the same host IP (D-R3.4.1). The digest-versioned bundle
// layout (D-R2.7: snapshots embed host paths, so each bundle is self-contained) is
// unchanged. A serving snapshot carries a NIC because the fresh cold boot created one
// before the bank; restoring it resumes a VM that already has its eth0.

// SnapshotServing pauses a live serving VM and writes a self-contained serving bundle
// (memfile + snapfile) under serving/<ref>, plus the pinned-IP sidecar. It does NOT
// resume: the caller Releases the VM immediately after (StopServing BANK destroys). It
// mirrors SnapshotSession; the only addition is persisting pinnedIP so a relight can
// re-acquire it. On any failure after the handle is confirmed the VM is torn down (a
// bank is destructive), so a failed bank never leaves a live/paused handle behind.
func (d *Driver) SnapshotServing(ctx context.Context, h substrate.Handle, snapshotRef, pinnedIP string) (substrate.SnapshotRef, error) {
	if snapshotRef == "" {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: SnapshotServing requires a snapshot_ref")
	}
	inst := d.get(h.ID)
	if inst == nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: snapshot-serving of unknown handle %q", h.ID)
	}
	banked := false
	defer func() {
		if !banked {
			_ = d.Release(ctx, h)
		}
	}()
	if err := os.MkdirAll(d.servingDir(snapshotRef), 0o700); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: mkdir serving bundle: %w", err)
	}
	snapPath := d.servingSnapfile(snapshotRef)
	memPath := d.servingMemfile(snapshotRef)
	snapTmp := snapPath + ".tmp"
	memTmp := memPath + ".tmp"

	if err := inst.client.Pause(ctx); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: pause serving: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapTmp, MemFilePath: memTmp}); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: create serving snapshot: %w", err)
	}
	// Publish memfile before snapfile (a restore reads the snapfile to locate the
	// memfile), then the IP sidecar. A rescan treats a bundle without a snapfile as
	// half-written and skips it, so the snapfile is published LAST.
	if err := os.Rename(memTmp, memPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish serving memfile: %w", err)
	}
	if pinnedIP != "" {
		if err := os.WriteFile(d.servingMetafile(snapshotRef), []byte(pinnedIP), 0o600); err != nil {
			return substrate.SnapshotRef{}, fmt.Errorf("driver: write serving pinned-ip: %w", err)
		}
	}
	if err := os.Rename(snapTmp, snapPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish serving snapfile: %w", err)
	}
	banked = true
	return substrate.SnapshotRef{
		ID:        snapshotRef,
		Node:      d.cfg.Node,
		Arch:      d.cfg.Arch,
		SizeBytes: bundleSize(snapPath, memPath),
	}, nil
}

// RestoreServing launches a fresh Firecracker process and loads a banked SERVING
// bundle (serving/<snapshotRef>), resuming it so the guest continues exactly where it
// was banked, WITH the NIC it captured at bank time. It mirrors RestoreSession. The
// caller has already re-created the host tap and re-acquired the pinned IP (D-R3.4.1)
// so the resumed guest's baked eth0 IP still routes. A missing bundle is an error the
// caller maps to FAILED_PRECONDITION (the snapshot is never deleted on a failed
// restore).
func (d *Driver) RestoreServing(ctx context.Context, snapshotRef string) (substrate.Handle, error) {
	if snapshotRef == "" {
		return substrate.Handle{}, fmt.Errorf("driver: RestoreServing requires a snapshot_ref")
	}
	snapPath := d.servingSnapfile(snapshotRef)
	if _, err := os.Stat(snapPath); err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: serving bundle missing for %q: %w", snapshotRef, err)
	}
	threadID := newID("serv")
	return d.loadInto(ctx, threadID, snapPath, d.servingMemfile(snapshotRef), "restore.sock")
}

// ServingPinnedIP reads the pinned tap IP a serving snapshot was banked with, for a
// relight to re-acquire (D-R3.4.1) and for a startup rescan to recover. An absent
// sidecar returns "" (a snapshot banked before the IP pin, or a fresh node): the
// caller then allocates a new IP, accepting the rare cold-node reallocation.
func (d *Driver) ServingPinnedIP(snapshotRef string) string {
	b, err := os.ReadFile(d.servingMetafile(snapshotRef))
	if err != nil {
		return ""
	}
	return string(b)
}

// RemoveServingBundle deletes a banked serving snapshot's on-disk bundle
// (serving/<snapshotRef>), including the IP sidecar. It is the serving EvictSnapshot
// mechanic. Idempotent: a missing bundle is not an error.
func (d *Driver) RemoveServingBundle(snapshotRef string) error {
	if snapshotRef == "" {
		return fmt.Errorf("driver: RemoveServingBundle requires a snapshot_ref")
	}
	if err := os.RemoveAll(d.servingDir(snapshotRef)); err != nil {
		return fmt.Errorf("driver: remove serving bundle: %w", err)
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
