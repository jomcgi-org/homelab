// Package config loads semgrep-scand configuration from the environment. The
// daemon is a node-affine service that boots a fresh semgrep-guest microVM per
// scan, so everything it needs (FC paths, guest sizing, the base rootfs) is
// injected via SEMGREP_SCAND_* env vars from the Helm values. The env-parsing
// style mirrors fc-agentd/internal/config.
package config

import (
	"fmt"
	"os"
	"runtime"
	"strconv"
	"time"
)

// Config is the fully-resolved semgrep-scand configuration.
type Config struct {
	// ListenAddr is the HTTP listen address for the /scan + /healthz handler.
	ListenAddr string

	// MaxConcurrent caps how many semgrep-guest microVMs may be live at once.
	// Each guest hard-caps its RAM, so MaxConcurrent * GuestMemMib bounds the
	// microVM memory in this daemon's cgroup. 0 disables the cap (unbounded).
	MaxConcurrent int

	// GuestMemMib and GuestVCPUs size each scanner microVM.
	GuestMemMib int
	GuestVCPUs  int
	// GuestOomScoreAdj is written to each Firecracker child's oom_score_adj so a
	// guest, never the daemon, is the kernel's first OOM victim under memory
	// pressure. 0 leaves the inherited score; 1000 strictly prefers guests.
	GuestOomScoreAdj int

	// BaseRootfsPath is the read-only semgrep-guest base rootfs image. Each scan
	// gets its own writable rootfs derived from it.
	BaseRootfsPath string
	// NvmeRoot is the directory holding per-scan FC bundles (the NVMe scratch
	// disk on node-4). It maps to the driver's SnapshotRoot.
	NvmeRoot string

	// BinPath is the firecracker binary the driver launches.
	BinPath string
	// KernelImagePath is the guest kernel (kata vmlinux.container on node-4).
	KernelImagePath string
	// KernelBootArgs are appended to the kernel command line on cold boot. Empty
	// uses the driver's default.
	KernelBootArgs string
	// HarnessInit is the in-guest init the kernel boots into (semgrep-guest-init).
	// A raw FC boot ignores the OCI entrypoint, so the driver appends init=<path>.
	HarnessInit string

	// Node and Arch pin this daemon to its host; FC guests are arch-affine.
	Node string
	Arch string

	// Provisioner selects the per-scan rootfs strategy: "copy" (default; full
	// file copy) or "devmapper" (copy-on-write thin-snapshot).
	Provisioner string
	// ThinPool is the devmapper thin-pool the devmapper provisioner snapshots
	// into; ignored by the copy provisioner.
	ThinPool string

	// ScanTimeout bounds the guest scan request/response leg.
	ScanTimeout time.Duration
	// BootReadyTimeout bounds how long the daemon waits for a freshly booted
	// guest to announce readiness (the KindHello on the control vsock).
	BootReadyTimeout time.Duration
}

// Load resolves configuration from the environment, applying defaults. It
// returns an error only for values that are present but malformed.
func Load() (Config, error) {
	c := Config{
		ListenAddr:       getenvDefault("SEMGREP_SCAND_LISTEN_ADDR", ":8080"),
		MaxConcurrent:    atoiDefault("SEMGREP_SCAND_MAX_CONCURRENT", 4),
		GuestMemMib:      atoiDefault("SEMGREP_SCAND_GUEST_MEM_MIB", 2048),
		GuestVCPUs:       atoiDefault("SEMGREP_SCAND_GUEST_VCPUS", 4),
		GuestOomScoreAdj: atoiDefault("SEMGREP_SCAND_GUEST_OOM_SCORE_ADJ", 1000),
		BaseRootfsPath:   os.Getenv("SEMGREP_SCAND_BASE_ROOTFS"),
		NvmeRoot:         getenvDefault("SEMGREP_SCAND_NVME_ROOT", "/disks/nvme-02/semgrep-scand"),
		BinPath:          getenvDefault("SEMGREP_SCAND_FIRECRACKER_BIN", "/opt/kata/bin/firecracker"),
		KernelImagePath:  getenvDefault("SEMGREP_SCAND_KERNEL_IMAGE", "/opt/kata/share/kata-containers/vmlinux.container"),
		KernelBootArgs:   os.Getenv("SEMGREP_SCAND_KERNEL_BOOT_ARGS"),
		HarnessInit:      getenvDefault("SEMGREP_SCAND_HARNESS_INIT", "/usr/local/bin/semgrep-guest-init"),
		Node:             os.Getenv("SEMGREP_SCAND_NODE"),
		Arch:             os.Getenv("SEMGREP_SCAND_ARCH"),
		Provisioner:      getenvDefault("SEMGREP_SCAND_PROVISIONER", "copy"),
		ThinPool:         getenvDefault("SEMGREP_SCAND_THIN_POOL", "devpool"),
		ScanTimeout:      60 * time.Second,
		BootReadyTimeout: 30 * time.Second,
	}

	if c.Node == "" {
		c.Node = os.Getenv("NODE_NAME")
	}
	if c.Arch == "" {
		c.Arch = runtime.GOARCH
	}

	if err := parseDuration("SEMGREP_SCAND_SCAN_TIMEOUT", &c.ScanTimeout); err != nil {
		return Config{}, err
	}
	if err := parseDuration("SEMGREP_SCAND_BOOT_READY_TIMEOUT", &c.BootReadyTimeout); err != nil {
		return Config{}, err
	}

	return c, nil
}

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

func getenvDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func atoiDefault(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
