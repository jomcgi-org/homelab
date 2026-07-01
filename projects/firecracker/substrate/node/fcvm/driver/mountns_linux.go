//go:build linux

package driver

import (
	"fmt"
	"os"
	"os/exec"
	"syscall"
)

// setUnshareMountNS makes the launched process start in a fresh mount namespace,
// so the bind-mount the __fcmount trampoline performs is private to that VM.
func setUnshareMountNS(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Unshareflags |= syscall.CLONE_NEWNS
}

// ExecMountTrampoline handles a re-exec of the form
//
//	<self> __fcmount <bindSrc> <bindTgt> <fcbin> <fcargs...>
//
// It runs in a fresh mount namespace (the parent launch set CLONE_NEWNS), makes
// mount propagation private so the bind never leaks to the host, bind-mounts
// bindSrc over bindTgt, then execs firecracker. Firecracker's vsock UDS — embedded
// in the base snapshot as <bindTgt>/vsock.sock — therefore lands in bindSrc (this
// VM's per-instance bundle dir, host-visible), giving every microVM restored from
// one warm base its own host-reachable vsock socket. On success it never returns
// (exec replaces the process); the caller invokes it at process start and it is a
// no-op unless argv[1] is "__fcmount".
func ExecMountTrampoline() {
	if len(os.Args) < 5 || os.Args[1] != "__fcmount" {
		return
	}
	bindSrc, bindTgt, fcbin := os.Args[2], os.Args[3], os.Args[4]
	rest := os.Args[5:]
	if err := syscall.Mount("", "/", "", syscall.MS_REC|syscall.MS_PRIVATE, ""); err != nil {
		fmt.Fprintf(os.Stderr, "fcmount: make / private: %v\n", err)
		os.Exit(90)
	}
	if err := os.MkdirAll(bindTgt, 0o750); err != nil {
		fmt.Fprintf(os.Stderr, "fcmount: mkdir %s: %v\n", bindTgt, err)
		os.Exit(91)
	}
	if err := syscall.Mount(bindSrc, bindTgt, "", syscall.MS_BIND, ""); err != nil {
		fmt.Fprintf(os.Stderr, "fcmount: bind %s -> %s: %v\n", bindSrc, bindTgt, err)
		os.Exit(92)
	}
	argv := append([]string{fcbin}, rest...)
	if err := syscall.Exec(fcbin, argv, os.Environ()); err != nil {
		fmt.Fprintf(os.Stderr, "fcmount: exec %s: %v\n", fcbin, err)
		os.Exit(93)
	}
}
