package driver

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// spontaneousExitProcess is an execProcess whose VMM has already died on its
// own: Wait returns at once and Kill finds nothing to signal.
func spontaneousExitProcess(vmID string) *execProcess {
	return &execProcess{
		vmID:         vmID,
		workload:     "w",
		logger:       slog.New(slog.NewTextHandler(io.Discard, nil)),
		killProcess:  func() error { return os.ErrProcessDone },
		waitProcess:  func() error { return nil },
		checkRunning: func() (bool, error) { return false, nil },
	}
}

// TestSpontaneousVMMExitStopsChargingBeforeRelease pins J14: a VMM that exits on
// its own stops counting toward LiveCount and ClaimedMib once reaped, while its
// live entry stays until Release so ownership and teardown are unchanged.
func TestSpontaneousVMMExitStopsChargingBeforeRelease(t *testing.T) {
	for _, wrapped := range []bool{false, true} {
		name := "exec process"
		if wrapped {
			name = "guest output wrapper"
		}
		t.Run(name, func(t *testing.T) {
			d := testDriver(t)
			exec := spontaneousExitProcess("vm-dead")
			var proc Process = exec
			if wrapped {
				// Production wraps every VMM for serial routing; the wrapper
				// must delegate Exited or the fix never reaches the driver.
				serial := filepath.Join(t.TempDir(), "serial.log")
				if err := os.WriteFile(serial, nil, 0o600); err != nil {
					t.Fatal(err)
				}
				var err error
				proc, err = d.routeGuestOutput(exec, serial, "w", guestPhaseVM)
				if err != nil {
					t.Fatal(err)
				}
			}
			dead := substrate.Handle{ThreadID: "t-dead", ID: "vm-dead"}
			alive := substrate.Handle{ThreadID: "t-alive", ID: "vm-alive"}
			d.track(&instance{handle: dead, proc: proc, memMib: 3072})
			d.track(&instance{handle: alive, proc: &fakeProcess{}, memMib: 1024})
			if d.LiveCount() != 2 || d.ClaimedMib() != 4096 {
				t.Fatalf("before exit LiveCount=%d ClaimedMib=%d, want 2 and 4096", d.LiveCount(), d.ClaimedMib())
			}

			// The launch watcher reaps the dead VMM.
			if err := proc.Wait(); err != nil {
				t.Fatal(err)
			}
			if d.LiveCount() != 1 || d.ClaimedMib() != 1024 {
				t.Fatalf("after exit LiveCount=%d ClaimedMib=%d, want 1 and 1024", d.LiveCount(), d.ClaimedMib())
			}
			if d.get(dead.ID) == nil {
				t.Fatal("the reaped VMM's live entry was dropped before Release")
			}
			if err := d.Release(context.Background(), dead); err != nil {
				t.Fatalf("Release of a reaped VMM: %v", err)
			}
			if d.get(dead.ID) != nil {
				t.Fatal("Release left the entry behind")
			}
			if err := d.Release(context.Background(), dead); err == nil {
				t.Fatal("a second Release of the same handle succeeded")
			}
		})
	}
}

// TestUnreapedVMMKeepsCharging keeps the Release rule: memory stays charged
// until the process is reaped and its cleanup has run.
func TestUnreapedVMMKeepsCharging(t *testing.T) {
	d := testDriver(t)
	proc := spontaneousExitProcess("vm-live")
	h := substrate.Handle{ThreadID: "t-live", ID: "vm-live"}
	d.track(&instance{handle: h, proc: proc, memMib: 512})
	if proc.Exited() {
		t.Fatal("an unwaited process reports exited")
	}
	if d.LiveCount() != 1 || d.ClaimedMib() != 512 {
		t.Fatalf("LiveCount=%d ClaimedMib=%d, want 1 and 512", d.LiveCount(), d.ClaimedMib())
	}
}

func TestJailerLaunchCountsReportsDaemonLaunchModes(t *testing.T) {
	var modes launchModeCounters
	modes.jailed.Add(3)
	modes.directExec.Add(2)
	modes.refused.Add(1)
	d := New(Config{SnapshotRoot: shortTempDir(t)}, &ExecLauncher{Bin: "/opt/fc/firecracker", JailerEnabled: true, launchModes: &modes}, nil)
	armed, jailed, directExec, refused := d.JailerLaunchCounts()
	if !armed || jailed != 3 || directExec != 2 || refused != 1 {
		t.Fatalf("JailerLaunchCounts() = %v %d %d %d, want armed 3 2 1", armed, jailed, directExec, refused)
	}
	disarmed := New(Config{SnapshotRoot: shortTempDir(t)}, &ExecLauncher{Bin: "/opt/fc/firecracker", launchModes: &modes}, nil)
	if armed, _, _, _ := disarmed.JailerLaunchCounts(); armed {
		t.Fatal("a disarmed launcher reported armed")
	}
	armed, jailed, directExec, refused = testDriver(t).JailerLaunchCounts()
	if armed || jailed != 0 || directExec != 0 || refused != 0 {
		t.Fatalf("a non-exec launcher reported %v %d %d %d", armed, jailed, directExec, refused)
	}
}

func TestPrepareJailerRejectsUnusableBinary(t *testing.T) {
	dir := t.TempDir()
	plain := filepath.Join(dir, "jailer")
	if err := os.WriteFile(plain, []byte("#!/bin/sh\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{
		"missing":        filepath.Join(dir, "absent"),
		"not executable": plain,
		"directory":      dir,
	} {
		err := PrepareJailer(path)
		if !errors.Is(err, ErrJailerContainment) {
			t.Errorf("%s: PrepareJailer(%q) = %v, want a containment error", name, path, err)
		}
	}
}
