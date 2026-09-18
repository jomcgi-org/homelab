//go:build !linux

package guestboot

import "log/slog"

// BringUpLoopback is a no-op off Linux; the guest is always Linux, this stub only
// keeps the package building for host (e.g. darwin) builds.
func BringUpLoopback(*slog.Logger) {}
