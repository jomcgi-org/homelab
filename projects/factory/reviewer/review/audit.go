package review

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	GoalAuditDirectory = "/var/lib/factory-reviewer/goal-audit"
	goalAuditRetention = 14 * 24 * time.Hour
	goalAuditSweep     = time.Hour
)

// GoalAuditRecord is private. It must never enter a check, comment or log.
// Reply holds the entire final reply before the verdict parser bounds it.
type GoalAuditRecord struct {
	Repository  string    `json:"repository"`
	PullRequest int       `json:"pull_request"`
	Head        string    `json:"head"`
	Issue       string    `json:"issue"`
	Digest      string    `json:"evidence_digest"`
	MessageID   string    `json:"message_id"`
	TaskID      string    `json:"task_id"`
	SessionID   string    `json:"session_id"`
	Phase       string    `json:"phase"`
	CreatedAt   time.Time `json:"created_at"`
	State       string    `json:"state"`
	Reply       string    `json:"reply"`
}

type GoalAudit interface {
	Save(GoalAuditRecord) error
}

// FileGoalAudit stores replies on the reviewer's private persistent volume.
// One service instance owns the directory. Files are atomic, synced writes;
// message and task IDs make retries idempotent without extending retention.
type FileGoalAudit struct {
	dir string
	mu  sync.Mutex
}

func OpenGoalAudit(dir string) (*FileGoalAudit, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		return nil, err
	}
	return &FileGoalAudit{dir: dir}, nil
}

func (s *FileGoalAudit) Save(r GoalAuditRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	sum := sha256.Sum256([]byte(r.MessageID + "\x00" + r.TaskID))
	name := filepath.Join(s.dir, hex.EncodeToString(sum[:])+".json")
	if _, err := os.Stat(name); err == nil {
		// Retry a directory sync too, if the first write failed at that step.
		return s.sync()
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	f, err := os.CreateTemp(s.dir, ".reply-*") // mode 0600
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if err := json.NewEncoder(f).Encode(r); err != nil {
		return err
	}
	// Retention follows the first receipt, including across process restarts.
	if err := os.Chtimes(f.Name(), r.CreatedAt, r.CreatedAt); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(f.Name(), name); err != nil {
		return err
	}
	return s.sync()
}

func (s *FileGoalAudit) sync() error {
	d, err := os.Open(s.dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

// Prune removes expired replies and temporary writes older than retention.
// A separate retained CronJob also calls it after a service rollback. Fresh
// temporary files may belong to an active writer; never remove those.
func (s *FileGoalAudit) Prune(now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() || (!strings.HasSuffix(entry.Name(), ".json") && !strings.HasPrefix(entry.Name(), ".reply-")) {
			continue
		}
		info, err := entry.Info()
		if errors.Is(err, os.ErrNotExist) {
			continue // Another pruner removed it.
		}
		if err != nil {
			return err
		}
		if !info.ModTime().After(now.Add(-goalAuditRetention)) {
			if err := os.Remove(filepath.Join(s.dir, entry.Name())); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
	}
	return s.sync()
}

func (s *FileGoalAudit) Run(ctx context.Context, onError func()) {
	ticker := time.NewTicker(goalAuditSweep)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			if err := s.Prune(now); err != nil {
				onError() // Never pass storage errors or reply content to logs.
			}
		}
	}
}
