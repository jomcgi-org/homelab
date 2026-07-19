//go:build linux

package main

import (
	"testing"

	"golang.org/x/sys/unix"
)

// TestLoopbackUpFlags covers the read-modify-write flag math bringUpLoopback uses:
// the up + running bits must be set, and any pre-existing bits (whatever
// SIOCGIFFLAGS returned) must be PRESERVED, not clobbered. Linux-only because the
// IFF_* constants and the whole loopback path are Linux-only.
func TestLoopbackUpFlags(t *testing.T) {
	for _, tc := range []struct {
		name string
		cur  uint16
		want uint16
	}{
		{"from down (zero flags)", 0, unix.IFF_UP | unix.IFF_RUNNING},
		{"already up stays up", unix.IFF_UP | unix.IFF_RUNNING, unix.IFF_UP | unix.IFF_RUNNING},
		{
			"preserves other bits (e.g. LOOPBACK)",
			unix.IFF_LOOPBACK,
			unix.IFF_LOOPBACK | unix.IFF_UP | unix.IFF_RUNNING,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := loopbackUpFlags(tc.cur); got != tc.want {
				t.Fatalf("loopbackUpFlags(%#x) = %#x, want %#x", tc.cur, got, tc.want)
			}
		})
	}
}
