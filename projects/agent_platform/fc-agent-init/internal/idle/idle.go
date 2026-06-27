// Package idle is the in-VM wrapper's idle detector. Only something inside the
// guest can observe the true idle condition ADR 022 requires: no activity AND
// quiescent, meaning no in-flight model or MCP call. The detector exposes that
// as a single Evaluate decision so the snapshot only ever happens at a
// between-turns boundary, never mid-call (which would corrupt an in-flight
// thread on restore).
package idle

import (
	"context"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// Detector tracks harness activity and decides when the thread is safely idle.
// It is safe for concurrent use: the harness calls Begin/End/Touch from its own
// goroutines while Run samples on a ticker.
type Detector struct {
	// IdleAfter is how long the thread must be quiescent with no activity before
	// it counts as idle.
	IdleAfter time.Duration
	// CPUIdle, if set, must return true for the thread to count as idle (the "no
	// CPU activity" half of the condition). Nil means CPU is not consulted.
	CPUIdle func() bool
	// Now returns the current time; injectable for tests. Defaults to time.Now.
	Now func() time.Time

	mu           sync.Mutex
	inFlight     int       // count of open model/MCP calls
	lastActivity time.Time // last Touch / End
	wake         vsockproto.WakeCondition
	fired        bool // idle already reported; re-armed on next activity
}

// WakeCondition the thread should be woken on once idle (defaults to manual).
func (d *Detector) wakeCondition() vsockproto.WakeCondition {
	if d.wake == "" {
		return vsockproto.WakeManual
	}
	return d.wake
}

func (d *Detector) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now()
}

// SetWakeCondition records why the thread expects to be woken (e.g. it is
// waiting on a Discord reply or a CI event). Reported with the idle signal.
func (d *Detector) SetWakeCondition(w vsockproto.WakeCondition) {
	d.mu.Lock()
	d.wake = w
	d.mu.Unlock()
}

// Begin marks the start of an in-flight model/MCP call. The thread is not
// quiescent while any call is open.
func (d *Detector) Begin() {
	d.mu.Lock()
	d.inFlight++
	d.fired = false
	d.mu.Unlock()
}

// End marks an in-flight call complete and records activity.
func (d *Detector) End() {
	d.mu.Lock()
	if d.inFlight > 0 {
		d.inFlight--
	}
	d.lastActivity = d.now()
	d.fired = false
	d.mu.Unlock()
}

// Touch records non-call activity (e.g. local work) and re-arms detection.
func (d *Detector) Touch() {
	d.mu.Lock()
	d.lastActivity = d.now()
	d.fired = false
	d.mu.Unlock()
}

// Evaluate reports whether the thread is idle right now, and the wake condition
// to attach to the signal. It returns idle=true at most once per quiescent
// stretch (re-armed by the next Begin/End/Touch), so the caller signals once.
func (d *Detector) Evaluate() (idle bool, wake vsockproto.WakeCondition) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if d.inFlight > 0 || d.fired {
		return false, ""
	}
	if d.lastActivity.IsZero() {
		// No activity ever recorded; treat first Run start as the baseline.
		d.lastActivity = d.now()
		return false, ""
	}
	if d.now().Sub(d.lastActivity) < d.IdleAfter {
		return false, ""
	}
	if d.CPUIdle != nil && !d.CPUIdle() {
		return false, ""
	}
	d.fired = true
	return true, d.wakeCondition()
}

// Run samples on an interval and calls onIdle once each time the thread enters a
// quiescent idle boundary. It returns when ctx is cancelled.
func (d *Detector) Run(ctx context.Context, sample time.Duration, onIdle func(wake vsockproto.WakeCondition)) {
	if sample <= 0 {
		sample = time.Second
	}
	ticker := time.NewTicker(sample)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if ok, wake := d.Evaluate(); ok {
				onIdle(wake)
			}
		}
	}
}
