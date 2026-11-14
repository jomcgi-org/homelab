// Package dispatch is the connector's dispatch loop: it claims itsaplan runs,
// sends them to kagent in each work item's session, keeps their leases,
// reports progress and results, retries engineering failures within a bound,
// blocks on credential and quota failures, and cancels kagent tasks until
// kagent confirms. One writer runs it, fenced by a Postgres advisory lock
// and an epoch that every external side effect checks first.
package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/metrics"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Itsaplan is the subset of the itsaplan client dispatch uses.
type Itsaplan interface {
	Claim(ctx context.Context, agent string) (*itsaplan.Run, error)
	Heartbeat(ctx context.Context, agent string, runID int64, attempt int) error
	Events(ctx context.Context, agent string, runID int64, attempt, offset int, events []itsaplan.Event) (int, error)
	Result(ctx context.Context, agent string, runID int64, r itsaplan.Result) error
	GetRun(ctx context.Context, runID int64) (*itsaplan.RunStatus, error)
	Comment(ctx context.Context, issueID int64, body string) error
}

// Tasks is the subset of the kagent A2A client dispatch uses.
type Tasks interface {
	Send(ctx context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error)
	GetTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
	ListTasks(ctx context.Context, agent kagent.AgentRef, contextID, pageToken string) ([]*kagent.Task, string, error)
	CancelTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
}

// Sessions is the subset of the kagent session client dispatch uses.
type Sessions interface {
	GetSession(ctx context.Context, id string) (*kagent.Session, error)
	RevertSession(ctx context.Context, id string) (*kagent.Session, error)
}

// Agent is an itsaplan agent key the connector claims runs for.
type Agent struct {
	Key         string
	Concurrency int
}

// Binding is a repository binding and the agent keys that may deliver it.
type Binding struct {
	store.Binding
	Agents []string
}

// Config holds dispatch settings.
type Config struct {
	Holder    string
	Agents    []Agent
	Bindings  []Binding
	Heartbeat time.Duration // itsaplan lease heartbeat, 60 s
	// DispatchWindow bounds how long kagent may still accept a send whose
	// response was lost: its dispatch reservation expires after two minutes
	// and late first saves are rejected. After it, a message that no task
	// holds was never accepted.
	DispatchWindow time.Duration
	Now            func() time.Time
}

// Engine runs dispatch.
type Engine struct {
	cfg      Config
	st       *store.Store
	its      Itsaplan
	tasks    Tasks
	sessions Sessions
	m        *metrics.Metrics
	log      *slog.Logger
	w        *store.Writer
}

// New builds an engine.
func New(cfg Config, st *store.Store, its Itsaplan, tasks Tasks, sessions Sessions, m *metrics.Metrics, log *slog.Logger) *Engine {
	if cfg.Heartbeat == 0 {
		cfg.Heartbeat = 60 * time.Second
	}
	if cfg.DispatchWindow == 0 {
		cfg.DispatchWindow = 150 * time.Second
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if m == nil {
		m = metrics.New(nil)
	}
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	return &Engine{cfg: cfg, st: st, its: its, tasks: tasks, sessions: sessions, m: m, log: log}
}

// Acquire tries to become the writer and records the bindings. It returns
// false while another process holds the lock.
func (e *Engine) Acquire(ctx context.Context) (bool, error) {
	w, err := e.st.TryAcquireWriter(ctx, e.cfg.Holder)
	if err != nil || w == nil {
		return false, err
	}
	e.w = w
	e.m.WriterEpoch.Set(float64(w.Epoch))
	for _, b := range e.cfg.Bindings {
		if err := e.st.UpsertBinding(ctx, b.Binding); err != nil {
			return false, err
		}
	}
	return true, nil
}

// Epoch is the writer epoch held, 0 before Acquire.
func (e *Engine) Epoch() int64 {
	if e.w == nil {
		return 0
	}
	return e.w.Epoch
}

// Release drops the writer lock.
func (e *Engine) Release() {
	e.w.Release()
	e.m.WriterEpoch.Set(0)
}

// guard checks the writer epoch. Every external side effect calls it first.
func (e *Engine) guard(ctx context.Context) error {
	return e.w.Check(ctx)
}

// guardAttempt also checks that the attempt was sent under the work item's
// current epoch: a reassignment fences the previous agent's attempt.
func (e *Engine) guardAttempt(ctx context.Context, a *store.Attempt, wi *store.WorkItem) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	if a.WorkItemEpoch != wi.Epoch {
		return errFenced
	}
	return nil
}

var errFenced = errors.New("dispatch: attempt fenced by a newer work item epoch")

// Tick runs one dispatch pass. It returns store.ErrNotWriter once another
// process has taken over; the caller must stop.
func (e *Engine) Tick(ctx context.Context) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	phases := []struct {
		name string
		fn   func(context.Context) error
	}{
		{"claims", e.claim},
		{"runs", e.stepRuns},
		{"cancellations", e.stepCancellations},
		{"outbox", e.flushOutbox},
	}
	for _, p := range phases {
		if err := p.fn(ctx); err != nil {
			if errors.Is(err, store.ErrNotWriter) {
				return err
			}
			e.m.TickErrors.WithLabelValues(p.name).Inc()
			e.log.ErrorContext(ctx, "dispatch phase failed", "phase", p.name, "error", err)
		}
	}
	return nil
}

func (e *Engine) stepRuns(ctx context.Context) error {
	runs, err := e.st.OpenRuns(ctx)
	if err != nil {
		return err
	}
	var errs []error
	for _, r := range runs {
		if err := e.step(ctx, r); err != nil {
			if errors.Is(err, store.ErrNotWriter) {
				return err
			}
			errs = append(errs, fmt.Errorf("run %d: %w", r.ID, err))
		}
	}
	return errors.Join(errs...)
}

// step advances one open run.
func (e *Engine) step(ctx context.Context, r *store.Run) error {
	switch r.State {
	case store.RunClaimed:
		if gone, err := e.heartbeat(ctx, r); err != nil || gone {
			return err
		}
		return e.dispatch(ctx, r)
	case store.RunWaiting:
		if gone, err := e.heartbeat(ctx, r); err != nil || gone {
			return err
		}
		return e.resumeWaiting(ctx, r)
	case store.RunDispatched:
		if gone, err := e.heartbeat(ctx, r); err != nil || gone {
			return err
		}
		return e.progress(ctx, r)
	case store.RunFinishing:
		_, err := e.heartbeat(ctx, r)
		return err
	case store.RunStale:
		return e.checkStale(ctx, r)
	}
	return nil
}

// heartbeat extends the lease when due. gone reports that the run left this
// attempt, and its state has been updated.
func (e *Engine) heartbeat(ctx context.Context, r *store.Run) (gone bool, err error) {
	now := e.cfg.Now()
	if now.Sub(r.LastHeartbeatAt) < e.cfg.Heartbeat {
		return false, nil
	}
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	err = e.its.Heartbeat(ctx, r.AgentKey, r.ID, r.Attempt)
	switch {
	case err == nil:
		e.m.Heartbeats.WithLabelValues("ok").Inc()
		return false, e.st.SetHeartbeat(ctx, r.ID, now)
	case errors.Is(err, itsaplan.ErrNotFound):
		e.m.Heartbeats.WithLabelValues("not_found").Inc()
		return e.runGone(ctx, r)
	default:
		e.m.Heartbeats.WithLabelValues("error").Inc()
		return false, err
	}
}

// runGone reads a run whose attempt itsaplan no longer accepts. A canceled
// run starts cancellation; a higher attempt makes this one stale.
func (e *Engine) runGone(ctx context.Context, r *store.Run) (bool, error) {
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	st, err := e.its.GetRun(ctx, r.ID)
	switch {
	case errors.Is(err, itsaplan.ErrNotFound):
		// The queue hides a run whose issue moved to another project, which is
		// how itsaplan v1.10.0 cancels a started run.
		return true, e.startCancel(ctx, r, "run not visible in the queue")
	case err != nil:
		return false, err
	}
	switch {
	case st.Status == "canceled":
		return true, e.startCancel(ctx, r, "run canceled")
	case st.Status == "pending" && st.Attempts > r.Attempt:
		e.log.InfoContext(ctx, "attempt is stale", "run", r.ID, "attempt", r.Attempt, "current", st.Attempts)
		return true, e.st.MarkStale(ctx, r.ID, r.Attempt)
	case st.Status != "pending":
		return true, e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunClosed, "finished in itsaplan: "+st.Status)
	}
	// Pending at this attempt but not accepting it: in retry backoff or not yet
	// executing. Try again next pass.
	return false, nil
}

// checkStale closes a stale run once itsaplan has finished it. A redelivery
// claim at the newer attempt resumes it instead.
func (e *Engine) checkStale(ctx context.Context, r *store.Run) error {
	now := e.cfg.Now()
	if now.Sub(r.LastHeartbeatAt) < e.cfg.Heartbeat {
		return nil
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	st, err := e.its.GetRun(ctx, r.ID)
	if err != nil && !errors.Is(err, itsaplan.ErrNotFound) {
		return err
	}
	if err := e.st.SetHeartbeat(ctx, r.ID, now); err != nil {
		return err
	}
	if st == nil || st.Status != "pending" {
		return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunClosed, "stale attempt; run finished in itsaplan")
	}
	return nil
}

// startCancel records a cancellation for the run's open attempt; a run that
// sent nothing closes at once.
func (e *Engine) startCancel(ctx context.Context, r *store.Run, why string) error {
	a, err := e.st.LatestAttempt(ctx, r.ID)
	if err != nil {
		return err
	}
	if a == nil || !a.Open() || a.State == store.AttemptSending {
		if a != nil && a.State == store.AttemptSending {
			if err := e.st.FinishAttempt(ctx, e.st.DB, a.ID, store.AttemptCanceled, ClassCanceled, why); err != nil {
				return err
			}
		}
		return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunClosed, why)
	}
	if err := e.st.RequestCancellation(ctx, a.ID, r.ID, store.CancelRunCanceled); err != nil {
		return err
	}
	e.m.Cancellations.WithLabelValues(store.CancelRunCanceled, "requested").Inc()
	return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunCanceling, "")
}

func (e *Engine) bindingFor(r *store.Run) (*Binding, error) {
	project := r.IssueKey
	if i := strings.LastIndex(project, "-"); i > 0 {
		project = project[:i]
	}
	for i := range e.cfg.Bindings {
		b := &e.cfg.Bindings[i]
		if b.ItsaplanProject != project {
			continue
		}
		for _, a := range b.Agents {
			if a == r.AgentKey {
				return b, nil
			}
		}
		return nil, fmt.Errorf("agent %s is not bound to project %s", r.AgentKey, project)
	}
	return nil, fmt.Errorf("no repository binding for project %q", project)
}

func (e *Engine) binding(name string) *Binding {
	for i := range e.cfg.Bindings {
		if e.cfg.Bindings[i].Name == name {
			return &e.cfg.Bindings[i]
		}
	}
	return nil
}

// failRun queues a failed result for a run that cannot be delivered.
func (e *Engine) failRun(ctx context.Context, r *store.Run, msg string) error {
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.Enqueue(ctx, tx, store.OutboxResult, resultKey(r.ID), r.ID, r.AgentKey,
			itsaplan.Result{Status: "failed", Error: clip(msg, maxErrorText)}); err != nil {
			return err
		}
		return e.st.SetRunState(ctx, tx, r.ID, store.RunFinishing, "")
	})
}

// dispatch resolves a claimed run's work item and sends it, or waits for
// the work item's previous task.
func (e *Engine) dispatch(ctx context.Context, r *store.Run) error {
	if r.IssueID == 0 {
		return e.failRun(ctx, r, "factory runs must belong to an issue")
	}
	b, err := e.bindingFor(r)
	if err != nil {
		return e.failRun(ctx, r, err.Error())
	}
	wi, reassigned, err := e.st.ResolveWorkItem(ctx, r.ID, store.NewWorkItem{
		Binding: b.Binding, IssueID: r.IssueID, IssueKey: r.IssueKey, AgentKey: r.AgentKey,
	})
	if err != nil {
		return err
	}
	r.WorkItemID = wi.ID
	prev, err := e.st.OpenAttemptForWork(ctx, wi.ID)
	if err != nil {
		return err
	}
	if prev != nil && prev.RunID == r.ID {
		prev = nil
	}
	if reassigned {
		e.m.Reassignments.Inc()
		e.log.InfoContext(ctx, "work item reassigned", "work_item", wi.ID, "run", r.ID, "epoch", wi.Epoch)
		if prev != nil {
			// The previous agent's task must stop before this agent continues the
			// session; its epoch is already fenced.
			if err := e.st.RequestCancellation(ctx, prev.ID, prev.RunID, store.CancelReassignment); err != nil {
				return err
			}
			e.m.Cancellations.WithLabelValues(store.CancelReassignment, "requested").Inc()
		}
	}
	if prev != nil {
		return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunWaiting, "")
	}
	pending, err := e.st.HasPendingCancellation(ctx, wi.ID)
	if err != nil {
		return err
	}
	if pending {
		return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunWaiting, "")
	}
	if reassigned {
		busy, err := e.sessionBusy(ctx, wi)
		if err != nil {
			return err
		}
		if busy {
			return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunWaiting, "")
		}
	}
	return e.send(ctx, r, wi, b, 0)
}

// sessionBusy confirms with kagent that the work item's session has no
// running task, asking kagent to cancel any it finds. kagent admits one
// non-quiescent task per session.
func (e *Engine) sessionBusy(ctx context.Context, wi *store.WorkItem) (bool, error) {
	if wi.SessionID == "" {
		return false, nil
	}
	ref := agentRef(wi)
	busy := false
	token := ""
	for {
		if err := e.guard(ctx); err != nil {
			return false, err
		}
		tasks, next, err := e.tasks.ListTasks(ctx, ref, wi.SessionID, token)
		if errors.Is(err, kagent.ErrTaskNotFound) {
			return false, nil
		}
		if err != nil {
			return false, err
		}
		for _, t := range tasks {
			if kagent.Quiescent(t.Status.State) {
				continue
			}
			busy = true
			if err := e.guard(ctx); err != nil {
				return false, err
			}
			if _, err := e.tasks.CancelTask(ctx, ref, t.ID); err != nil {
				e.log.WarnContext(ctx, "cancel request failed", "task", t.ID, "error", err)
			}
		}
		if next == "" {
			return busy, nil
		}
		token = next
	}
}

// resumeWaiting sends a waiting run once the work item has no running task
// and no pending cancellation.
func (e *Engine) resumeWaiting(ctx context.Context, r *store.Run) error {
	wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
	if err != nil {
		return err
	}
	pending, err := e.st.HasPendingCancellation(ctx, wi.ID)
	if err != nil || pending {
		return err
	}
	prev, err := e.st.OpenAttemptForWork(ctx, wi.ID)
	if err != nil {
		return err
	}
	if prev != nil && prev.RunID != r.ID {
		return nil
	}
	b := e.binding(wi.BindingName)
	if b == nil {
		return e.failRun(ctx, r, "repository binding "+wi.BindingName+" is no longer configured")
	}
	busy, err := e.sessionBusy(ctx, wi)
	if err != nil || busy {
		return err
	}
	return e.send(ctx, r, wi, b, 0)
}

func messageID(runID int64, decision string) string {
	return "itsaplan-run/" + strconv.FormatInt(runID, 10) + "/" + decision
}

func resultKey(runID int64) string { return "result/" + strconv.FormatInt(runID, 10) }

func agentRef(wi *store.WorkItem) kagent.AgentRef {
	return kagent.AgentRef{Namespace: wi.KagentNamespace, Name: wi.KagentAgent}
}

// taskText frames the run for the agent. Repository and branch come from the
// binding, never from the prompt.
func taskText(r *store.Run, wi *store.WorkItem, b *Binding) string {
	var s strings.Builder
	if r.SystemPrompt != "" {
		s.WriteString(r.SystemPrompt)
		s.WriteString("\n\n")
	}
	s.WriteString(r.Prompt)
	fmt.Fprintf(&s, "\n\n## Factory delivery\nRepository: %s/%s\nBase branch: %s\nWork branch: %s\n",
		b.Owner, b.Repo, b.BaseBranch, wi.Branch)
	return s.String()
}

// send records a delivery attempt and sends it. The attempt and its message
// ID are written before the send, so a lost response is reconciled by
// looking the message up rather than by sending again. The decision ID says
// why: the work item's initial send, a revision in its session, an
// engineering retry (retry > 0) or a replacement of a lost session.
func (e *Engine) send(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, retry int) error {
	if wi.SessionID != "" {
		ok, err := e.prepareSession(ctx, r, wi)
		if err != nil || !ok {
			return err
		}
	}
	kind, decision := "initial", "initial"
	switch {
	case wi.SessionID != "" && retry > 0:
		kind, decision = "continuation", "retry-"+strconv.Itoa(retry)
	case wi.SessionID != "":
		kind, decision = "continuation", "revision"
	case wi.SessionGeneration > 0 || retry > 0:
		decision = "replacement-" + strconv.Itoa(wi.SessionGeneration) + "-" + strconv.Itoa(retry)
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	a, err := e.st.RecordAttempt(ctx, e.st.DB, store.Attempt{
		RunID: r.ID, RunAttempt: r.Attempt, WorkItemEpoch: wi.Epoch, DecisionID: decision, Kind: kind,
		MessageID: messageID(r.ID, decision), KagentNamespace: wi.KagentNamespace, KagentAgent: wi.KagentAgent,
		ContextID: wi.SessionID, WriterEpoch: e.w.Epoch,
	})
	if err != nil {
		return err
	}
	if err := e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunDispatched, ""); err != nil {
		return err
	}
	return e.issue(ctx, r, wi, b, a)
}

// issue sends a recorded attempt that kagent has not accepted.
func (e *Engine) issue(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) error {
	if err := e.guardAttempt(ctx, a, wi); err != nil {
		return err
	}
	// Mark the send issued before making it: after this, only a lookup by
	// message ID may decide whether kagent holds it.
	if err := e.st.MarkSent(ctx, a.ID, e.cfg.Now(), store.AttemptAmbiguous); err != nil {
		return err
	}
	task, err := e.tasks.Send(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, kagent.Message{
		MessageID: a.MessageID,
		ContextID: a.ContextID,
		Parts:     []kagent.Part{{Text: taskText(r, wi, b)}},
		Metadata: map[string]any{
			"factory.jomcgi.dev/run":        r.ID,
			"factory.jomcgi.dev/issue":      wi.IssueKey,
			"factory.jomcgi.dev/repository": b.Owner + "/" + b.Repo,
			"factory.jomcgi.dev/branch":     wi.Branch,
		},
	})
	switch {
	case err == nil:
		e.m.Sends.WithLabelValues(a.Kind, "accepted").Inc()
		return e.attach(ctx, a, wi, task)
	case errors.Is(err, kagent.ErrNotAccepted):
		// Nothing was dispatched: the same message goes again next pass.
		e.m.Sends.WithLabelValues(a.Kind, "not_accepted").Inc()
		_, err := e.st.DB.ExecContext(ctx,
			`UPDATE delivery_attempt SET state = 'sending', sent_at = NULL WHERE id = $1 AND state = 'ambiguous'`, a.ID)
		return err
	case errors.Is(err, kagent.ErrTaskNotFound) && a.Kind == "initial":
		e.m.Sends.WithLabelValues(a.Kind, "not_found").Inc()
		if err := e.st.FinishAttempt(ctx, e.st.DB, a.ID, store.AttemptFailed, "", "kagent agent not found"); err != nil {
			return err
		}
		return e.failRun(ctx, r, fmt.Sprintf("kagent agent %s/%s not found", a.KagentNamespace, a.KagentAgent))
	case errors.Is(err, kagent.ErrTaskNotFound):
		// The context expired or was deleted: replace the session next pass.
		e.m.Sends.WithLabelValues(a.Kind, "not_found").Inc()
		if err := e.st.FinishAttempt(ctx, e.st.DB, a.ID, store.AttemptFailed, "", "session not found"); err != nil {
			return err
		}
		if err := e.st.ClearSession(ctx, wi.ID); err != nil {
			return err
		}
		return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunClaimed, "")
	default:
		e.m.Sends.WithLabelValues(a.Kind, "ambiguous").Inc()
		e.log.WarnContext(ctx, "send outcome unknown; reconciling by message ID", "run", r.ID, "message_id", a.MessageID, "error", err)
		return nil
	}
}

// attach records the task holding an attempt's message.
func (e *Engine) attach(ctx context.Context, a *store.Attempt, wi *store.WorkItem, t *kagent.Task) error {
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.MarkAccepted(ctx, tx, a.ID, t.ContextID, t.ID); err != nil {
			return err
		}
		a.State, a.ContextID, a.TaskID = store.AttemptAccepted, t.ContextID, t.ID
		if wi.Epoch == a.WorkItemEpoch {
			return e.st.SetSession(ctx, tx, wi.ID, t.ContextID)
		}
		return nil
	})
}

// prepareSession checks a work item's session before a continuation. A
// crashed runtime is reverted; a deleted or expired session is cleared so
// the send starts a replacement. ok is false while a revert is pending.
func (e *Engine) prepareSession(ctx context.Context, r *store.Run, wi *store.WorkItem) (bool, error) {
	if e.sessions == nil {
		return true, nil
	}
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	s, err := e.sessions.GetSession(ctx, wi.SessionID)
	switch {
	case errors.Is(err, kagent.ErrSessionNotFound):
		return true, e.replaceSession(ctx, r, wi, "expired or deleted")
	case err != nil:
		return false, err
	}
	switch {
	case s.Gone():
		return true, e.replaceSession(ctx, r, wi, "deleted")
	case s.Crashed():
		if err := e.guard(ctx); err != nil {
			return false, err
		}
		s, err = e.sessions.RevertSession(ctx, wi.SessionID)
		if err != nil {
			// Retry the revert next pass; nothing retries it in the background.
			return false, fmt.Errorf("revert session: %w", err)
		}
		if s.Crashed() {
			return false, nil
		}
	}
	return true, nil
}

func (e *Engine) replaceSession(ctx context.Context, r *store.Run, wi *store.WorkItem, why string) error {
	if err := e.st.ClearSession(ctx, wi.ID); err != nil {
		return err
	}
	old := wi.SessionID
	wi.SessionID = ""
	return e.st.Enqueue(ctx, e.st.DB, store.OutboxComment,
		fmt.Sprintf("comment/session-replaced/%d/%d", wi.ID, wi.SessionGeneration), r.ID, "",
		commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
			"The factory replaced kagent session %s (%s) for %s. The new session starts from this issue's context.",
			old, why, wi.IssueKey)})
}

type commentPayload struct {
	IssueID int64  `json:"issueId"`
	Body    string `json:"body"`
}

// findTask looks for the task holding an attempt's message in the session
// history, or in the agent's tasks when no context is known yet.
func (e *Engine) findTask(ctx context.Context, a *store.Attempt) (*kagent.Task, error) {
	ref := kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	if a.TaskID != "" {
		return e.tasks.GetTask(ctx, ref, a.TaskID)
	}
	token := ""
	for {
		tasks, next, err := e.tasks.ListTasks(ctx, ref, a.ContextID, token)
		if err != nil {
			if errors.Is(err, kagent.ErrTaskNotFound) {
				return nil, nil
			}
			return nil, err
		}
		for _, t := range tasks {
			if t.HasMessage(a.MessageID) {
				return t, nil
			}
		}
		if next == "" {
			return nil, nil
		}
		token = next
	}
}

// reconcile resolves an attempt whose send outcome is unknown. A task holding
// its message is attached; nothing is sent again while kagent might still
// accept the original. After the dispatch window a message no task holds was
// never accepted, and the same message is issued again.
func (e *Engine) reconcile(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	t, err := e.findTask(ctx, a)
	if err != nil {
		return err
	}
	if t != nil {
		e.log.InfoContext(ctx, "attached to task by message ID", "run", r.ID, "message_id", a.MessageID, "task", t.ID)
		return e.attach(ctx, a, wi, t)
	}
	if e.cfg.Now().Sub(a.SentAt) < e.cfg.DispatchWindow {
		return nil
	}
	b := e.binding(wi.BindingName)
	if b == nil {
		return e.failRun(ctx, r, "repository binding "+wi.BindingName+" is no longer configured")
	}
	e.m.Sends.WithLabelValues(a.Kind, "reissued").Inc()
	return e.issue(ctx, r, wi, b, a)
}

// progress follows a dispatched run's latest attempt: reconcile, post the
// transcript, and handle the end of the turn.
func (e *Engine) progress(ctx context.Context, r *store.Run) error {
	a, err := e.st.LatestAttempt(ctx, r.ID)
	if err != nil {
		return err
	}
	if a == nil {
		return e.dispatch(ctx, r)
	}
	wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
	if err != nil {
		return err
	}
	if a.WorkItemEpoch != wi.Epoch {
		// Reassignment fenced this run's attempt; its cancellation reports it.
		return nil
	}
	switch a.State {
	case store.AttemptSending:
		b := e.binding(wi.BindingName)
		if b == nil {
			return e.failRun(ctx, r, "repository binding "+wi.BindingName+" is no longer configured")
		}
		return e.issue(ctx, r, wi, b, a)
	case store.AttemptAmbiguous:
		if err := e.reconcile(ctx, r, wi, a); err != nil || a.State != store.AttemptAccepted {
			return err
		}
	case store.AttemptAccepted:
	default:
		return nil
	}
	if err := e.guardAttempt(ctx, a, wi); err != nil {
		return err
	}
	t, err := e.tasks.GetTask(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.TaskID)
	if err != nil {
		return err
	}
	if gone, err := e.postEvents(ctx, r, t); err != nil || gone {
		return err
	}
	if kagent.Quiescent(t.Status.State) {
		return e.complete(ctx, r, wi, a, t)
	}
	return nil
}

// postEvents sends the task's events past the persisted offset. A 409 means
// the stored offset is behind what itsaplan holds (a crash after its ack);
// probing with one event finds the server's offset, since itsaplan
// re-acknowledges events it already has.
func (e *Engine) postEvents(ctx context.Context, r *store.Run, t *kagent.Task) (bool, error) {
	events := Events(t)
	offset, base := r.EventOffset, r.EventTaskBase
	if r.EventAttempt != r.Attempt {
		offset, base = 0, 0
	}
	if r.EventTaskID != t.ID {
		base = offset
	}
	for {
		start := offset - base
		if start < 0 || start >= len(events) {
			break
		}
		batch := events[start:min(start+maxBatch, len(events))]
		if err := e.guard(ctx); err != nil {
			return false, err
		}
		next, err := e.its.Events(ctx, r.AgentKey, r.ID, r.Attempt, offset, batch)
		if errors.Is(err, itsaplan.ErrConflict) {
			e.m.Events.WithLabelValues("conflict").Inc()
			next, err = e.its.Events(ctx, r.AgentKey, r.ID, r.Attempt, offset, batch[:1])
		}
		switch {
		case errors.Is(err, itsaplan.ErrNotFound):
			e.m.Events.WithLabelValues("not_found").Inc()
			return e.runGone(ctx, r)
		case err != nil:
			e.m.Events.WithLabelValues("error").Inc()
			return false, err
		}
		e.m.Events.WithLabelValues("ok").Inc()
		if next <= offset {
			return false, fmt.Errorf("itsaplan acknowledged offset %d at %d", next, offset)
		}
		offset = next
		if err := e.st.SetEventOffset(ctx, r.ID, r.Attempt, offset, t.ID, base); err != nil {
			return false, err
		}
		r.EventAttempt, r.EventOffset, r.EventTaskID, r.EventTaskBase = r.Attempt, offset, t.ID, base
	}
	return false, nil
}

// complete handles a quiescent task: the turn finished, failed or waits.
func (e *Engine) complete(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt, t *kagent.Task) error {
	state := t.Status.State
	switch state {
	case kagent.StateCompleted, kagent.StateInputRequired:
		res := itsaplan.Result{Status: "success", Output: Output(t), Usage: LastCallUsage(t)}
		return e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCompleted, "", ""); err != nil {
				return err
			}
			if state == kagent.StateCompleted {
				if err := e.st.SetWorkState(ctx, tx, wi.ID, store.WorkReview); err != nil {
					return err
				}
			}
			if err := e.st.Enqueue(ctx, tx, store.OutboxResult, resultKey(r.ID), r.ID, r.AgentKey, res); err != nil {
				return err
			}
			return e.st.SetRunState(ctx, tx, r.ID, store.RunFinishing, "")
		})
	}
	msg := StatusText(t)
	if msg == "" {
		msg = strings.ToLower(strings.TrimPrefix(state, "TASK_STATE_"))
	}
	class := Classify(state, msg)
	switch class {
	case ClassCredential, ClassQuota:
		e.m.Blocked.WithLabelValues(class).Inc()
		reason := fmt.Sprintf("blocked (%s): %s", class, msg)
		return e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, class, msg); err != nil {
				return err
			}
			if err := e.st.Block(ctx, tx, wi.ID, class, clip(msg, maxErrorText)); err != nil {
				return err
			}
			if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/blocked/%d/%d", r.ID, a.Seq), r.ID, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"The factory stopped %s: a %s failure needs a person before it can continue. %s",
					wi.IssueKey, class, clip(msg, maxErrorText))}); err != nil {
				return err
			}
			if err := e.st.Enqueue(ctx, tx, store.OutboxResult, resultKey(r.ID), r.ID, r.AgentKey,
				itsaplan.Result{Status: "failed", Error: clip(reason, maxErrorText), Output: Output(t)}); err != nil {
				return err
			}
			return e.st.SetRunState(ctx, tx, r.ID, store.RunFinishing, "")
		})
	case ClassCanceled:
		return e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, class, msg); err != nil {
				return err
			}
			if err := e.st.Enqueue(ctx, tx, store.OutboxResult, resultKey(r.ID), r.ID, r.AgentKey,
				itsaplan.Result{Status: "failed", Error: "kagent canceled the task"}); err != nil {
				return err
			}
			return e.st.SetRunState(ctx, tx, r.ID, store.RunFinishing, "")
		})
	}
	// Engineering failure: retry in the same session within the work item's
	// budget.
	b := e.binding(wi.BindingName)
	budget := 0
	if b != nil {
		budget = b.MaxEngineeringRetries
	}
	var n int
	var ok bool
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, class, msg); err != nil {
			return err
		}
		var err error
		n, ok, err = e.st.TakeEngineeringRetry(ctx, tx, wi.ID, budget)
		if err != nil || ok {
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/retries/%d/%d", r.ID, a.Seq), r.ID, "",
			commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
				"The factory gave up on %s after %d engineering retries. Last error: %s",
				wi.IssueKey, budget, clip(msg, maxErrorText))}); err != nil {
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxResult, resultKey(r.ID), r.ID, r.AgentKey,
			itsaplan.Result{Status: "failed", Error: clip(fmt.Sprintf("engineering retries exhausted (%d): %s", budget, msg), maxErrorText)}); err != nil {
			return err
		}
		return e.st.SetRunState(ctx, tx, r.ID, store.RunFinishing, "")
	})
	if err != nil {
		return err
	}
	if !ok {
		e.m.Retries.WithLabelValues("exhausted").Inc()
		return nil
	}
	e.m.Retries.WithLabelValues("sent").Inc()
	if b == nil {
		return nil
	}
	return e.send(ctx, r, wi, b, n)
}

// stepCancellations drives pending cancellations until kagent reports each
// task in a terminal state.
func (e *Engine) stepCancellations(ctx context.Context) error {
	cs, err := e.st.PendingCancellations(ctx)
	if err != nil {
		return err
	}
	var errs []error
	for _, c := range cs {
		if err := e.stepCancellation(ctx, c); err != nil {
			if errors.Is(err, store.ErrNotWriter) {
				return err
			}
			errs = append(errs, fmt.Errorf("cancellation %d: %w", c.ID, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) stepCancellation(ctx context.Context, c *store.Cancellation) error {
	a, err := e.st.GetAttempt(ctx, c.AttemptID)
	if err != nil {
		return err
	}
	ref := kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}
	if a.TaskID == "" {
		if a.State == store.AttemptSending {
			return e.confirmCancel(ctx, c, a, "not_sent")
		}
		t, err := e.findTask(ctx, a)
		if err != nil {
			return err
		}
		if t == nil {
			if e.cfg.Now().Sub(a.SentAt) >= e.cfg.DispatchWindow {
				return e.confirmCancel(ctx, c, a, "not_dispatched")
			}
			return nil
		}
		if _, err := e.st.DB.ExecContext(ctx,
			`UPDATE delivery_attempt SET task_id = $2, context_id = $3 WHERE id = $1`, a.ID, t.ID, t.ContextID); err != nil {
			return err
		}
		a.TaskID = t.ID
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	t, err := e.tasks.GetTask(ctx, ref, a.TaskID)
	if err != nil {
		return err
	}
	if kagent.Terminal(t.Status.State) {
		return e.confirmCancel(ctx, c, a, strings.ToLower(strings.TrimPrefix(t.Status.State, "TASK_STATE_")))
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	if err := e.st.CountCancelRequest(ctx, c.ID); err != nil {
		return err
	}
	if _, err := e.tasks.CancelTask(ctx, ref, a.TaskID); err != nil {
		// The task may have finished meanwhile; the next read decides.
		e.log.WarnContext(ctx, "cancel request failed", "task", a.TaskID, "error", err)
	}
	return nil
}

// confirmCancel records kagent's terminal outcome and finishes the run the
// cancellation belonged to.
func (e *Engine) confirmCancel(ctx context.Context, c *store.Cancellation, a *store.Attempt, outcome string) error {
	e.m.Cancellations.WithLabelValues(c.Reason, "confirmed").Inc()
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.ConfirmCancellation(ctx, tx, c.ID, outcome); err != nil {
			return err
		}
		if a.Open() {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, ClassCanceled, c.Reason+": "+outcome); err != nil {
				return err
			}
		}
		if c.Reason == store.CancelRunCanceled {
			return e.st.SetRunState(ctx, tx, c.RunID, store.RunClosed, "canceled in itsaplan; kagent task "+outcome)
		}
		// Reassignment: the superseded run reports that another agent took over.
		var agent string
		if err := tx.QueryRowContext(ctx, `SELECT agent_key FROM run WHERE id = $1`, c.RunID).Scan(&agent); err != nil {
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxResult, resultKey(c.RunID), c.RunID, agent,
			itsaplan.Result{Status: "failed", Error: "superseded: the work item was reassigned to another agent"}); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE run SET state = 'finishing', updated_at = now() WHERE id = $1 AND state <> 'closed'`, c.RunID)
		return err
	})
}

// claim takes new runs for each agent key up to its concurrency.
func (e *Engine) claim(ctx context.Context) error {
	var errs []error
	for _, ag := range e.cfg.Agents {
		limit := ag.Concurrency
		if limit <= 0 {
			limit = 1
		}
		for {
			n, err := e.st.CountActiveRuns(ctx, ag.Key)
			if err != nil {
				return err
			}
			if n >= limit {
				break
			}
			if err := e.guard(ctx); err != nil {
				return err
			}
			run, err := e.its.Claim(ctx, ag.Key)
			if err != nil {
				e.m.Claims.WithLabelValues(ag.Key, "error").Inc()
				errs = append(errs, fmt.Errorf("claim %s: %w", ag.Key, err))
				break
			}
			if run == nil {
				e.m.Claims.WithLabelValues(ag.Key, "empty").Inc()
				break
			}
			if err := e.HandleClaim(ctx, ag.Key, run); err != nil {
				if errors.Is(err, store.ErrNotWriter) {
					return err
				}
				errs = append(errs, fmt.Errorf("run %d: %w", run.ID, err))
			}
		}
	}
	return errors.Join(errs...)
}

// HandleClaim records a claimed run and advances it. A run already known at
// this attempt is a duplicate and changes nothing. A higher attempt of a
// known run is a transport redelivery: its open attempt is reconciled by
// message ID and attached, never sent again.
func (e *Engine) HandleClaim(ctx context.Context, agent string, run *itsaplan.Run) error {
	var issueID int64
	if run.IssueID != nil {
		issueID = *run.IssueID
	}
	r, outcome, err := e.st.RecordClaim(ctx, store.ClaimedRun{
		ID: run.ID, AgentKey: agent, IssueID: issueID, IssueKey: run.IssueIdentifier, Trigger: run.Trigger,
		Prompt: run.Prompt, SystemPrompt: run.SystemPrompt, Attempt: run.Attempts,
	})
	if err != nil {
		return err
	}
	e.m.Claims.WithLabelValues(agent, string(outcome)).Inc()
	if outcome == store.ClaimDuplicate {
		e.log.InfoContext(ctx, "duplicate claim ignored", "run", run.ID, "attempt", run.Attempts)
		return nil
	}
	if outcome == store.ClaimRedelivery {
		e.log.InfoContext(ctx, "run redelivered", "run", run.ID, "attempt", run.Attempts, "state", r.State)
	}
	// The claim itself renewed the lease.
	if err := e.st.SetHeartbeat(ctx, r.ID, e.cfg.Now()); err != nil {
		return err
	}
	r.LastHeartbeatAt = e.cfg.Now()
	return e.step(ctx, r)
}

// flushOutbox delivers queued results and comments in order.
func (e *Engine) flushOutbox(ctx context.Context) error {
	items, err := e.st.PendingOutbox(ctx, 100)
	if err != nil {
		return err
	}
	var errs []error
	for _, it := range items {
		if err := e.deliver(ctx, it); err != nil {
			if errors.Is(err, store.ErrNotWriter) {
				return err
			}
			_ = e.st.OutboxFailed(ctx, it.ID, clip(err.Error(), maxErrorText))
			errs = append(errs, fmt.Errorf("outbox %d: %w", it.ID, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) deliver(ctx context.Context, it *store.OutboxItem) error {
	switch it.Kind {
	case store.OutboxComment:
		var p commentPayload
		if err := jsonUnmarshal(it.Payload, &p); err != nil {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
		}
		if err := e.guard(ctx); err != nil {
			return err
		}
		if err := e.its.Comment(ctx, p.IssueID, p.Body); err != nil {
			return err
		}
		return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
	case store.OutboxResult:
		var res itsaplan.Result
		if err := jsonUnmarshal(it.Payload, &res); err != nil {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
		}
		r, err := e.st.GetRun(ctx, it.RunID)
		if err != nil {
			return err
		}
		if r.State == store.RunStale {
			return nil // a redelivery claim brings the current attempt
		}
		res.Attempt = r.Attempt
		if err := e.guard(ctx); err != nil {
			return err
		}
		err = e.its.Result(ctx, it.AgentKey, it.RunID, res)
		switch {
		case err == nil:
			e.m.Results.WithLabelValues(res.Status).Inc()
			return e.st.Tx(ctx, func(tx *sql.Tx) error {
				if err := e.st.OutboxDelivered(ctx, tx, it.ID); err != nil {
					return err
				}
				return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, "reported "+res.Status)
			})
		case errors.Is(err, itsaplan.ErrNotFound):
			gone, err := e.runGone(ctx, r)
			if err != nil || !gone {
				return err
			}
			r, err = e.st.GetRun(ctx, it.RunID)
			if err != nil {
				return err
			}
			if r.State == store.RunStale {
				return nil
			}
			return e.st.Tx(ctx, func(tx *sql.Tx) error {
				if err := e.st.OutboxAbandon(ctx, tx, it.ID, "run no longer accepts a result"); err != nil {
					return err
				}
				if r.State == store.RunCanceling {
					return nil
				}
				return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, "result not accepted")
			})
		default:
			return err
		}
	}
	return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "unknown kind "+it.Kind)
}

func jsonUnmarshal(b []byte, v any) error { return json.Unmarshal(b, v) }
