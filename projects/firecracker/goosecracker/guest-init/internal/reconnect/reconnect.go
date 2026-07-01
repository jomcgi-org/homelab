// Package reconnect re-establishes the guest's outbound connections after a
// restore. Firecracker drops TCP and vsock on resume (ADR 022 security note:
// "guest networking is re-established, not resumed"), so before the wrapper
// hands control back to the harness it must re-open the model, MCP, and git
// clients. No stale privileged channel survives a restore.
package reconnect

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// Reconnector re-establishes one client. It must be idempotent: it may be called
// on every resume.
type Reconnector struct {
	Name string
	Fn   func(ctx context.Context) error
}

// Manager runs the registered reconnectors on each wake, in registration order.
type Manager struct {
	// Attempts is the number of tries per reconnector before failing (>=1).
	Attempts int
	// Backoff is the base delay between attempts; it grows linearly per attempt.
	Backoff time.Duration
	// Sleep is injectable for tests; defaults to time.Sleep.
	Sleep func(time.Duration)

	mu           sync.Mutex
	reconnectors []Reconnector
}

// Register adds a reconnector. Order is preserved (e.g. git before the harness
// that uses it).
func (m *Manager) Register(name string, fn func(ctx context.Context) error) {
	m.mu.Lock()
	m.reconnectors = append(m.reconnectors, Reconnector{Name: name, Fn: fn})
	m.mu.Unlock()
}

func (m *Manager) sleep(d time.Duration) {
	if m.Sleep != nil {
		m.Sleep(d)
		return
	}
	time.Sleep(d)
}

// Reconnect runs every reconnector with bounded retries. It stops at the first
// reconnector that exhausts its attempts, returning that error, so the wrapper
// does not send ResumeAck (and the harness is not handed a half-live env).
func (m *Manager) Reconnect(ctx context.Context) error {
	m.mu.Lock()
	rs := make([]Reconnector, len(m.reconnectors))
	copy(rs, m.reconnectors)
	m.mu.Unlock()

	attempts := m.Attempts
	if attempts < 1 {
		attempts = 1
	}

	for _, r := range rs {
		var lastErr error
		for i := 0; i < attempts; i++ {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if err := r.Fn(ctx); err == nil {
				lastErr = nil
				break
			} else {
				lastErr = err
				if i < attempts-1 {
					m.sleep(m.Backoff * time.Duration(i+1))
				}
			}
		}
		if lastErr != nil {
			return fmt.Errorf("reconnect %q failed after %d attempts: %w", r.Name, attempts, lastErr)
		}
	}
	return nil
}
