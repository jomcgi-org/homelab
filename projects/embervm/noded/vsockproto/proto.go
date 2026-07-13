// Package vsockproto is embervm-noded's forked copy of the Firecracker vsock
// addressing constants the host-side transport and egress forwarder need. It is
// a DELIBERATE, minimal FORK of projects/firecracker/substrate/vsockproto (ADR
// embervm/001: embervm-noded shares no Go packages with projects/firecracker).
//
// These port numbers ARE the frozen guest contract: the guest images embervm
// runs listen on GuestHTTPPort for inbound task delivery and dial EgressPort for
// tunnelled egress, exactly as the fc-invoke guests do. Because the contract is
// frozen (embervm consumes the SAME guest images through the SAME wire), copying
// the constants cannot drift; if the guest contract ever changes it changes in
// lock-step on both sides by definition. Only the subset the KEEP packages
// reference is carried here; the control-message and scan wire protocols do not
// cross the noded boundary and are omitted.
package vsockproto

// Firecracker vsock addressing. The host is always context-id 2; guests get a
// fixed id (the daemon reaches a guest by its per-thread host UDS, not by CID).
// The guest dials these ports on the host.
const (
	// HostCID is the Firecracker-reserved host context id.
	HostCID uint32 = 2
	// GuestCID is the fixed guest context id every microVM is assigned.
	GuestCID uint32 = 3
	// EgressPort carries one tunnelled outbound HTTP request each, from the guest
	// to the pod-local egress-proxy sidecar. Unused by the task class (task VMs
	// get no NIC and egress is disabled), but kept so the forwarder compiles and
	// a future egress-enabled workload needs no wire change.
	EgressPort uint32 = 1025
	// GuestHTTPPort carries the inbound HTTP request the daemon delivers to the
	// guest's shim HTTP server over vsock. This is the frozen guest contract port
	// (ADR 030); the transport dials it via the Firecracker CONNECT handshake.
	GuestHTTPPort uint32 = 1027
)
