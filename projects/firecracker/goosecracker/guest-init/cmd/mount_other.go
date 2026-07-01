//go:build !linux

package main

import "log/slog"

// mountTmpfs is a no-op off Linux; the guest is always Linux, this stub only
// keeps the package building for host (e.g. darwin) builds.
func mountTmpfs(*slog.Logger, string, string) {}
