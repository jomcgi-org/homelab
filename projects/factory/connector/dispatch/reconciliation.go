package dispatch

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Caller pins one projected credential for the complete observation/action
// sequence. The witness contains identity metadata only, never the credential.
type Caller interface {
	Pin(context.Context) (context.Context, kagent.CallerIdentity, error)
}

var errCallerUncertain = errors.New("dispatch: execution ownership is uncertain; restore the original caller or reconcile ownership")

func callerUncertain(err error) error {
	if fatal(err) {
		return err
	}
	return errCallerUncertain
}

// guardCaller preserves the identity that owns this work item's executions.
// A missing task is not absence under a different caller: kagent deliberately
// hides sessions belonging to another principal with NOT_FOUND.
func (e *Engine) guardCaller(ctx context.Context, wi *store.WorkItem, current *store.Attempt) (context.Context, error) {
	if err := e.guard(ctx); err != nil {
		return ctx, err
	}
	if e.cfg.Caller == nil {
		return ctx, errCallerUncertain
	}
	ctx, identity, err := e.cfg.Caller.Pin(ctx)
	if err != nil || !identity.Valid() {
		return ctx, callerUncertain(err)
	}
	key := identity.Key()
	if wi.CallerIdentity != "" {
		if wi.CallerIdentity != key {
			return ctx, errCallerUncertain
		}
		return ctx, nil
	}
	// Re-read: another run of this work item may have bound the witness.
	stored, err := e.st.GetWorkItem(ctx, wi.ID)
	if err != nil {
		return ctx, err
	}
	if stored.CallerIdentity != "" {
		wi.CallerIdentity = stored.CallerIdentity
		if wi.CallerIdentity != key {
			return ctx, errCallerUncertain
		}
		return ctx, nil
	}
	sends, err := e.st.SentAttemptsForWork(ctx, wi.ID)
	if err != nil {
		return ctx, err
	}
	var sent *store.Attempt
	if len(sends) > 0 {
		sent = sends[0]
	}
	if current == nil {
		current, err = e.st.OpenAttemptForWork(ctx, wi.ID)
		if err != nil {
			return ctx, err
		}
	}
	// Only truly never-sent work may establish an identity without evidence.
	// An unsent retry of legacy work is not a new work item.
	if sent != nil || wi.SessionID != "" {
		if e.sessions == nil {
			return ctx, errCallerUncertain
		}
		proofCtx, cancel := context.WithTimeout(ctx, time.Minute)
		defer cancel()
		proof := sent
		if current != nil && current.FirstSentAt.Unix() > 0 {
			proof = current
		}
		session := wi.SessionID
		if proof != nil {
			// A cancellation can refer to an older run after the work
			// item mapped a newer session. Prove this attempt's caller,
			// never borrow a newer session's positive ownership.
			session = proof.ContextID
		}
		if session == "" && proof != nil {
			t, err := e.taskWithMessage(proofCtx, kagent.AgentRef{Namespace: proof.KagentNamespace, Name: proof.KagentAgent}, "", proof.MessageID)
			if err != nil {
				return ctx, callerUncertain(err)
			}
			if t == nil || (current != nil && current.MessageID != proof.MessageID && !kagent.Quiescent(t.Status.State)) {
				return ctx, errCallerUncertain
			}
			session = t.ContextID
		}
		if session == "" {
			return ctx, errCallerUncertain
		}
		if err := e.guard(proofCtx); err != nil {
			return ctx, err
		}
		s, err := e.sessions.GetSession(proofCtx, session)
		if err != nil || s == nil || s.Creator != identity.PrincipalID() {
			return ctx, callerUncertain(err)
		}
		// Old code could already have retried an unknown send. A readable
		// newer session does not prove those earlier executions stopped.
		// Completed turns and confirmed cancellations with a task ID have
		// durable quiescence evidence; every other predecessor needs proof.
		for _, prior := range sends {
			if (current != nil && current.MessageID == prior.MessageID) || prior.State == store.AttemptCompleted ||
				(prior.State == store.AttemptCanceled && prior.TaskID != "") {
				continue
			}
			t, err := e.taskWithMessage(proofCtx, kagent.AgentRef{Namespace: prior.KagentNamespace, Name: prior.KagentAgent}, prior.ContextID, prior.MessageID)
			if err != nil {
				return ctx, callerUncertain(err)
			}
			if t == nil || !kagent.Quiescent(t.Status.State) {
				return ctx, errCallerUncertain
			}
			if err := e.guard(proofCtx); err != nil {
				return ctx, err
			}
			owner, err := e.sessions.GetSession(proofCtx, t.ContextID)
			if err != nil || owner == nil || owner.Creator != identity.PrincipalID() {
				return ctx, callerUncertain(err)
			}
		}
	}
	if err := e.guard(ctx); err != nil {
		return ctx, err
	}
	bound, err := e.st.BindCaller(ctx, wi.ID, key)
	if err != nil {
		return ctx, err
	}
	if !bound {
		return ctx, errCallerUncertain
	}
	wi.CallerIdentity = key
	return ctx, nil
}

// taskListCoverage treats kagent's total as a consistency witness, not a
// transaction snapshot. Its COUNT and page queries may race with inserts or
// deletion. Such a scan can still find the original task, but cannot prove
// absence; a later scan starts fresh once the visible set settles.
type taskListCoverage struct {
	total        int
	started      bool
	inconsistent bool
	ids          map[string]bool
}

func (c *taskListCoverage) observe(page *kagent.TaskPage) error {
	if page == nil || page.TotalSize < 0 || page.PageSize < 1 || page.PageSize > 100 || len(page.Tasks) > page.PageSize ||
		(page.NextPageToken != "" && len(page.Tasks) != page.PageSize) {
		return errors.New("task scan: invalid page metadata")
	}
	if !c.started {
		c.started, c.total, c.ids = true, page.TotalSize, map[string]bool{}
	} else if c.total != page.TotalSize {
		c.inconsistent = true
	}
	for _, task := range page.Tasks {
		if task == nil || task.ID == "" {
			return errors.New("task scan: invalid task listing")
		}
		if c.ids[task.ID] {
			c.inconsistent = true
		}
		c.ids[task.ID] = true
	}
	return nil
}

func (c *taskListCoverage) complete() error {
	if !c.started || c.inconsistent || len(c.ids) != c.total {
		return errors.New("task scan: inconsistent total or incomplete coverage")
	}
	return nil
}

// taskWithMessage searches every task state and page. The absence result is
// valid only after a complete successful scan under the pinned original caller.
// A listing or history error, a repeated cursor, or a bounded scan timeout is
// uncertainty, never proof that a previous send did not execute.
func (e *Engine) taskWithMessage(ctx context.Context, ref kagent.AgentRef, contextID, messageID string) (*kagent.Task, error) {
	return e.findMessage(ctx, ref, contextID, messageID, nil)
}

func (e *Engine) findMessage(ctx context.Context, ref kagent.AgentRef, contextID, messageID string, beforeRead func(context.Context, string) (context.Context, error)) (*kagent.Task, error) {
	if beforeRead == nil {
		beforeRead = func(ctx context.Context, _ string) (context.Context, error) { return ctx, e.guard(ctx) }
	}
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	token := ""
	seen := map[string]bool{}
	var cursors []*kagent.Task
	incomplete := false
	var coverage taskListCoverage
	for {
		readCtx, err := beforeRead(ctx, "list/"+token)
		if err != nil {
			return nil, err
		}
		page, err := e.tasks.ListTasks(readCtx, ref, contextID, "", token)
		if err != nil {
			return nil, fmt.Errorf("reconcile original message: list tasks: %w", err)
		}
		if err := coverage.observe(page); err != nil {
			return nil, err
		}
		tasks, next := page.Tasks, page.NextPageToken
		for _, c := range tasks {
			if c == nil || c.ID == "" || (contextID != "" && c.ContextID != contextID) {
				return nil, errors.New("reconcile original message: invalid task listing")
			}
			readCtx, err := beforeRead(ctx, "task/"+c.ID)
			if err != nil {
				return nil, err
			}
			t, err := e.tasks.GetTask(readCtx, ref, c.ID)
			if err != nil {
				return nil, fmt.Errorf("reconcile original message: read task: %w", err)
			}
			if t == nil || t.ID != c.ID || t.ContextID == "" || t.ContextID != c.ContextID {
				return nil, errors.New("reconcile original message: invalid task history")
			}
			if t.HasMessage(messageID) {
				return t, nil
			}
			// Every connector input opens a task. kagent can expose a
			// SUBMITTED placeholder for a first terminal/waiting save while
			// native cleanup still owns effects, hiding the accepted input
			// until settlement. A non-match excludes our input only when
			// that task's opening user message is public. Apply this check
			// in every state; an empty/malformed history is never absence.
			if len(t.History) == 0 || t.History[0] == nil || t.History[0].MessageID == "" ||
				t.History[0].Role != "ROLE_USER" || t.History[0].TaskID != t.ID || t.History[0].ContextID != t.ContextID {
				incomplete = true
			}
		}
		if next == "" {
			if err := coverage.complete(); err != nil {
				return nil, err
			}
			if incomplete {
				return nil, errors.New("reconcile original message: task opening input is not public")
			}
			// kagent pages by the last task's position. Deleting that
			// cursor between requests makes its next-page query return no
			// rows. Verify every boundary still exists before treating an
			// empty final page as a complete absence proof.
			for _, cursor := range cursors {
				readCtx, err := beforeRead(ctx, "task/"+cursor.ID)
				if err != nil {
					return nil, err
				}
				t, err := e.tasks.GetTask(readCtx, ref, cursor.ID)
				if err != nil {
					return nil, fmt.Errorf("reconcile original message: verify page boundary: %w", err)
				}
				if t == nil || t.ID != cursor.ID || t.ContextID != cursor.ContextID {
					return nil, errors.New("reconcile original message: invalid page boundary")
				}
			}
			return nil, nil
		}
		if len(tasks) == 0 {
			return nil, errors.New("reconcile original message: empty intermediate page")
		}
		if seen[next] {
			return nil, errors.New("reconcile original message: repeated page token")
		}
		seen[next] = true
		cursors = append(cursors, tasks[len(tasks)-1])
		token = next
	}
}
