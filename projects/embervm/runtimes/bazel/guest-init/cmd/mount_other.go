//go:build !linux

package main

import "log/slog"

// mountProc / mountTmpfsTmp are no-ops off Linux (the mount syscalls are Linux
// -only). The guest runs inside a Linux microVM; these stubs keep the package
// building under the host toolchain for unit tests and local builds.
func mountProc(_ *slog.Logger)     {}
func mountTmpfsTmp(_ *slog.Logger) {}
