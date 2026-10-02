package driver

import (
	"bytes"
	"log/slog"
	"sync"
	"syscall"
)

// ExitReason is an evidence-backed VMM exit classification. A guest application
// OOM leaves the VMM running and is deliberately not an exit reason.
type ExitReason string

const (
	ExitHostRequested    ExitReason = "host_requested"
	ExitHostCgroupOOM    ExitReason = "host_cgroup_oom"
	ExitGuestKernelPanic ExitReason = "guest_kernel_panic"
	ExitUnclassified     ExitReason = "unclassified"
)

type exitCgroup interface {
	OOMKilled() (bool, error)
	Path() string
	Remove() error
}

var exitReasons = [...]ExitReason{ExitHostRequested, ExitHostCgroupOOM, ExitGuestKernelPanic, ExitUnclassified}

// VMMExitCounters is a daemon-lifetime accumulator shared by restore and build
// drivers. Only the four fixed reason labels are retained, never VM/workload IDs.
type VMMExitCounters struct {
	mu     sync.Mutex
	counts [4]uint64
}

func (d *Driver) recordVMMExit(reason ExitReason) {
	d.exitCounters.mu.Lock()
	defer d.exitCounters.mu.Unlock()
	for i, known := range exitReasons {
		if reason == known {
			d.exitCounters.counts[i]++
			return
		}
	}
}

// VMMExitCounts snapshots cumulative counts since this daemon started.
// The returned copy cannot mutate the counters or add unbounded labels.
func (d *Driver) VMMExitCounts() map[string]uint64 {
	d.exitCounters.mu.Lock()
	defer d.exitCounters.mu.Unlock()
	counts := make(map[string]uint64, len(exitReasons))
	for i, reason := range exitReasons {
		counts[string(reason)] = d.exitCounters.counts[i]
	}
	return counts
}

func (p *execProcess) observeExit() {
	logger := p.logger
	if logger == nil {
		logger = slog.Default()
	}
	p.exitMu.Lock()
	hostRequested := p.hostRequested
	p.exitMu.Unlock()
	reason := ExitUnclassified
	cgroupState := "unsupported"
	// Positive cgroup OOM evidence takes precedence over every other reason.
	// Otherwise a successful host kill takes precedence; only spontaneous exits
	// can be guest panics. Missing/error evidence always stays unclassified.
	if p.cgroup != nil {
		killed, err := p.cgroup.OOMKilled()
		if err != nil {
			cgroupState = "read_error"
		} else if killed {
			cgroupState = "oom_kill"
			reason = ExitHostCgroupOOM
		} else {
			cgroupState = "no_oom_kill"
		}
	}
	serialState := "not_needed"
	if reason != ExitHostCgroupOOM {
		if hostRequested {
			reason = ExitHostRequested
		} else {
			tail, ok := serialTail(p.serialPath, serialTailBytes)
			serialState = "read_error"
			if ok {
				serialState = "no_panic_marker"
				if bytes.Contains(tail, []byte("Kernel panic - not syncing")) {
					serialState = "panic_marker"
					reason = ExitGuestKernelPanic
				}
			}
		}
	}
	attrs := []any{"vm", p.vmID, "workload", p.workload, "reason", string(reason),
		"cgroup_evidence", cgroupState, "serial_evidence", serialState}
	if p.cmd != nil && p.cmd.ProcessState != nil {
		if code := p.cmd.ProcessState.ExitCode(); code >= 0 {
			attrs = append(attrs, "exit_code", code)
		}
		if state, ok := p.cmd.ProcessState.Sys().(interface {
			Signaled() bool
			Signal() syscall.Signal
		}); ok && state.Signaled() {
			attrs = append(attrs, "signal", state.Signal().String())
		}
	}
	if p.waitErr != nil {
		attrs = append(attrs, "wait_error", p.waitErr)
	}
	logger.Info("driver: vmm exited", attrs...)
	if p.onExit != nil {
		p.onExit(reason)
	}
}
