// Package vsockproto holds the Firecracker vsock addressing constants the
// guest-init binaries share: the ports a guest listens on or dials for the
// frozen host/guest contract (ADR 030). The fc-agentd control-message protocol
// that once lived here (ADR 022) was retired with that daemon; embervm-noded
// carries its own copy of these constants in projects/embervm/noded/vsockproto
// (ADR embervm/001) and the two must stay in lock-step.
package vsockproto

// Firecracker vsock addressing. The host is always context-id 2; the daemon
// reaches a guest by its per-thread host UDS, not by CID. The guest dials
// EgressPort on the host and listens on GuestHTTPPort.
const (
	// HostCID is the Firecracker-reserved host context id.
	HostCID uint32 = 2
	// EgressPort carries one tunnelled outbound HTTP request each, from the guest
	// to the host-side egress proxy.
	EgressPort uint32 = 1025
	// GuestHTTPPort carries the inbound HTTP request the host delivers to the
	// guest's shim HTTP server (ADR 030). Separate from the egress port so an
	// invocation never contends with it. (1024 was the retired control-message
	// port and 1026 the retired semgrep scan-RPC port; both are left free.)
	GuestHTTPPort uint32 = 1027
)
