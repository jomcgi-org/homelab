//go:build !linux

package main

import "log/slog"

// The guest is always Linux. These no-op stubs keep host toolchain builds
// possible on platforms without the mount syscalls.
func mountProc(_ *slog.Logger)     {}
func mountTmpfsTmp(_ *slog.Logger) {}
