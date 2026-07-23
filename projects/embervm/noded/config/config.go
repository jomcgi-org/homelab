// Package config loads embervm-noded's daemon configuration from the
// environment. It is the node daemon's counterpart to fc-invoke's catalog
// loader, reshaped for the fork: there is NO workload table here. Concurrency,
// egress posture, and warm-pool sizing are the control plane's concern now and
// arrive per-call over the gRPC contract; the daemon reads only node-side
// substrate paths and a node-level backstop cap.
//
// Artifact-decoupling Phase 2: the image->rootfs identity table that USED to live
// here (EMBERVM_NODED_IMAGES) is retired. Workload identity (rootfs ref, harness
// init, sizing) is now PUSHED by the control plane over the SyncRegistry verb, so
// the daemon boots with an EMPTY registry and readiness gates on the first replay.
// The only registry-related config left here is the NVMe cache PATH the daemon
// persists the last-synced table to (never-warm-to-dead, ADR embervm/012).
package config

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Image is the node-side identity of one guest image the daemon can build a base
// for. The gRPC BuildBase request names the image by ref; the daemon resolves
// that ref to the on-disk rootfs an init container (rootfs-builder) baked, plus
// the guest's PID-1 init path. This is NOT the retired fc-invoke workload catalog
// (no concurrency/egress/warmBase/sessioned knobs) - only image identity.
type Image struct {
	// RootfsPath is the host path to this image's base rootfs ext4, booted
	// read-only (every mutable guest path is a tmpfs captured in the snapshot).
	RootfsPath string `json:"rootfsPath"`
	// HarnessInit is the in-guest PID-1 path the kernel boots into for this
	// image. Empty falls back to the daemon-global HarnessInit.
	HarnessInit string `json:"harnessInit"`
}

// Config is the fully-resolved embervm-noded daemon configuration.
type Config struct {
	// ListenAddr is the gRPC listen address. Default ":9090".
	ListenAddr string
	// HealthAddr is the plain-HTTP /healthz listen address for kubelet probes
	// (gRPC health-checking a privileged single-replica pod is more moving parts
	// than a 20-line HTTP handler). Default ":8080".
	HealthAddr string
	// Node identifies the Kubernetes node this daemon is pinned to (injected via
	// the Downward API as EMBERVM_NODED_NODE / spec.nodeName). Reported as
	// node_id in NodeStatus and stamped into snapshot node-pinning.
	Node string
	// Arch is the host CPU architecture; FC guests are arch-affine. Defaults to
	// runtime.GOARCH when EMBERVM_NODED_ARCH is unset.
	Arch string
	// CpuVendor is the CPUID vendor this node reports ("amd", "intel"), used to
	// key vendor-bound warmth (R7, standing decisions 1 and 11): Firecracker
	// snapshot restore never crosses the AMD/Intel boundary, so bases, sessions,
	// serving/stateful bundles, and group sets are keyed and validated per vendor
	// exactly like Arch is today. Detected from /proc/cpuinfo's vendor_id line
	// when EMBERVM_NODED_CPU_VENDOR is unset; override for tests and darwin
	// (where /proc/cpuinfo does not exist).
	CpuVendor string
	// CpuTemplate names the conservative fleet-wide Firecracker CPU template
	// this node boots guests with (PR-E, ADR embervm/012): the daemon stamps
	// (CpuVendor, CpuTemplate) as this node's cpu_sku into every snapshot it
	// cuts, and validates a restoring artifact's stamped cpu_sku against its
	// own on every restore path, fail-closed on a mismatch exactly like
	// CpuVendor. Defaults per-vendor via defaultCPUTemplate (a T2-family name
	// for Intel, a fixed AMD default profile name; AMD has no FC CPUID-masking
	// template today) when EMBERVM_NODED_CPU_TEMPLATE is unset and CpuVendor is
	// known; empty when CpuVendor is empty (an undetected vendor skips the sku
	// check entirely, same as an empty CpuVendor skips the vendor check).
	CpuTemplate string
	// BearerToken, when set, gates every gRPC call: the caller must present
	// "authorization: Bearer <token>" in call metadata. Empty runs the daemon
	// open and logs a startup warning (mirrors fc-invoke's fail-loud-not-silent
	// posture); a Cilium/Linkerd policy is the defence-in-depth layer on top.
	BearerToken string

	// MaxLiveVMs is the node-level backstop cap on concurrently live microVMs
	// (primed + assigning). The control plane owns real concurrency; this only
	// stops a runaway from exhausting the node. Prime returns RESOURCE_EXHAUSTED
	// at the cap. Default 8. Zero or negative means unbounded (no backstop).
	//
	// After the budget-agnostic daemon (ADR embervm/005 item 4), no capacity
	// decision may read MaxLiveVMs; it keeps only this runaway-backstop
	// meaning. The real slot ceiling for a brick size-class is derived from
	// MemBudgetMib/CpuBudgetMillicores (see server/budget.go), reported on
	// NodeStatus for the control plane to consume.
	MaxLiveVMs int

	// MemRejectFloorMib is the memory cushion (MiB) the node-side cheap-rejection
	// predicate keeps free above a workload's need before it admits a boot verb
	// (Prime/Start*): free schedulable memory must exceed need + this floor or the
	// verb is rejected RESOURCE_EXHAUSTED with reason `pressure:mem` (ADR
	// embervm/014 decision 3). It is one smallest-workload footprint by default
	// (minSlotWorkloadMib, 512) so a brick never admits a boot that would drive it
	// to the memory edge; a zero/unset value falls back to that same default in
	// the predicate (the floor is never accidentally disabled). Env
	// EMBERVM_NODED_MEM_REJECT_FLOOR_MIB. Read only by the pressure predicate; a
	// cgroup reporting unknown (unlimited) headroom fails the check open regardless.
	MemRejectFloorMib int

	// DaemonReserveMib is subtracted from the cgroup memory.max ceiling
	// before it is reported as NodeStatus.mem_budget_mib, covering the
	// daemon's own RSS so the reported budget is guest-schedulable memory,
	// not the raw pod cgroup limit. Env EMBERVM_NODED_DAEMON_RESERVE_MIB.
	// Default 512.
	DaemonReserveMib int

	// SnapshotRoot is the directory holding FC bundle + base snapshots on the
	// NVMe scratch disk. Maps to the driver's SnapshotRoot. Bases (the
	// node-SHARED rootfs snapshots under SnapshotRoot/bases) always live here,
	// even for a brick: the same base rootfs is never rebuilt per instance.
	SnapshotRoot string
	// WarmthRoot is the root for per-INSTANCE warmth: the regenerable snapshot
	// state a fresh instance rebuilds rather than shares (sessions, serving,
	// stateful bundles, group sets, per-thread bundles, checkpoints, group
	// networks). Derived in Load, never read from env. For a BRICK (a sized
	// instance, SizeClass and PodUID both set) it nests under
	// SnapshotRoot/i/<short-uid> (see instanceSegment) so two bricks co-located
	// on one node never clobber each other's warmth; for the legacy DaemonSet
	// (empty SizeClass) it equals SnapshotRoot, keeping the flat pre-brick layout
	// byte-for-byte so the DS pod repaths nothing. The per-instance segment is
	// kept DELIBERATELY SHORT (i/<12 hex>, not instances/<36-char uuid>) because
	// the firecracker unix API/restore/vsock sockets nest a per-op thread-<hex>
	// dir under it, and the full socket path must stay under the 108-byte
	// sockaddr_un SUN_LEN limit; the long layout overflowed it and every VM op on
	// a brick failed with "path must be shorter than SUN_LEN". Bases stay on
	// SnapshotRoot; VolumeRoot (durable data) and RegistryCachePath are
	// instance-agnostic and unchanged. The driver falls back to SnapshotRoot when
	// this is empty, so a Config built directly (tests) without deriving it keeps
	// the flat layout.
	WarmthRoot string
	// BinPath is the firecracker binary (baked into the image at /opt/fc).
	BinPath string
	// KernelImagePath is the guest kernel (baked at /opt/fc), shared by every VM.
	KernelImagePath string
	// KernelBootArgs are appended to the kernel command line on cold boot. Empty
	// uses the driver's default.
	KernelBootArgs string
	// HarnessInit is the daemon-global in-guest PID-1 init path, used when an
	// Image entry does not override it.
	HarnessInit string
	// CanonicalVsockDir is the fixed dir whose vsock.sock the base snapshot
	// embeds; the launcher bind-mounts each microVM's bundle over it per instance
	// so concurrent restores each get their own host-reachable socket.
	CanonicalVsockDir string
	// GuestOomScoreAdj is written to each firecracker child's oom_score_adj so a
	// guest, never the daemon, is the kernel's first OOM victim. Default 1000.
	GuestOomScoreAdj int

	// BootReadyTimeout bounds the readiness poll after a COLD boot (BuildBase).
	BootReadyTimeout time.Duration
	// RestoreReadyTimeout bounds the readiness poll after a WARM restore (Prime).
	// A restored guest is already warm; this short budget only covers WaitReady
	// retrying past the Firecracker post-restore vsock RX-queue race. Default 2s.
	RestoreReadyTimeout time.Duration
	// DrainTimeout bounds graceful shutdown: on SIGTERM the daemon publishes a
	// drain deadline (now + DrainTimeout) via NodeStatus and HOLDS the gRPC
	// surface up, serving lifecycle RPCs, until every managed (session/serving/
	// stateful/group) VM has left the registry (the control plane force-banks
	// them, R6) or this budget elapses; only then does it drain in-flight Assigns
	// and stop. The pod's terminationGracePeriodSeconds must exceed it (chart sets
	// drain + 30s). Default 110s: the 2m spot-instance preemption notice minus
	// notification latency (ADR embervm/009 resolved-question 5).
	DrainTimeout time.Duration

	// EgressSidecarAddr is the pod-local egress-proxy sidecar TCP address. The
	// task class gets no NIC and egress is disabled, so this is unused today but
	// carried so a future egress-enabled workload needs no config reshape.
	// Default "127.0.0.1:8888".
	EgressSidecarAddr string

	// ArchiveFetchTimeout bounds a single zip-lane archive HTTP GET (the R1 zip
	// lane fetches the archive from the in-cluster SeaweedFS read path on the pod
	// network). A hung filer must not stall a BuildBase indefinitely. Default 60s.
	ArchiveFetchTimeout time.Duration
	// ArchiveMaxBytes caps how many bytes a zip-lane archive fetch buffers before
	// failing, so a runaway or malicious archive_url cannot exhaust node memory or
	// the scratch disk. The bytes are opaque to noded (the guest shim unpacks);
	// this is only a size backstop. Default 512 MiB.
	ArchiveMaxBytes int64

	// Images maps a gRPC BuildBase image_ref to its node-side identity. It is now
	// ALWAYS EMPTY at load time (artifact-decoupling Phase 2 retired the
	// EMBERVM_NODED_IMAGES env parse): workload identity is PUSHED by the control
	// plane over SyncRegistry and held in the server's in-memory workload registry,
	// which the image-resolution paths consult first. The field is retained (empty)
	// so the legacy image_ref-keyed resolution fallbacks compile unchanged until a
	// later PR migrates every consumer to the pushed registry.
	Images map[string]Image

	// RegistryCachePath is where the daemon persists the last control-plane-pushed
	// workload registry (never-warm-to-dead, ADR embervm/012). On boot the daemon
	// loads it (marked STALE: serves existing warmth, admits no new work) and the
	// first live SyncRegistry clears the stale mark. Derived from the NVMe root
	// (alongside SnapshotRoot) as <nvmeRoot>/embervm-noded/registry.json when unset
	// and SnapshotRoot is set; override with EMBERVM_NODED_REGISTRY_CACHE (tests).
	// Empty disables persistence entirely (a daemon with no NVMe root).
	RegistryCachePath string

	// PodIP is noded's own routable pod IP (injected via the Downward API as
	// EMBERVM_NODED_POD_IP / status.podIP). Serving endpoints are projected as
	// PodIP:vmPort and reached through a per-VM prerouting DNAT rule to the tap
	// (D-R3.11.4), so a pod-network Envoy on ANY node can dial them. Empty disables
	// DNAT and falls back to reporting the node-internal tap IP (tests/local); the
	// daemon logs a startup warning in that case.
	PodIP string

	// PodUID is this noded pod's Kubernetes UID (injected via the Downward API as
	// EMBERVM_POD_UID / metadata.uid). It is the daemon's INSTANCE identity: the
	// control plane keys its node registry and capacity ledger by (Node, PodUID),
	// so two noded instances on one node during a surge roll never alias. Reported
	// as NodeStatus.pod_uid and advertised in the dial-home registration body.
	// Empty (an out-of-cluster run with no Downward API) collapses the control
	// plane to node-scoped keying, matching the pre-dial-home behaviour.
	PodUID string
	// SizeClass is this instance's T-shirt brick size-class label ("2gi",
	// "4gi", "8gi", "16gi"), injected by the brick Deployment via
	// EMBERVM_NODED_SIZE_CLASS. It is a pure LABEL the daemon reports as
	// NodeStatus.size_class; the control plane's BrickLedger buckets
	// per-instance headroom by it and places whole VMs onto a brick of the
	// matching class (ADR embervm/013 bricks-everywhere). EMPTY on the legacy
	// DaemonSet (which the control plane treats as the wildcard class, so
	// DS-only placement is unchanged) and on any out-of-cluster run. The
	// daemon never sizes itself from this; the pod's own resource requests are
	// what actually bound it, and the cgroup budget reader (NodeStatus fields
	// 26/27) reports the real ceiling.
	SizeClass string
	// ControlPlaneURL is the control plane's HTTP base URL the daemon dials home to
	// (EMBERVM_NODED_CONTROL_PLANE_URL, e.g. "http://embervm.embervm.svc:8080").
	// On start and on a jittered interval the daemon POSTs its identity
	// ({node, pod_uid, address, boot_id}) to <URL>/v1/nodes/register so the control
	// plane adopts it without ever listing pods. Empty disables dial-home (tests,
	// out-of-cluster); the daemon logs a startup notice and never registers.
	ControlPlaneURL string
	// ControlPlaneTokenPath is the file the daemon reads its bearer token from for
	// the dial-home POST (EMBERVM_NODED_CONTROL_PLANE_TOKEN_PATH). Default the
	// projected ServiceAccount token at
	// /var/run/secrets/kubernetes.io/serviceaccount/token; the control plane
	// TokenReviews it and checks it is the noded ServiceAccount. Read fresh per
	// request so a rotated projected token is picked up without a restart. Empty
	// (or an unreadable file) sends no Authorization header.
	ControlPlaneTokenPath string
	// RegisterInterval is how often the daemon re-advertises via dial-home (a
	// jittered re-POST), so a control-plane restart re-adopts it promptly and a
	// re-pointed pod IP propagates. Default 30s. Env EMBERVM_NODED_REGISTER_INTERVAL.
	RegisterInterval time.Duration

	// TapPrealloc is the number of serving taps EnsureNetwork pre-creates at brick
	// boot (ADR embervm/014 decision 4), left down until AllocateTap draws one:
	// pre-provisioning removes netlink create/attach work from the instance boot
	// path. Zero (the default) disables pre-provisioning; AllocateTap/ReleaseTap
	// fall back to today's create-on-demand/delete-on-release behaviour. The
	// daemon entrypoint clamps a positive value to the brick's slot ceiling
	// (server.Server.SlotCeiling): pre-creating more taps than a brick could ever
	// host wastes boot-time setup. Env EMBERVM_NODED_TAP_PREALLOC.
	TapPrealloc int

	// ServingPortBase is the base of the deterministic per-VM DNAT port space:
	// vmPort = ServingPortBase + hostOffset(tapIP). A /24 yields ports base+2..base+254,
	// clear of noded's own 8080/9090. Default 30000. NewManager rejects a base that
	// would push the top offset past 65535. Env EMBERVM_NODED_SERVING_PORT_BASE.
	ServingPortBase int
	// ServingBridge is the host bridge device serving-class VM taps attach to. One
	// per node; the daemon creates it on start. Default "embervm-serv0".
	ServingBridge string
	// ServingSubnetCIDR is the RFC1918 subnet the daemon allocates serving VM tap
	// IPs from (the bridge takes .1, VMs get .2+). Node-local routable from pods on
	// this node (Task 6). Default 172.31.0.0/24 (172.16/12 space to avoid colliding
	// with the 10.0.0.0/8 pod-CIDR range); the real non-colliding range is verified
	// against the cluster pod CIDR before the live drill. Reported as
	// NodeStatus.serving_subnet_cidr. A malformed CIDR fails Load loudly.
	ServingSubnetCIDR string
	// ServingProbeInterval is how often the daemon health-probes each live serving
	// VM over its tap. Default 5s (the Task 1 contract default).
	ServingProbeInterval time.Duration
	// ServingUnhealthyThreshold is the number of consecutive probe failures that
	// flips a serving VM healthy->false; one success flips it back. Default 3.
	ServingUnhealthyThreshold int

	// VolumeRoot is the directory holding per-workload stateful volume files
	// (R4), a sibling of SnapshotRoot's bases/sessions/serving/stateful bundle
	// dirs but deliberately a SEPARATE root: a volume is durable data that
	// outlives every VM instance and must never be swept by any bundle-GC
	// policy scoped to SnapshotRoot. Default SnapshotRoot's parent + "/volumes"
	// when unset and SnapshotRoot is set, else empty (stateful verbs disabled).
	VolumeRoot string
	// StatefulProbeInterval / StatefulUnhealthyThreshold configure the
	// TCP-connect health-probe loop for stateful VMs (R4), mirroring the
	// serving HTTP-probe knobs. Default 5s / 3.
	StatefulProbeInterval      time.Duration
	StatefulUnhealthyThreshold int

	// CompositeSupernet is the values-declared supernet the control plane carves a
	// per-group /24 out of for each composite-workload group (R5). CreateGroupNetwork
	// VALIDATES the control-plane-assigned cidr is a /24 wholly within this supernet
	// (and non-overlapping with an existing group bridge). Default 10.101.0.0/16
	// (distinct from the serving 172.31/12 space and the 10.0/8 pod CIDR is verified
	// against the cluster before the live drill). A malformed supernet fails
	// GroupManager construction loudly. Env EMBERVM_NODED_COMPOSITE_SUPERNET.
	CompositeSupernet string
	// GroupProbeInterval / GroupUnhealthyThreshold configure the TCP-connect
	// health-probe loop for live group member VMs (R5), mirroring the stateful
	// knobs. Carried now so Task 5's member lifecycle needs no config reshape;
	// unused in Task 4. Default 5s / 3.
	GroupProbeInterval      time.Duration
	GroupUnhealthyThreshold int

	// StoreEndpoint is the base URL of the S3-API object store the continuity
	// verbs (R6) move banked artifacts to and from. No in-code default: the
	// in-cluster SeaweedFS S3 gateway (anonymous, standing decision 5) is set
	// explicitly by the chart (values.yaml noded.store.endpoint), since a
	// hardcoded .svc.cluster.local default here would silently break if the
	// release name ever changes. An EMPTY endpoint DISABLES the store
	// entirely: exports are skipped, restore-on-miss is impossible, and
	// ExportArtifact/RestoreArtifact refuse FAILED_PRECONDITION, so a build
	// without a store (tests, a cluster without SeaweedFS) still runs with
	// only local durability. Env EMBERVM_NODED_STORE_ENDPOINT.
	StoreEndpoint string
	// StoreBucket is the single bucket every artifact key lives under (Fork 3).
	// Default "embervm". Env EMBERVM_NODED_STORE_BUCKET.
	StoreBucket string

	// RequireBlessing rejects any writable stateful attach (FRESH/RELIGHT/COLD)
	// carrying blessed_generation == 0 with FAILED_PRECONDITION, once the
	// control plane has started issuing blessed generations (R7, ADR
	// embervm/011, standing decision 4). Defaults false so a rollout can land
	// the control-plane side first; the chart flips this true in the SAME
	// version so a mixed CP/noded state cannot outlive the roll. Env
	// EMBERVM_NODED_REQUIRE_BLESSING.
	RequireBlessing bool
}

// Load resolves configuration from the environment, applying defaults for all
// optional fields. It errors only on values that are present but malformed.
func Load() (Config, error) {
	c := Config{
		ListenAddr:       getenvDefault("EMBERVM_NODED_LISTEN_ADDR", ":9090"),
		HealthAddr:       getenvDefault("EMBERVM_NODED_HEALTH_ADDR", ":8080"),
		Node:             os.Getenv("EMBERVM_NODED_NODE"),
		Arch:             os.Getenv("EMBERVM_NODED_ARCH"),
		CpuVendor:        os.Getenv("EMBERVM_NODED_CPU_VENDOR"),
		CpuTemplate:      os.Getenv("EMBERVM_NODED_CPU_TEMPLATE"),
		BearerToken:      os.Getenv("EMBERVM_NODED_BEARER_TOKEN"),
		MaxLiveVMs:       atoiDefault("EMBERVM_NODED_MAX_LIVE_VMS", 8),
		DaemonReserveMib: atoiDefault("EMBERVM_NODED_DAEMON_RESERVE_MIB", 512),
		// Default 512 == server.minSlotWorkloadMib (one smallest-workload
		// footprint); the two live in different packages (config cannot import
		// server), so the literal is duplicated with this note tying them together.
		MemRejectFloorMib:   atoiDefault("EMBERVM_NODED_MEM_REJECT_FLOOR_MIB", 512),
		SnapshotRoot:        os.Getenv("EMBERVM_NODED_SNAPSHOT_ROOT"),
		BinPath:             getenvDefault("EMBERVM_NODED_FIRECRACKER_BIN", "/opt/fc/firecracker"),
		KernelImagePath:     getenvDefault("EMBERVM_NODED_KERNEL_IMAGE", "/opt/fc/vmlinux.container"),
		KernelBootArgs:      os.Getenv("EMBERVM_NODED_KERNEL_BOOT_ARGS"),
		HarnessInit:         getenvDefault("EMBERVM_NODED_HARNESS_INIT", "/usr/local/bin/fc-shim-init"),
		CanonicalVsockDir:   getenvDefault("EMBERVM_NODED_CANONICAL_VSOCK_DIR", "/disks/nvme-02/embervm-noded-vsock"),
		GuestOomScoreAdj:    atoiDefault("EMBERVM_NODED_GUEST_OOM_SCORE_ADJ", 1000),
		BootReadyTimeout:    60 * time.Second,
		RestoreReadyTimeout: 2 * time.Second,
		DrainTimeout:        110 * time.Second,
		EgressSidecarAddr:   getenvDefault("EMBERVM_NODED_EGRESS_SIDECAR_ADDR", "127.0.0.1:8888"),
		ArchiveFetchTimeout: 60 * time.Second,
		ArchiveMaxBytes:     512 << 20,

		PodIP: os.Getenv("EMBERVM_NODED_POD_IP"),
		// Dial-home registration (R0 PR-2): the daemon advertises its identity to
		// the control plane instead of being discovered via EndpointSlices.
		PodUID:                os.Getenv("EMBERVM_POD_UID"),
		SizeClass:             os.Getenv("EMBERVM_NODED_SIZE_CLASS"),
		ControlPlaneURL:       os.Getenv("EMBERVM_NODED_CONTROL_PLANE_URL"),
		ControlPlaneTokenPath: getenvDefault("EMBERVM_NODED_CONTROL_PLANE_TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token"),
		RegisterInterval:      30 * time.Second,

		TapPrealloc:               atoiDefault("EMBERVM_NODED_TAP_PREALLOC", 0),
		ServingPortBase:           atoiDefault("EMBERVM_NODED_SERVING_PORT_BASE", 30000),
		ServingBridge:             getenvDefault("EMBERVM_NODED_SERVING_BRIDGE", "embervm-serv0"),
		ServingSubnetCIDR:         getenvDefault("EMBERVM_NODED_SERVING_SUBNET_CIDR", "172.31.0.0/24"),
		ServingProbeInterval:      5 * time.Second,
		ServingUnhealthyThreshold: atoiDefault("EMBERVM_NODED_SERVING_UNHEALTHY_THRESHOLD", 3),

		VolumeRoot:                 os.Getenv("EMBERVM_NODED_VOLUME_ROOT"),
		StatefulProbeInterval:      5 * time.Second,
		StatefulUnhealthyThreshold: atoiDefault("EMBERVM_NODED_STATEFUL_UNHEALTHY_THRESHOLD", 3),

		CompositeSupernet:       getenvDefault("EMBERVM_NODED_COMPOSITE_SUPERNET", "10.101.0.0/16"),
		GroupProbeInterval:      5 * time.Second,
		GroupUnhealthyThreshold: atoiDefault("EMBERVM_NODED_GROUP_UNHEALTHY_THRESHOLD", 3),

		StoreEndpoint: os.Getenv("EMBERVM_NODED_STORE_ENDPOINT"),
		StoreBucket:   getenvDefault("EMBERVM_NODED_STORE_BUCKET", "embervm"),

		RequireBlessing: boolDefault("EMBERVM_NODED_REQUIRE_BLESSING", false),
	}

	if c.Node == "" {
		c.Node = os.Getenv("NODE_NAME")
	}
	if c.Arch == "" {
		c.Arch = runtime.GOARCH
	}
	if c.CpuVendor == "" {
		c.CpuVendor = detectCPUVendor()
	}
	if c.CpuTemplate == "" {
		c.CpuTemplate = defaultCPUTemplate(c.CpuVendor)
	}
	if c.VolumeRoot == "" && c.SnapshotRoot != "" {
		// Default alongside the snapshot root but as a SIBLING directory, not
		// nested under it: volumes are durable data outliving every VM instance
		// and must stay outside any bundle-GC policy scoped to SnapshotRoot's
		// bases/sessions/serving/stateful subtree.
		c.VolumeRoot = filepath.Join(filepath.Dir(c.SnapshotRoot), filepath.Base(c.SnapshotRoot)+"-volumes")
	}

	// Per-instance warmth root (brick-capacity). A brick (SizeClass + PodUID both
	// set) nests warmth under SnapshotRoot/i/<short-uid> (instanceSegment); every
	// other case (the legacy DaemonSet with no SizeClass, or an out-of-cluster run
	// with no PodUID) keeps warmth flat at SnapshotRoot, byte-for-byte the
	// pre-brick layout. The segment is kept short so the firecracker
	// thread-<hex>/{api,restore,vsock}.sock paths nested under it stay under the
	// 108-byte sockaddr_un SUN_LEN limit. Only derived when SnapshotRoot is set (an
	// unset root disables the driver entirely, so WarmthRoot stays empty too).
	if c.SnapshotRoot != "" {
		if c.SizeClass != "" && c.PodUID != "" {
			c.WarmthRoot = filepath.Join(c.SnapshotRoot, InstanceWarmthSubdir, instanceSegment(c.PodUID))
		} else {
			c.WarmthRoot = c.SnapshotRoot
		}
	}

	if err := parseDuration("EMBERVM_NODED_BOOT_READY_TIMEOUT", &c.BootReadyTimeout); err != nil {
		return Config{}, err
	}
	if err := parseDuration("EMBERVM_NODED_RESTORE_READY_TIMEOUT", &c.RestoreReadyTimeout); err != nil {
		return Config{}, err
	}
	if err := parseDuration("EMBERVM_NODED_DRAIN_TIMEOUT", &c.DrainTimeout); err != nil {
		return Config{}, err
	}
	if err := parseDuration("EMBERVM_NODED_ARCHIVE_FETCH_TIMEOUT", &c.ArchiveFetchTimeout); err != nil {
		return Config{}, err
	}
	if err := parseDuration("EMBERVM_NODED_SERVING_PROBE_INTERVAL", &c.ServingProbeInterval); err != nil {
		return Config{}, err
	}
	if err := parseDuration("EMBERVM_NODED_STATEFUL_PROBE_INTERVAL", &c.StatefulProbeInterval); err != nil {
		return Config{}, err
	}
	if err := parseDuration("EMBERVM_NODED_GROUP_PROBE_INTERVAL", &c.GroupProbeInterval); err != nil {
		return Config{}, err
	}
	if err := parseDuration("EMBERVM_NODED_REGISTER_INTERVAL", &c.RegisterInterval); err != nil {
		return Config{}, err
	}
	if v := os.Getenv("EMBERVM_NODED_ARCHIVE_MAX_BYTES"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return Config{}, fmt.Errorf("invalid EMBERVM_NODED_ARCHIVE_MAX_BYTES %q: %w", v, err)
		}
		c.ArchiveMaxBytes = n
	}

	// Artifact-decoupling Phase 2: the workload registry is PUSHED, not parsed from
	// env. Images always starts empty; the pushed table (server.workloadRegistry)
	// is the authority the image-resolution paths consult.
	c.Images = map[string]Image{}

	// Registry cache path: explicit override, else derive alongside SnapshotRoot
	// under the NVMe root (SnapshotRoot is <nvmeRoot>/embervm-noded/snapshots, so
	// its parent is <nvmeRoot>/embervm-noded). Empty when neither is set (no NVMe
	// root: persistence disabled, the daemon simply always waits for a live sync).
	c.RegistryCachePath = os.Getenv("EMBERVM_NODED_REGISTRY_CACHE")
	if c.RegistryCachePath == "" && c.SnapshotRoot != "" {
		c.RegistryCachePath = filepath.Join(filepath.Dir(c.SnapshotRoot), "registry.json")
	}

	return c, nil
}

// PruneStaleInstanceWarmth removes per-instance (brick) warmth directories under
// SnapshotRoot/i/ that do NOT belong to this daemon's own pod UID, reclaiming the
// regenerable snapshot state left behind by evicted or rolled-out co-located
// bricks (nothing GCs dead-instance warmth today, so orphan dirs accumulate).
// It is deliberately narrow and fail-soft:
//
//   - It ONLY touches SnapshotRoot/i/<segment> entries. It never removes bases/
//     (node-shared rootfs snapshots), the VolumeRoot (durable data, a separate
//     root entirely), or the daemon's OWN live warmth segment.
//   - It is a no-op unless this instance is itself a brick (SizeClass + PodUID
//     both set): the legacy DaemonSet's warmth is flat at SnapshotRoot with no
//     i/ subtree to sweep, so there is nothing (and nothing safe) to prune.
//   - A missing i/ directory, an unreadable entry, or a failed removal is logged
//     via removeErr (if non-nil) and skipped, never fatal: warmth is regenerable,
//     so a boot must not block on a GC hiccup.
//
// It returns the list of segments it removed (for logging/tests). removeErr, when
// non-nil, is called once per entry that could not be removed.
func PruneStaleInstanceWarmth(c Config, removeErr func(segment string, err error)) []string {
	if c.SnapshotRoot == "" || c.SizeClass == "" || c.PodUID == "" {
		return nil
	}
	instancesDir := filepath.Join(c.SnapshotRoot, InstanceWarmthSubdir)
	entries, err := os.ReadDir(instancesDir)
	if err != nil {
		// A missing i/ dir (first brick on this node, or nothing warmed yet) is the
		// common case and not an error worth surfacing; any other read error is
		// reported through removeErr with an empty segment so a caller can log it.
		if !os.IsNotExist(err) && removeErr != nil {
			removeErr("", err)
		}
		return nil
	}
	ownSegment := instanceSegment(c.PodUID)
	var removed []string
	for _, e := range entries {
		if e.Name() == ownSegment {
			continue // never reap our own live warmth
		}
		if err := os.RemoveAll(filepath.Join(instancesDir, e.Name())); err != nil {
			if removeErr != nil {
				removeErr(e.Name(), err)
			}
			continue
		}
		removed = append(removed, e.Name())
	}
	return removed
}

// InstanceWarmthSubdir is the single-letter parent directory under SnapshotRoot
// that per-instance (brick) warmth roots nest inside: SnapshotRoot/i/<short-uid>.
// It is intentionally one byte ("i", not "instances") to keep the firecracker
// unix socket paths nested under it (thread-<hex>/{api,restore,vsock}.sock) well
// under the 108-byte sockaddr_un SUN_LEN limit. GC scans this directory to reap
// dead-instance warmth (see PruneStaleInstanceWarmth).
const InstanceWarmthSubdir = "i"

// instanceSegmentLen is how many hex characters of the pod UID the per-instance
// warmth segment keeps. Kubernetes pod UIDs are RFC 4122 v4 UUIDs (36 chars with
// hyphens, 32 hex nibbles = 128 bits); 10 hex chars is 40 bits, collision-safe
// for the handful (single digits) of noded instances that ever co-locate on one
// node, while keeping the worst-case firecracker socket path comfortably under
// the 108-byte SUN_LEN limit (98 bytes with the longest fleet snapshot root).
// Deterministic: the same pod UID always maps to the same segment, so a restarted
// daemon reattaches to its own warmth rather than orphaning it.
const instanceSegmentLen = 10

// instanceSegment derives the short, deterministic per-instance warmth path
// segment from a pod UID. It strips the UUID hyphens (so the 12 kept characters
// are all entropy-bearing hex, not layout punctuation) and lowercases; a UID
// shorter than instanceSegmentLen is used whole. The result is filesystem-safe
// and stable across restarts of the same pod.
func instanceSegment(podUID string) string {
	s := strings.ToLower(strings.ReplaceAll(podUID, "-", ""))
	if len(s) > instanceSegmentLen {
		s = s[:instanceSegmentLen]
	}
	return s
}

// vendorUnsafe strips anything but letters/digits from an unrecognised
// vendor_id string so detectCPUVendor's best-effort fallback token is still
// filesystem- and store-key-safe.
var vendorUnsafe = regexp.MustCompile(`[^A-Za-z0-9]`)

// cpuinfoPath is the standard Linux procfs path detectCPUVendor reads.
// Overridable only via detectCPUVendorFrom (tests), never at runtime: a real
// daemon always reads the real path or EMBERVM_NODED_CPU_VENDOR wins first.
const cpuinfoPath = "/proc/cpuinfo"

// detectCPUVendor reads /proc/cpuinfo's vendor_id line and maps it to the short
// vendor token the R7 warmth keys use ("amd", "intel"). Missing /proc/cpuinfo
// (non-Linux, e.g. darwin dev machines and CI) or a missing vendor_id line
// yields "": the daemon still starts, but every restore/base-key vendor check
// treats an empty node vendor as never matching a stamped ref, which is
// deliberately fail-closed rather than a silent guess. EMBERVM_NODED_CPU_VENDOR
// overrides this entirely (checked by the caller before detectCPUVendor is ever
// invoked), which is how non-Linux dev loops set a vendor without a real
// /proc/cpuinfo; tests instead call detectCPUVendorFrom with a fixture path.
func detectCPUVendor() string {
	return detectCPUVendorFrom(cpuinfoPath)
}

// detectCPUVendorFrom is detectCPUVendor's testable core: it reads path's
// vendor_id line (the /proc/cpuinfo format) and maps it to the short vendor
// token. GenuineIntel -> "intel", AuthenticAMD -> "amd", anything else
// recognisable -> a lowercase best-effort token derived from the raw vendor_id
// with everything but letters/digits stripped. A missing file or a missing
// vendor_id line yields "".
func detectCPUVendorFrom(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "vendor_id") {
			continue
		}
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		raw := strings.TrimSpace(parts[1])
		switch raw {
		case "GenuineIntel":
			return "intel"
		case "AuthenticAMD":
			return "amd"
		default:
			return strings.ToLower(vendorUnsafe.ReplaceAllString(raw, ""))
		}
	}
	return ""
}

// intelDefaultCPUTemplate and amdDefaultCPUTemplate are the conservative
// fleet-wide Firecracker CPU templates chosen per vendor (PR-E). Both names
// are config values, not computed: the template's correctness is NOT yet
// proven on real silicon (that boot + BuildBase + restore round-trip per
// vendor is the plan's separate verify step, still outstanding on the
// Alder Lake-S masters and Zen4 node-4) and must run BEFORE either name is
// treated as load-bearing rather than a placeholder identity label.
// "t2-conservative" names Intel's intended T2-family baseline (a
// homogeneous, no-AVX512 CPUID mask, chosen for the masters' hybrid P/E
// topology, but unverified pending that drill). AMD Firecracker has no
// CPUID-masking template today, so "amd-default" is a fixed logical profile
// name, not an FC wire value: it exists purely so node-4's cpu_sku has a
// non-empty template half, matching Intel's shape, for the mismatch gate to
// compare uniformly across vendors.
const (
	intelDefaultCPUTemplate = "t2-conservative"
	amdDefaultCPUTemplate   = "amd-default"
)

// defaultCPUTemplate resolves the conservative fleet-wide template for a known
// vendor. An unknown or empty vendor returns "" (an undetected vendor already
// skips the vendor check entirely; a template can never be more specific than
// an unknown vendor).
func defaultCPUTemplate(vendor string) string {
	switch vendor {
	case "intel":
		return intelDefaultCPUTemplate
	case "amd":
		return amdDefaultCPUTemplate
	default:
		return ""
	}
}

// getenvDefault returns the named env var, or def when unset or empty.
func getenvDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// atoiDefault returns the named env var parsed as an int, or def when unset or
// unparseable.
func atoiDefault(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// boolDefault returns the named env var parsed as a bool, or def when unset or
// unparseable.
func boolDefault(key string, def bool) bool {
	if v := os.Getenv(key); v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			return b
		}
	}
	return def
}

// parseDuration overrides *dst with the named env var parsed as a duration. An
// unset var leaves *dst unchanged; a malformed value is an error so a
// misconfiguration fails loudly at startup.
func parseDuration(key string, dst *time.Duration) error {
	v := os.Getenv(key)
	if v == "" {
		return nil
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return fmt.Errorf("invalid %s %q: %w", key, v, err)
	}
	*dst = d
	return nil
}
