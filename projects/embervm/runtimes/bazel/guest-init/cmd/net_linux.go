//go:build linux

package main

import (
	"log/slog"
	"os"
	"os/exec"

	"golang.org/x/sys/unix"
)

// guestHostname is the name set on the guest and matched in the baked /etc/hosts
// (see the BUILD's guest_init_tar). Any stable, resolvable name works; it exists
// only so InetAddress.getLocalHost() resolves without a DNS round trip.
const guestHostname = "ember-bazel"

// bringUpLoopback brings the loopback interface UP. A raw Firecracker boot leaves
// `lo` DOWN, and the bazel client connects to the bazel SERVER over a gRPC socket
// on 127.0.0.1; with lo down that connect blocks forever and the warming VM sits
// at ZERO CPU with no output. This is best-effort by design: on failure it logs
// LOUDLY to the console (so a still-hung warming is diagnosable) but does not
// abort the boot.
//
// It shells out to busybox rather than issuing the SIOCSIFFLAGS ioctl directly:
// the vendored x/sys/unix has no generic Ifreq helper, so a raw ioctl would mean
// hand-laying the ifreq union struct, which is fragile across arches; busybox
// (already in the image) provides `ip` and `ifconfig` applets that do exactly
// this. `ip link set lo up` is tried first, `ifconfig lo up` as a fallback.
func bringUpLoopback(logger *slog.Logger) {
	attempts := [][]string{
		{"ip", "link", "set", "lo", "up"},
		{"ifconfig", "lo", "up"},
	}
	for _, args := range attempts {
		cmd := exec.Command(args[0], args[1:]...) // nosemgrep: no-shell-command-injection
		out, err := cmd.CombinedOutput()
		if err == nil {
			logger.Info("ember-bazel-init: loopback up", "via", args[0])
			return
		}
		logger.Warn("ember-bazel-init: loopback up attempt failed", "cmd", args, "err", err, "out", string(out))
	}
	logger.Error("ember-bazel-init: could NOT bring up loopback; bazel client may block connecting to the server on 127.0.0.1 (warming will hang)")
}

// setHostname sets the guest hostname when it is unset or the default. It pairs
// with the baked /etc/hosts (127.0.0.1 localhost + guestHostname), so a JVM
// InetAddress.getLocalHost() resolves locally instead of stalling on a lookup.
// Best-effort: a failure is logged, not fatal.
func setHostname(logger *slog.Logger) {
	if cur, err := os.Hostname(); err == nil && cur != "" && cur != "(none)" && cur != "localhost" {
		// A meaningful hostname is already set (e.g. noded injected one); leave it.
		logger.Info("ember-bazel-init: hostname already set", "hostname", cur)
		return
	}
	if err := unix.Sethostname([]byte(guestHostname)); err != nil {
		logger.Warn("ember-bazel-init: sethostname failed", "hostname", guestHostname, "err", err)
		return
	}
	logger.Info("ember-bazel-init: hostname set", "hostname", guestHostname)
}
