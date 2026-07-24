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
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
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
	// Node-SHARED base rootfs snapshots (SnapshotRoot/bases) always live here.
	SnapshotRoot string
	// WarmthRoot is the root for per-INSTANCE warmth (sessions/serving/stateful
	// bundles/group sets/per-thread bundles/checkpoints/group networks): a brick
	// nests it under SnapshotRoot/i/<short-uid> (a SHORT segment so the nested
	// firecracker thread-<hex>/*.sock paths stay under SUN_LEN); the legacy
	// DaemonSet leaves it equal to SnapshotRoot (flat, unchanged). Bases stay on
	// SnapshotRoot. Empty falls back to SnapshotRoot (see warmthRoot), so a driver
	// built from a Config that never derived it keeps the flat pre-brick layout.
	WarmthRoot string
	// Node and Arch pin where snapshots may be restored.
	Node string
	Arch string
	// Vendor is the node's CPUID vendor ("amd", "intel"), stamped into every
	// SnapshotRef this driver produces and checked on every restore exactly
	// like Arch (R7, standing decisions 1 and 11): Firecracker snapshots never
	// cross the AMD/Intel boundary. Empty behaves like an unset Arch check
	// (skipped), which only happens pre-R7 or in a test build.
	Vendor string
	// Template names the Firecracker CPU template this node boots guests with
	// (PR-E, ADR embervm/012), stamped alongside Vendor into every SnapshotRef
	// this driver produces and checked on every restore. Empty behaves like an
	// unset Vendor check (skipped), which only happens pre-PR-E or in a test
	// build.
	//
	// This is a LOGICAL identity/versioning label only in this PR: it is NOT
	// yet wired into PutMachineConfig's wire-level cpu_template (Firecracker's
	// real CPUID-masking parameter, Intel-only; AMD FC has no equivalent
	// today). Wiring an unverified value into the live boot path is exactly
	// the risk the plan's verify step (boot + BuildBase + restore round-trip
	// per vendor, on real silicon) exists to catch before it is load-bearing;
	// until that verify step runs on the Alder Lake-S masters, this field only
	// drives the sku stamp/mismatch/grandfather gate, never the FC API call.
	Template string
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
	// checkpoints holds in-flight interruptible-bank checkpoints (ADR embervm/008),
	// keyed by the opaque token CheckpointStateful returns: a PAUSED-but-not-
	// destroyed VM plus its temp snapshot, awaiting a commit or abort. Guarded by mu.
	checkpoints map[string]*statefulCheckpoint
}

// statefulCheckpoint is one paused-awaiting-resolve stateful VM (ADR embervm/008):
// its live handle, the bundle ref a commit will publish under, the volume
// generation stamped at pause time, and the temp dir (OUTSIDE stateful/) holding
// the not-yet-published snapfile + memfile.
type statefulCheckpoint struct {
	handle      substrate.Handle
	snapshotRef string
	generation  uint64
	tmpDir      string
	// pinnedIP is the tap IP the paused VM held, recorded so a COMMIT publishes it
	// as the bundle's pinned-IP sidecar and a later relight re-acquires the same IP.
	pinnedIP string
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
		cfg:         cfg.withDefaults(),
		launcher:    launcher,
		newClient:   newClient,
		live:        make(map[string]*instance),
		checkpoints: make(map[string]*statefulCheckpoint),
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

// legacyVendorAlias is the vendor a ref with an empty Vendor is treated as (the
// node-4 alias, standing decision 11): every snapshot captured before vendor
// keying shipped predates any vendor other than node-4's, so an empty ref
// vendor is read as "amd" rather than "unset", and never re-exports or
// false-mismatches merely for predating this field.
const legacyVendorAlias = "amd"

// vendorMismatch reports whether a SnapshotRef's vendor conflicts with the
// node's own vendor, mirroring the arch mismatch checks above. An empty
// refVendor is aliased to legacyVendorAlias before comparing (a pre-R7
// snapshot). An empty nodeVendor skips the check entirely (an undetected node
// vendor, e.g. a non-Linux test build), matching how an empty d.cfg.Arch skips
// the arch check. It returns the vendor string used for the comparison so the
// caller's error message reports what actually mismatched (the aliased value,
// not a blank one).
func vendorMismatch(refVendor, nodeVendor string) (bool, string) {
	if nodeVendor == "" {
		return false, refVendor
	}
	effective := refVendor
	if effective == "" {
		effective = legacyVendorAlias
	}
	return effective != nodeVendor, effective
}

// templateMismatch reports whether a SnapshotRef's CPU template conflicts with
// the node's own (PR-E, ADR embervm/012's grandfather rule). UNLIKE
// vendorMismatch, an empty refTemplate is NEVER aliased to a guessed value: it
// is read as UNSTAMPED (a legacy artifact cut before template stamping
// existed, or one this same daemon cut pre-PR-E) and is ALWAYS compatible,
// regardless of the node's own template, because refusing a grandfathered
// artifact for a missing stamp is data loss. A NON-EMPTY refTemplate that
// differs from the node's own is a hard mismatch. An empty nodeTemplate skips
// the check entirely (an undetected/unconfigured node template), mirroring how
// an empty nodeVendor already skips the vendor check. It returns the template
// string used for the comparison so the caller's error message reports what
// actually mismatched.
func templateMismatch(refTemplate, nodeTemplate string) (bool, string) {
	if nodeTemplate == "" || refTemplate == "" {
		return false, refTemplate
	}
	return refTemplate != nodeTemplate, refTemplate
}

// warmthRoot is the root for per-INSTANCE warmth (sessions, serving, stateful
// bundles, group sets, per-thread bundles, checkpoints, group networks). For a
// brick it is SnapshotRoot/i/<short-uid> (set by config.Load); for the
// legacy DaemonSet it equals SnapshotRoot. Falls back to SnapshotRoot when unset
// so a driver built from a Config that never derived WarmthRoot (tests) keeps the
// flat pre-brick layout. Bases (baseDir) deliberately do NOT go through here:
// they stay node-shared on SnapshotRoot.
func (d *Driver) warmthRoot() string {
	if d.cfg.WarmthRoot != "" {
		return d.cfg.WarmthRoot
	}
	return d.cfg.SnapshotRoot
}

// threadDir is the bundle directory for a thread (per-instance warmth).
func (d *Driver) threadDir(threadID string) string {
	return filepath.Join(d.warmthRoot(), threadID)
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
	return d.bootArgsFor(coldBootSpec{})
}

// nicIfaceID is the Firecracker network-interface id for a serving VM's single tap.
const nicIfaceID = "eth0"

// bootArgsFor is bootArgs plus, for a serving-class cold boot (nic != nil), the
// kernel `ip=` directive that statically configures the guest's interface at boot:
// ip=<vmip>::<gwip>:<mask>::<iface>:off (autoconf off; the daemon owns the address).
// Task/session boots pass an empty spec (nil nic, empty handlerDiskPath) and get
// exactly the previous boot args (byte-unchanged). The IP is baked at THIS cold boot;
// a later snapshot resume keeps it (a resume does not re-run kernel init), which is
// why the serving IP is pinned across bank/relight (D-R3.4.1).
func (d *Driver) bootArgsFor(cb coldBootSpec) string {
	args := d.cfg.KernelBootArgs
	// Prefer the per-boot harness init (the serving cold-boot resolves it from the
	// runtime image it boots) over the driver-global, which is empty on the daemon
	// driver. Without init= the kernel finds no init and drops to /bin/sh.
	harnessInit := cb.harnessInit
	if harnessInit == "" {
		harnessInit = d.cfg.HarnessInit
	}
	if harnessInit != "" {
		args += " init=" + harnessInit
	}
	if nic := cb.nic; nic != nil {
		iface := nic.IfaceName
		if iface == "" {
			iface = nicIfaceID
		}
		mask := prefixLenToMask(nic.PrefixLen)
		// Linux kernel ip= directive: client-ip::gw-ip:netmask:hostname:device:autoconf
		args += fmt.Sprintf(" ip=%s::%s:%s::%s:off", nic.IP, nic.GatewayIP, mask, iface)
		// Serving cold boot (R3, D-R3.11.1): tell the guest to answer HTTP over
		// the tap NIC on this TCP port instead of vsock. guest-init reads this
		// token from /proc/cmdline into EMBER_SERVING_PORT for the python shim.
		// It is the SAME port the daemon later health-probes and publishes.
		if nic.ServingPort > 0 {
			args += fmt.Sprintf(" ember.serving_port=%d", nic.ServingPort)
		}
	}
	// Serving handler disk (R3, D-R3.11.2): signal the guest to import the handler
	// off the second read-only drive before serving. ember.handler_disk=<dev> names
	// the block device; ember.handler_zip_bytes=<N> is the EXACT zip length so the
	// guest reads ONLY the payload. This length is LOAD-BEARING for two reasons.
	// (1) noded pads the artifact file UP to a whole 512-byte sector on write
	// (WriteServingHandlerArtifact), because Firecracker FLOORS a drive to whole
	// sectors and drops the remainder ("the remainder will not be visible to the
	// guest"); without the pad a 3056-byte zip becomes a 2560-byte device and the
	// EOCD-bearing tail is truncated, short-reading the guest. (2) With the pad, the
	// raw device is the zip followed by ≤511 trailing zero bytes, and Python's zipfile
	// scans BACKWARD from the end for the End-Of-Central-Directory signature, so
	// trailing padding past the real EOCD can break that scan (BadZipFile). Conveying
	// the exact length lets the guest read ONLY the payload instead of guessing where
	// the zip ends. This is the sector-pad/EOCD-trim bug class the R1 zip lane hit and
	// retired when it moved archive delivery to vsock hydration; here the artifact is
	// legitimately a block device again (a serving cold boot has no vsock archive
	// channel), so we defuse it with pad-on-write plus exact-length-on-read. Only emitted when a handler disk is attached;
	// task/session boots never carry these tokens.
	if cb.handlerDiskPath != "" {
		args += fmt.Sprintf(" ember.handler_disk=%s ember.handler_zip_bytes=%d", handlerDiskDevice, cb.handlerZipBytes)
	}
	// Stateful volume (R4): signal guest-init to mkfs-if-blank and mount the
	// writable volume drive at the workload's declared mount path. ember.volume_dev
	// names the fixed device convention (statefulVolumeDevice); ember.volume_mount
	// is the guest path from the CR's volumeMountPath, threaded verbatim (its
	// content is opaque to the daemon). Only emitted when a volume is attached;
	// every other boot class carries neither token.
	if cb.volumeDiskPath != "" {
		// Drives land on /dev/vd{a,b,c...} in ATTACH ORDER: rootfs is always vda.
		// A serving-style stateful boot attaches the read-only handler as drive 2
		// (vdb), pushing the writable volume to drive 3 (vdc). An image-lane
		// stateful boot (opaque-L4, e.g. Postgres) has NO handler drive, so the
		// volume is drive 2 and lands on vdb. Signal the ACTUAL device so guest-init
		// mounts the right one; a fixed /dev/vdc would be a nonexistent device on
		// the handler-less path.
		volumeDev := statefulVolumeDeviceNoHandler
		if cb.handlerDiskPath != "" {
			volumeDev = statefulVolumeDevice
		}
		args += fmt.Sprintf(" ember.volume_dev=%s ember.volume_mount=%s", volumeDev, cb.volumeMount)
	}
	// MMDS-lite over boot-args (R4, D-R4.PR-7.1): a stateful workload's first-boot
	// secrets (e.g. a Postgres superuser password) ride the kernel cmdline as
	// ember.env.<KEY>=<base64url(value)> tokens, one per mmdsEnv entry, sorted by
	// key for a deterministic boot-arg string (test-friendly, and stable across
	// identical calls). This is a deliberate MMDS substitute: no metadata service
	// exists yet, the workload is cluster-internal and low-stakes (a scratch
	// datastore), and the cmdline is a few small secrets only (length + charset
	// limits on the kernel command line rule out anything bulk). See DECISIONS.md
	// D-R4.PR-7.1 for the full tradeoff and the migration path to a real MMDS.
	// SECURITY: do not log mmdsEnv values anywhere in this package; only key
	// names are safe to log (see mmdsEnvKeyNames below).
	if len(cb.mmdsEnv) > 0 {
		args += " " + mmdsEnvBootArgs(cb.mmdsEnv)
	}
	return args
}

// mmdsEnvKeyPattern is the allowed charset for an mmds_env key: a shell/kernel-
// cmdline-safe identifier. A key outside this set is silently skipped (never
// included in the boot-arg string) rather than failing the whole boot, since a
// single malformed key must not block every other secret from being delivered.
func isValidMmdsEnvKey(key string) bool {
	if key == "" {
		return false
	}
	for _, r := range key {
		if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' {
			continue
		}
		return false
	}
	return true
}

// mmdsEnvBootArgs renders mmdsEnv as space-separated ember.env.<KEY>=<base64url>
// tokens, one per entry, sorted by key so the output is deterministic. Keys are
// validated against isValidMmdsEnvKey (skipped, not fatal, if invalid); values
// are base64url-encoded (RawURLEncoding: no padding, no '+'/'/' characters that
// would need cmdline escaping) so an arbitrary secret value survives the kernel
// command line's space-separated token parsing intact.
func mmdsEnvBootArgs(mmdsEnv map[string]string) string {
	keys := make([]string, 0, len(mmdsEnv))
	for k := range mmdsEnv {
		if isValidMmdsEnvKey(k) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	tokens := make([]string, 0, len(keys))
	for _, k := range keys {
		encoded := base64.RawURLEncoding.EncodeToString([]byte(mmdsEnv[k]))
		tokens = append(tokens, fmt.Sprintf("ember.env.%s=%s", k, encoded))
	}
	return strings.Join(tokens, " ")
}

// mmdsEnvKeyNames returns the sorted key names of mmdsEnv for logging. It NEVER
// returns values: mmds_env carries first-boot secrets (e.g. a Postgres
// password), and the boot-args tradeoff already puts the value on the guest's
// /proc/cmdline (D-R4.PR-7.1) -- the daemon's own logs must not compound that by
// also persisting the plaintext value. Callers log only this, never cb.mmdsEnv
// directly.
func mmdsEnvKeyNames(mmdsEnv map[string]string) []string {
	keys := make([]string, 0, len(mmdsEnv))
	for k := range mmdsEnv {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// handlerDiskDevice is the guest block-device path Firecracker assigns to the
// SECOND drive (the rootfs is the root device /dev/vda; the handler drive, added
// next, is /dev/vdb). guest-init exports this to the shim as EMBER_HANDLER_ZIP.
const handlerDiskDevice = "/dev/vdb"

// statefulVolumeDevice / statefulVolumeDeviceNoHandler are the guest block-device
// paths Firecracker assigns to a stateful VM's writable volume drive (R4),
// selected by whether a drive-2 handler artifact is present. Drives land on
// /dev/vd{a,b,c...} in ATTACH ORDER. rootfs is always drive 1 / /dev/vda.
//
//   - WITH a handler (a serving-style stateful boot, if one ever carries a zip
//     handler): handler is drive 2 / /dev/vdb, so the writable volume is drive 3
//     and lands on /dev/vdc.
//   - WITHOUT a handler (an image-lane, opaque-L4 stateful guest like Postgres):
//     there is no drive 2, so the writable volume is drive 2 and lands on
//     /dev/vdb.
//
// guest-init reads ember.volume_dev from /proc/cmdline and mounts exactly the
// signalled device; the host never mounts it. The device is derived (not fixed)
// because the handler drive's presence shifts the volume's position by one.
const (
	statefulVolumeDevice          = "/dev/vdc"
	statefulVolumeDeviceNoHandler = "/dev/vdb"
)

// sectorSizeBytes is Firecracker's block-device sector size. A drive backing file
// must be a whole multiple of it or FC floors the exposed device to the nearest
// lower sector and drops the remainder, so the handler artifact is padded up to it.
const sectorSizeBytes = 512

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

// baseHandlerZip / baseHandlerRuntimeRef are the serving cold-boot handler artifact
// (D-R3.11.2) and its runtime-ref sidecar, colocated in the base bundle dir alongside
// the memory snapshot. handler.zip is the verified zip bytes noded holds, attached as a
// read-only drive on a serving cold boot; runtime.ref records the runtime image whose
// rootfs is drive 1 so a startup rescan can rebuild the serving-images inventory without
// a control-plane round-trip.
func (d *Driver) baseHandlerZip(key string) string {
	return filepath.Join(d.baseDir(key), "handler.zip")
}

func (d *Driver) baseHandlerRuntimeRef(key string) string {
	return filepath.Join(d.baseDir(key), "runtime.ref")
}

// baseHandlerLen records the EXACT (pre-sector-pad) zip length so a startup rescan
// conveys the same exact byte count the build path did, rather than the padded
// on-disk file size: the guest must read only the real payload, and the artifact
// file is padded up to a whole sector on write (see WriteServingHandlerArtifact).
func (d *Driver) baseHandlerLen(key string) string {
	return filepath.Join(d.baseDir(key), "handler.len")
}

// WriteServingHandlerArtifact writes the verified zip bytes for a serving base to
// bases/<key>/handler.zip plus a runtime.ref sidecar (the runtime image whose rootfs is
// the cold-boot drive 1), and returns the zip's host path and exact byte length
// (D-R3.11.2). It is idempotent (a re-write overwrites in place) and creates the base
// dir if the serving artifact is written before the memory snapshot. The exact length
// is the EOCD-padding defence: the guest reads only this many bytes off the
// (sector-padded) block device. The sidecar lets a startup rescan rebuild the
// serving-images inventory (base key -> handler + runtime ref) with no control-plane
// round-trip.
func (d *Driver) WriteServingHandlerArtifact(baseKey, runtimeImageRef string, zip []byte) (string, int64, error) {
	if err := os.MkdirAll(d.baseDir(baseKey), 0o750); err != nil {
		return "", 0, fmt.Errorf("driver: mkdir base bundle for handler artifact: %w", err)
	}
	path := d.baseHandlerZip(baseKey)
	// Pad the on-disk file UP to a whole 512-byte sector before writing. Firecracker
	// exposes a block device sized to the FLOOR of the backing file in sectors and
	// drops the remainder ("Disk size N is not a multiple of sector size 512; the
	// remainder will not be visible to the guest"), so an unpadded 3056-byte zip
	// becomes a 2560-byte device: the trailing bytes carrying the zip's EOCD vanish
	// and the guest short-reads. Padding with zeros to the next sector makes FC expose
	// the ENTIRE zip; the guest still reads only the exact length (returned below), so
	// the ≤511 trailing zero bytes are never handed to zipfile.
	padded := zip
	if rem := len(zip) % sectorSizeBytes; rem != 0 {
		padded = make([]byte, len(zip)+(sectorSizeBytes-rem))
		copy(padded, zip)
	}
	if err := os.WriteFile(path, padded, 0o640); err != nil {
		return "", 0, fmt.Errorf("driver: write handler artifact: %w", err)
	}
	if err := os.WriteFile(d.baseHandlerRuntimeRef(baseKey), []byte(runtimeImageRef), 0o640); err != nil {
		return "", 0, fmt.Errorf("driver: write handler runtime ref sidecar: %w", err)
	}
	// Persist the EXACT zip length so a post-restart rescan conveys the same byte
	// count the guest reads, not the padded on-disk file size.
	if err := os.WriteFile(d.baseHandlerLen(baseKey), []byte(strconv.FormatInt(int64(len(zip)), 10)), 0o640); err != nil {
		return "", 0, fmt.Errorf("driver: write handler length sidecar: %w", err)
	}
	return path, int64(len(zip)), nil
}

// ServingHandlerArtifactPath returns a base's handler-artifact path and whether it
// exists on disk.
func (d *Driver) ServingHandlerArtifactPath(baseKey string) (string, bool) {
	path := d.baseHandlerZip(baseKey)
	if _, err := os.Stat(path); err != nil {
		return "", false
	}
	return path, true
}

// ScanServingHandlerArtifacts globs bases/*/handler.zip on startup and returns each
// discovered artifact with its base key, path, size, and runtime ref (from the
// runtime.ref sidecar), so the daemon re-seeds its serving-images inventory after a
// restart. A base dir with a memory snapshot but no handler.zip (a task/session-only
// base) is skipped; a handler.zip without a readable sidecar is skipped with no error
// (it will be rebuilt on the next BuildBase), keeping the rescan best-effort like the
// banked-snapshot rescan.
func (d *Driver) ScanServingHandlerArtifacts() []substrate.ServingHandlerArtifact {
	basesDir := filepath.Join(d.cfg.SnapshotRoot, "bases")
	entries, err := os.ReadDir(basesDir)
	if err != nil {
		return nil // no bases dir yet (fresh node): nothing to rescan
	}
	out := make([]substrate.ServingHandlerArtifact, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		baseKey := e.Name()
		zipPath := d.baseHandlerZip(baseKey)
		fi, err := os.Stat(zipPath)
		if err != nil {
			continue // not a serving base (no handler artifact)
		}
		runtimeRef, rerr := os.ReadFile(d.baseHandlerRuntimeRef(baseKey))
		if rerr != nil {
			continue // artifact without a runtime sidecar: skip, BuildBase rebuilds it
		}
		// Exact zip length from the sidecar (the on-disk file is sector-padded, so
		// its size is >= the real zip; the guest must read only the payload). Fall
		// back to the padded file size for a legacy artifact with no length sidecar;
		// the pad is <= one sector, within zipfile's backward EOCD scan tolerance.
		sizeBytes := fi.Size()
		if lenBytes, lerr := os.ReadFile(d.baseHandlerLen(baseKey)); lerr == nil {
			if n, perr := strconv.ParseInt(strings.TrimSpace(string(lenBytes)), 10, 64); perr == nil && n > 0 {
				sizeBytes = n
			}
		}
		out = append(out, substrate.ServingHandlerArtifact{
			BaseKey:         baseKey,
			Path:            zipPath,
			RuntimeImageRef: strings.TrimSpace(string(runtimeRef)),
			SizeBytes:       sizeBytes,
		})
	}
	return out
}

// SessionsDir is the parent directory holding all banked SESSION snapshot bundles
// (one bundle dir per session snapshot_ref). It is a sibling of bases/ under the
// snapshot root so a session bundle is never confused with a base or a per-thread
// bundle, and so the daemon can rescan exactly this dir on start to report its
// banked-session inventory. Callers own its 0700 permission and daemon-ownership.
func (d *Driver) SessionsDir() string { return filepath.Join(d.warmthRoot(), "sessions") }

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
func (d *Driver) ServingDir() string { return filepath.Join(d.warmthRoot(), "serving") }

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
		if vmis, refVendor := vendorMismatch(ref.Vendor, d.cfg.Vendor); vmis {
			return substrate.Handle{}, fmt.Errorf("driver: base vendor mismatch: ref %q != node %q (snapshots non-portable across CPU vendor)", refVendor, d.cfg.Vendor)
		}
		if tmis, refTemplate := templateMismatch(ref.Template, d.cfg.Template); tmis {
			return substrate.Handle{}, fmt.Errorf("driver: base cpu_sku mismatch: template %q != node %q (snapshots non-portable across CPU template)", refTemplate, d.cfg.Template)
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
	// harnessInit, when non-empty, is the guest-init path emitted as init=<path> on
	// the kernel command line for THIS cold boot, overriding the driver-global
	// cfg.HarnessInit. The serving cold-boot (ClaimServing) resolves it per call from
	// the runtime image whose rootfs it boots (img.HarnessInit), because the daemon's
	// driver-global HarnessInit is empty: without it the kernel finds no init and
	// falls back to /bin/sh, so the shim never runs and the guest is reaped. Empty for
	// task/session boots, which keep the driver-global (byte-unchanged).
	harnessInit string
	// handlerDiskPath, when non-empty, is a per-workload handler artifact (the
	// verified zip bytes noded wrote host-side at BuildBase) attached as a SECOND
	// read-only drive on a serving cold boot (D-R3.11.2). The guest reads the zip
	// off this device and imports the handler before serving, so a NIC cold boot
	// carries the handler without resuming the vsock-only base memory snapshot.
	// Empty for task/session boots and for a serving relight (which resumes a
	// NIC-bearing serving snapshot instead). handlerZipBytes is the EXACT zip
	// length so the guest reads only the payload and not the block device's
	// sector padding (see bootArgsFor for the EOCD-padding rationale).
	handlerDiskPath string
	handlerZipBytes int64
	// volumeDiskPath, when non-empty, is a stateful workload's writable volume
	// file (R4) attached as a THIRD drive (after rootfs and, when present, the
	// handler artifact) with IsReadOnly=false: the guest's durable data, the one
	// device the host never mounts or parses. volumeMount is threaded into
	// boot-args as ember.volume_mount so guest-init knows where to mount it (the
	// device path itself, /dev/vdc, is a fixed convention guest-init also knows;
	// see statefulVolumeDevice). Empty for every task/session/serving boot, which
	// keeps their drive set byte-unchanged.
	volumeDiskPath string
	volumeMount    string
	// mmdsEnv carries a stateful FRESH/COLD boot's first-boot secrets (R4,
	// D-R4.PR-7.1: MMDS-lite over boot-args), encoded into ember.env.<KEY>=
	// boot-args by bootArgsFor. Empty for every task/session/serving boot and
	// for a stateful RELIGHT (a relight resumes a memory snapshot; the kernel
	// never re-inits, so boot-args are not read again -- the secret was already
	// consumed at first boot and baked into the volume's initialized data).
	mmdsEnv map[string]string
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
		if err := client.PutBootSource(ctx, fcclient.BootSource{KernelImagePath: d.cfg.KernelImagePath, BootArgs: d.bootArgsFor(cb)}); err != nil {
			return err
		}
		if err := client.PutDrive(ctx, fcclient.Drive{DriveID: "rootfs", PathOnHost: rootfsPath, IsRootDevice: true, IsReadOnly: d.cfg.RootfsReadOnly}); err != nil {
			return err
		}
		// Serving handler disk (R3, D-R3.11.2): attach the per-workload handler
		// artifact as a SECOND read-only drive so the guest can read the zip and
		// import the handler on this NIC cold boot. Task/session boots leave
		// handlerDiskPath empty and never reach this, so their drive set is
		// byte-unchanged (rootfs-only). It is a non-root, read-only device; the
		// guest never writes it and the cold boot is not snapshotted, so this
		// re-introduces none of the block-device snapshot-backing-dep bug class
		// the R1 zip lane retired (a serving cold boot does not snapshot the base).
		if cb.handlerDiskPath != "" {
			if err := client.PutDrive(ctx, fcclient.Drive{DriveID: "handler", PathOnHost: cb.handlerDiskPath, IsRootDevice: false, IsReadOnly: true}); err != nil {
				return err
			}
		}
		// Stateful volume (R4): attach the workload's writable volume file as the
		// THIRD drive (after rootfs and the handler artifact), landing on
		// statefulVolumeDevice (/dev/vdc). IsReadOnly=false: this is the ONE
		// writable device a stateful guest gets, and the host never mounts or
		// parses its filesystem (guest-init owns mkfs-if-blank + mount). Empty for
		// every task/session/serving boot, so their drive set is unaffected.
		if cb.volumeDiskPath != "" {
			if err := client.PutDrive(ctx, fcclient.Drive{DriveID: "volume", PathOnHost: cb.volumeDiskPath, IsRootDevice: false, IsReadOnly: false}); err != nil {
				return err
			}
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
// handlerDiskPath/handlerZipBytes, when set, attach the per-workload zip handler
// artifact as a second read-only drive and tell the guest to import it before
// serving (D-R3.11.2, zip lane). They are empty/zero for an image-lane serving
// cold boot (whose handler is already in the rootfs), keeping that boot path
// unchanged.
func (d *Driver) ClaimServing(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec, handlerDiskPath string, handlerZipBytes int64) (substrate.Handle, error) {
	if nic.HostDevName == "" {
		return substrate.Handle{}, fmt.Errorf("driver: ClaimServing requires a host tap device")
	}
	// The daemon driver's global HarnessInit is empty; the serving rootfs boots the
	// same guest-init as every other guest, but that path lives in the runtime image
	// config (img.HarnessInit), resolved by startServingFresh and passed here. Thread
	// it into the boot so the kernel runs the guest-init (which reads the serving-port
	// and handler-disk boot-args) instead of falling back to /bin/sh.
	vcpus = orDefault(vcpus, d.cfg.VCPUs)
	memMib = orDefault(memMib, d.cfg.MemMib)
	nicCopy := nic
	return d.coldBoot(ctx, newID("serv"), coldBootSpec{
		rootfsPath:      rootfsPath,
		vcpus:           vcpus,
		memMib:          memMib,
		nic:             &nicCopy,
		harnessInit:     harnessInit,
		handlerDiskPath: handlerDiskPath,
		handlerZipBytes: handlerZipBytes,
	})
}

// orDefault returns v when positive, else def.
func orDefault(v, def int) int {
	if v > 0 {
		return v
	}
	return def
}

// ClaimStateful cold-boots a stateful-class microVM (R4) from a per-workload
// rootfs WITH a tap NIC (exactly as ClaimServing) PLUS the workload's writable
// volume attached as a third drive. It mirrors ClaimServing's shape precisely
// (same NIC requirement, same harness-init threading, same coldBoot reuse) with
// the addition of volumeDiskPath/volumeMount and mmdsEnv, kept as explicit
// parameters (not folded into a struct) so the call site names every field the
// way ClaimServing's call sites do. The caller (server.StartStateful) is
// responsible for bumping the volume's generation BEFORE calling this: the
// driver has no notion of the generation ledger, only the raw file path.
// mmdsEnv (R4, D-R4.PR-7.1: MMDS-lite over boot-args) is only meaningful on a
// FRESH/COLD boot; callers on the RELIGHT path must pass nil/empty, since a
// relight resumes a memory snapshot and never re-reads boot-args.
func (d *Driver) ClaimStateful(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec, handlerDiskPath string, handlerZipBytes int64, volumeDiskPath, volumeMount string, mmdsEnv map[string]string) (substrate.Handle, error) {
	if nic.HostDevName == "" {
		return substrate.Handle{}, fmt.Errorf("driver: ClaimStateful requires a host tap device")
	}
	if volumeDiskPath == "" {
		return substrate.Handle{}, fmt.Errorf("driver: ClaimStateful requires a volume disk path")
	}
	vcpus = orDefault(vcpus, d.cfg.VCPUs)
	memMib = orDefault(memMib, d.cfg.MemMib)
	nicCopy := nic
	return d.coldBoot(ctx, newID("state"), coldBootSpec{
		rootfsPath:      rootfsPath,
		vcpus:           vcpus,
		memMib:          memMib,
		nic:             &nicCopy,
		harnessInit:     harnessInit,
		handlerDiskPath: handlerDiskPath,
		handlerZipBytes: handlerZipBytes,
		volumeDiskPath:  volumeDiskPath,
		volumeMount:     volumeMount,
		mmdsEnv:         mmdsEnv,
	})
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
		Vendor:    d.cfg.Vendor,
		Template:  d.cfg.Template,
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
	if vmis, refVendor := vendorMismatch(ref.Vendor, d.cfg.Vendor); vmis {
		return substrate.Handle{}, fmt.Errorf("driver: vendor mismatch on restore: ref %q != node %q (snapshots non-portable across CPU vendor)", refVendor, d.cfg.Vendor)
	}
	if tmis, refTemplate := templateMismatch(ref.Template, d.cfg.Template); tmis {
		return substrate.Handle{}, fmt.Errorf("driver: cpu_sku mismatch on restore: template %q != node %q (snapshots non-portable across CPU template)", refTemplate, d.cfg.Template)
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
		Vendor:    d.cfg.Vendor,
		Template:  d.cfg.Template,
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
		Vendor:    d.cfg.Vendor,
		Template:  d.cfg.Template,
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
		Vendor:    d.cfg.Vendor,
		Template:  d.cfg.Template,
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

// ---- stateful snapshot mechanics (R4) ---------------------------------------
//
// These mirror the R3 serving bank/relight/evict mechanics exactly, under the
// stateful/ prefix instead of serving/, with ONE addition: a "gen" sidecar
// stamps the volume's generation AT BANK TIME (not a pinned IP; a stateful
// bundle's NIC still travels with the snapshot the same way a serving one
// does, but IP re-pinning is not part of the R4 v1 contract since a stateful
// VM's endpoint is control-plane-addressed, not guest-baked-critical the way a
// serving relight's eth0 IP is). The volume file itself is NEVER copied,
// hashed, or touched by any of these: the generation ledger (owned by the
// volume package, not here) is the entire pairing mechanism, and the memory
// snapshot only pre-pays cache warmth (ADR embervm/001's state split).

// StatefulDir is the parent directory holding all banked STATEFUL snapshot
// bundles, under the stateful/ prefix of the snapshot root (a sibling of
// bases/, sessions/, and serving/). The daemon rescans exactly this dir on
// start via ScanStatefulBundles. Callers own its 0700 permission and
// daemon-ownership.
func (d *Driver) StatefulDir() string { return filepath.Join(d.warmthRoot(), "stateful") }

// statefulDir is the bundle directory for one banked stateful snapshot, keyed
// by an opaque snapshot_ref.
func (d *Driver) statefulDir(ref string) string { return filepath.Join(d.StatefulDir(), ref) }

func (d *Driver) statefulSnapfile(ref string) string {
	return filepath.Join(d.statefulDir(ref), "snapfile")
}

func (d *Driver) statefulMemfile(ref string) string {
	return filepath.Join(d.statefulDir(ref), "memfile")
}

// statefulGenfile is the sidecar recording the volume generation a stateful
// bundle was banked at (the pair key StartStateful(RELIGHT) checks against the
// volume's CURRENT generation). It must survive a daemon restart exactly like
// the serving pinned-IP sidecar, so ScanStatefulBundles reads it back.
func (d *Driver) statefulGenfile(ref string) string {
	return filepath.Join(d.statefulDir(ref), "gen")
}

// statefulMetafile is the sidecar recording the tap IP a stateful bundle was
// banked with, the exact analogue of the serving pinned-IP sidecar. A relight
// resumes the guest from its memory snapshot, which has this IP baked into eth0,
// so the relight MUST re-acquire the SAME tap IP or the resumed guest answers on
// an address the fresh tap does not have and is unreachable ("guest not ready over
// tap"). Published alongside the gen sidecar before the completeness snapfile, so
// StatefulPinnedIP reads it back after a restart.
func (d *Driver) statefulMetafile(ref string) string {
	return filepath.Join(d.statefulDir(ref), "pinned-ip")
}

// StatefulPinnedIP reads the tap IP a stateful bundle was banked with, for a
// relight to re-acquire before restoring. "" when the sidecar is absent (a bundle
// banked before pinning, or an unreadable sidecar), in which case the caller falls
// back to a fresh tap.
func (d *Driver) StatefulPinnedIP(snapshotRef string) string {
	b, err := os.ReadFile(d.statefulMetafile(snapshotRef))
	if err != nil {
		return ""
	}
	return string(b)
}

// writeStatefulPinnedIP publishes the pinned-IP sidecar, a no-op for an empty IP
// (which leaves the relight to fall back to a fresh tap, so an older bundle with no
// sidecar still relights, just without the IP-match guarantee).
func (d *Driver) writeStatefulPinnedIP(snapshotRef, pinnedIP string) error {
	if pinnedIP == "" {
		return nil
	}
	if err := os.WriteFile(d.statefulMetafile(snapshotRef), []byte(pinnedIP), 0o600); err != nil {
		return fmt.Errorf("driver: write stateful pinned-ip sidecar: %w", err)
	}
	return nil
}

// SnapshotStateful pauses a live stateful VM and writes a self-contained
// stateful bundle (memfile + snapfile) under stateful/<ref>, plus the generation
// and pinned-IP sidecars. It does NOT resume: the caller Releases the VM
// immediately after (StopStateful BANK destroys). It mirrors SnapshotServing
// (which also pins the tap IP) and stamps NOTHING about the volume itself: the
// volume file is not opened, copied, or hashed here. On any failure after the
// handle is confirmed the VM is torn down (a bank is destructive), so a failed
// bank never leaves a live/paused handle behind.
func (d *Driver) SnapshotStateful(ctx context.Context, h substrate.Handle, snapshotRef string, generation uint64, pinnedIP string) (substrate.SnapshotRef, error) {
	if snapshotRef == "" {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: SnapshotStateful requires a snapshot_ref")
	}
	inst := d.get(h.ID)
	if inst == nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: snapshot-stateful of unknown handle %q", h.ID)
	}
	banked := false
	defer func() {
		if !banked {
			_ = d.Release(ctx, h)
		}
	}()
	if err := os.MkdirAll(d.statefulDir(snapshotRef), 0o700); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: mkdir stateful bundle: %w", err)
	}
	snapPath := d.statefulSnapfile(snapshotRef)
	memPath := d.statefulMemfile(snapshotRef)
	snapTmp := snapPath + ".tmp"
	memTmp := memPath + ".tmp"

	if err := inst.client.Pause(ctx); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: pause stateful: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapTmp, MemFilePath: memTmp}); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: create stateful snapshot: %w", err)
	}
	// Publish memfile before snapfile (a restore reads the snapfile to locate the
	// memfile), then the generation sidecar, then the snapfile LAST so a rescan
	// that finds a snapfile always finds a complete bundle (mirrors serving's
	// publish-last-so-a-half-written-bundle-is-skipped discipline).
	if err := os.Rename(memTmp, memPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish stateful memfile: %w", err)
	}
	if err := os.WriteFile(d.statefulGenfile(snapshotRef), []byte(strconv.FormatUint(generation, 10)), 0o600); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: write stateful generation sidecar: %w", err)
	}
	if err := d.writeStatefulPinnedIP(snapshotRef, pinnedIP); err != nil {
		return substrate.SnapshotRef{}, err
	}
	if err := os.Rename(snapTmp, snapPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish stateful snapfile: %w", err)
	}
	banked = true
	return substrate.SnapshotRef{
		ID:        snapshotRef,
		Node:      d.cfg.Node,
		Arch:      d.cfg.Arch,
		Vendor:    d.cfg.Vendor,
		Template:  d.cfg.Template,
		SizeBytes: bundleSize(snapPath, memPath),
	}, nil
}

// ---- interruptible-bank checkpoint/resolve (ADR embervm/008) ----------------
//
// The two-phase, abortable bank splits SnapshotStateful's pause-snapshot-destroy
// into a CHECKPOINT (pause + snapshot to a temp OUTSIDE stateful/, VM left paused)
// and a RESOLVE that either COMMITs (publish the temp as the bundle, destroy) or
// ABORTs (delete the temp, resume). The atomic SnapshotStateful above is UNCHANGED
// and remains the default; these are used only for a workload that opted in.

// CheckpointsDir holds in-flight interruptible-bank checkpoint temps, a SIBLING of
// stateful/ (not a child), so ScanStatefulBundles (which globs stateful/*/snapfile)
// can NEVER mistake a temp for a committed bundle (ADR embervm/008, guarantee 1).
// GCStatefulCheckpoints sweeps it on start. Callers own its 0700 daemon-ownership.
func (d *Driver) CheckpointsDir() string {
	return filepath.Join(d.warmthRoot(), "stateful-checkpoints")
}

func (d *Driver) checkpointTmpDir(token string) string {
	return filepath.Join(d.CheckpointsDir(), token)
}

// CheckpointStateful is phase one of the interruptible bank (ADR embervm/008): it
// PAUSES a live stateful VM and writes its snapshot to a temp dir OUTSIDE the
// stateful/ bundle dir (invisible to ScanStatefulBundles), leaving the VM PAUSED
// and resumable. It publishes NO bundle and does NOT Release. The returned opaque
// token is passed to ResolveStatefulCommit (publish the temp as the bundle,
// destroy) or ResolveStatefulAbort (delete the temp, resume). On a pause/snapshot
// failure the VM is resumed best-effort and the temp removed, so a FAILED
// checkpoint leaves the VM live rather than stranded paused.
func (d *Driver) CheckpointStateful(ctx context.Context, h substrate.Handle, snapshotRef string, generation uint64, pinnedIP string) (string, error) {
	if snapshotRef == "" {
		return "", fmt.Errorf("driver: CheckpointStateful requires a snapshot_ref")
	}
	inst := d.get(h.ID)
	if inst == nil {
		return "", fmt.Errorf("driver: checkpoint-stateful of unknown handle %q", h.ID)
	}
	token := newID("ckpt")
	tmpDir := d.checkpointTmpDir(token)
	if err := os.MkdirAll(tmpDir, 0o700); err != nil {
		return "", fmt.Errorf("driver: mkdir checkpoint temp: %w", err)
	}
	snapPath := filepath.Join(tmpDir, "snapfile")
	memPath := filepath.Join(tmpDir, "memfile")
	if err := inst.client.Pause(ctx); err != nil {
		_ = os.RemoveAll(tmpDir)
		return "", fmt.Errorf("driver: pause stateful for checkpoint: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapPath, MemFilePath: memPath}); err != nil {
		// The VM is paused; resume it so a checkpoint FAILURE leaves it live
		// (mirrors serving's resume-on-snapshot-failure), then discard the temp.
		_ = inst.client.Resume(ctx)
		_ = os.RemoveAll(tmpDir)
		return "", fmt.Errorf("driver: create checkpoint snapshot: %w", err)
	}
	d.mu.Lock()
	d.checkpoints[token] = &statefulCheckpoint{handle: h, snapshotRef: snapshotRef, generation: generation, tmpDir: tmpDir, pinnedIP: pinnedIP}
	d.mu.Unlock()
	return token, nil
}

// takeCheckpoint atomically fetches and REMOVES a checkpoint by token. Removing on
// lookup is the driver's half of single-resolve (ADR embervm/008): a second
// resolve of the same token finds nothing and errors.
func (d *Driver) takeCheckpoint(token string) (*statefulCheckpoint, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	cp, ok := d.checkpoints[token]
	if ok {
		delete(d.checkpoints, token)
	}
	return cp, ok
}

// ResolveStatefulCommit is phase two (COMMIT) of the interruptible bank: it
// publishes the checkpoint's temp snapshot as the workload's bundle (memfile, the
// generation sidecar, then the snapfile LAST per the completeness discipline),
// Releases (destroys) the paused VM, and returns the bundle ref. It consumes the
// token (single-resolve). A commit is destructive: on any publish failure the
// paused VM is torn down and any half-published bundle dir removed, so a failed
// commit never leaves a paused handle or a partial bundle behind.
func (d *Driver) ResolveStatefulCommit(ctx context.Context, token string) (substrate.SnapshotRef, error) {
	cp, ok := d.takeCheckpoint(token)
	if !ok {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: ResolveStatefulCommit of unknown checkpoint token %q", token)
	}
	ref := cp.snapshotRef
	committed := false
	defer func() {
		if !committed {
			_ = d.Release(ctx, cp.handle)
			_ = os.RemoveAll(cp.tmpDir)
			_ = d.RemoveStatefulBundle(ref)
		}
	}()
	if err := os.MkdirAll(d.statefulDir(ref), 0o700); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: mkdir stateful bundle: %w", err)
	}
	snapPath := d.statefulSnapfile(ref)
	memPath := d.statefulMemfile(ref)
	// Publish memfile first, then the generation sidecar, then the snapfile LAST,
	// so a crash mid-publish leaves NO snapfile and the rescan skips the dir
	// (identical to SnapshotStateful's publish-last discipline).
	if err := os.Rename(filepath.Join(cp.tmpDir, "memfile"), memPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish stateful memfile: %w", err)
	}
	if err := os.WriteFile(d.statefulGenfile(ref), []byte(strconv.FormatUint(cp.generation, 10)), 0o600); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: write stateful generation sidecar: %w", err)
	}
	if err := d.writeStatefulPinnedIP(ref, cp.pinnedIP); err != nil {
		return substrate.SnapshotRef{}, err
	}
	if err := os.Rename(filepath.Join(cp.tmpDir, "snapfile"), snapPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish stateful snapfile: %w", err)
	}
	committed = true
	_ = os.RemoveAll(cp.tmpDir)
	// The bundle is published; tear the now-banked VM down best-effort (a Release
	// error here does not invalidate the bundle, mirroring the reap discipline).
	_ = d.Release(ctx, cp.handle)
	return substrate.SnapshotRef{
		ID:        ref,
		Node:      d.cfg.Node,
		Arch:      d.cfg.Arch,
		Vendor:    d.cfg.Vendor,
		Template:  d.cfg.Template,
		SizeBytes: bundleSize(snapPath, memPath),
	}, nil
}

// ResolveStatefulAbort is phase two (ABORT) of the interruptible bank: it DELETES
// the checkpoint's temp snapshot and RESUMES the paused VM, returning it to
// serving on the SAME process image (genuinely hot, no relight). It consumes the
// token (single-resolve). The generation bump the ADR requires BEFORE this resume
// is the CALLER's (the server bumps the volume ledger before invoking abort), so
// the on-disk order is bump, delete temp, resume (guarantees 2 and 3): a temp that
// survives a crash then implies the resume was never issued, and any post-resume
// write is witnessed by the bumped generation. If Resume fails the VM is torn down
// (the dead-handle discipline) and the error surfaced so the caller falls back to
// a committed-destroy on the next cycle rather than wedging on a stranded VM.
func (d *Driver) ResolveStatefulAbort(ctx context.Context, token string) error {
	cp, ok := d.takeCheckpoint(token)
	if !ok {
		return fmt.Errorf("driver: ResolveStatefulAbort of unknown checkpoint token %q", token)
	}
	// Delete the temp BEFORE resuming (guarantee 3): a surviving temp after a crash
	// then implies the resume was never issued, so the volume is still
	// snapshot-consistent.
	_ = os.RemoveAll(cp.tmpDir)
	inst := d.get(cp.handle.ID)
	if inst == nil {
		return fmt.Errorf("driver: ResolveStatefulAbort: paused instance %q for token %q is gone", cp.handle.ID, token)
	}
	if err := inst.client.Resume(ctx); err != nil {
		_ = d.Release(ctx, cp.handle)
		return fmt.Errorf("driver: resume on abort: %w", err)
	}
	return nil
}

// GCStatefulCheckpoints removes every orphaned interruptible-bank checkpoint temp
// on startup (ADR embervm/008): a noded restart kills every paused checkpoint VM,
// so its temp can never be resolved and is swept both for correctness (guarantee
// 1's rescan-invisibility is only meaningful if orphans do not accumulate) and as
// data-at-rest hygiene (a temp holds guest memory, possibly a first-boot secret).
// Returns the count removed. Idempotent; an absent dir is a no-op.
func (d *Driver) GCStatefulCheckpoints() int {
	entries, err := os.ReadDir(d.CheckpointsDir())
	if err != nil {
		return 0
	}
	removed := 0
	for _, e := range entries {
		if err := os.RemoveAll(filepath.Join(d.CheckpointsDir(), e.Name())); err == nil {
			removed++
		}
	}
	return removed
}

// RestoreStateful launches a fresh Firecracker process and loads a banked
// STATEFUL bundle (stateful/<snapshotRef>), resuming it so the guest continues
// exactly where it was banked, WITH the NIC it captured at bank time. It
// mirrors RestoreServing. volumeDiskPath is accepted so the call site's intent
// is explicit (a stateful relight always re-attaches the SAME backing volume
// file, never a copy), but the memory snapshot restore path itself does not
// touch drives: the caller's earlier ClaimStateful-vs-restore choice is what
// actually attaches a drive. In v1 a relight resumes the memory snapshot; the
// volume was never detached from the VM's device model in the snapshot (it was
// captured with the VM), so no separate re-attach step exists here. The
// parameter is kept for interface symmetry with ClaimStateful and so a future
// revision that needs to re-validate the path (e.g. assert it has not moved)
// has an obvious place to do it.
func (d *Driver) RestoreStateful(ctx context.Context, snapshotRef, _ string) (substrate.Handle, error) {
	if snapshotRef == "" {
		return substrate.Handle{}, fmt.Errorf("driver: RestoreStateful requires a snapshot_ref")
	}
	snapPath := d.statefulSnapfile(snapshotRef)
	if _, err := os.Stat(snapPath); err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: stateful bundle missing for %q: %w", snapshotRef, err)
	}
	threadID := newID("state")
	return d.loadInto(ctx, threadID, snapPath, d.statefulMemfile(snapshotRef), "restore.sock")
}

// RemoveStatefulBundle deletes a banked stateful snapshot's on-disk bundle
// (stateful/<snapshotRef>), including the generation sidecar. It is the
// stateful EvictSnapshot-equivalent mechanic, called both when a fresh bank
// evicts the prior bundle for a workload (at most one banked bundle per
// workload, D-R4) and when a generation-mismatch relight discards a stale
// bundle. Idempotent: a missing bundle is not an error. It NEVER touches the
// volume file (a separate dir under VolumeRoot, not under this bundle dir).
func (d *Driver) RemoveStatefulBundle(snapshotRef string) error {
	if snapshotRef == "" {
		return fmt.Errorf("driver: RemoveStatefulBundle requires a snapshot_ref")
	}
	if err := os.RemoveAll(d.statefulDir(snapshotRef)); err != nil {
		return fmt.Errorf("driver: remove stateful bundle: %w", err)
	}
	return nil
}

// ScanStatefulBundles globs stateful/*/snapfile on startup and returns each
// discovered bundle with its stamped generation (from the gen sidecar), so a
// restarted daemon reports what banked stateful warmth survives and the
// control plane can recompute pair validity (bundle generation vs the volume's
// CURRENT generation, read separately via the volume package) purely from node
// truth. A bundle dir without a snapfile is half-written or mid-evict and is
// skipped; a snapfile whose gen sidecar is missing or malformed is reported
// with generation 0 AND is effectively unusable for a relight (any real volume
// will have advanced past 0), which is the safe direction: an unreadable
// generation must never be treated as a false match.
func (d *Driver) ScanStatefulBundles() []substrate.StatefulBundleInfo {
	root := d.StatefulDir()
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil // no stateful dir yet (fresh node): nothing to rescan
	}
	out := make([]substrate.StatefulBundleInfo, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		ref := e.Name()
		snapPath := d.statefulSnapfile(ref)
		fi, err := os.Stat(snapPath)
		if err != nil {
			continue // half-written or evicting: skip
		}
		size := fi.Size()
		createdMs := fi.ModTime().UnixMilli()
		if mfi, err := os.Stat(d.statefulMemfile(ref)); err == nil {
			size += mfi.Size()
		}
		var gen uint64
		if raw, err := os.ReadFile(d.statefulGenfile(ref)); err == nil {
			if n, perr := strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 64); perr == nil {
				gen = n
			}
		}
		out = append(out, substrate.StatefulBundleInfo{
			SnapshotRef:     ref,
			Generation:      gen,
			SizeBytes:       size,
			CreatedAtUnixMs: createdMs,
		})
	}
	return out
}

// ---- Group networks (R5) ---------------------------------------------------
//
// A composite group's per-instance bridge lives in noded's pod netns and dies
// with the pod, so the DURABLE truth is a small on-disk record. These methods own
// that record under group_networks/<group_instance_id>/config.json (a sibling of
// bases/, sessions/, serving/, and stateful/ under the snapshot root). The daemon
// writes it on CreateGroupNetwork, removes it on DeleteGroupNetwork, and rescans
// it on start (ScanGroupNetworks) to re-seed NodeStatus.group_networks. The bridge
// itself is NOT persisted (it is netns state); the record is what lets the control
// plane re-issue an idempotent CreateGroupNetwork to rebuild the bridge.

// GroupNetworksDir is the parent directory holding all on-disk group-network
// records, under the group_networks/ prefix of the snapshot root. The daemon
// rescans exactly this dir on start via ScanGroupNetworks.
func (d *Driver) GroupNetworksDir() string {
	return filepath.Join(d.warmthRoot(), "group_networks")
}

// groupNetworkRecordPath is the config.json path for one group-network record,
// keyed by the opaque group_instance_id.
func (d *Driver) groupNetworkRecordPath(groupInstanceID string) string {
	return filepath.Join(d.GroupNetworksDir(), groupInstanceID, "config.json")
}

// WriteGroupNetworkRecord persists a group-network record atomically (write a
// temp file, then rename), so a rescan never reads a half-written config.json. It
// is idempotent: re-writing the same record for the same group_instance_id
// overwrites in place (CreateGroupNetwork is idempotent, so a re-issue re-writes
// the identical record). The dir is 0700 (daemon-owned).
func (d *Driver) WriteGroupNetworkRecord(rec substrate.GroupNetworkRecord) error {
	if rec.GroupInstanceID == "" {
		return fmt.Errorf("driver: WriteGroupNetworkRecord requires a group_instance_id")
	}
	dir := filepath.Dir(d.groupNetworkRecordPath(rec.GroupInstanceID))
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("driver: mkdir group network record dir: %w", err)
	}
	data, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("driver: marshal group network record: %w", err)
	}
	path := d.groupNetworkRecordPath(rec.GroupInstanceID)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("driver: write group network record: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("driver: publish group network record: %w", err)
	}
	return nil
}

// RemoveGroupNetworkRecord deletes a group-network record dir from disk
// (idempotent: a missing record is not an error). Called on DeleteGroupNetwork.
func (d *Driver) RemoveGroupNetworkRecord(groupInstanceID string) error {
	if groupInstanceID == "" {
		return fmt.Errorf("driver: RemoveGroupNetworkRecord requires a group_instance_id")
	}
	dir := filepath.Dir(d.groupNetworkRecordPath(groupInstanceID))
	if err := os.RemoveAll(dir); err != nil {
		return fmt.Errorf("driver: remove group network record: %w", err)
	}
	return nil
}

// ScanGroupNetworks globs group_networks/*/config.json on startup and returns
// each valid record, so a restarted daemon re-seeds its group-network inventory
// and reports it in NodeStatus.group_networks. A record dir without a readable,
// parseable config.json is skipped (half-written or corrupt); a fresh node with
// no group_networks dir yields nil. The bridges these records name no longer
// exist (they died with the prior pod), so the control plane re-issues
// CreateGroupNetwork to rebuild them; this rescan is purely the durable-truth
// re-seed.
func (d *Driver) ScanGroupNetworks() []substrate.GroupNetworkRecord {
	root := d.GroupNetworksDir()
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil // no group_networks dir yet (fresh node): nothing to rescan
	}
	out := make([]substrate.GroupNetworkRecord, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		raw, err := os.ReadFile(d.groupNetworkRecordPath(e.Name()))
		if err != nil {
			continue // no config.json yet (mid-write) or unreadable: skip
		}
		var rec substrate.GroupNetworkRecord
		if err := json.Unmarshal(raw, &rec); err != nil {
			continue // corrupt record: skip
		}
		if rec.GroupInstanceID == "" {
			// A record whose dir name is the id but whose body omitted it: recover
			// the id from the dir name so the rescan is robust to an older writer.
			rec.GroupInstanceID = e.Name()
		}
		out = append(out, rec)
	}
	return out
}

// ---- Group member snapshot mechanics (R5) -----------------------------------
//
// A composite-group member VM banks to a self-contained bundle exactly like a
// session/serving/stateful VM (memfile + snapfile, no archive backing file), but
// the bundle layout is group/<set_id>/<member_name>/ so the control plane can
// address a whole RELIGHTABLE SET by set_id and the daemon reports member bundles
// GROUPED BY set dir (ScanGroupBundleSets). The server writes member.json beside
// each completed bundle with the exact pinned IP and guest health port held at
// bank time. The pinned world (tap name + MAC + IP) MUST be recreated identically
// BEFORE the resume, because the guest's eth0 keeps the address baked at fresh
// boot and a snapshot resume never re-runs kernel init.

// GroupSetsDir is the parent directory holding all banked GROUP member bundles,
// under the group/ prefix of the snapshot root (a sibling of bases/, sessions/,
// serving/, and stateful/). The daemon rescans exactly this dir on start via
// ScanGroupBundleSets. Callers own its 0700 permission and daemon-ownership.
func (d *Driver) GroupSetsDir() string { return filepath.Join(d.warmthRoot(), "group") }

// groupMemberDir is the bundle directory for one banked member snapshot, keyed by
// the opaque set_id and the member_name: group/<set_id>/<member_name>/.
func (d *Driver) groupMemberDir(setID, memberName string) string {
	return filepath.Join(d.GroupSetsDir(), setID, memberName)
}

func (d *Driver) groupMemberSnapfile(setID, memberName string) string {
	return filepath.Join(d.groupMemberDir(setID, memberName), "snapfile")
}

func (d *Driver) groupMemberMemfile(setID, memberName string) string {
	return filepath.Join(d.groupMemberDir(setID, memberName), "memfile")
}

// ClaimGroupMember cold-boots a composite-group member microVM (R5) from a
// per-member rootfs WITH the given tap NIC on the group bridge (exactly as
// ClaimServing) PLUS the member's first-boot env delivered via the MMDS-lite
// boot-args seam (D-R4.PR-7.1), and NO handler artifact and NO writable volume (a
// group member is a plain NIC guest). It mirrors ClaimServing's shape precisely
// (same NIC requirement, same harness-init threading, same coldBoot reuse) with the
// addition of mmdsEnv. env is only meaningful on a FRESH cold boot; a RELIGHT
// resumes a memory snapshot via RestoreGroupMember and never re-reads boot-args, so
// the resumed member keeps its BIRTH env.
func (d *Driver) ClaimGroupMember(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec, env map[string]string) (substrate.Handle, error) {
	if nic.HostDevName == "" {
		return substrate.Handle{}, fmt.Errorf("driver: ClaimGroupMember requires a host tap device")
	}
	vcpus = orDefault(vcpus, d.cfg.VCPUs)
	memMib = orDefault(memMib, d.cfg.MemMib)
	nicCopy := nic
	return d.coldBoot(ctx, newID("grpm"), coldBootSpec{
		rootfsPath:  rootfsPath,
		vcpus:       vcpus,
		memMib:      memMib,
		nic:         &nicCopy,
		harnessInit: harnessInit,
		mmdsEnv:     env,
	})
}

// SnapshotGroupMember pauses a live member VM and writes a self-contained member
// bundle (memfile + snapfile) under group/<set_id>/<member_name>/. It does NOT
// resume: the caller Releases the VM immediately after (StopGroupMember BANK
// destroys). It mirrors SnapshotSession exactly; the server publishes member.json
// after this returns because the live registry owns the pinned IP and port. On any
// failure after the handle is confirmed the VM is torn down (a bank is destructive),
// so a failed bank never leaves a live/paused handle behind for the server to
// misreport as capacity.
func (d *Driver) SnapshotGroupMember(ctx context.Context, h substrate.Handle, setID, memberName string) (substrate.SnapshotRef, error) {
	if setID == "" || memberName == "" {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: SnapshotGroupMember requires a set_id and member_name")
	}
	inst := d.get(h.ID)
	if inst == nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: snapshot-group-member of unknown handle %q", h.ID)
	}
	banked := false
	defer func() {
		if !banked {
			_ = d.Release(ctx, h)
		}
	}()
	if err := os.MkdirAll(d.groupMemberDir(setID, memberName), 0o700); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: mkdir group member bundle: %w", err)
	}
	snapPath := d.groupMemberSnapfile(setID, memberName)
	memPath := d.groupMemberMemfile(setID, memberName)
	snapTmp := snapPath + ".tmp"
	memTmp := memPath + ".tmp"

	if err := inst.client.Pause(ctx); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: pause group member: %w", err)
	}
	if err := inst.client.CreateSnapshot(ctx, fcclient.SnapshotCreate{SnapshotPath: snapTmp, MemFilePath: memTmp}); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: create group member snapshot: %w", err)
	}
	// Publish memfile before snapfile (a restore reads the snapfile to locate the
	// memfile), then the snapfile LAST so a rescan that finds a snapfile always
	// finds a complete bundle (the same publish-last discipline every other class
	// uses so a half-written bundle is skipped).
	if err := os.Rename(memTmp, memPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish group member memfile: %w", err)
	}
	if err := os.Rename(snapTmp, snapPath); err != nil {
		return substrate.SnapshotRef{}, fmt.Errorf("driver: publish group member snapfile: %w", err)
	}
	banked = true
	return substrate.SnapshotRef{
		ID:        filepath.Join("group", setID, memberName),
		Node:      d.cfg.Node,
		Arch:      d.cfg.Arch,
		Vendor:    d.cfg.Vendor,
		Template:  d.cfg.Template,
		SizeBytes: bundleSize(snapPath, memPath),
	}, nil
}

// RestoreGroupMember launches a fresh Firecracker process and loads a banked member
// bundle (group/<set_id>/<member_name>/), resuming it so the guest continues exactly
// where it was banked, WITH the NIC it captured at bank time. It mirrors
// RestoreServing. The caller has already recreated the host tap (same name + MAC)
// and configured the pinned IP on the group bridge BEFORE this runs (the node-local
// activator reads that exact IP from member.json), so the resumed guest's baked
// eth0 still routes. A missing bundle is an error the caller
// maps to FAILED_PRECONDITION (the bundle is NEVER deleted on a failed restore: a
// lost member must surface loudly).
func (d *Driver) RestoreGroupMember(ctx context.Context, setID, memberName string) (substrate.Handle, error) {
	if setID == "" || memberName == "" {
		return substrate.Handle{}, fmt.Errorf("driver: RestoreGroupMember requires a set_id and member_name")
	}
	snapPath := d.groupMemberSnapfile(setID, memberName)
	if _, err := os.Stat(snapPath); err != nil {
		return substrate.Handle{}, fmt.Errorf("driver: group member bundle missing for group/%s/%s: %w", setID, memberName, err)
	}
	threadID := newID("grpm")
	return d.loadInto(ctx, threadID, snapPath, d.groupMemberMemfile(setID, memberName), "restore.sock")
}

// RemoveGroupMemberBundle deletes a banked member snapshot's on-disk bundle
// (group/<set_id>/<member_name>/). It is the member EvictSnapshot-equivalent
// mechanic. Idempotent: a missing bundle is not an error.
func (d *Driver) RemoveGroupMemberBundle(setID, memberName string) error {
	if setID == "" || memberName == "" {
		return fmt.Errorf("driver: RemoveGroupMemberBundle requires a set_id and member_name")
	}
	if err := os.RemoveAll(d.groupMemberDir(setID, memberName)); err != nil {
		return fmt.Errorf("driver: remove group member bundle: %w", err)
	}
	return nil
}

// ScanGroupBundleSets globs group/*/*/snapfile on startup and returns each set dir
// with the per-member bundles found under it, so a restarted daemon reports what
// banked group warmth survives GROUPED BY set. The daemon makes NO completeness
// judgment (whether a set has every member it needs to relight is the control
// plane's to decide); it reports refs grouped by the set dir it wrote them under. A
// member dir without a snapfile is half-written or mid-evict and is skipped; a set
// dir with no complete member bundle is omitted entirely.
func (d *Driver) ScanGroupBundleSets() []substrate.GroupBundleSetInfo {
	root := d.GroupSetsDir()
	setEntries, err := os.ReadDir(root)
	if err != nil {
		return nil // no group dir yet (fresh node): nothing to rescan
	}
	out := make([]substrate.GroupBundleSetInfo, 0, len(setEntries))
	for _, se := range setEntries {
		if !se.IsDir() {
			continue
		}
		setID := se.Name()
		memberEntries, merr := os.ReadDir(filepath.Join(root, setID))
		if merr != nil {
			continue
		}
		members := make([]substrate.GroupBundleMemberInfo, 0, len(memberEntries))
		var createdMs int64
		for _, me := range memberEntries {
			if !me.IsDir() {
				continue
			}
			memberName := me.Name()
			snapPath := d.groupMemberSnapfile(setID, memberName)
			fi, serr := os.Stat(snapPath)
			if serr != nil {
				continue // half-written or evicting: skip
			}
			if ms := fi.ModTime().UnixMilli(); ms > createdMs {
				createdMs = ms
			}
			size := fi.Size()
			if mfi, err := os.Stat(d.groupMemberMemfile(setID, memberName)); err == nil {
				size += mfi.Size()
			}
			var meta substrate.GroupBundleMemberMetadata
			if raw, err := os.ReadFile(filepath.Join(d.groupMemberDir(setID, memberName), substrate.GroupBundleMemberMetadataFile)); err == nil {
				var decoded substrate.GroupBundleMemberMetadata
				if json.Unmarshal(raw, &decoded) == nil {
					meta = decoded
				}
			}
			members = append(members, substrate.GroupBundleMemberInfo{
				MemberName:  memberName,
				SnapshotRef: filepath.Join("group", setID, memberName),
				SizeBytes:   size,
				PinnedIP:    meta.PinnedIP,
				Port:        meta.Port,
			})
		}
		if len(members) == 0 {
			continue // a set dir with no complete member bundle is not reported
		}
		sort.Slice(members, func(i, j int) bool { return members[i].MemberName < members[j].MemberName })
		out = append(out, substrate.GroupBundleSetInfo{
			SetID:           setID,
			Members:         members,
			CreatedAtUnixMs: createdMs,
		})
	}
	return out
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
