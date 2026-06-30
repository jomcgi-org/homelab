// Package watchdog detects a wedged agent run: goose stops producing output
// while it is still supposed to be working (e.g. a model or MCP call hangs).
// The harness tees goose's stdout/stderr through Monitor, so any output re-arms
// it; Run fires onStall once when output has been silent for StallAfter. The
// clock is injectable so the decision is unit-testable without real time.
package watchdog

import (
	"context"
	"sync"
	"time"
)

type Monitor struct {
	// StallAfter is the silence window after which a run is considered wedged.
	StallAfter time.Duration

	now  func() time.Time // injectable clock; nil => time.Now
	mu   sync.Mutex
	last time.Time
	done bool // onStall already fired for the current stall
}

func (m *Monitor) clock() time.Time {
	if m.now != nil {
		return m.now()
	}
	return time.Now()
}

// Write records activity (it is an io.Writer so it can sit on the goose output
// tee) and re-arms the monitor. It never consumes or copies the bytes.
func (m *Monitor) Write(b []byte) (int, error) {
	m.mu.Lock()
	m.last = m.clock()
	m.done = false
	m.mu.Unlock()
	return len(b), nil
}

// IdleFor reports how long output has been silent. Zero before the first write.
func (m *Monitor) IdleFor() time.Duration {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.last.IsZero() {
		return 0
	}
	return m.clock().Sub(m.last)
}

// Stalled reports whether the silence window has elapsed since the last write.
func (m *Monitor) Stalled() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return !m.last.IsZero() && m.clock().Sub(m.last) >= m.StallAfter
}

// Run polls every interval and calls onStall exactly once per stall (re-armed by
// the next Write). It returns when ctx is cancelled. The first Write should
// happen before or early in the run; until then the monitor is unarmed.
func (m *Monitor) Run(ctx context.Context, interval time.Duration, onStall func()) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			m.mu.Lock()
			fire := !m.last.IsZero() && !m.done && m.clock().Sub(m.last) >= m.StallAfter
			if fire {
				m.done = true
			}
			m.mu.Unlock()
			if fire {
				onStall()
			}
		}
	}
}
