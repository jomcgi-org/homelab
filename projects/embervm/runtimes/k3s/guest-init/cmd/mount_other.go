//go:build !linux

package main

import "log/slog"

// mountGuestFilesystems is a no-op off Linux. The k3s guest runs inside a Linux
// microVM; this stub keeps the package building under the host (e.g. darwin)
// toolchain for unit tests (which exercise the pure argv/env helpers, not the
// mount path).
func mountGuestFilesystems(_ *slog.Logger) {}
