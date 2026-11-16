package dispatch

import (
	"context"
	"database/sql"
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
	// for a run that resumes it. A later run opens a new session with the
	// issue brief.
	stoppedSessionKeep = 24 * time.Hour
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
// to delete a session during a turn. A session left DELETING is deleted
// again.
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
			listed, err := e.callerSessions(ctx, ref, identity)
			if err != nil {
				if fatal(err) {
					return err
				}
				errs = append(errs, fmt.Errorf("list sessions of %s/%s: %w", ref.Namespace, ref.Name, err))
				continue
			}
			for _, f := range byAgent[ref] {
				if s := listed[f.ContextID]; s == nil || s.State == kagent.RuntimeDeleted {
					continue
				}
				if budget == 0 {
					return errors.Join(errs...)
				}
				budget--
				if err := e.deleteFinished(ctx, f); err != nil {
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
// well. A listing cut short by maxSessionPages only leaves sessions for a
// later sweep.
func (e *Engine) callerSessions(ctx context.Context, agent kagent.AgentRef, identity kagent.CallerIdentity) (map[string]*kagent.Session, error) {
	out := map[string]*kagent.Session{}
	token := ""
	for page := 0; page < maxSessionPages; page++ {
		if err := e.guard(ctx); err != nil {
			return nil, err
		}
		ss, next, err := e.sessions.ListSessions(ctx, agent, token)
		if err != nil {
			return nil, err
		}
		for _, s := range ss {
			if s != nil && s.ID != "" && (s.Creator == "" || s.Creator == identity.PrincipalID()) {
				out[s.ID] = s
			}
		}
		if next == "" || next == token {
			break
		}
		token = next
	}
	return out, nil
}

// deleteFinished releases a session its work item still records, then
// deletes it. A failed delete leaves a session the work item no longer
// records, which the next sweep deletes.
func (e *Engine) deleteFinished(ctx context.Context, f store.FinishedSession) error {
	if f.Current {
		released := false
		err := e.w.Tx(ctx, func(tx *sql.Tx) error {
			var err error
			released, err = e.st.ReleaseSession(ctx, tx, f.WorkItemID, f.ContextID)
			return err
		})
		if err != nil || !released {
			return err
		}
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	if _, err := e.sessions.DeleteSession(ctx, f.ContextID); err != nil && !errors.Is(err, kagent.ErrSessionNotFound) {
		return fmt.Errorf("delete session %s of %s: %w", f.ContextID, f.IssueKey, err)
	}
	e.m.SessionsDeleted.WithLabelValues(f.Reason).Inc()
	e.log.InfoContext(ctx, "deleted a finished session", "context_id", f.ContextID, "work_item", f.WorkItemID,
		"issue", f.IssueKey, "reason", f.Reason)
	return nil
}
