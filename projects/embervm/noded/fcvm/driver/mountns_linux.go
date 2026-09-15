//go:build linux

package driver

import (
	"os/exec"
	"syscall"
)

// setUnshareMountNS keeps direct-exec Firecracker processes in a private mount
// namespace. The namespace remains a containment boundary even though restore-
// time vsock overrides make the old canonical-path bind mount unnecessary.
func setUnshareMountNS(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Unshareflags |= syscall.CLONE_NEWNS
}
