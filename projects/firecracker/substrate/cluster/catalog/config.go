// Package catalog loads fc-invoke configuration from the environment: global
// daemon settings from FC_INVOKE_* env vars and the workload table (as
// substrate.Workload specs) from FC_INVOKE_WORKLOADS (inline) or
// FC_INVOKE_WORKLOADS_FILE (a JSON file path, which takes precedence). It is the
// control plane's view of "what workloads exist" (ADR 031); the Workload spec
// itself lives in the neutral substrate seam so the node plane can consume it
// without importing the cluster plane.
package catalog

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
)

// Config is the fully-resolved fc-invoke daemon configuration.
type Config struct {
	// ListenAddr is the HTTP listen address for the invoke handler and /healthz.
	// Default ":8080".
	ListenAddr string
	// Node identifies the Kubernetes node this daemon is pinned to (injected
	// via the Downward API as FC_INVOKE_NODE).
	Node string
	// Arch is the host CPU architecture; FC guests are arch-affine. Defaults
	// to runtime.GOARCH when FC_INVOKE_ARCH is unset.
	Arch string
	// SnapshotRoot is the directory holding FC bundle snapshots on the NVMe
	// scratch disk. It maps to the driver's SnapshotRoot.
	SnapshotRoot string

	// BinPath is the firecracker binary the driver launches.
	BinPath string
	// KernelImagePath is the guest kernel (kata's vmlinux.container, baked into
	// the image at /opt/fc), shared by every workload.
	KernelImagePath string
	// KernelBootArgs are appended to the kernel command line on cold boot.
	// Empty uses the driver's default.
	KernelBootArgs string
	// HarnessInit is the in-guest init the kernel boots into (the guest shim
	// init). A raw FC boot ignores the OCI entrypoint, so the driver appends
	// init=<path>.
	HarnessInit string
	// CanonicalVsockDir is the fixed directory whose vsock.sock the base
	// snapshot embeds; the launcher bind-mounts each microVM's bundle dir over
	// it per instance so concurrent restores each get their own host-reachable
	// socket. Pairs with the ExecLauncher's VsockBindTarget.
	CanonicalVsockDir string
	// GuestOomScoreAdj is written to each Firecracker child's oom_score_adj so
	// a guest, never the daemon, is the kernel's first OOM victim under memory
	// pressure. 0 leaves the inherited score; 1000 strictly prefers guests.
	GuestOomScoreAdj int
	// BootReadyTimeout bounds how long an invoker waits for a freshly booted or
	// restored guest to announce readiness over its shim.
	BootReadyTimeout time.Duration
	// DrainTimeout bounds graceful shutdown: on SIGTERM the daemon stops
	// accepting new invocations and waits up to this long for in-flight guests
	// to finish before exiting, so a rollout never drops a running task. It must
	// cover the longest workload RequestTimeout, and the pod's
	// terminationGracePeriodSeconds must exceed it so Kubernetes does not SIGKILL
	// mid-drain. Overridable via FC_INVOKE_DRAIN_TIMEOUT; defaults to the longest
	// workload RequestTimeout plus a flush/discard margin (or BootReadyTimeout
	// when no workloads are configured).
	DrainTimeout time.Duration

	// EgressSidecarAddr is the pod-local egress-proxy sidecar TCP address
	// (ADR 023 phase 6a). Egress-enabled workloads tunnel each guest's vsock
	// egress connections here; the daemon holds no secrets and never parses the
	// bytes. Daemon-global (one sidecar per pod serves every egress-enabled
	// workload). Default "127.0.0.1:8888".
	EgressSidecarAddr string

	// AllowedCallers is the allow-list of Kubernetes caller identities permitted
	// to POST /invoke, as full usernames (e.g.
	// "system:serviceaccount:monolith:monolith"), parsed from the
	// comma-separated FC_INVOKE_ALLOWED_CALLERS. When empty, caller
	// authentication is DISABLED and the daemon logs a startup warning: /invoke
	// is then open to any in-cluster client. Production deployments always set
	// it; the empty default keeps the pre-substrate (firecracker.enabled=false)
	// and non-Kubernetes test paths runnable without the TokenReview API.
	AllowedCallers []string

	// Workloads is the set of named workloads the daemon can dispatch, keyed
	// by workload name. Empty when no workload table is configured.
	Workloads map[string]substrate.Workload
}

// Load resolves configuration from the environment, applying defaults for all
// optional fields. It returns an error only for values that are present but
// malformed (bad JSON or an unparseable duration string).
func Load() (Config, error) {
	c := Config{
		ListenAddr:        getenvDefault("FC_INVOKE_LISTEN_ADDR", ":8080"),
		Node:              os.Getenv("FC_INVOKE_NODE"),
		Arch:              os.Getenv("FC_INVOKE_ARCH"),
		SnapshotRoot:      os.Getenv("FC_INVOKE_SNAPSHOT_ROOT"),
		BinPath:           getenvDefault("FC_INVOKE_FIRECRACKER_BIN", "/opt/fc/firecracker"),
		KernelImagePath:   getenvDefault("FC_INVOKE_KERNEL_IMAGE", "/opt/fc/vmlinux.container"),
		KernelBootArgs:    os.Getenv("FC_INVOKE_KERNEL_BOOT_ARGS"),
		HarnessInit:       getenvDefault("FC_INVOKE_HARNESS_INIT", "/usr/local/bin/fc-shim-init"),
		CanonicalVsockDir: getenvDefault("FC_INVOKE_CANONICAL_VSOCK_DIR", "/disks/nvme-02/fc-invoke-vsock"),
		GuestOomScoreAdj:  atoiDefault("FC_INVOKE_GUEST_OOM_SCORE_ADJ", 1000),
		BootReadyTimeout:  60 * time.Second,
		EgressSidecarAddr: getenvDefault("FC_INVOKE_EGRESS_SIDECAR_ADDR", "127.0.0.1:8888"),
		AllowedCallers:    splitList(os.Getenv("FC_INVOKE_ALLOWED_CALLERS")),
	}

	if c.Node == "" {
		c.Node = os.Getenv("NODE_NAME")
	}
	if c.Arch == "" {
		c.Arch = runtime.GOARCH
	}

	if err := parseDuration("FC_INVOKE_BOOT_READY_TIMEOUT", &c.BootReadyTimeout); err != nil {
		return Config{}, err
	}

	workloads, err := loadWorkloads()
	if err != nil {
		return Config{}, err
	}
	c.Workloads = workloads

	// Derive the drain budget from the workload table so it can never silently
	// fall behind a requestTimeout bump, then let FC_INVOKE_DRAIN_TIMEOUT pin it.
	c.DrainTimeout = defaultDrainTimeout(c.Workloads, c.BootReadyTimeout)
	if err := parseDuration("FC_INVOKE_DRAIN_TIMEOUT", &c.DrainTimeout); err != nil {
		return Config{}, err
	}

	return c, nil
}

// defaultDrainTimeout is the graceful-shutdown budget when FC_INVOKE_DRAIN_TIMEOUT
// is unset: the longest workload RequestTimeout plus a margin for the guest
// response to flush and the microVM to be discarded, so an in-flight invocation
// is never cut short. Falls back to bootReady when no workloads are configured.
func defaultDrainTimeout(workloads map[string]substrate.Workload, bootReady time.Duration) time.Duration {
	var longest time.Duration
	for _, w := range workloads {
		if w.RequestTimeout > longest {
			longest = w.RequestTimeout
		}
	}
	if longest <= 0 {
		return bootReady
	}
	return longest + 30*time.Second
}

// loadWorkloads parses the workload table from FC_INVOKE_WORKLOADS_FILE (if
// set, takes precedence) or FC_INVOKE_WORKLOADS (inline JSON object). An
// absent or empty source yields an empty map without error. Per-workload
// defaults are applied after unmarshalling.
func loadWorkloads() (map[string]substrate.Workload, error) {
	var raw []byte
	var source string // used in error messages to name the JSON source

	if filePath := os.Getenv("FC_INVOKE_WORKLOADS_FILE"); filePath != "" {
		data, err := os.ReadFile(filePath)
		if err != nil {
			return nil, fmt.Errorf("reading FC_INVOKE_WORKLOADS_FILE %q: %w", filePath, err)
		}
		raw = data
		source = "file " + filePath
	} else if inline := os.Getenv("FC_INVOKE_WORKLOADS"); inline != "" {
		raw = []byte(inline)
		source = "FC_INVOKE_WORKLOADS"
	}

	if len(raw) == 0 {
		return map[string]substrate.Workload{}, nil
	}

	var table map[string]substrate.Workload
	if err := json.Unmarshal(raw, &table); err != nil {
		return nil, fmt.Errorf("parsing workloads JSON from %s: %w", source, err)
	}
	if table == nil {
		return map[string]substrate.Workload{}, nil
	}

	// Apply per-workload defaults and parse durations. Map entries are
	// value types, so each must be copied out, mutated, and stored back.
	for name, w := range table {
		if w.VCPUs <= 0 {
			w.VCPUs = 2
		}
		if w.MemMib <= 0 {
			w.MemMib = 2048
		}
		if w.Concurrency <= 0 {
			w.Concurrency = 4
		}
		if w.ReadyPath == "" {
			w.ReadyPath = "/shim/ready"
		}

		timeoutStr := w.RequestTimeoutStr
		if timeoutStr == "" {
			timeoutStr = "90s"
		}
		d, err := time.ParseDuration(timeoutStr)
		if err != nil {
			return nil, fmt.Errorf("workload %q: invalid requestTimeout %q: %w", name, timeoutStr, err)
		}
		w.RequestTimeout = d

		table[name] = w
	}

	return table, nil
}

// getenvDefault returns the value of the named environment variable, or def
// when the variable is unset or empty.
func getenvDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// splitList parses a comma-separated environment value into a slice of trimmed,
// non-empty entries. An empty or all-whitespace value yields a nil slice.
func splitList(v string) []string {
	if v == "" {
		return nil
	}
	var out []string
	for _, part := range strings.Split(v, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// atoiDefault returns the named environment variable parsed as an int, or def
// when the variable is unset or unparseable.
func atoiDefault(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// parseDuration overrides *dst with the named environment variable parsed as a
// duration. An unset variable leaves *dst unchanged; a malformed value is an
// error so misconfiguration fails loudly at startup.
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
