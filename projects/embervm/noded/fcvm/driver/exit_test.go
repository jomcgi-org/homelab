package driver

import (
	"bytes"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type fakeExitCgroup struct {
	killed  bool
	err     error
	removed bool
}

func (c *fakeExitCgroup) OOMKilled() (bool, error) {
	if c.removed {
		panic("OOM evidence read after removal")
	}
	return c.killed, c.err
}
func (c *fakeExitCgroup) Path() string  { return "fake-cgroup" }
func (c *fakeExitCgroup) Remove() error { c.removed = true; return nil }

func TestVMMExitClassification(t *testing.T) {
	for _, tc := range []struct {
		name          string
		killErr       error
		alreadyExited bool
		cgroup        *fakeExitCgroup
		serial        string
		missingSerial bool
		want          ExitReason
		cgroupState   string
		serialState   string
	}{
		{name: "running host kill", want: ExitHostRequested, cgroupState: "unsupported", serialState: "not_needed"},
		{name: "already exited panic", killErr: os.ErrProcessDone, serial: "Kernel panic - not syncing: fatal", want: ExitGuestKernelPanic, cgroupState: "unsupported", serialState: "panic_marker"},
		{name: "already reaped panic", alreadyExited: true, serial: "Kernel panic - not syncing: fatal", want: ExitGuestKernelPanic, cgroupState: "unsupported", serialState: "panic_marker"},
		{name: "already exited no evidence", killErr: os.ErrProcessDone, want: ExitUnclassified, cgroupState: "unsupported", serialState: "no_panic_marker"},
		{name: "missing serial", killErr: os.ErrProcessDone, missingSerial: true, want: ExitUnclassified, cgroupState: "unsupported", serialState: "read_error"},
		{name: "unknown serial", killErr: os.ErrProcessDone, serial: "Out of memory: Killed process 42", want: ExitUnclassified, cgroupState: "unsupported", serialState: "no_panic_marker"},
		{name: "cgroup OOM precedence over panic and host kill", cgroup: &fakeExitCgroup{killed: true}, serial: "Kernel panic - not syncing", want: ExitHostCgroupOOM, cgroupState: "oom_kill", serialState: "not_needed"},
		{name: "cgroup read error is not OOM", killErr: os.ErrProcessDone, cgroup: &fakeExitCgroup{killed: true, err: errors.New("read failed")}, want: ExitUnclassified, cgroupState: "read_error", serialState: "no_panic_marker"},
		{name: "cgroup read error with independent panic", killErr: os.ErrProcessDone, cgroup: &fakeExitCgroup{err: errors.New("read failed")}, serial: "Kernel panic - not syncing", want: ExitGuestKernelPanic, cgroupState: "read_error", serialState: "panic_marker"},
		{name: "cgroup zero", killErr: os.ErrProcessDone, cgroup: &fakeExitCgroup{}, want: ExitUnclassified, cgroupState: "no_oom_kill", serialState: "no_panic_marker"},
		{name: "failed host kill", killErr: errors.New("permission denied"), want: ExitUnclassified, cgroupState: "unsupported", serialState: "no_panic_marker"},
		{name: "host kill ignores panic marker", serial: "Kernel panic - not syncing", want: ExitHostRequested, cgroupState: "unsupported", serialState: "not_needed"},
		{name: "panic outside bounded tail", killErr: os.ErrProcessDone, serial: "Kernel panic - not syncing" + strings.Repeat("x", serialTailBytes), want: ExitUnclassified, cgroupState: "unsupported", serialState: "no_panic_marker"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "serial.log")
			if !tc.missingSerial {
				if err := os.WriteFile(path, []byte(tc.serial), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			var logs bytes.Buffer
			var calls, waits, releases int
			p := &execProcess{
				vmID: "host-vm", workload: "host-workload", serialPath: path,
				logger:      slog.New(slog.NewJSONHandler(&logs, nil)),
				killProcess: func() error { return tc.killErr },
				waitProcess: func() error { waits++; return nil },
				releaseID:   func() { releases++ },
				onExit: func(reason ExitReason) {
					calls++
					if reason != tc.want {
						t.Errorf("reason = %q, want %q", reason, tc.want)
					}
				},
			}
			if tc.cgroup != nil {
				p.cgroup = tc.cgroup
			}
			if tc.alreadyExited {
				_ = p.Wait()
			}
			if err := p.Kill(); tc.killErr != nil && !errors.Is(tc.killErr, os.ErrProcessDone) && !errors.Is(err, tc.killErr) {
				t.Fatalf("Kill error = %v", err)
			}
			_ = p.Wait()
			_ = p.Kill()
			if calls != 1 || waits != 1 || releases != 1 {
				t.Fatalf("calls=%d waits=%d releases=%d, want one each", calls, waits, releases)
			}
			var event map[string]any
			if err := json.Unmarshal(logs.Bytes(), &event); err != nil {
				t.Fatal(err)
			}
			for key, want := range map[string]string{"msg": "driver: vmm exited", "vm": "host-vm", "workload": "host-workload", "reason": string(tc.want), "cgroup_evidence": tc.cgroupState, "serial_evidence": tc.serialState} {
				if event[key] != want {
					t.Errorf("%s = %v, want %s", key, event[key], want)
				}
			}
		})
	}
}

func TestVMMExitWatcherReportsBeforeRelease(t *testing.T) {
	done := make(chan struct{})
	observed := make(chan ExitReason, 1)
	var waits atomic.Int32
	p := &execProcess{
		waitProcess: func() error { waits.Add(1); <-done; return errors.New("unexpected wait error") },
		killProcess: func() error { return os.ErrProcessDone },
		logger:      slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil)),
		onExit:      func(reason ExitReason) { observed <- reason },
	}
	go func() { _ = p.Wait() }()
	close(done)
	select {
	case reason := <-observed:
		if reason != ExitUnclassified {
			t.Fatalf("unexpected death = %s", reason)
		}
	case <-time.After(time.Second):
		t.Fatal("unexpected exit was not observed before Release")
	}
	_ = p.Kill()
	_ = p.Wait()
	if waits.Load() != 1 {
		t.Fatalf("reaped %d times", waits.Load())
	}
}

func TestVMMExitConcurrentKillAndWaitReapOnce(t *testing.T) {
	done := make(chan struct{})
	var killOnce sync.Once
	var waits, events atomic.Int32
	p := &execProcess{
		waitProcess: func() error { waits.Add(1); <-done; return nil },
		killProcess: func() error { killOnce.Do(func() { close(done) }); return nil },
		logger:      slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil)),
		onExit:      func(ExitReason) { events.Add(1) },
	}
	var workers sync.WaitGroup
	for range 10 {
		workers.Go(func() { _ = p.Wait() })
		workers.Go(func() { _ = p.Kill() })
	}
	workers.Wait()
	if waits.Load() != 1 || events.Load() != 1 {
		t.Fatalf("waits=%d events=%d, want one each", waits.Load(), events.Load())
	}
}

func TestVMMExitCountsSharedAcrossBuildAndRestoreDrivers(t *testing.T) {
	counters := &VMMExitCounters{}
	restore := New(Config{ExitCounters: counters}, nil, nil)
	build := New(Config{ExitCounters: counters}, nil, nil)
	restore.recordVMMExit(ExitHostCgroupOOM)
	build.recordVMMExit(ExitHostCgroupOOM)
	build.recordVMMExit(ExitHostRequested)
	counts := restore.VMMExitCounts()
	if counts[string(ExitHostCgroupOOM)] != 2 || counts[string(ExitHostRequested)] != 1 {
		t.Fatalf("shared counts = %v", counts)
	}
}

func TestVMMExitCountsBoundedAndCopied(t *testing.T) {
	d := New(Config{}, nil, nil)
	for _, reason := range exitReasons {
		d.recordVMMExit(reason)
	}
	d.recordVMMExit(ExitReason("guest-controlled-label"))
	counts := d.VMMExitCounts()
	if len(counts) != 4 {
		t.Fatalf("%d labels, want 4", len(counts))
	}
	for _, reason := range exitReasons {
		if counts[string(reason)] != 1 {
			t.Fatalf("%s count = %d", reason, counts[string(reason)])
		}
	}
	counts[string(ExitUnclassified)] = 99
	if d.VMMExitCounts()[string(ExitUnclassified)] != 1 {
		t.Fatal("returned counts alias driver state")
	}
}
