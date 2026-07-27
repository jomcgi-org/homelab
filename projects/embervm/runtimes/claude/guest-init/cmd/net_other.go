//go:build !linux

package main

import "log/slog"

// bringUpLoopback is a no-op off Linux. The guest runs inside a Linux microVM;
// this stub keeps host builds working.
func bringUpLoopback(_ *slog.Logger) {}
