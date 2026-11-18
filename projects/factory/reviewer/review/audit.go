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

// GoalHistory finds an earlier goal check's private reply, so the next
// head's goal check starts from its reasons. The reply stays on the private
// side: only another goal check prompt receives it.
type GoalHistory interface {
	// Reply is the newest saved reply for the PR's head that judged issue
	// on the evidence digest, or false.
	Reply(repo string, pr int, head, issue, digest string) (string, bool)
}

// maxHistoryReply bounds an earlier reply in the next goal prompt.
const maxHistoryReply = 8000

// Reply implements GoalHistory. A read error finds nothing: the goal check
// then judges the head without the earlier reasons.
func (s *FileGoalAudit) Reply(repo string, pr int, head, issue, digest string) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return "", false
	}
	var best GoalAuditRecord
	found := false
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		b, err := os.ReadFile(filepath.Join(s.dir, entry.Name()))
		if err != nil {
			continue
		}
		var r GoalAuditRecord
		if json.Unmarshal(b, &r) != nil || r.Repository != repo || r.PullRequest != pr || r.Head != head ||
			r.Issue != issue || r.Digest != digest || strings.TrimSpace(r.Reply) == "" {
			continue
		}
		if !found || r.CreatedAt.After(best.CreatedAt) {
			best, found = r, true
		}
	}
	return best.Reply, found
}

// GoalTask is the private durable index from a stable goal key to its own
// dispatch. Save the message before Send and the task ID as soon as it returns.
// Prepared means no send could have executed. Issued includes the write-ahead
// window and ambiguous sends; only read-only reconciliation can adopt its task.
type GoalTask struct {
	Key       string    `json:"key"`
	MessageID string    `json:"message_id"`
	TaskID    string    `json:"task_id,omitempty"`
	ContextID string    `json:"context_id,omitempty"`
	State     string    `json:"state,omitempty"`
	Nonce     string    `json:"nonce,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

const (
	goalPrepared = "prepared"
	goalIssued   = "issued"
	goalAccepted = "accepted"
)

type GoalTasks interface {
	LoadGoalTask(string) (*GoalTask, error)
	SaveGoalTask(GoalTask) error
	DeleteGoalTask(string) error
}

// FileGoalAudit stores replies and task indexes on the private persistent volume.
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
	return s.write(name, r, r.CreatedAt)
}

func (s *FileGoalAudit) goalTaskName(key string) string {
	sum := sha256.Sum256([]byte(key))
	// Keep the .json suffix so the retained cleanup job also prunes indexes
	// if the reviewer service is rolled back to an older release.
	return filepath.Join(s.dir, "goal-"+hex.EncodeToString(sum[:])+".json")
}

func (s *FileGoalAudit) LoadGoalTask(key string) (*GoalTask, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := os.ReadFile(s.goalTaskName(key))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var r GoalTask
	if err := json.Unmarshal(data, &r); err != nil {
		return nil, err
	}
	if r.Key != key || r.MessageID == "" || r.CreatedAt.IsZero() {
		return nil, errors.New("invalid goal task index")
	}
	switch r.State {
	case "": // Legacy indexes are only recovered, never treated as prepared.
	case goalPrepared, goalIssued, goalAccepted:
		if len(r.Nonce) != 32 {
			return nil, errors.New("invalid goal dispatch nonce")
		}
	default:
		return nil, errors.New("invalid goal dispatch state")
	}
	return &r, nil
}

func (s *FileGoalAudit) SaveGoalTask(r GoalTask) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.write(s.goalTaskName(r.Key), r, r.CreatedAt)
}

func (s *FileGoalAudit) DeleteGoalTask(key string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.Remove(s.goalTaskName(key)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return s.sync()
}

// Caller holds mu. The index and replies use the same atomic, private writes.
func (s *FileGoalAudit) write(name string, r any, createdAt time.Time) error {
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
	if err := os.Chtimes(f.Name(), createdAt, createdAt); err != nil {
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

// Prune removes expired replies, task indexes and temporary writes.
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
