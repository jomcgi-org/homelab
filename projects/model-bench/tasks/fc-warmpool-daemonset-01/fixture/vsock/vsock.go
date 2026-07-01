// Package vsock models the firecracker vsock connection handed out for a warm
// microVM slot. It is a separate package so the pool imports it across the module.
package vsock

// Conn is a handle to a warm microVM reachable over vsock.
type Conn struct {
	// ID is a unique identifier for this connection instance.
	ID string
	// Slot is the pool slot index this connection occupies.
	Slot int
}
