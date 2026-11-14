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
	"sync"
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
	GetIssue(ctx context.Context, issueID int64) (*itsaplan.Issue, error)
	ProjectColumns(ctx context.Context, projectKey string) ([]itsaplan.Column, error)
	MoveIssue(ctx context.Context, issueID, columnID int64) error
}

// Tasks is the subset of the kagent A2A client dispatch uses.
type Tasks interface {
	Send(ctx context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error)
	GetTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
	ListTasks(ctx context.Context, agent kagent.AgentRef, contextID, state, pageToken string) ([]*kagent.Task, string, error)
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

// Binding is a repository binding, the agent keys that may deliver it and
// its review policy.
type Binding struct {
	store.Binding
	Agents []string
	Review ReviewPolicy
}

// Config holds dispatch settings.
type Config struct {
	Holder    string
	Agents    []Agent
	Bindings  []Binding
	Heartbeat time.Duration // itsaplan lease heartbeat, 60 s
	// ReissueInterval spaces sends of a message whose outcome is unknown.
	ReissueInterval time.Duration
	// DispatchWindow is how long a sent message may still be taken up by
	// kagent: the A2A call timeout, kagent's two-minute dispatch reservation
	// and clock skew. An unresolved attempt fails only after it.
	DispatchWindow time.Duration
	// MaxIssues bounds the spaced sends of one message. FinalLookups more
	// follow, one per DispatchWindow, before the attempt fails.
	MaxIssues    int
	FinalLookups int
	// CancelInterval spaces CancelTask requests for one task.
	CancelInterval time.Duration
	// ReviewPoll spaces polls of one owned PR, 60 s.
	ReviewPoll time.Duration
	// SessionCheck spaces checks of a running turn's session for a crash.
	SessionCheck time.Duration
	// MaxCrashResends bounds the resends of one run's input after its
	// session crashed and was reverted.
	MaxCrashResends int
	Links           Links
	Now             func() time.Time
}

// MinDispatchWindow is the smallest resend window that covers a send's
// timeout, kagent's dispatch reservation and 30 s of skew.
func MinDispatchWindow(callTimeout time.Duration) time.Duration {
	return callTimeout + 2*time.Minute + 30*time.Second
}

// Engine runs dispatch.
type Engine struct {
	cfg      Config
	st       *store.Store
	its      Itsaplan
	tasks    Tasks
	sessions Sessions
	gh       GitHub
	m        *metrics.Metrics
	log      *slog.Logger
	w        *store.Writer
	locks    sync.Map // run ID -> *sync.Mutex
	// checked holds when each running attempt's session was last checked.
	checked sync.Map // attempt ID -> sessionCheck
}

// New builds an engine. A nil gh leaves delivery (PRs, review and merge) off.
func New(cfg Config, st *store.Store, its Itsaplan, tasks Tasks, sessions Sessions, gh GitHub, m *metrics.Metrics, log *slog.Logger) *Engine {
	if cfg.Heartbeat == 0 {
		cfg.Heartbeat = 60 * time.Second
	}
	if cfg.ReissueInterval == 0 {
		cfg.ReissueInterval = 15 * time.Second
	}
	if cfg.DispatchWindow == 0 {
		cfg.DispatchWindow = MinDispatchWindow(kagent.DefaultTimeout)
	}
	if cfg.MaxIssues == 0 {
		cfg.MaxIssues = 20
	}
	if cfg.FinalLookups == 0 {
		cfg.FinalLookups = 3
	}
	if cfg.CancelInterval == 0 {
		cfg.CancelInterval = 30 * time.Second
	}
	if cfg.ReviewPoll == 0 {
		cfg.ReviewPoll = 60 * time.Second
	}
	if cfg.SessionCheck == 0 {
		cfg.SessionCheck = cfg.Heartbeat
	}
	if cfg.MaxCrashResends == 0 {
		cfg.MaxCrashResends = 2
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
	return &Engine{cfg: cfg, st: st, its: its, tasks: tasks, sessions: sessions, gh: gh, m: m, log: log}
}

// Acquire tries to become the writer and records the bindings. It returns
// false while another process holds the lock, and releases the lock again
// when recording the bindings fails.
func (e *Engine) Acquire(ctx context.Context) (bool, error) {
	w, err := e.st.TryAcquireWriter(ctx, e.cfg.Holder)
	if err != nil || w == nil {
		return false, err
	}
	for _, b := range e.cfg.Bindings {
		if err := e.st.UpsertBinding(ctx, b.Binding); err != nil {
			w.Release()
			return false, fmt.Errorf("record binding %s: %w", b.Name, err)
		}
	}
	e.w = w
	e.m.WriterEpoch.Set(float64(w.Epoch))
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

// lockRun serializes work on one run between the tick and the heartbeat loop.
func (e *Engine) lockRun(id int64) func() {
	v, _ := e.locks.LoadOrStore(id, &sync.Mutex{})
	mu := v.(*sync.Mutex)
	mu.Lock()
	return mu.Unlock
}

// tryLockRun takes the run's lock only if it is free.
func (e *Engine) tryLockRun(id int64) (func(), bool) {
	v, _ := e.locks.LoadOrStore(id, &sync.Mutex{})
	mu := v.(*sync.Mutex)
	if !mu.TryLock() {
		return nil, false
	}
	return mu.Unlock, true
}

// pruneLocks drops the locks of runs that are no longer open.
func (e *Engine) pruneLocks(open map[int64]bool) {
	e.locks.Range(func(k, v any) bool {
		id := k.(int64)
		if open[id] {
			return true
		}
		if mu := v.(*sync.Mutex); mu.TryLock() {
			e.locks.Delete(id)
			mu.Unlock()
		}
		return true
	})
	e.checked.Range(func(k, v any) bool {
		if !open[v.(sessionCheck).run] {
			e.checked.Delete(k)
		}
		return true
	})
}

func fatal(err error) bool {
	return errors.Is(err, store.ErrNotWriter) || errors.Is(err, context.Canceled)
}

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
		{"heartbeats", e.Heartbeats},
		{"claims", e.claim},
		{"runs", e.stepRuns},
		{"cancellations", e.stepCancellations},
		{"delivery", e.stepDelivery},
		{"outbox", e.flushOutbox},
	}
	for _, p := range phases {
		if err := p.fn(ctx); err != nil {
			if fatal(err) {
				return err
			}
			e.m.TickErrors.WithLabelValues(p.name).Inc()
			e.log.ErrorContext(ctx, "dispatch phase failed", "phase", p.name, "error", err)
		}
	}
	return nil
}

// RunHeartbeats keeps leases alive independently of the tick, so a slow
// kagent or session call cannot let itsaplan expire a run.
func (e *Engine) RunHeartbeats(ctx context.Context, every time.Duration) error {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		if err := e.Heartbeats(ctx); err != nil {
			if fatal(err) {
				return err
			}
			e.log.ErrorContext(ctx, "heartbeats failed", "error", err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		}
	}
}

// Heartbeats extends every held lease that is due.
func (e *Engine) Heartbeats(ctx context.Context) error {
	runs, err := e.st.OpenRuns(ctx)
	if err != nil {
		return err
	}
	var errs []error
	for _, r := range runs {
		switch r.State {
		case store.RunClaimed, store.RunWaiting, store.RunDispatched, store.RunFinishing:
		default:
			continue
		}
		if e.cfg.Now().Sub(r.LastHeartbeatAt) < e.cfg.Heartbeat {
			continue
		}
		if err := e.heartbeatRun(ctx, r.ID); err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("run %d: %w", r.ID, err))
		}
	}
	return errors.Join(errs...)
}

// heartbeatRun skips a run whose lock a slow step holds, so it does not
// delay the other runs' heartbeats; the next pass tries again.
func (e *Engine) heartbeatRun(ctx context.Context, id int64) error {
	unlock, ok := e.tryLockRun(id)
	if !ok {
		return nil
	}
	defer unlock()
	r, err := e.st.GetRun(ctx, id)
	if err != nil {
		return err
	}
	switch r.State {
	case store.RunClaimed, store.RunWaiting, store.RunDispatched, store.RunFinishing:
	default:
		return nil
	}
	now := e.cfg.Now()
	if now.Sub(r.LastHeartbeatAt) < e.cfg.Heartbeat {
		return nil
	}
	if r.Connector() {
		// A review round has no itsaplan lease; its heartbeat is the check
		// that its issue and PR still want it.
		return e.checkRound(ctx, r)
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	err = e.its.Heartbeat(ctx, r.AgentKey, r.ID, r.Attempt)
	switch {
	case err == nil:
		e.m.Heartbeats.WithLabelValues("ok").Inc()
		return e.st.SetHeartbeat(ctx, r.ID, now)
	case errors.Is(err, itsaplan.ErrNotFound):
		e.m.Heartbeats.WithLabelValues("not_found").Inc()
		_, err := e.runGone(ctx, r)
		return err
	default:
		e.m.Heartbeats.WithLabelValues("error").Inc()
		return err
	}
}

// runGone reads a run whose attempt itsaplan no longer accepts. A canceled
// or finished run cancels its kagent task; a higher attempt makes this one
// stale. gone reports that the run left this attempt.
func (e *Engine) runGone(ctx context.Context, r *store.Run) (gone bool, err error) {
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	st, err := e.its.GetRun(ctx, r.ID)
	switch {
	case errors.Is(err, itsaplan.ErrNotFound):
		if !r.SeenVisible {
			// Without a sighting, an empty lookup may be a permission gap.
			e.log.WarnContext(ctx, "run not visible to the coordinator", "run", r.ID)
			return false, nil
		}
		// The queue hides a run whose issue moved to another project, which is
		// how itsaplan v1.10.0 cancels a started run.
		return true, e.startCancel(ctx, r, store.CancelRunCanceled, "run no longer visible in the queue")
	case err != nil:
		return false, err
	}
	if !r.SeenVisible {
		if err := e.st.MarkVisible(ctx, r.ID); err != nil {
			return false, err
		}
		r.SeenVisible = true
	}
	switch {
	case st.Status == "canceled":
		return true, e.startCancel(ctx, r, store.CancelRunCanceled, "run canceled")
	case st.Status == "pending" && st.Attempts > r.Attempt:
		e.log.InfoContext(ctx, "attempt is stale", "run", r.ID, "attempt", r.Attempt, "current", st.Attempts)
		return true, e.st.MarkStale(ctx, r.ID, r.Attempt)
	case st.Status != "pending":
		return true, e.startCancel(ctx, r, store.CancelRunClosed, "finished in itsaplan: "+st.Status)
	}
	// Pending at this attempt but not accepting it: in retry backoff or not yet
	// executing. Try again next pass.
	return false, nil
}

// checkStale closes a stale run, cancelling its task, once itsaplan has
// finished it. A redelivery claim at the newer attempt resumes it instead.
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
	if st == nil && !r.SeenVisible {
		return nil
	}
	if st == nil || st.Status != "pending" {
		return e.startCancel(ctx, r, store.CancelRunClosed, "stale attempt; run finished in itsaplan")
	}
	return nil
}

// startCancel stops a run that itsaplan ended. Its open kagent task gets a
// pending cancellation and the run closes when kagent confirms; a run with
// nothing in kagent closes at once.
func (e *Engine) startCancel(ctx context.Context, r *store.Run, reason, why string) error {
	a, err := e.st.LatestAttempt(ctx, r.ID)
	if err != nil {
		return err
	}
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if a == nil || !a.Open() || a.State == store.AttemptSending {
			if a != nil && a.State == store.AttemptSending {
				if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, ClassCanceled, why); err != nil {
					return err
				}
			}
			if err := abandonResult(ctx, tx, r.ID); err != nil {
				return err
			}
			return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, why)
		}
		if err := e.st.RequestCancellation(ctx, tx, a.ID, r.ID, reason); err != nil {
			return err
		}
		e.m.Cancellations.WithLabelValues(reason, "requested").Inc()
		return e.st.SetRunState(ctx, tx, r.ID, store.RunCanceling, why)
	})
}

func abandonResult(ctx context.Context, q store.Querier, runID int64) error {
	_, err := q.ExecContext(ctx, `
		UPDATE outbox SET abandoned_at = now(), last_error = 'run ended in itsaplan'
		WHERE run_id = $1 AND kind = 'result' AND delivered_at IS NULL AND abandoned_at IS NULL`, runID)
	return err
}

func (e *Engine) stepRuns(ctx context.Context) error {
	runs, err := e.st.OpenRuns(ctx)
	if err != nil {
		return err
	}
	open := make(map[int64]bool, len(runs))
	for _, r := range runs {
		open[r.ID] = true
	}
	defer e.pruneLocks(open)
	var errs []error
	for _, r := range runs {
		if err := e.stepLocked(ctx, r.ID); err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("run %d: %w", r.ID, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) stepLocked(ctx context.Context, id int64) error {
	defer e.lockRun(id)()
	r, err := e.st.GetRun(ctx, id)
	if err != nil {
		return err
	}
	return e.step(ctx, r)
}

// step advances one open run. The caller holds the run's lock.
func (e *Engine) step(ctx context.Context, r *store.Run) error {
	if r.Connector() && (r.State == store.RunClaimed || r.State == store.RunWaiting || r.State == store.RunDispatched) {
		wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
		if err != nil {
			return err
		}
		if wi.State == store.WorkStopped {
			// Cancellation reaches execution: the round's task is canceled
			// and the run stays canceling until kagent confirms.
			return e.startCancel(ctx, r, store.CancelRoundStopped, "delivery stopped: "+wi.StoppedReason)
		}
	}
	switch r.State {
	case store.RunClaimed:
		return e.dispatch(ctx, r)
	case store.RunWaiting:
		return e.resumeWaiting(ctx, r)
	case store.RunDispatched:
		return e.progress(ctx, r)
	case store.RunStale:
		return e.checkStale(ctx, r)
	case store.RunCanceling:
		return e.checkCanceling(ctx, r)
	case store.RunFinishing:
		return e.checkFinishing(ctx, r)
	}
	return nil
}

// checkCanceling closes a canceling run whose cancellations are all
// confirmed, so no run waits on a cancellation that already ended.
func (e *Engine) checkCanceling(ctx context.Context, r *store.Run) error {
	var pending bool
	if err := e.st.DB.QueryRowContext(ctx,
		`SELECT EXISTS (SELECT 1 FROM cancellation WHERE run_id = $1 AND state = 'pending')`, r.ID).Scan(&pending); err != nil || pending {
		return err
	}
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := abandonResult(ctx, tx, r.ID); err != nil {
			return err
		}
		return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, "canceled; kagent confirmed")
	})
}

// checkFinishing closes a finishing run whose result left the outbox without
// being delivered.
func (e *Engine) checkFinishing(ctx context.Context, r *store.Run) error {
	var queued bool
	if err := e.st.DB.QueryRowContext(ctx, `
		SELECT EXISTS (SELECT 1 FROM outbox WHERE run_id = $1 AND kind = 'result'
			AND delivered_at IS NULL AND abandoned_at IS NULL)`, r.ID).Scan(&queued); err != nil || queued {
		return err
	}
	return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunClosed, "result not delivered")
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
func (e *Engine) failRun(ctx context.Context, q store.Querier, r *store.Run, msg string) error {
	return e.finishRun(ctx, q, r, itsaplan.Result{Status: "failed", Error: clip(msg, maxErrorText)})
}

// finishRun ends a run with its result. An itsaplan run queues the result
// for the outbox. A review round has no itsaplan record and closes; a failed
// one returns its work item to review, where the PR waits for a person,
// unless another run already holds the work item.
func (e *Engine) finishRun(ctx context.Context, q store.Querier, r *store.Run, res itsaplan.Result) error {
	if !r.Connector() {
		if err := e.st.Enqueue(ctx, q, store.OutboxResult, resultKey(r.ID), r.ID, r.AgentKey, res); err != nil {
			return err
		}
		return e.st.SetRunState(ctx, q, r.ID, store.RunFinishing, "")
	}
	if res.Status != "success" && r.WorkItemID != 0 {
		if _, err := q.ExecContext(ctx, `
			UPDATE work_item SET state = 'review', updated_at = now()
			WHERE id = $1 AND state = 'active' AND NOT EXISTS (
				SELECT 1 FROM run WHERE work_item_id = $1 AND id <> $2 AND state NOT IN ('closed', 'stale'))`,
			r.WorkItemID, r.ID); err != nil {
			return err
		}
	}
	reason := "review round " + res.Status
	if res.Error != "" {
		reason += ": " + res.Error
	}
	return e.st.SetRunState(ctx, q, r.ID, store.RunClosed, clip(reason, maxErrorText))
}

// dispatch resolves a claimed run's work item and sends it, or waits for
// the work item's previous task or its grant. It is idempotent: a run that
// already recorded an attempt resumes that attempt.
func (e *Engine) dispatch(ctx context.Context, r *store.Run) error {
	if r.IssueID == 0 {
		return e.failRun(ctx, e.st.DB, r, "factory runs must belong to an issue")
	}
	b, err := e.bindingFor(r)
	if err != nil {
		return e.failRun(ctx, e.st.DB, r, err.Error())
	}
	wi, reassigned, err := e.st.ResolveWorkItem(ctx, r.ID, store.NewWorkItem{
		Binding: b.Binding, IssueID: r.IssueID, IssueKey: r.IssueKey, AgentKey: r.AgentKey,
	})
	if err != nil {
		return err
	}
	r.WorkItemID = wi.ID
	if a, err := e.st.LatestAttempt(ctx, r.ID); err != nil {
		return err
	} else if a != nil && a.Open() {
		if err := e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunDispatched, ""); err != nil {
			return err
		}
		r.State = store.RunDispatched
		return e.progress(ctx, r)
	}
	if reassigned {
		e.m.Reassignments.Inc()
		e.log.InfoContext(ctx, "work item reassigned", "work_item", wi.ID, "run", r.ID, "epoch", wi.Epoch)
	}
	ready, err := e.ready(ctx, r, wi)
	if err != nil {
		return err
	}
	if !ready {
		return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunWaiting, "")
	}
	return e.sendNext(ctx, r, wi, b, 0)
}

// ready reports whether a run may send now: its work item and grant are not
// blocked, no other run's attempt or cancellation is open on the work item,
// and kagent shows no running task in the session. A blocked run waits for
// the operator's unblock without holding a concurrency slot.
func (e *Engine) ready(ctx context.Context, r *store.Run, wi *store.WorkItem) (bool, error) {
	if wi.State == store.WorkBlocked {
		return false, nil
	}
	paused, _, err := e.st.GrantBlocked(ctx, store.GrantScope(wi.KagentNamespace, wi.KagentAgent))
	if err != nil || paused {
		return false, err
	}
	pending, err := e.st.HasPendingCancellation(ctx, wi.ID)
	if err != nil || pending {
		return false, err
	}
	prev, err := e.st.OpenAttemptForWork(ctx, wi.ID)
	if err != nil {
		return false, err
	}
	if prev != nil && prev.RunID != r.ID {
		return false, nil
	}
	busy, err := e.sessionBusy(ctx, wi)
	return !busy, err
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
	for _, state := range []string{kagent.StateSubmitted, kagent.StateWorking} {
		token := ""
		for page := 0; page < 10; page++ {
			if err := e.guard(ctx); err != nil {
				return false, err
			}
			tasks, next, err := e.tasks.ListTasks(ctx, ref, wi.SessionID, state, token)
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
				break
			}
			token = next
		}
	}
	return busy, nil
}

// runningTaskWithMessage finds the running task whose history holds a
// message: in the session when contextID is set, otherwise in the Agent's
// sessions that no work item maps, where a first send with a lost response
// runs. Other runs' unattached first sends also run there, so only the
// message ID, which kagent keys a first send's session by, picks the task.
func (e *Engine) runningTaskWithMessage(ctx context.Context, ref kagent.AgentRef, contextID, messageID string) (*kagent.Task, error) {
	for _, state := range []string{kagent.StateSubmitted, kagent.StateWorking} {
		token := ""
		for page := 0; page < 10; page++ {
			if err := e.guard(ctx); err != nil {
				return nil, err
			}
			tasks, next, err := e.tasks.ListTasks(ctx, ref, contextID, state, token)
			if errors.Is(err, kagent.ErrTaskNotFound) {
				return nil, nil
			}
			if err != nil {
				return nil, err
			}
			for _, c := range tasks {
				if contextID == "" {
					mapped, err := e.st.SessionMapped(ctx, c.ContextID)
					if err != nil {
						return nil, err
					}
					if mapped {
						continue
					}
				}
				if err := e.guard(ctx); err != nil {
					return nil, err
				}
				t, err := e.tasks.GetTask(ctx, ref, c.ID)
				if err != nil {
					return nil, err
				}
				if t.HasMessage(messageID) {
					return t, nil
				}
			}
			if next == "" {
				break
			}
			token = next
		}
	}
	return nil, nil
}

// resumeWaiting sends a waiting run once its work item is ready.
func (e *Engine) resumeWaiting(ctx context.Context, r *store.Run) error {
	wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
	if err != nil {
		return err
	}
	b := e.binding(wi.BindingName)
	if b == nil {
		return e.failRun(ctx, e.st.DB, r, "repository binding "+wi.BindingName+" is no longer configured")
	}
	ready, err := e.ready(ctx, r, wi)
	if err != nil || !ready {
		return err
	}
	// Runs a block held were claimed without taking a slot; they resume
	// within the agent key's concurrency.
	busy, err := e.st.CountBusyRuns(ctx, r.AgentKey)
	if err != nil || busy >= e.concurrency(r.AgentKey) {
		return err
	}
	return e.sendNext(ctx, r, wi, b, 0)
}

func (e *Engine) concurrency(agent string) int {
	for _, a := range e.cfg.Agents {
		if a.Key == agent && a.Concurrency > 0 {
			return a.Concurrency
		}
	}
	return 1
}

// messageID names a send. A review round's run has no itsaplan id, so its
// messages are named for the connector's run.
func messageID(r *store.Run, decision string) string {
	if r.Connector() {
		return "factory-review/" + strconv.FormatInt(-r.ID, 10) + "/" + decision
	}
	return "itsaplan-run/" + strconv.FormatInt(r.ID, 10) + "/" + decision
}

func resultKey(runID int64) string { return "result/" + strconv.FormatInt(runID, 10) }

func agentRef(wi *store.WorkItem) kagent.AgentRef {
	return kagent.AgentRef{Namespace: wi.KagentNamespace, Name: wi.KagentAgent}
}

// taskText frames the run for the agent. Repository and branch come from the
// binding, never from the prompt. It is deterministic, so a resend of the
// same message carries the same input: a replacement's context is recorded
// with its attempt.
func taskText(r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) string {
	var s strings.Builder
	if r.SystemPrompt != "" {
		s.WriteString(r.SystemPrompt)
		s.WriteString("\n\n")
	}
	s.WriteString(r.Prompt)
	fmt.Fprintf(&s, "\n\n## Factory delivery\nRepository: %s/%s\nBase branch: %s\nWork branch: %s\n",
		b.Owner, b.Repo, b.BaseBranch, wi.Branch)
	if a.Context != "" {
		s.WriteString("\n")
		s.WriteString(a.Context)
	}
	return s.String()
}

// recordNext records the run's next attempt and moves the run to dispatched
// in q's transaction. The decision ID says why it sends: the work item's
// initial send, a revision in its session, an engineering retry (retry > 0)
// or a replacement of a lost session.
func (e *Engine) recordNext(ctx context.Context, q store.Querier, r *store.Run, wi *store.WorkItem, retry int) (*store.Attempt, error) {
	kind, decision := nextDecision(wi, retry)
	return e.record(ctx, q, r, wi, kind, decision, "")
}

func nextDecision(wi *store.WorkItem, retry int) (kind, decision string) {
	switch {
	case wi.SessionID != "" && retry > 0:
		return "continuation", "retry-" + strconv.Itoa(retry)
	case wi.SessionID != "":
		return "continuation", "revision"
	case wi.SessionGeneration > 0 || retry > 0:
		return "initial", "replacement-" + strconv.Itoa(wi.SessionGeneration) + "-" + strconv.Itoa(retry)
	}
	return "initial", "initial"
}

// record records an attempt in the work item's session, or in a new one when
// it has none, and moves the run to dispatched, in q's transaction.
func (e *Engine) record(ctx context.Context, q store.Querier, r *store.Run, wi *store.WorkItem, kind, decision, background string) (*store.Attempt, error) {
	a, err := e.st.RecordAttempt(ctx, q, store.Attempt{
		RunID: r.ID, RunAttempt: r.Attempt, WorkItemEpoch: wi.Epoch, DecisionID: decision, Kind: kind,
		MessageID: messageID(r, decision), KagentNamespace: wi.KagentNamespace, KagentAgent: wi.KagentAgent,
		ContextID: wi.SessionID, WriterEpoch: e.w.Epoch, Context: background,
	})
	if err != nil {
		return nil, err
	}
	if err := e.st.SetRunState(ctx, q, r.ID, store.RunDispatched, ""); err != nil {
		return nil, err
	}
	r.State = store.RunDispatched
	return a, nil
}

// sendNext records the next attempt with the run's state, then sends it.
func (e *Engine) sendNext(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, retry int) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	var a *store.Attempt
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
		var err error
		a, err = e.recordNext(ctx, tx, r, wi, retry)
		return err
	})
	if err != nil {
		return err
	}
	return e.issue(ctx, r, wi, b, a)
}

// issue sends a recorded attempt that kagent has not accepted, or sends it
// again when its outcome is unknown: kagent answers a repeated task-opening
// message with the task that holds it, so the same message is the lookup.
func (e *Engine) issue(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) error {
	now := e.cfg.Now()
	if a.State == store.AttemptAmbiguous {
		if a.Issues >= e.cfg.MaxIssues {
			// Past the cap, sends are only lookups, one per dispatch window.
			// kagent can commit the last send until its reservation ends, and
			// a lookup then answers with that task. The attempt fails only a
			// whole window after its final lookup, when nothing it sent can
			// still start.
			if now.Sub(a.SentAt) < e.cfg.DispatchWindow {
				return nil
			}
			if a.Issues >= e.cfg.MaxIssues+e.cfg.FinalLookups {
				return e.unresolved(ctx, r, wi, a)
			}
		} else if now.Sub(a.SentAt) < e.cfg.ReissueInterval {
			return nil
		}
	}
	if a.Kind == "continuation" {
		ok, err := e.prepareSession(ctx, r, wi, a)
		if err != nil || !ok {
			return err
		}
	}
	if err := e.guardAttempt(ctx, a, wi); err != nil {
		return err
	}
	if err := e.st.MarkIssued(ctx, a, now); err != nil {
		return err
	}
	task, err := e.tasks.Send(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, kagent.Message{
		MessageID: a.MessageID,
		ContextID: a.ContextID,
		Parts:     []kagent.Part{{Text: taskText(r, wi, b, a)}},
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
		// Nothing was dispatched for this call. An earlier send may still be
		// in flight, so the attempt stays ambiguous and goes again later.
		e.m.Sends.WithLabelValues(a.Kind, "not_accepted").Inc()
		return nil
	case errors.Is(err, kagent.ErrTaskNotFound) && a.Kind == "initial":
		e.m.Sends.WithLabelValues(a.Kind, "not_found").Inc()
		return e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, "", "kagent agent not found"); err != nil {
				return err
			}
			return e.failRun(ctx, tx, r, fmt.Sprintf("kagent agent %s/%s not found", a.KagentNamespace, a.KagentAgent))
		})
	case errors.Is(err, kagent.ErrTaskNotFound):
		e.m.Sends.WithLabelValues(a.Kind, "not_found").Inc()
		return e.replaceSession(ctx, r, wi, b, a, "expired or deleted")
	case errors.Is(err, kagent.ErrRejected):
		e.m.Sends.WithLabelValues(a.Kind, "rejected").Inc()
		return e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, "", clip(err.Error(), maxErrorText)); err != nil {
				return err
			}
			return e.failRun(ctx, tx, r, "kagent rejected the message: "+err.Error())
		})
	default:
		e.m.Sends.WithLabelValues(a.Kind, "ambiguous").Inc()
		e.log.WarnContext(ctx, "send outcome unknown; resending the same message later", "run", r.ID, "message_id", a.MessageID, "error", err)
		return nil
	}
}

// unresolved fails an attempt whose sends all went unanswered and whose last
// send is a dispatch window old, so kagent can no longer take any of them;
// the turn then follows the engineering failure path.
func (e *Engine) unresolved(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	e.m.Sends.WithLabelValues(a.Kind, "unresolved").Inc()
	msg := fmt.Sprintf("kagent did not take the message after %d sends", a.Issues)
	return e.engineeringFailure(ctx, r, wi, a, msg)
}

// attach records the task holding an attempt's message.
func (e *Engine) attach(ctx context.Context, a *store.Attempt, wi *store.WorkItem, t *kagent.Task) error {
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.MarkAccepted(ctx, tx, a.ID, t.ContextID, t.ID); err != nil {
			return err
		}
		if wi.Epoch == a.WorkItemEpoch {
			return e.st.SetSession(ctx, tx, wi.ID, t.ContextID)
		}
		return nil
	})
	if err != nil {
		return err
	}
	a.State, a.ContextID, a.TaskID = store.AttemptAccepted, t.ContextID, t.ID
	if wi.Epoch == a.WorkItemEpoch && wi.SessionID != t.ContextID {
		// SetSession counts a generation for every new session.
		wi.SessionGeneration++
		wi.SessionID = t.ContextID
	}
	return nil
}

// prepareSession checks a work item's session before a continuation. A
// crashed runtime is reverted; a deleted or expired session is replaced.
// ok is false when the send must wait or was replaced.
func (e *Engine) prepareSession(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) (bool, error) {
	if e.sessions == nil || a.ContextID == "" {
		return true, nil
	}
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	s, err := e.sessions.GetSession(ctx, a.ContextID)
	switch {
	case sessionLost(err):
		return false, e.replaceSession(ctx, r, wi, e.binding(wi.BindingName), a, "expired or deleted")
	case err != nil:
		return false, err
	}
	switch {
	case s.Gone():
		return false, e.replaceSession(ctx, r, wi, e.binding(wi.BindingName), a, "deleted")
	case s.Crashed():
		if err := e.guard(ctx); err != nil {
			return false, err
		}
		s, err = e.sessions.RevertSession(ctx, a.ContextID)
		if sessionLost(err) {
			return false, e.replaceSession(ctx, r, wi, e.binding(wi.BindingName), a, "unrecoverable after a crash")
		}
		if err != nil {
			// The attempt stays recorded; the revert is retried next pass, and
			// nothing retries it in the background.
			return false, fmt.Errorf("revert session: %w", err)
		}
		if s.Crashed() {
			return false, nil
		}
		e.m.SessionRecoveries.WithLabelValues("revert").Inc()
	}
	return true, nil
}

type sessionCheck struct {
	run int64
	at  time.Time
}

// sessionLost reports a session kagent no longer has (NOT_FOUND) or tells
// the client to replace (FAILED_PRECONDITION).
func sessionLost(err error) bool {
	return errors.Is(err, kagent.ErrSessionNotFound) || errors.Is(err, kagent.ErrSessionUnrecoverable)
}

// watchSession checks a running turn's session every SessionCheck. A crashed
// runtime does not finish its task, so the connector looks: a crashed
// session is reverted and the turn's input sent again, and a lost one is
// replaced.
func (e *Engine) watchSession(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	if e.sessions == nil || a.ContextID == "" {
		return nil
	}
	now := e.cfg.Now()
	last, seen := e.checked.LoadOrStore(a.ID, sessionCheck{run: r.ID, at: now})
	if !seen || now.Sub(last.(sessionCheck).at) < e.cfg.SessionCheck {
		return nil
	}
	e.checked.Store(a.ID, sessionCheck{run: r.ID, at: now})
	if err := e.guard(ctx); err != nil {
		return err
	}
	s, err := e.sessions.GetSession(ctx, a.ContextID)
	switch {
	case sessionLost(err):
		return e.replaceSession(ctx, r, wi, e.binding(wi.BindingName), a, "expired or deleted")
	case err != nil:
		return err
	case s.Gone():
		return e.replaceSession(ctx, r, wi, e.binding(wi.BindingName), a, "deleted")
	case s.Crashed():
		return e.recoverCrash(ctx, r, wi, a)
	}
	return nil
}

// recoverCrash reverts a session whose runtime crashed during a turn. The
// revert restores the last checkpoint and fails the interrupted turn, so the
// turn's input is sent again in the same session, a bounded number of times.
// The resend and the end of the interrupted attempt commit together.
func (e *Engine) recoverCrash(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	b := e.binding(wi.BindingName)
	if err := e.guard(ctx); err != nil {
		return err
	}
	s, err := e.sessions.RevertSession(ctx, a.ContextID)
	switch {
	case sessionLost(err):
		return e.replaceSession(ctx, r, wi, b, a, "unrecoverable after a crash")
	case err != nil:
		return fmt.Errorf("revert session: %w", err)
	case s.Crashed():
		return nil // the revert has not finished; look again next check
	}
	e.m.SessionRecoveries.WithLabelValues("revert").Inc()
	if b == nil {
		return e.failRun(ctx, e.st.DB, r, "repository binding "+wi.BindingName+" is no longer configured")
	}
	n, err := e.st.CountResends(ctx, r.ID)
	if err != nil {
		return err
	}
	if n >= e.cfg.MaxCrashResends {
		return e.engineeringFailure(ctx, r, wi, a, fmt.Sprintf("session crashed again after %d resends", n))
	}
	var next *store.Attempt
	err = e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, "", "session crashed; reverted to its last checkpoint"); err != nil {
			return err
		}
		var err error
		next, err = e.record(ctx, tx, r, wi, "continuation", "resend-"+strconv.Itoa(a.Seq), a.Context)
		return err
	})
	if err != nil {
		return err
	}
	e.checked.Delete(a.ID)
	return e.issue(ctx, r, wi, b, next)
}

// replaceSession ends an attempt whose session kagent no longer has and, in
// the same transaction, records the replacement attempt and its
// announcement; then it sends the replacement.
func (e *Engine) replaceSession(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt, why string) error {
	if b == nil {
		return e.failRun(ctx, e.st.DB, r, "repository binding "+wi.BindingName+" is no longer configured")
	}
	old := a.ContextID
	background, err := e.replacementContext(ctx, wi, b, old, why)
	if err != nil {
		return err
	}
	e.m.SessionRecoveries.WithLabelValues("replace").Inc()
	var next *store.Attempt
	err = e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, "", "session "+why); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE work_item SET session_id = NULL, updated_at = now() WHERE id = $1`, wi.ID); err != nil {
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxComment,
			fmt.Sprintf("comment/session-replaced/%d/%d", wi.ID, wi.SessionGeneration), r.ID, "",
			commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
				"The factory replaced kagent session %s (%s) for %s. The new session starts from the issue, "+
					"its pull request, the branch head and the open review threads.",
				old, why, wi.IssueKey)}); err != nil {
			return err
		}
		wi.SessionID = ""
		kind, decision := nextDecision(wi, 0)
		var err error
		next, err = e.record(ctx, tx, r, wi, kind, decision, background)
		return err
	})
	if err != nil {
		return err
	}
	return e.issue(ctx, r, wi, b, next)
}

type commentPayload struct {
	IssueID int64  `json:"issueId"`
	Body    string `json:"body"`
}

// progress follows a dispatched run's latest attempt: send or resend it,
// post the transcript, and handle the end of the turn.
func (e *Engine) progress(ctx context.Context, r *store.Run) error {
	a, err := e.st.LatestAttempt(ctx, r.ID)
	if err != nil {
		return err
	}
	if a == nil {
		r.State = store.RunClaimed
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
	case store.AttemptSending, store.AttemptAmbiguous:
		b := e.binding(wi.BindingName)
		if b == nil {
			return e.failRun(ctx, e.st.DB, r, "repository binding "+wi.BindingName+" is no longer configured")
		}
		if err := e.issue(ctx, r, wi, b, a); err != nil || a.State != store.AttemptAccepted {
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
		e.checked.Delete(a.ID)
		return e.complete(ctx, r, wi, a, t)
	}
	return e.watchSession(ctx, r, wi, a)
}

// postEvents sends the task's events past the persisted offset. A 409 means
// the stored offset is behind what itsaplan holds (a crash after its ack);
// probing with one event finds the server's offset, since itsaplan
// re-acknowledges events it already has.
func (e *Engine) postEvents(ctx context.Context, r *store.Run, t *kagent.Task) (bool, error) {
	if r.Connector() {
		return false, nil // a review round has no itsaplan transcript
	}
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
				// The turn finished; delivery opens or updates the PR from the
				// pushed branch. The issue is Done only once the merge is seen.
				if err := e.st.FinishTurn(ctx, tx, wi.ID, a.ID); err != nil {
					return err
				}
			}
			return e.finishRun(ctx, tx, r, res)
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
			started, err := e.st.Block(ctx, tx, wi, class, clip(msg, maxErrorText))
			if err != nil {
				return err
			}
			if !started {
				return e.reportBlocked(ctx, tx, r, t, reason)
			}
			if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/blocked/%d/%d", r.ID, a.Seq), r.ID, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"The factory stopped %s: a %s failure needs a person before it can continue. "+
						"Work items using kagent agent %s/%s wait until an operator fixes the grant "+
						"and runs `connector unblock %s`. %s",
					wi.IssueKey, class, wi.KagentNamespace, wi.KagentAgent,
					store.GrantScope(wi.KagentNamespace, wi.KagentAgent), clip(msg, maxErrorText))}); err != nil {
				return err
			}
			return e.reportBlocked(ctx, tx, r, t, reason)
		})
	case ClassCanceled:
		return e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, class, msg); err != nil {
				return err
			}
			return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: "kagent canceled the task"})
		})
	}
	return e.engineeringFailure(ctx, r, wi, a, msg)
}

// reportBlocked queues the failed result of the turn that hit a block.
func (e *Engine) reportBlocked(ctx context.Context, tx *sql.Tx, r *store.Run, t *kagent.Task, reason string) error {
	return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: clip(reason, maxErrorText), Output: Output(t)})
}

// engineeringFailure retries in the same session within the work item's
// budget. Taking a retry and recording the attempt it sends commit together,
// so a failure afterwards leaves a recorded attempt that progress sends.
func (e *Engine) engineeringFailure(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt, msg string) error {
	b := e.binding(wi.BindingName)
	budget := 0
	if b != nil {
		budget = b.MaxEngineeringRetries
	}
	var next *store.Attempt
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, ClassEngineering, msg); err != nil {
			return err
		}
		n, ok, err := e.st.TakeEngineeringRetry(ctx, tx, wi.ID, budget)
		if err != nil {
			return err
		}
		if ok && b != nil {
			next, err = e.recordNext(ctx, tx, r, wi, n)
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/retries/%d/%d", r.ID, a.Seq), r.ID, "",
			commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
				"The factory gave up on %s after %d engineering retries. Last error: %s",
				wi.IssueKey, budget, clip(msg, maxErrorText))}); err != nil {
			return err
		}
		return e.finishRun(ctx, tx, r, itsaplan.Result{
			Status: "failed",
			Error:  clip(fmt.Sprintf("engineering retries exhausted (%d): %s", budget, msg), maxErrorText),
		})
	})
	if err != nil {
		return err
	}
	if next == nil {
		e.m.Retries.WithLabelValues("exhausted").Inc()
		return nil
	}
	e.m.Retries.WithLabelValues("sent").Inc()
	return e.issue(ctx, r, wi, b, next)
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
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("cancellation %d: %w", c.ID, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) stepCancellation(ctx context.Context, c *store.Cancellation) error {
	defer e.lockRun(c.RunID)()
	a, err := e.st.GetAttempt(ctx, c.AttemptID)
	if err != nil {
		return err
	}
	if a.State == store.AttemptSending {
		// Recorded but never sent: kagent cannot hold it.
		return e.confirmCancel(ctx, c, a, "not_sent")
	}
	if !a.Open() {
		return e.confirmCancel(ctx, c, a, a.State)
	}
	ref := kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}
	if a.TaskID == "" {
		// The send's outcome is unknown. A cancellation never sends: that
		// could start the turn it is meant to stop. kagent can commit a sent
		// message only within its reservation, so a whole dispatch window
		// after the last send no new task can appear.
		if e.cfg.Now().Sub(a.SentAt) < e.cfg.DispatchWindow {
			return nil
		}
		// Only the task holding this attempt's message is a target. A first
		// send runs in a session no work item maps yet; the listing holds
		// only the connector's sessions.
		t, err := e.runningTaskWithMessage(ctx, ref, a.ContextID, a.MessageID)
		if err != nil {
			return err
		}
		if t == nil {
			return e.confirmCancel(ctx, c, a, "not_dispatched")
		}
		if err := e.st.MarkAccepted(ctx, e.st.DB, a.ID, t.ContextID, t.ID); err != nil {
			return err
		}
		a.TaskID, a.ContextID = t.ID, t.ContextID
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
	if c.Requests > 0 && e.cfg.Now().Sub(c.LastAt) < e.cfg.CancelInterval {
		return nil
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	if err := e.st.CountCancelRequest(ctx, c.ID, e.cfg.Now()); err != nil {
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
		r := &store.Run{ID: c.RunID}
		if err := tx.QueryRowContext(ctx, `SELECT agent_key, state, origin, coalesce(work_item_id, 0) FROM run WHERE id = $1`,
			c.RunID).Scan(&r.AgentKey, &r.State, &r.Origin, &r.WorkItemID); err != nil {
			return err
		}
		if r.State == store.RunClosed {
			return nil
		}
		// A run itsaplan ended closes whatever reason the cancellation had:
		// it may have started as a reassignment and then gone canceling.
		if c.Reason != store.CancelReassignment || r.State == store.RunCanceling {
			if err := abandonResult(ctx, tx, c.RunID); err != nil {
				return err
			}
			return e.st.SetRunState(ctx, tx, c.RunID, store.RunClosed, "canceled ("+c.Reason+"); kagent task "+outcome)
		}
		// Reassignment: the superseded run reports that another agent took over.
		return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: "superseded: the work item was reassigned to another agent"})
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
		for i := 0; i <= limit; i++ {
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
				if fatal(err) {
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
// known run is a transport redelivery: its open attempt resumes, and kagent
// answers a resend of its message with the task already holding it.
func (e *Engine) HandleClaim(ctx context.Context, agent string, run *itsaplan.Run) error {
	defer e.lockRun(run.ID)()
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
	if !r.SeenVisible {
		// One sighting makes a later empty lookup mean canceled.
		if err := e.guard(ctx); err != nil {
			return err
		}
		if _, err := e.its.GetRun(ctx, r.ID); err == nil {
			if err := e.st.MarkVisible(ctx, r.ID); err != nil {
				return err
			}
			r.SeenVisible = true
		} else {
			e.log.WarnContext(ctx, "claimed run not visible to the coordinator", "run", r.ID, "error", err)
		}
	}
	return e.step(ctx, r)
}

// Outbox delivery limits.
const (
	outboxBatch    = 100
	outboxMaxTries = 20
)

// flushOutbox delivers due results and comments in order. A failing item
// backs off without holding back newer ones and is abandoned after
// outboxMaxTries.
func (e *Engine) flushOutbox(ctx context.Context) error {
	items, err := e.st.PendingOutbox(ctx, e.cfg.Now(), outboxBatch)
	if err != nil {
		return err
	}
	var errs []error
	for _, it := range items {
		if err := e.deliver(ctx, it); err != nil {
			if fatal(err) {
				return err
			}
			if it.Tries+1 >= outboxMaxTries {
				_ = e.st.OutboxAbandon(ctx, e.st.DB, it.ID, clip(err.Error(), maxErrorText))
			} else {
				backoff := min(time.Duration(1<<min(it.Tries, 10))*time.Second, 10*time.Minute)
				_ = e.st.OutboxFailed(ctx, it.ID, clip(err.Error(), maxErrorText), e.cfg.Now().Add(backoff))
			}
			errs = append(errs, fmt.Errorf("outbox %d: %w", it.ID, err))
		}
	}
	return errors.Join(errs...)
}

// terminalHTTP reports an itsaplan answer that a retry cannot change.
func terminalHTTP(err error) bool {
	var h *itsaplan.HTTPError
	if errors.Is(err, itsaplan.ErrNotFound) || errors.Is(err, itsaplan.ErrConflict) {
		return true
	}
	return errors.As(err, &h) && h.Status >= 400 && h.Status < 500 && h.Status != 408 && h.Status != 429
}

func (e *Engine) deliver(ctx context.Context, it *store.OutboxItem) error {
	switch it.Kind {
	case store.OutboxComment:
		var p commentPayload
		if err := json.Unmarshal(it.Payload, &p); err != nil {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
		}
		if err := e.guard(ctx); err != nil {
			return err
		}
		err := e.its.Comment(ctx, p.IssueID, p.Body)
		switch {
		case err == nil:
			return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
		case terminalHTTP(err):
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, clip(err.Error(), maxErrorText))
		}
		return err
	case store.OutboxResult:
		var res itsaplan.Result
		if err := json.Unmarshal(it.Payload, &res); err != nil {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
		}
		defer e.lockRun(it.RunID)()
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
			// Terminal for this attempt. Read the run: a newer attempt keeps the
			// result for the redelivery; anything else ends it.
			gone, err := e.runGone(ctx, r)
			if err != nil {
				return err
			}
			if !gone {
				return e.st.OutboxFailed(ctx, it.ID, "result not accepted; run still pending", e.cfg.Now().Add(e.cfg.Heartbeat))
			}
			r, err = e.st.GetRun(ctx, it.RunID)
			if err != nil || r.State == store.RunStale {
				return err
			}
			return e.st.Tx(ctx, func(tx *sql.Tx) error {
				if err := e.st.OutboxAbandon(ctx, tx, it.ID, "run no longer accepts a result"); err != nil {
					return err
				}
				if r.State == store.RunCanceling || r.State == store.RunClosed {
					return nil
				}
				return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, "result not accepted")
			})
		case terminalHTTP(err):
			return e.st.Tx(ctx, func(tx *sql.Tx) error {
				if err := e.st.OutboxAbandon(ctx, tx, it.ID, clip(err.Error(), maxErrorText)); err != nil {
					return err
				}
				return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, "result refused")
			})
		default:
			return err
		}
	}
	if it.Kind == store.OutboxIssueDone {
		return e.deliverIssueDone(ctx, it)
	}
	return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "unknown kind "+it.Kind)
}
