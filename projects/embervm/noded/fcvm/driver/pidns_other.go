//go:build !linux

package driver

import "os/exec"

// setNewPIDNS is a no-op off Linux. Jailed launches already fail there because
// cgroup v2 delegation requires Linux, but the package also builds on other
// hosts.
func setNewPIDNS(*exec.Cmd) {}
