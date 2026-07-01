//go:build !linux

package driver

import "os/exec"

// setUnshareMountNS is a no-op off Linux; per-instance vsock mount namespaces only
// run on node-4. This keeps the package building on the darwin CI path.
func setUnshareMountNS(*exec.Cmd) {}

// ExecMountTrampoline is a no-op off Linux (the daemon only runs the trampoline on
// node-4); keeps the package building for host builds.
func ExecMountTrampoline() {}
