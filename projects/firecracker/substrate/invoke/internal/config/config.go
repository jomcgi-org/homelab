// Package config loads fc-invoke configuration from the environment. The
// daemon multiplexes named workloads over a shared pool of Firecracker
// microVMs; global daemon settings come from FC_INVOKE_* env vars and the
// workload table is provided as JSON via FC_INVOKE_WORKLOADS (inline) or
// FC_INVOKE_WORKLOADS_FILE (path to a JSON file, takes precedence).
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"time"
)

// Workload describes one named guest workload that fc-invoke can dispatch.
// Fields are loaded from the JSON workload table; per-workload defaults are
// applied by Load after unmarshalling.
type Workload struct {
	// Image is the logical guest rootfs image name, resolved to a path by the
	// launcher.
	Image string `json:"image"`
	// RootfsPath is the host path to this workload's base rootfs ext4 image.
	// Each workload boots its own guest image's rootfs, attached read-only
	// (all mutable guest state is tmpfs), so one file backs every microVM for
	// the workload with no per-request copy.
	RootfsPath string `json:"rootfsPath"`
	// HarnessInit is the in-guest PID-1 path the kernel boots into (the guest
	// shim server) for this workload. Different guest images install their init
	// at different paths (semgrep-guest-init vs the agent's), so this is
	// per-workload; empty falls back to the daemon-global HarnessInit.
	HarnessInit string `json:"harnessInit"`
	// VCPUs is the number of virtual CPUs to allocate per microVM. Default 2.
	VCPUs int `json:"vcpus"`
	// MemMib is the guest memory in MiB. Default 2048.
	MemMib int `json:"memMib"`
	// Concurrency is the maximum number of live microVMs for this workload.
	// Default 4.
	Concurrency int `json:"concurrency"`

	// EgressEnabled controls whether the egress-proxy sidecar is attached to
	// microVMs launched for this workload.
	EgressEnabled bool `json:"egressEnabled"`
	// EgressSecrets lists the 1Password secret names whose values the egress
	// proxy should swap into outgoing requests.
	EgressSecrets []string `json:"egressSecrets"`

	// WarmBase enables the snapshot-restore hot path: restore a pre-warmed
	// microVM per request rather than booting cold.
	WarmBase bool `json:"warmBase"`
	// ReadyPath is the guest-side vsock path the shim exposes to signal
	// readiness. Default "/shim/ready".
	ReadyPath string `json:"readyPath"`
	// Sessioned marks the workload as session-aware: the caller may reuse a
	// live microVM across multiple requests within a session.
	Sessioned bool `json:"sessioned"`

	// RequestTimeout is the parsed form of RequestTimeoutStr. It is excluded
	// from JSON marshalling; Load sets it from RequestTimeoutStr (default 90s).
	RequestTimeout time.Duration `json:"-"`
	// RequestTimeoutStr is the human-readable timeout for a single request
	// (e.g. "90s", "3m"). Parsed into RequestTimeout by Load.
	RequestTimeoutStr string `json:"requestTimeout"`
}

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
	// KernelImagePath is the guest kernel (kata vmlinux.container on node-4),
	// shared by every workload.
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

	// EgressSidecarAddr is the pod-local egress-proxy sidecar TCP address
	// (ADR 023 phase 6a). Egress-enabled workloads tunnel each guest's vsock
	// egress connections here; the daemon holds no secrets and never parses the
	// bytes. Daemon-global (one sidecar per pod serves every egress-enabled
	// workload). Default "127.0.0.1:8888".
	EgressSidecarAddr string

	// Workloads is the set of named workloads the daemon can dispatch, keyed
	// by workload name. Empty when no workload table is configured.
	Workloads map[string]Workload
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
		BinPath:           getenvDefault("FC_INVOKE_FIRECRACKER_BIN", "/opt/kata/bin/firecracker"),
		KernelImagePath:   getenvDefault("FC_INVOKE_KERNEL_IMAGE", "/opt/kata/share/kata-containers/vmlinux.container"),
		KernelBootArgs:    os.Getenv("FC_INVOKE_KERNEL_BOOT_ARGS"),
		HarnessInit:       getenvDefault("FC_INVOKE_HARNESS_INIT", "/usr/local/bin/fc-shim-init"),
		CanonicalVsockDir: getenvDefault("FC_INVOKE_CANONICAL_VSOCK_DIR", "/disks/nvme-02/fc-invoke-vsock"),
		GuestOomScoreAdj:  atoiDefault("FC_INVOKE_GUEST_OOM_SCORE_ADJ", 1000),
		BootReadyTimeout:  60 * time.Second,
		EgressSidecarAddr: getenvDefault("FC_INVOKE_EGRESS_SIDECAR_ADDR", "127.0.0.1:8888"),
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

	return c, nil
}

// loadWorkloads parses the workload table from FC_INVOKE_WORKLOADS_FILE (if
// set, takes precedence) or FC_INVOKE_WORKLOADS (inline JSON object). An
// absent or empty source yields an empty map without error. Per-workload
// defaults are applied after unmarshalling.
func loadWorkloads() (map[string]Workload, error) {
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
		return map[string]Workload{}, nil
	}

	var table map[string]Workload
	if err := json.Unmarshal(raw, &table); err != nil {
		return nil, fmt.Errorf("parsing workloads JSON from %s: %w", source, err)
	}
	if table == nil {
		return map[string]Workload{}, nil
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
