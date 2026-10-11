package main

import (
	"io"
	"log/slog"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

func TestNewExecLauncherCarriesContainmentConfig(t *testing.T) {
	cfg := config.Config{
		BinPath:          "/opt/fc/firecracker",
		JailerBinPath:    "/opt/fc/jailer",
		JailerEnabled:    true,
		DaemonReserveMib: 256,
		SnapshotRoot:     "/var/lib/embervm/scratch/snapshots",
		GuestOomScoreAdj: 1000,
	}
	l := newExecLauncher(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if !l.JailerEnabled || l.JailerBin != cfg.JailerBinPath || l.Bin != cfg.BinPath {
		t.Fatalf("launcher binaries/arming = %+v", l)
	}
	if l.DaemonReserveMib != 256 {
		t.Fatalf("DaemonReserveMib = %d, want the brick reserve", l.DaemonReserveMib)
	}
	if l.UIDLeaseDir != cfg.SnapshotRoot {
		t.Fatalf("UIDLeaseDir = %q, want the node-shared snapshot root %q", l.UIDLeaseDir, cfg.SnapshotRoot)
	}
	if !l.MountNamespace || l.OOMScoreAdj != 1000 {
		t.Fatalf("launcher hardening = %+v", l)
	}
}
