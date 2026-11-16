package dispatch

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// gateUnsent is proof, on a volume independent of PostgreSQL, that the HTTP
// request has not been sent (or was definitely refused). Write and fsync it
// BEFORE the marker transaction. Remove and fsync it BEFORE sending HTTP.
// A database outage plus SIGKILL therefore cannot lose an unsent COMMIT's
// proof. An absent file never authorizes retry of an ambiguous HTTP request.
type gateUnsent struct {
	MessageID string
	Epoch     int64
	Send      int
}

// Pair the database writer fence with a nonblocking volume lock. A successor
// must not replay proof while a live predecessor is between COMMIT and POST.
// The kernel releases this lock even on SIGKILL; every writer uses this volume.
func (e *Engine) lockHumanGateJournal() (func(), error) {
	if e.cfg.HumanGateJournal == "" {
		return func() {}, nil
	}
	d, err := os.Open(e.cfg.HumanGateJournal)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(d.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		d.Close()
		return nil, err
	}
	return func() { d.Close() }, nil
}

func (e *Engine) gateUnsentPath(p gateUnsent) string {
	return filepath.Join(e.cfg.HumanGateJournal, fmt.Sprintf("%x.json", sha256.Sum256([]byte(p.MessageID))))
}

func (e *Engine) prepareHumanGateSend(p gateUnsent) error {
	if e.cfg.HumanGateJournal == "" {
		return errors.New("human-gate creation requires a persistent journal directory")
	}
	// The mounted directory must already exist. Never silently create an
	// ephemeral substitute when the persistent volume is missing.
	path := e.gateUnsentPath(p)
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("human-gate journal needs recovery before send: %s: %v", p.MessageID, err)
	}
	data, err := json.Marshal(p)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(e.cfg.HumanGateJournal, ".unsent-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if _, err := f.Write(data); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(f.Name(), path); err != nil {
		return err
	}
	return e.syncHumanGateDirectory()
}

func (e *Engine) forgetHumanGateSend(p gateUnsent) error {
	if err := os.Remove(e.gateUnsentPath(p)); err != nil {
		return err
	}
	return e.syncHumanGateDirectory()
}

// preserveHumanGateSend needs one durable store to acknowledge the unsent
// proof. A failed journal restore must not leave a send marker set merely
// because its caller has no file to replay. The epoch/send token prevents
// this compensation from clearing a successor's request.
func (e *Engine) preserveHumanGateSend(ctx context.Context, p gateUnsent) error {
	if e.gateUnsentPending == nil {
		e.gateUnsentPending = make(map[string]gateUnsent)
	}
	e.gateUnsentPending[p.MessageID] = p
	if err := e.prepareHumanGateSend(p); err != nil {
		// Shutdown may already have canceled the tick. Make one bounded
		// attempt; do not reintroduce a synchronous retry loop.
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), time.Second)
		defer cancel()
		if clearErr := e.st.ClearHumanGateSend(cleanup, p.MessageID, p.Epoch, p.Send); clearErr != nil {
			return fmt.Errorf("cannot persist unsent human-gate proof for %s (epoch %d, send %d): %w",
				p.MessageID, p.Epoch, p.Send, errors.Join(err, clearErr))
		}
	}
	delete(e.gateUnsentPending, p.MessageID)
	return nil
}

func (e *Engine) recoverPendingHumanGateSends(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	for key, p := range e.gateUnsentPending {
		if err := e.st.ClearHumanGateSend(ctx, p.MessageID, p.Epoch, p.Send); err != nil {
			return err
		}
		delete(e.gateUnsentPending, key)
	}
	return nil
}

func (e *Engine) syncHumanGateDirectory() error {
	if e.cfg.SyncJournalDirectory != nil {
		return e.cfg.SyncJournalDirectory(e.cfg.HumanGateJournal)
	}
	return syncGateDirectory(e.cfg.HumanGateJournal)
}

func syncGateDirectory(path string) error {
	d, err := os.Open(path)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

func (e *Engine) recoverHumanGateSends(ctx context.Context) error {
	if e.cfg.HumanGateJournal == "" {
		return nil
	}
	entries, err := os.ReadDir(e.cfg.HumanGateJournal)
	if err != nil {
		return err
	}
	// One budget for the entire pass, including a row lock or database outage.
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	for _, entry := range entries {
		if err := ctx.Err(); err != nil {
			return err
		}
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue // an interrupted preparation never reached the DB marker
		}
		path := filepath.Join(e.cfg.HumanGateJournal, entry.Name())
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		var p gateUnsent
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		if p.MessageID == "" || p.Epoch <= 0 || p.Send <= 0 || e.gateUnsentPath(p) != path {
			return fmt.Errorf("invalid human-gate journal entry: %s", entry.Name())
		}
		if err := e.st.ClearHumanGateSend(ctx, p.MessageID, p.Epoch, p.Send); err != nil {
			return err // retain proof and retry on a later tick, including cancellation
		}
		// The database has already acknowledged the proof. If removing this
		// redundant file fails, no compensation is needed before returning.
		if err := e.forgetHumanGateSend(p); err != nil {
			return err
		}
	}
	return nil
}
