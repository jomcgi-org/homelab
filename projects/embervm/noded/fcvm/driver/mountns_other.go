//go:build !linux

package driver

import "os/exec"

// setUnshareMountNS is a no-op off Linux. Firecracker runs on Linux nodes, but
// the package also builds on other hosts.
func setUnshareMountNS(*exec.Cmd) {}
