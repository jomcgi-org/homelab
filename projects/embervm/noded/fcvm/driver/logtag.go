package driver

import (
	"bytes"
	"log/slog"
	"sync"
)

const (
	guestPhaseInit = "init"
	guestPhaseVM   = "vm"
	// guestLogLineBytes bounds daemon memory retained for a guest-controlled
	// unterminated line. Firecracker already rate-limits UART bytes on disk, and
	// this second bound keeps structured routing from reintroducing an in-memory
	// flood path.
	guestLogLineBytes = 64 * 1024
)

// tagWriter turns a byte stream into one structured log record per line. A
// writer belongs to one Firecracker stream or one guest serial follower. The
// mutex protects partial line and lifecycle state from concurrent writers.
type tagWriter struct {
	mu        sync.Mutex
	logger    *slog.Logger
	phase     string
	partial   []byte
	truncated bool
}

func newTagWriter(logger *slog.Logger, source, workload, phase string) *tagWriter {
	return &tagWriter{logger: logger.With(
		"source", source,
		"workload", workload,
	), phase: phase}
}

// Write implements io.Writer. slog has no write error to return, so every input
// byte is accepted even when it ends in a partial line that must wait for a later
// call. The only retained allocation is the current incomplete line.
func (w *tagWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	written := len(p)
	for len(p) > 0 {
		i := bytes.IndexByte(p, '\n')
		if i < 0 {
			w.appendPartial(p)
			break
		}

		if len(w.partial) == 0 && !w.truncated && i <= guestLogLineBytes {
			w.emit(p[:i])
		} else {
			w.appendPartial(p[:i])
			w.emit(w.partial)
			w.partial = w.partial[:0]
			w.truncated = false
		}
		p = p[i+1:]
	}
	return written, nil
}

// Flush emits a final unterminated line. execProcess calls it after cmd.Wait,
// the point at which the launcher owns positive end-of-stream evidence.
func (w *tagWriter) Flush() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if len(w.partial) == 0 {
		return
	}
	w.emit(w.partial)
	w.partial = w.partial[:0]
	w.truncated = false
}

// SetPhase closes any unterminated line under the old phase before advancing the
// lifecycle boundary.
func (w *tagWriter) SetPhase(phase string) {
	w.mu.Lock()
	if len(w.partial) > 0 {
		w.emit(w.partial)
		w.partial = w.partial[:0]
		w.truncated = false
	}
	w.phase = phase
	w.mu.Unlock()
}

func (w *tagWriter) MarkTruncated() {
	w.mu.Lock()
	w.partial = w.partial[:0]
	w.truncated = true
	w.mu.Unlock()
}

func (w *tagWriter) appendPartial(p []byte) {
	if len(p) >= guestLogLineBytes {
		w.partial = append(w.partial[:0], p[len(p)-guestLogLineBytes:]...)
		w.truncated = true
		return
	}
	if overflow := len(w.partial) + len(p) - guestLogLineBytes; overflow > 0 {
		copy(w.partial, w.partial[overflow:])
		w.partial = w.partial[:len(w.partial)-overflow]
		w.truncated = true
	}
	w.partial = append(w.partial, p...)
}

func (w *tagWriter) emit(line []byte) {
	if w.truncated {
		w.logger.Info(string(line), "phase", w.phase, "truncated", true)
		return
	}
	w.logger.Info(string(line), "phase", w.phase)
}
