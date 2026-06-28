// Package config loads fc-agentd configuration from the environment. The
// controller is a node-4 daemon; everything it needs is injected via env vars
// from the Helm values (Postgres DSN, node/arch identity, snapshot root, OTel).
package config

import (
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Config is the fully-resolved fc-agentd configuration.
type Config struct {
	// DatabaseURL is the monolith Postgres DSN holding the agent_threads
	// registry. Empty disables the Postgres-backed reconcile loop (dry run).
	DatabaseURL string
	// Node is the host this daemon manages; snapshots are node-affine so the
	// reconcile loop only touches threads pinned here.
	Node string
	// Arch is the CPU architecture of this node; FC snapshots are non-portable
	// and a mismatched restore fails closed (ADR 022).
	Arch string
	// SnapshotRoot is the directory holding per-thread snapshot bundles
	// (/disks/nvme-02 on node-4).
	SnapshotRoot string
	// ReconcileInterval is how often the loop polls desired vs actual state.
	ReconcileInterval time.Duration

	// FirecrackerBin is the firecracker binary the driver launches
	// (/opt/kata/bin/firecracker on node-4).
	FirecrackerBin string
	// KernelImagePath is the guest kernel (kata vmlinux.container on node-4).
	KernelImagePath string
	// RootfsPath is a shared/static rootfs, used only when BaseRootfsPath is empty.
	RootfsPath string
	// BaseRootfsPath is the read-only base rootfs image (the flattened harness
	// image). When set, each thread gets its own writable copy.
	BaseRootfsPath string
	// HarnessInit is the in-guest init the kernel boots into (fc-agent-init).
	HarnessInit string
	// GuestVCPUs and GuestMemMib size each microVM.
	GuestVCPUs  int
	GuestMemMib int

	// MaxConcurrent caps how many microVMs may be live (RUNNING or restored) at
	// once on this node. The reconcile loop refuses to claim past it, leaving
	// excess threads PENDING (a queue drained as live VMs idle or complete).
	// Because Firecracker hard-caps each guest's RAM, MaxConcurrent * GuestMemMib
	// is a true upper bound on the microVM memory in this daemon's cgroup, which
	// is what keeps a burst of submissions from OOM-killing the controller. 0
	// disables the cap (unbounded; tests/dry-run).
	MaxConcurrent int

	// GuestOOMScoreAdj is written to each Firecracker child's
	// /proc/<pid>/oom_score_adj so the guest, never the daemon, is the kernel's
	// first OOM victim under cgroup or node memory pressure (ADR platform/010's
	// disposable-victim intent, applied to processes because the guests are child
	// processes, not pods). 0 leaves the inherited score; a high value (e.g.
	// 1000) makes guests strictly preferred for the kill.
	GuestOOMScoreAdj int

	// EgressSidecar is the localhost address of the egress-proxy sidecar (ADR
	// 023) the loop forwards guest egress to. Empty disables egress forwarding.
	EgressSidecar string
	// InjectedEnv is harness environment injected into each guest via the Assign
	// message, gathered from FC_AGENTD_INJECT_<NAME> env vars (the prefix is
	// stripped). Lets chart values supply the goose provider/model + the model
	// base URL without hardcoding cluster config in the guest binary.
	InjectedEnv map[string]string
}

// injectEnvPrefix marks env vars fc-agentd forwards into the guest (stripped).
const injectEnvPrefix = "FC_AGENTD_INJECT_"

// Load resolves configuration from the environment, applying defaults. It
// returns an error only for values that are present but malformed.
func Load() (Config, error) {
	c := Config{
		DatabaseURL:       os.Getenv("DATABASE_URL"),
		Node:              os.Getenv("FC_AGENTD_NODE"),
		Arch:              os.Getenv("FC_AGENTD_ARCH"),
		SnapshotRoot:      getenvDefault("FC_AGENTD_SNAPSHOT_ROOT", "/disks/nvme-02/agent-threads"),
		ReconcileInterval: 5 * time.Second,
		FirecrackerBin:    getenvDefault("FC_AGENTD_FIRECRACKER_BIN", "/opt/kata/bin/firecracker"),
		KernelImagePath:   getenvDefault("FC_AGENTD_KERNEL_IMAGE", "/opt/kata/share/kata-containers/vmlinux.container"),
		RootfsPath:        os.Getenv("FC_AGENTD_ROOTFS_PATH"),
		BaseRootfsPath:    os.Getenv("FC_AGENTD_BASE_ROOTFS"),
		HarnessInit:       getenvDefault("FC_AGENTD_HARNESS_INIT", "/usr/local/bin/fc-agent-init"),
		GuestVCPUs:        atoiDefault("FC_AGENTD_GUEST_VCPUS", 1),
		GuestMemMib:       atoiDefault("FC_AGENTD_GUEST_MEM_MIB", 1024),
		MaxConcurrent:     atoiDefault("FC_AGENTD_MAX_CONCURRENT", 8),
		GuestOOMScoreAdj:  atoiDefault("FC_AGENTD_GUEST_OOM_SCORE_ADJ", 1000),
		EgressSidecar:     os.Getenv("FC_AGENTD_EGRESS_SIDECAR"),
		InjectedEnv:       injectedEnv(),
	}

	if c.Node == "" {
		// Default to the pod's node via the downward API hostname fallback.
		c.Node = os.Getenv("NODE_NAME")
	}
	if c.Arch == "" {
		c.Arch = runtime.GOARCH
	}

	if v := os.Getenv("FC_AGENTD_RECONCILE_INTERVAL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid FC_AGENTD_RECONCILE_INTERVAL %q: %w", v, err)
		}
		c.ReconcileInterval = d
	}

	return c, nil
}

// injectedEnv collects FC_AGENTD_INJECT_<NAME> env vars into a {<NAME>: value}
// map (prefix stripped), the harness env the controller forwards to each guest.
func injectedEnv() map[string]string {
	out := map[string]string{}
	for _, kv := range os.Environ() {
		name, val, ok := strings.Cut(kv, "=")
		if !ok || !strings.HasPrefix(name, injectEnvPrefix) {
			continue
		}
		if stripped := strings.TrimPrefix(name, injectEnvPrefix); stripped != "" {
			out[stripped] = val
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
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
