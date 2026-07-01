package substrate

import "time"

// Workload describes one named guest workload the substrate can dispatch. It is
// the shared spec both planes depend on: the control plane (cluster/catalog)
// loads a table of these from config, and the data plane (node/invoker) consumes
// one to size and boot a microVM. It lives in this neutral seam package so that
// neither plane imports the other (ADR 031).
//
// Fields are loaded from the JSON workload table; per-workload defaults are
// applied by the catalog loader after unmarshalling.
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
	// from JSON marshalling; the catalog loader sets it from RequestTimeoutStr
	// (default 90s).
	RequestTimeout time.Duration `json:"-"`
	// RequestTimeoutStr is the human-readable timeout for a single request
	// (e.g. "90s", "3m"). Parsed into RequestTimeout by the catalog loader.
	RequestTimeoutStr string `json:"requestTimeout"`
}
