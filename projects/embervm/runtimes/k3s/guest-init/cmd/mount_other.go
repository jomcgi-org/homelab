//go:build !linux

package main

import (
	"fmt"
	"log/slog"
	"runtime"
)

// mountGuestFilesystems is a no-op off Linux. The k3s guest runs inside a Linux
// microVM; this stub keeps the package building under the host (e.g. darwin)
// toolchain for unit tests (which exercise the pure argv/env helpers, not the
// mount path).
func mountGuestFilesystems(_ *slog.Logger) {}

// mountStatefulVolume always fails off Linux: mkfs/mount are Linux-only. The
// stateful path only runs inside the microVM; this stub keeps the package
// building under the host toolchain for unit tests, which never reach it.
func mountStatefulVolume(_ *slog.Logger, dev, _ string) error {
	return fmt.Errorf("ember-k3s-init: stateful volume mount unsupported on %s (dev=%s)", runtime.GOOS, dev)
}
