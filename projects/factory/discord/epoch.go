package discord

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// AgentRevisions reads the revision kagent pins a new session to
// (kagent.Sessions.AgentRevision).
type AgentRevisions interface {
	AgentRevision(ctx context.Context, agent kagent.AgentRef) (string, error)
}

// epochTTL is how long RevisionEpoch reuses a revision it read. A rollout
// reaches each channel on its first turn after this.
const epochTTL = time.Minute

// RevisionEpoch derives a server agent's channel session epoch from its
// Agent's current revision (HOME-944). kagent pins a session to the
// revision that created it, and the revision covers the runner image and
// the Agent spec, so any runner or config change gives a new epoch. Each
// channel moves to a session on the new revision on its next turn:
//
//	epoch = hex(sha256(Manual + "\n" + revision))[:12]
//
// Manual is the policy's sessionEpoch: a new value moves every channel to a
// new session without a new revision.
type RevisionEpoch struct {
	Revisions AgentRevisions
	Agent     kagent.AgentRef
	Manual    string
	Log       *slog.Logger
	Now       func() time.Time

	mu     sync.Mutex
	epoch  string
	readAt time.Time
}

// errNoRevision is an Agent with no successful revision: kagent can create
// no session on it.
var errNoRevision = errors.New("discord: agent has no successful revision")

// Epoch returns the current epoch. It reads the revision at most once per
// epochTTL. A failed read keeps the last epoch; with none yet, it returns
// the error, and the turn fails as it would without kagent.
func (r *RevisionEpoch) Epoch(ctx context.Context) (string, error) {
	now := time.Now()
	if r.Now != nil {
		now = r.Now()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.epoch != "" && now.Sub(r.readAt) < epochTTL {
		return r.epoch, nil
	}
	rev, err := r.Revisions.AgentRevision(ctx, r.Agent)
	if err == nil && rev == "" {
		err = errNoRevision
	}
	if err != nil {
		if r.epoch != "" {
			if r.Log != nil {
				r.Log.WarnContext(ctx, "agent revision unreadable; keeping the session epoch", "agent", r.Agent.Namespace+"/"+r.Agent.Name, "epoch", r.epoch, "error", err)
			}
			return r.epoch, nil
		}
		return "", err
	}
	epoch := DeriveEpoch(r.Manual, rev)
	if epoch != r.epoch && r.Log != nil {
		r.Log.InfoContext(ctx, "channel session epoch", "agent", r.Agent.Namespace+"/"+r.Agent.Name, "revision", rev, "epoch", epoch)
	}
	r.epoch, r.readAt = epoch, now
	return epoch, nil
}

// DeriveEpoch is the channel session epoch of a manual epoch and an Agent
// revision: 12 lower-case hex digits, inside the sessionEpoch limits.
func DeriveEpoch(manual, revision string) string {
	sum := sha256.Sum256([]byte(manual + "\n" + revision))
	return hex.EncodeToString(sum[:])[:12]
}
