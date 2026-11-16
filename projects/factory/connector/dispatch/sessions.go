package dispatch

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

const (
	// sessionSweepEvery spaces the sweeps of finished sessions.
	sessionSweepEvery = 10 * time.Minute
	// sessionQuiet keeps a session whose last attempt finished recently.
	sessionQuiet = 15 * time.Minute
	// stoppedSessionKeep is how long a stopped work item keeps its session
	// for a run that resumes it soon after the stop. A later run opens a new
	// session with the issue brief, the work branch and the PR.
	stoppedSessionKeep = time.Hour
	// maxSessionPages bounds one sweep's listing of an agent's sessions.
	maxSessionPages = 50
	// maxSessionDeletes bounds the deletes of one sweep, so a slow kagent
	// cannot hold the tick for long. Each delete runs inline in kagent.
	maxSessionDeletes = 10
)

// sweepSessions deletes the coder sessions no run will send to again
// (store.FinishedSessions). Each keeps a Substrate snapshot, about 340 MB,
// until it is deleted, and kagent expires an idle session only after its idle
// TTL (HOME-136). It deletes only sessions kagent lists for the pinned
// caller, so only the connector's own, and never with force: kagent refuses
// to delete a session during a turn. Each delete is recorded before it is
// sent and confirmed once kagent no longer has the session, so a lost
// response or a session left DELETING is deleted again.
func (e *Engine) sweepSessions(ctx context.Context) error {
	if e.sessions == nil || e.cfg.Caller == nil {
		return nil
	}
	now := e.cfg.Now()
	if !e.swept.IsZero() && now.Sub(e.swept) < sessionSweepEvery {
		return nil
	}
	e.swept = now
	ctx, identity, err := e.cfg.Caller.Pin(ctx)
	if err != nil || !identity.Valid() {
		return callerUncertain(err)
	}
	budget := maxSessionDeletes
	var errs []error
	for _, b := range e.cfg.Bindings {
		finished, err := e.st.FinishedSessions(ctx, b.Name, stoppedSessionKeep, sessionQuiet)
		if err != nil {
			return err
		}
		var agents []kagent.AgentRef
		byAgent := map[kagent.AgentRef][]store.FinishedSession{}
		for _, f := range finished {
			ref := kagent.AgentRef{Namespace: f.KagentNamespace, Name: f.KagentAgent}
			if byAgent[ref] == nil {
				agents = append(agents, ref)
			}
			byAgent[ref] = append(byAgent[ref], f)
		}
		for _, ref := range agents {
			listed, complete, err := e.callerSessions(ctx, ref, identity)
			if err != nil {
				if fatal(err) {
					return err
				}
				errs = append(errs, fmt.Errorf("list sessions of %s/%s: %w", ref.Namespace, ref.Name, err))
				continue
			}
			for _, f := range byAgent[ref] {
				if s := listed[f.ContextID]; s == nil || s.State == kagent.RuntimeDeleted {
					// A recorded delete has taken effect when a complete
					// listing by the same caller no longer shows its session.
					// kagent scopes a listing to the caller, so another
					// caller's listing proves nothing.
					if complete && f.RequestedBy != "" && f.RequestedBy == identity.Key() {
						if err := e.confirmDeletion(ctx, f.ContextID); err != nil {
							return err
						}
					}
					continue
				}
				if budget == 0 {
					return errors.Join(errs...)
				}
				budget--
				if err := e.deleteFinished(ctx, f, identity.Key()); err != nil {
					if fatal(err) {
						return err
					}
					errs = append(errs, err)
				}
			}
		}
	}
	return errors.Join(errs...)
}

// callerSessions lists the caller's sessions of agent by ID. kagent lists
// only the caller's own; a session that names another creator is skipped as
// well. complete is false for a listing cut short by maxSessionPages or a
// repeated page token: an unlisted session may then still exist. Each page
// request is journaled before it is sent (BeginStatusRead), under a message
// ID that a retry of the same request shares.
func (e *Engine) callerSessions(ctx context.Context, agent kagent.AgentRef, identity kagent.CallerIdentity) (map[string]*kagent.Session, bool, error) {
	out := map[string]*kagent.Session{}
	token := ""
	for page := 0; page < maxSessionPages; page++ {
		if err := e.BeginStatusRead(ctx, listSessionsID(agent, token)); err != nil {
			return nil, false, err
		}
		ss, next, err := e.sessions.ListSessions(ctx, agent, token)
		if err != nil {
			return nil, false, err
		}
		for _, s := range ss {
			if s != nil && s.ID != "" && (s.Creator == "" || s.Creator == identity.PrincipalID()) {
				out[s.ID] = s
			}
		}
		if next == "" {
			return out, true, nil
		}
		if next == token {
			break
		}
		token = next
	}
	return out, false, nil
}

// listSessionsID is the read journal's message ID of one ListSessions page
// request: the agent and a digest of the opaque page token.
func listSessionsID(agent kagent.AgentRef, token string) string {
	sum := sha256.Sum256([]byte(token))
	return "kagent/ListSessions/" + agent.Namespace + "/" + agent.Name + "/" + hex.EncodeToString(sum[:8])
}

// deleteFinished records the delete, releasing a session its work item
// still records in the same transaction, then sends it. A failed delete
// leaves a recorded session the work item no longer records, which the next
// sweep deletes again.
func (e *Engine) deleteFinished(ctx context.Context, f store.FinishedSession, caller string) error {
	released := !f.Current
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		if f.Current {
			var err error
			if released, err = e.st.ReleaseSession(ctx, tx, f.WorkItemID, f.ContextID); err != nil || !released {
				return err
			}
		}
		return e.st.RecordSessionDeletion(ctx, tx, f, e.w.Epoch, caller)
	})
	if err != nil || !released {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	_, err = e.sessions.DeleteSession(ctx, f.ContextID)
	switch {
	case errors.Is(err, kagent.ErrSessionNotFound):
		if err := e.confirmDeletion(ctx, f.ContextID); err != nil {
			return err
		}
	case err != nil:
		return fmt.Errorf("delete session %s of %s: %w", f.ContextID, f.IssueKey, err)
	}
	e.m.SessionsDeleted.WithLabelValues(f.Reason).Inc()
	e.log.InfoContext(ctx, "deleted a finished session", "context_id", f.ContextID, "work_item", f.WorkItemID,
		"issue", f.IssueKey, "reason", f.Reason)
	return nil
}

// confirmDeletion records that kagent no longer has the session.
func (e *Engine) confirmDeletion(ctx context.Context, id string) error {
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		return e.st.ConfirmSessionDeletion(ctx, tx, id)
	})
}
