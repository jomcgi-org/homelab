// Package config loads embervm-noded's daemon configuration from the
// environment. It is the node daemon's counterpart to fc-invoke's catalog
// loader, reshaped for the fork: there is NO workload table here. Concurrency,
// egress posture, and warm-pool sizing are the control plane's concern now and
// arrive per-call over the gRPC contract; the daemon reads only node-side
// substrate paths, a node-level backstop cap, and a small image->rootfs identity
// table (the one thing the gRPC BuildBase request deliberately does not carry:
// "Node-side image identity (rootfs path, harness init) is daemon configuration").
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"strconv"
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
	// BearerToken, when set, gates every gRPC call: the caller must present
	// "authorization: Bearer <token>" in call metadata. Empty runs the daemon
	// open and logs a startup warning (mirrors fc-invoke's fail-loud-not-silent
	// posture); a Cilium/Linkerd policy is the defence-in-depth layer on top.
	BearerToken string

	// MaxLiveVMs is the node-level backstop cap on concurrently live microVMs
	// (primed + assigning). The control plane owns real concurrency; this only
	// stops a runaway from exhausting the node. Prime returns RESOURCE_EXHAUSTED
	// at the cap. Default 8. Zero or negative means unbounded (no backstop).
	MaxLiveVMs int

	// SnapshotRoot is the directory holding FC bundle + base snapshots on the
	// NVMe scratch disk. Maps to the driver's SnapshotRoot.
	SnapshotRoot string
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
	// DrainTimeout bounds graceful shutdown: on SIGTERM the daemon stops
	// accepting new RPCs and waits up to this long for in-flight Assigns to
	// finish. The pod's terminationGracePeriodSeconds must exceed it. Default 60s.
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

	// Images maps a gRPC BuildBase image_ref to its node-side identity. Parsed
	// from EMBERVM_NODED_IMAGES (inline JSON object) or _FILE (path, precedence).
	// Empty is valid: BuildBase for an unknown image fails FAILED_PRECONDITION,
	// which is correct until the control plane provisions images (Task 11+).
	Images map[string]Image
}

// Load resolves configuration from the environment, applying defaults for all
// optional fields. It errors only on values that are present but malformed.
func Load() (Config, error) {
	c := Config{
		ListenAddr:          getenvDefault("EMBERVM_NODED_LISTEN_ADDR", ":9090"),
		HealthAddr:          getenvDefault("EMBERVM_NODED_HEALTH_ADDR", ":8080"),
		Node:                os.Getenv("EMBERVM_NODED_NODE"),
		Arch:                os.Getenv("EMBERVM_NODED_ARCH"),
		BearerToken:         os.Getenv("EMBERVM_NODED_BEARER_TOKEN"),
		MaxLiveVMs:          atoiDefault("EMBERVM_NODED_MAX_LIVE_VMS", 8),
		SnapshotRoot:        os.Getenv("EMBERVM_NODED_SNAPSHOT_ROOT"),
		BinPath:             getenvDefault("EMBERVM_NODED_FIRECRACKER_BIN", "/opt/fc/firecracker"),
		KernelImagePath:     getenvDefault("EMBERVM_NODED_KERNEL_IMAGE", "/opt/fc/vmlinux.container"),
		KernelBootArgs:      os.Getenv("EMBERVM_NODED_KERNEL_BOOT_ARGS"),
		HarnessInit:         getenvDefault("EMBERVM_NODED_HARNESS_INIT", "/usr/local/bin/fc-shim-init"),
		CanonicalVsockDir:   getenvDefault("EMBERVM_NODED_CANONICAL_VSOCK_DIR", "/disks/nvme-02/embervm-noded-vsock"),
		GuestOomScoreAdj:    atoiDefault("EMBERVM_NODED_GUEST_OOM_SCORE_ADJ", 1000),
		BootReadyTimeout:    60 * time.Second,
		RestoreReadyTimeout: 2 * time.Second,
		DrainTimeout:        60 * time.Second,
		EgressSidecarAddr:   getenvDefault("EMBERVM_NODED_EGRESS_SIDECAR_ADDR", "127.0.0.1:8888"),
		ArchiveFetchTimeout: 60 * time.Second,
		ArchiveMaxBytes:     512 << 20,
	}

	if c.Node == "" {
		c.Node = os.Getenv("NODE_NAME")
	}
	if c.Arch == "" {
		c.Arch = runtime.GOARCH
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
	if v := os.Getenv("EMBERVM_NODED_ARCHIVE_MAX_BYTES"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return Config{}, fmt.Errorf("invalid EMBERVM_NODED_ARCHIVE_MAX_BYTES %q: %w", v, err)
		}
		c.ArchiveMaxBytes = n
	}

	images, err := loadImages()
	if err != nil {
		return Config{}, err
	}
	c.Images = images

	return c, nil
}

// loadImages parses the image identity table from EMBERVM_NODED_IMAGES_FILE (if
// set, takes precedence) or EMBERVM_NODED_IMAGES (inline JSON object). An absent
// or empty source yields an empty map without error.
func loadImages() (map[string]Image, error) {
	var raw []byte
	source := "EMBERVM_NODED_IMAGES"
	if filePath := os.Getenv("EMBERVM_NODED_IMAGES_FILE"); filePath != "" {
		data, err := os.ReadFile(filePath)
		if err != nil {
			return nil, fmt.Errorf("reading EMBERVM_NODED_IMAGES_FILE %q: %w", filePath, err)
		}
		raw = data
		source = "file " + filePath
	} else if inline := os.Getenv("EMBERVM_NODED_IMAGES"); inline != "" {
		raw = []byte(inline)
	}
	if len(raw) == 0 {
		return map[string]Image{}, nil
	}
	var table map[string]Image
	if err := json.Unmarshal(raw, &table); err != nil {
		return nil, fmt.Errorf("parsing images JSON from %s: %w", source, err)
	}
	if table == nil {
		return map[string]Image{}, nil
	}
	return table, nil
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
