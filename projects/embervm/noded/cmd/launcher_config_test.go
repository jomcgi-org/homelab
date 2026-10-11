package main

import (
	"io"
	"log/slog"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver"
)

func TestNewExecLauncherCarriesContainmentConfig(t *testing.T) {
	cfg := config.Config{
		BinPath:            "/opt/fc/firecracker",
		JailerBinPath:      "/opt/fc/jailer",
		JailerEnabled:      true,
		DaemonReserveMib:   256,
		SnapshotRoot:       "/var/lib/embervm/scratch/snapshots",
		GuestOomScoreAdj:   1000,
		JailerUIDBlockBase: 20,
	}
	l := newExecLauncher(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if !l.JailerEnabled || l.JailerBin != cfg.JailerBinPath || l.Bin != cfg.BinPath {
		t.Fatalf("launcher binaries/arming = %+v", l)
	}
	if l.DaemonReserveMib != 256 {
		t.Fatalf("DaemonReserveMib = %d, want the brick reserve", l.DaemonReserveMib)
	}
	if l.UIDLeaseDir != cfg.SnapshotRoot {
		t.Fatalf("UIDLeaseDir = %q, want the release's snapshot root %q", l.UIDLeaseDir, cfg.SnapshotRoot)
	}
	if l.UIDBlockBase != 20 {
		t.Fatalf("UIDBlockBase = %d, want the configured jail uid block base 20", l.UIDBlockBase)
	}
	if !l.MountNamespace || l.OOMScoreAdj != 1000 {
		t.Fatalf("launcher hardening = %+v", l)
	}
}

// TestJailerUIDBlockCountMatchesDriver: config validates
// EMBERVM_NODED_JAILER_UID_BLOCK_BASE against its own copy of the block count,
// because config stays free of the driver. The two must agree.
func TestJailerUIDBlockCountMatchesDriver(t *testing.T) {
	if config.JailerUIDBlocks != driver.JailUIDBlocks {
		t.Fatalf("config.JailerUIDBlocks = %d, driver.JailUIDBlocks = %d", config.JailerUIDBlocks, driver.JailUIDBlocks)
	}
}
