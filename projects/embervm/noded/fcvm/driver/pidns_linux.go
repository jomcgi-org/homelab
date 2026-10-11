//go:build linux

package driver

import (
	"os/exec"
	"syscall"
)

// setNewPIDNS spawns the jailer as init of a fresh PID namespace. The jailer
// then execs Firecracker in place, so the VMM is PID 1 of its own namespace
// while cmd.Process stays the VMM itself: execProcess keeps waiting on, killing
// and reading /proc for the real Firecracker pid. The jailer's own --new-pid-ns
// is deliberately not used: it clones Firecracker into a grandchild and exits,
// which leaves noded reaping the jailer while the VMM it must kill is no longer
// its child. A clone failure (EPERM, or the pid_namespaces limit) fails the
// launch at cmd.Start; there is no fallback to an un-namespaced jailer.
func setNewPIDNS(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Cloneflags |= syscall.CLONE_NEWPID
}
