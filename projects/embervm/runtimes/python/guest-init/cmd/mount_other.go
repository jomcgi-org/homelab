//go:build !linux

package main

import "log/slog"

// mountTmpfsTmp is a no-op off Linux; the guest is always Linux, this stub only
// keeps the package building for host (e.g. darwin) builds.
func mountTmpfsTmp(*slog.Logger) {}

// mountProc is a no-op off Linux (see mountTmpfsTmp); the guest is always Linux.
func mountProc(*slog.Logger) {}
