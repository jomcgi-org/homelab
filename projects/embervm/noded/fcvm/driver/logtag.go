package driver

import (
	"bytes"
	"log/slog"
	"sync"
)

const (
	guestPhaseInit = "init"
	guestPhaseVM   = "vm"
)

// tagWriter turns a byte stream into one structured guest log record per line.
// A writer belongs to one stdout or stderr stream. The mutex protects partial
// line state because exec.Cmd and test muxes may call Write concurrently.
type tagWriter struct {
	mu      sync.Mutex
	logger  *slog.Logger
	partial []byte
}

func newTagWriter(logger *slog.Logger, workload, phase string) *tagWriter {
	return &tagWriter{logger: logger.With(
		"source", "guest",
		"workload", workload,
		"phase", phase,
	)}
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
			w.partial = append(w.partial, p...)
			break
		}

		if len(w.partial) == 0 {
			w.emit(p[:i])
		} else {
			w.partial = append(w.partial, p[:i]...)
			w.emit(w.partial)
			w.partial = w.partial[:0]
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
}

func (w *tagWriter) emit(line []byte) {
	w.logger.Info(string(line))
}
