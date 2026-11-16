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
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
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
	ListTasks(ctx context.Context, agent kagent.AgentRef, contextID, state, pageToken string) (*kagent.TaskPage, error)
	CancelTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
}

// Sessions is the subset of the kagent session client dispatch uses.
type Sessions interface {
	GetSession(ctx context.Context, id string) (*kagent.Session, error)
	RevertSession(ctx context.Context, id string) (*kagent.Session, error)
	ListSessions(ctx context.Context, agent kagent.AgentRef, pageToken string) ([]*kagent.Session, string, error)
	DeleteSession(ctx context.Context, id string) (*kagent.Session, error)
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
	// ArtifactTranscripts admits artifact text into journals. Existing
	// artifact journals always resume, even when admission is off.
	ArtifactTranscripts bool

	Caller    Caller
	Holder    string
	Agents    []Agent
	Bindings  []Binding
	Heartbeat time.Duration // itsaplan lease heartbeat, 60 s
	// ReissueInterval spaces proven-unaccepted retries and delays the first
	// read-only reconciliation; subsequent reads follow the poll interval.
	ReissueInterval time.Duration
	// DispatchWindow is how long a sent message may still be taken up by
	// kagent: the A2A call timeout, kagent's two-minute dispatch reservation
	// and clock skew. Afterwards, unresolved ambiguity becomes an operator hold.
	DispatchWindow time.Duration
	// MaxIssues bounds retries of definitively unaccepted calls. FinalLookups
	// more follow, one per DispatchWindow, before the attempt fails.
	MaxIssues    int
	FinalLookups int
	// CancelInterval spaces CancelTask requests for one task.
	CancelInterval time.Duration
	// ReviewPoll spaces polls of one owned PR, 60 s.
	ReviewPoll time.Duration
	// SessionCheck spaces checks of a running turn's session for a crash.
	SessionCheck time.Duration
	// Quota reads the broker's quota while a quota failure pauses a grant
	// or a running turn has had no task event for QuotaStall (10 min), at
	// most once per QuotaPoll (60 s). Nil leaves quota blocks to the
	// operator's unblock and cancels no quiet turn.
	Quota      QuotaSource
	QuotaPoll  time.Duration
	QuotaStall time.Duration
	Links      Links
	Now        func() time.Time
}

// QuotaSource reads the broker's quota reading for its Codex grant.
type QuotaSource interface {
	Quota(ctx context.Context) (github.QuotaReading, error)
}

// MinDispatchWindow is the smallest uncertainty window that covers a send's
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
	// refusals holds when itsaplan began refusing the coordinator key for
	// each bounded lookup. The heartbeat goroutine and the tick share it.
	refusalsMu sync.Mutex
	refusals   map[refusalKey]refusal
	// swept is when the tick last swept finished sessions.
	swept time.Time
	// quotaRead is when the tick last read the broker's quota.
	quotaRead time.Time
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
	if cfg.QuotaPoll == 0 {
		cfg.QuotaPoll = 60 * time.Second
	}
	if cfg.QuotaStall == 0 {
		cfg.QuotaStall = 10 * time.Minute
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

// RecordNotifyAttempt checks the current epoch and commits the request audit
// before the notification client makes each external call.
func (e *Engine) RecordNotifyAttempt(ctx context.Context, key, phase string) error {
	return e.st.RecordNotifyAttempt(ctx, e.w, key, phase)
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

// BeginStatusRead commits an attempt on the writer's lock connection before
// the transport sends it. The final check fences a takeover after the commit.
func (e *Engine) BeginStatusRead(ctx context.Context, messageID string) error {
	if err := e.w.RecordStatusRead(ctx, messageID); err != nil {
		return err
	}
	return e.guard(ctx)
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
	now := e.cfg.Now()
	e.refusalsMu.Lock()
	for k, rf := range e.refusals {
		if now.Sub(rf.last) >= authRefusalLimit {
			delete(e.refusals, k)
		}
	}
	e.refusalsMu.Unlock()
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
		{"quota", e.checkQuota},
		{"runs", e.stepRuns},
		{"cancellations", e.stepCancellations},
		{"delivery", e.stepDelivery},
		{"outbox", e.flushOutbox},
		{"sessions", e.sweepSessions},
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

// checkQuota reads the broker's quota while a quota block stands or a
// running turn has had no task event for QuotaStall (HOME-127). An
// exhausted reading cancels each such quiet turn; kagent's confirmation
// pauses it as a quota failure. A turn can go quiet for longer during a
// long tool call, so a usable reading cancels nothing.
//
// A usable reading clears quota blocks. Only a reading received after a
// block began clears it: the failed turn's own readings predate the block,
// so a grant that keeps failing pauses again until the broker's next
// reading, about every 30 minutes, instead of on every poll.
func (e *Engine) checkQuota(ctx context.Context) error {
	if e.cfg.Quota == nil || e.cfg.Now().Sub(e.quotaRead) < e.cfg.QuotaPoll {
		return nil
	}
	blocked, err := e.st.QuotaBlocked(ctx)
	if err != nil {
		return err
	}
	quiet, err := e.st.QuietAttempts(ctx, e.cfg.Now().Add(-e.cfg.QuotaStall))
	if err != nil || (!blocked && len(quiet) == 0) {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	e.quotaRead = e.cfg.Now()
	q, err := e.cfg.Quota.Quota(ctx)
	if err != nil {
		return err
	}
	if q.Exhausted {
		return e.cancelQuiet(ctx, quiet)
	}
	if !blocked || q.ReceivedAt.IsZero() {
		return nil
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	scopes, err := e.st.ClearQuotaBlocks(ctx, q.ReceivedAt)
	for _, scope := range scopes {
		e.log.InfoContext(ctx, "quota block cleared", "scope", scope, "reading_received_at", q.ReceivedAt)
	}
	return err
}

// cancelQuiet requests the cancellation of each quiet turn. It stays
// pending until kagent confirms it, as every cancellation does.
func (e *Engine) cancelQuiet(ctx context.Context, quiet []*store.Attempt) error {
	for _, a := range quiet {
		if err := e.st.RequestCancellation(ctx, e.st.DB, a.ID, a.RunID, store.CancelQuotaStall); err != nil {
			return err
		}
		e.m.Cancellations.WithLabelValues(store.CancelQuotaStall, "requested").Inc()
		e.log.WarnContext(ctx, "canceling a quiet turn: the broker reads the quota exhausted", "run", a.RunID,
			"attempt", a.Seq, "task", a.TaskID, "quiet_for", e.cfg.Now().Sub(lastEvent(a)))
	}
	return nil
}

// lastEvent is when the connector last saw the attempt's task change, or
// its send before the first read.
func lastEvent(a *store.Attempt) time.Time {
	if a.EventAt.After(a.SentAt) {
		return a.EventAt
	}
	return a.SentAt
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
		if err := e.st.SetHeartbeat(ctx, r.ID, now); err != nil {
			return err
		}
		// A PR closed during the turn stops delivery and cancels the turn.
		return e.checkPRClosed(ctx, r)
	case errors.Is(err, itsaplan.ErrNotFound):
		e.m.Heartbeats.WithLabelValues("not_found").Inc()
		_, err := e.runGone(ctx, r)
		return err
	default:
		e.m.Heartbeats.WithLabelValues("error").Inc()
		e.countRefusal(roleAgent, "heartbeat", err)
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
	if why := e.lookupRefused(ctx, refusalKey{lookupRun, r.ID, r.Attempt}, err); why != "" {
		// The agent key's call already failed for this attempt, and the
		// coordinator cannot say why: end the run rather than hold its slot.
		return true, e.startCancel(ctx, r, store.CancelRunClosed, why)
	}
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
		// A stale run holds no slot: a 401 or 403 just waits for the key.
		e.countRefusal(roleCoordinator, lookupRun, err)
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
		if a == nil || !a.Open() || (a.State == store.AttemptSending && a.Error == "") {
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
	// Only an unlinked itsaplan run is new work that may resume delivery.
	// A claimed run waiting for its brief has already been admitted and must
	// honor any later stop, just like a waiting or dispatched run.
	live := r.State == store.RunWaiting || r.State == store.RunDispatched || r.State == store.RunClaimed
	if live && r.StopRequested {
		return e.stopRun(ctx, r, r.StoppedReason)
	}
	if live && r.WorkItemID != 0 {
		wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
		if err != nil {
			return err
		}
		if wi.State == store.WorkStopped {
			return e.stopRun(ctx, r, wi.StoppedReason)
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

// stopRun stops a run whose delivery stopped or admission was superseded.
// An open kagent task gets a pending cancellation, and the run reports its
// failure, with the reason, only when kagent confirms. A run with nothing
// in kagent reports at once.
func (e *Engine) stopRun(ctx context.Context, r *store.Run, reason string) error {
	a, err := e.st.LatestAttempt(ctx, r.ID)
	if err != nil {
		return err
	}
	if a != nil && a.Open() && (a.State != store.AttemptSending || a.Error != "") {
		return e.st.RequestCancellation(ctx, e.st.DB, a.ID, r.ID, store.CancelRoundStopped)
	}
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if a != nil && a.State == store.AttemptSending {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, ClassCanceled, "delivery stopped"); err != nil {
				return err
			}
		}
		return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: clip("delivery stopped: "+reason, maxErrorText)})
	})
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
	var wi *store.WorkItem
	var reassigned bool
	if r.Connector() || r.WorkItemID != 0 {
		// Resolve only during first admission. Repeating it for a linked run
		// could reactivate a work item stopped while its brief was unavailable.
		wi, err = e.st.GetWorkItem(ctx, r.WorkItemID)
	} else {
		wi, reassigned, err = e.st.ResolveWorkItem(ctx, r.ID, store.NewWorkItem{
			Binding: b.Binding, IssueID: r.IssueID, IssueKey: r.IssueKey, AgentKey: r.AgentKey,
		})
	}
	if err != nil {
		return err
	}
	r.WorkItemID = wi.ID
	if wi.State == store.WorkStopped {
		return e.stopRun(ctx, r, wi.StoppedReason)
	}
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
		if r.State == store.RunDispatched {
			return nil // an explicit recovery hold keeps its concurrency slot
		}
		return e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunWaiting, "")
	}
	return e.sendNext(ctx, r, wi, b, 0)
}

// ready reports whether a run may send now: its work item and grant are not
// blocked, no other run's attempt or cancellation is open on the work item,
// and kagent shows no running task in the session. A blocked run waits for
// its unblock without holding a concurrency slot.
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
	busy, err := e.sessionBusy(ctx, r, wi)
	return !busy, err
}

// sessionBusy confirms with kagent that the work item's session has no
// running task, asking kagent to cancel any it finds. kagent admits one
// non-quiescent task per session.
func (e *Engine) sessionBusy(ctx context.Context, r *store.Run, wi *store.WorkItem) (bool, error) {
	if wi.SessionID == "" {
		return false, nil
	}
	var err error
	ctx, err = e.guardCaller(ctx, wi, nil)
	if err != nil {
		return true, e.holdIdleSession(ctx, r, wi, "caller ownership unproven")
	}
	ref := agentRef(wi)
	busy := false
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	for _, state := range []string{kagent.StateSubmitted, kagent.StateWorking} {
		token := ""
		seen := map[string]bool{}
		var coverage taskListCoverage
		for {
			if err := e.guard(ctx); err != nil {
				return false, err
			}
			page, err := e.tasks.ListTasks(ctx, ref, wi.SessionID, state, token)
			if errors.Is(err, kagent.ErrTaskNotFound) {
				return true, e.holdIdleSession(ctx, r, wi, recoveryLifecycle)
			}
			if err != nil {
				return false, err
			}
			if err := coverage.observe(page); err != nil {
				return false, err
			}
			tasks, next := page.Tasks, page.NextPageToken
			for _, t := range tasks {
				if t == nil || t.ID == "" || t.ContextID != wi.SessionID {
					return false, errors.New("session tasks: invalid task listing")
				}
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
				if err := coverage.complete(); err != nil {
					return false, err
				}
				break
			}
			if seen[next] {
				return false, errors.New("session tasks: repeated page token")
			}
			seen[next] = true
			token = next
		}
	}
	return busy, nil
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
	a, err := e.st.LatestAttempt(ctx, r.ID)
	if err != nil {
		return err
	}
	// Fence before ready can inspect or cancel tasks in the current session.
	if grantFailure(a) && a.WorkItemEpoch != wi.Epoch {
		return e.failRun(ctx, e.st.DB, r, "superseded: the work item was reassigned to another agent")
	}
	if grantFailure(a) {
		if finished, err := e.finishSupersededGrantFailure(ctx, r, a); err != nil || finished {
			return err
		}
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
	if grantFailure(a) && a.ErrorClass == ClassQuota {
		return e.resumeQuotaFailure(ctx, r, wi, b, a)
	}
	if grantFailure(a) {
		return e.engineeringFailure(ctx, r, wi, a, a.Error)
	}
	return e.sendNext(ctx, r, wi, b, 0)
}

// finishSupersededGrantFailure resolves a failed turn that a newer run overtook
// after unblock. It never returns the newer work to review or spends a retry.
func (e *Engine) finishSupersededGrantFailure(ctx context.Context, r *store.Run, a *store.Attempt) (bool, error) {
	var superseded bool
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		var err error
		superseded, err = e.st.SupersededGrantFailure(ctx, tx, r, a)
		if err != nil || !superseded {
			return err
		}
		const reason = "superseded: a newer run started work on this issue"
		if r.Connector() {
			return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, reason)
		}
		return e.failRun(ctx, tx, r, reason)
	})
	return superseded && err == nil, err
}

// resumeQuotaFailure resends a turn the quota stopped once its grant is
// unblocked. The limit failed, not the work, so it spends no engineering
// retry (HOME-127). The decision names the failed attempt, so each resend
// has its own message ID.
func (e *Engine) resumeQuotaFailure(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	brief, err := e.issueBrief(ctx, r, wi)
	if err != nil {
		return err
	}
	kind := "initial"
	if wi.SessionID != "" {
		kind, brief = "continuation", ""
	}
	var next *store.Attempt
	err = e.st.Tx(ctx, func(tx *sql.Tx) error {
		var err error
		next, err = e.record(ctx, tx, r, wi, kind, "quota-resume-"+strconv.Itoa(a.Seq), brief)
		return err
	})
	if err != nil {
		return err
	}
	e.log.InfoContext(ctx, "resuming a turn the quota stopped", "run", r.ID, "attempt", a.Seq, "work_item", wi.ID)
	return e.issue(ctx, r, wi, b, next)
}

// grantFailure is durable evidence that a terminal external task needs a
// resend once its grant is unblocked: an engineering retry after a
// credential failure, a quota resume after a quota failure.
func grantFailure(a *store.Attempt) bool {
	return a != nil && a.State == store.AttemptFailed &&
		(a.ErrorClass == ClassCredential || a.ErrorClass == ClassQuota)
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
// or a replacement of a lost session. brief, from issueBrief, is the context
// of a send that opens a session.
func (e *Engine) recordNext(ctx context.Context, q store.Querier, r *store.Run, wi *store.WorkItem, retry int, brief string) (*store.Attempt, error) {
	kind, decision := nextDecision(wi, retry)
	if kind != "initial" {
		brief = ""
	}
	return e.record(ctx, q, r, wi, kind, decision, brief)
}

// issueBrief quotes the run's issue for a send that opens a session. The
// agent has no itsaplan tools and itsaplan's prompt only names the issue, so
// without it a new session would not know what was asked. A send in an
// existing session gets none: the session already has it. Recorded with
// the attempt, the brief keeps resends identical.
func (e *Engine) issueBrief(ctx context.Context, r *store.Run, wi *store.WorkItem) (string, error) {
	if wi.SessionID != "" || wi.IssueID == 0 {
		return "", nil
	}
	if err := e.guard(ctx); err != nil {
		return "", err
	}
	issue, err := e.its.GetIssue(ctx, wi.IssueID)
	e.countRefusal(roleCoordinator, lookupBrief, err)
	if err == nil && (issue == nil || issue.ID != wi.IssueID || strings.TrimSpace(issue.Title+issue.Description) == "") {
		err = errors.New("missing authoritative issue text")
	}
	if err != nil {
		// Required input never expires into permission to start without it.
		// Announce the hold once per run attempt, without exposing response bodies.
		noticeErr := e.w.Tx(ctx, func(tx *sql.Tx) error {
			return e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/brief-held/%d/%d", r.ID, r.Attempt), r.ID, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"The factory is holding %s run %d until its issue title and description can be read. No new agent session will start without the issue text. Check the issue and the coordinator's itsaplan access; the lookup will retry automatically.",
					wi.IssueKey, r.ID)})
		})
		return "", errors.Join(fmt.Errorf("issue brief required before opening a session: %w", err), noticeErr)
	}
	var s strings.Builder
	s.WriteString("## Issue\nThis session has no itsaplan tools, so the issue is quoted here. The factory " +
		"reports your final message on the issue and opens the pull request from the work branch once you " +
		"push commits to it.\n")
	fmt.Fprintf(&s, "\n### Issue %s: %s\n\n%s\n", wi.IssueKey, issue.Title, clipDescription(issue.Description))
	return s.String(), nil
}

func clipDescription(description string) string {
	clipped := clip(description, 8000)
	if len(clipped) == len(description) {
		return description
	}
	return fmt.Sprintf("%s\n\n(description clipped at %d of %d bytes)", clipped, len(clipped), len(description))
}

// hasIssueBrief distinguishes recorded issue text from legacy unavailable
// placeholders, whose issue heading had no title. Only the first issue heading
// counts: an error body could itself contain a heading that looks like a brief.
func hasIssueBrief(background, issueKey string) bool {
	_, issue, ok := strings.Cut(background, "\n### Issue ")
	return ok && strings.HasPrefix(issue, issueKey+": ")
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
	brief, err := e.issueBrief(ctx, r, wi)
	if err != nil {
		return err
	}
	var a *store.Attempt
	err = e.st.Tx(ctx, func(tx *sql.Tx) error {
		var err error
		a, err = e.recordNext(ctx, tx, r, wi, retry, brief)
		return err
	})
	if err != nil {
		return err
	}
	return e.issue(ctx, r, wi, b, a)
}

// issue sends only a ready attempt whose earlier calls were definitively
// unaccepted. An ambiguous call is reconciled read-only: kagent may prune its
// session creation receipt, so even the same message ID can start new work.
func (e *Engine) issue(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) error {
	var err error
	ctx, err = e.guardCaller(ctx, wi, a)
	if err != nil {
		return e.recoverHeld(ctx, r, wi, a, "caller ownership unproven")
	}
	if reason, err := e.recoveryReason(ctx, a); err != nil {
		return err
	} else if reason == recoveryLifecycle {
		return e.recoverHeld(ctx, r, wi, a, reason)
	}
	now := e.cfg.Now()
	if a.State == store.AttemptAmbiguous {
		if now.Sub(a.SentAt) < e.cfg.ReissueInterval {
			return nil
		}
		// This branch must precede prepareSession: replacing a missing context
		// would also create a second writer for an unknown continuation.
		return e.unresolved(ctx, r, wi, a)
	}
	if a.Issues > 0 {
		if a.Issues >= e.cfg.MaxIssues {
			if now.Sub(a.SentAt) < e.cfg.DispatchWindow {
				return nil
			}
			if a.Issues >= e.cfg.MaxIssues+e.cfg.FinalLookups {
				return e.engineeringFailure(ctx, r, wi, a, "kagent definitively refused every send")
			}
		} else if now.Sub(a.SentAt) < e.cfg.ReissueInterval {
			return nil
		}
	}

	if a.Kind == "initial" && !hasIssueBrief(a.Context, wi.IssueKey) {
		// An upgrade may find a definitively unaccepted attempt with a legacy
		// placeholder. Retire it through the bounded retry path so corrected
		// text gets a new message ID. Ambiguous sends stay read-only above.
		return e.engineeringFailure(ctx, r, wi, a, "recorded attempt has no required issue brief")
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
	if issued, err := e.st.MarkIssued(ctx, e.w, a, r.Attempt, now); err != nil || !issued {
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
		// Only ready attempts reach Send. Every previous call was also
		// definitively unaccepted, so this refusal can safely permit retry.
		e.m.Sends.WithLabelValues(a.Kind, "not_accepted").Inc()
		return e.st.MarkNotAccepted(ctx, a)
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
		// A missing Agent does not stop a previously dispatched actor. An
		// earlier send remains uncertain until the original message or the
		// session lifecycle can be reconciled under the same caller.
		return nil
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
		e.log.WarnContext(ctx, "send outcome unknown; reconciling the original message read-only", "run", r.ID, "message_id", a.MessageID, "error", err)
		return nil
	}
}

// unresolved only adopts positive evidence of the original message. Absence
// cannot prove physical cessation: NodeGone deletion may hide an orphan actor,
// and idle expiry can remove even its initial-message creation receipt.
func (e *Engine) unresolved(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	t, err := e.taskWithMessage(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.ContextID, a.MessageID)
	if err == nil && t != nil {
		return e.attach(ctx, a, wi, t)
	}
	if e.cfg.Now().Sub(a.SentAt) >= e.cfg.DispatchWindow {
		if holdErr := e.holdUncertain(ctx, r, wi, a); holdErr != nil {
			return holdErr
		}
	}
	return err
}

// holdUncertain keeps the original attempt open and announces the hold once.
// Grant blocks are inappropriate here: they free waiting-run slots and permit
// a fresh retry when cleared, without proving the old writer stopped.
func (e *Engine) holdUncertain(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	return e.recoverHeld(ctx, r, wi, a, "send outcome unknown")
}

// attach records the task holding an attempt's message.
func (e *Engine) attach(ctx context.Context, a *store.Attempt, wi *store.WorkItem, t *kagent.Task) error {
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.InvalidateRecovery(ctx, tx, a.ID); err != nil {
			return err
		}
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

// prepareSession holds an unsent continuation when the old runtime's
// lifecycle is uncertain. READY later cannot clear that sticky hold.
func (e *Engine) prepareSession(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) (bool, error) {
	if e.sessions == nil || a.ContextID == "" {
		return true, nil
	}
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	session, err := e.sessions.GetSession(ctx, a.ContextID)
	if sessionLost(err) || (err == nil && session != nil && (session.Gone() || session.Crashed())) {
		return false, e.recoverHeld(ctx, r, wi, a, recoveryLifecycle)
	}
	return err == nil, err
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

// watchSession checks a running turn's session every SessionCheck. Lifecycle
// loss requires explicit fencing; controller state cannot prove actor cessation.
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
		return e.recoverHeld(ctx, r, wi, a, recoveryLifecycle)
	case err != nil:
		return err
	case s.State == kagent.RuntimeDeleting:
		return e.recoverHeld(ctx, r, wi, a, recoveryLifecycle)
	case s.Gone():
		return e.recoverHeld(ctx, r, wi, a, recoveryLifecycle)
	case s.Crashed():
		return e.recoverHeld(ctx, r, wi, a, recoveryLifecycle)
	}
	return nil
}

type commentPayload struct {
	IssueID int64  `json:"issueId"`
	Body    string `json:"body"`
	// Once posts the comment at most once: itsaplan comments have no
	// idempotency key, so a send whose outcome is unknown is not repeated.
	Once bool `json:"once,omitempty"`
}

// notCreated reports an error that proves itsaplan created nothing: the
// connection was never made, or itsaplan answered with a 4xx refusal, such
// as a 401 or 403 while a coordinator key rotates. A 5xx or a lost response
// proves neither.
func notCreated(err error) bool {
	var op *net.OpError
	if errors.As(err, &op) && op.Op == "dial" {
		return true
	}
	var dns *net.DNSError
	if errors.As(err, &dns) {
		return true
	}
	var he *itsaplan.HTTPError
	if errors.As(err, &he) && he.Status >= 400 && he.Status < 500 {
		return true
	}
	return errors.Is(err, itsaplan.ErrConflict)
}

// progress follows a dispatched run's latest attempt: send or reconcile it,
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
	ctx, err = e.guardCaller(ctx, wi, a)
	if err != nil {
		return e.recoverHeld(ctx, r, wi, a, "caller ownership unproven")
	}
	// A stale waiting run resumes as dispatched on transport redelivery.
	// Its finished grant failure still requires waiting admission, including
	// the reassignment fence for attempts that need no cancellation.
	if grantFailure(a) {
		if err := e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunWaiting, ""); err != nil {
			return err
		}
		r.State = store.RunWaiting
		return e.resumeWaiting(ctx, r)
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
	if errors.Is(err, kagent.ErrTaskNotFound) {
		return e.missingTask(ctx, r, wi, a)
	}
	if err != nil {
		return err
	}
	if sticky, err := e.originalVisible(ctx, a); err != nil {
		return err
	} else if sticky {
		return e.recoverHeld(ctx, r, wi, a, recoveryLifecycle)
	}
	if gone, err := e.postEvents(ctx, r, t); err != nil || gone {
		return err
	}
	if n := taskSize(t); n != a.EventCount {
		if err := e.st.SetAttemptEvents(ctx, a.ID, n, e.cfg.Now()); err != nil {
			return err
		}
	}
	if kagent.Quiescent(t.Status.State) {
		// A pending cancellation reports the turn once kagent confirms it.
		if pending, err := e.st.CancelPending(ctx, a.ID); err != nil || pending {
			return err
		}
		e.checked.Delete(a.ID)
		return e.complete(ctx, r, wi, a, t)
	}
	return e.watchSession(ctx, r, wi, a)
}

// taskSize grows with every task event the connector reads: history
// messages and artifact text.
func taskSize(t *kagent.Task) int {
	n := len(t.History)
	for _, a := range t.Artifacts {
		if a != nil {
			for _, p := range a.Parts {
				n += len(p.Text)
			}
		}
	}
	return n
}

// postEvents sends the task's events past the persisted offset. A 409 means
// the stored offset is behind what itsaplan holds (a crash after its ack);
// probing with one event finds the server's offset, since itsaplan
// re-acknowledges events it already has.
func (e *Engine) postEvents(ctx context.Context, r *store.Run, t *kagent.Task) (bool, error) {
	if r.Connector() {
		return false, nil // a review round has no itsaplan transcript
	}
	offset, base := r.EventOffset, r.EventTaskBase
	if r.EventAttempt != r.Attempt {
		offset, base = 0, 0
	}
	if r.EventTaskID != t.ID {
		base = offset
	}
	events, base, err := e.prepareEvents(ctx, r, t, base)
	if err != nil {
		return false, err
	}
	if offset < base || offset > base+len(events) {
		return false, fmt.Errorf("transcript offset %d outside persisted range [%d, %d]", offset, base, base+len(events))
	}
	// Persist raw text for cursor validation and replay. The destination's
	// JSONB storage needs NUL replaced without changing the journal's offsets.
	for i := range events {
		events[i].Delta = itsaplanText(events[i].Delta)
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
			e.countRefusal(roleAgent, "events", err)
			return false, err
		}
		e.m.Events.WithLabelValues("ok").Inc()
		if next <= offset || next > base+len(events) {
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
		return e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
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
		return e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error { return e.blockTurn(ctx, tx, r, wi, a, class, msg) })
	case ClassCanceled:
		return e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, class, msg); err != nil {
				return err
			}
			return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: "kagent canceled the task"})
		})
	}
	return e.engineeringFailure(ctx, r, wi, a, msg)
}

// blockTurn finishes a turn that failed on its grant, pauses the grant,
// announces the block once and leaves the run waiting without a slot.
func (e *Engine) blockTurn(ctx context.Context, tx *sql.Tx, r *store.Run, wi *store.WorkItem, a *store.Attempt, class, msg string) error {
	e.m.Blocked.WithLabelValues(class).Inc()
	e.log.WarnContext(ctx, "turn blocked", "run", r.ID, "attempt", a.Seq, "work_item", wi.ID,
		"class", class, "status", msg)
	if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, class, msg); err != nil {
		return err
	}
	started, err := e.st.Block(ctx, tx, wi, class, clip(msg, maxErrorText))
	if err != nil {
		return err
	}
	if !started {
		return e.st.SetRunState(ctx, tx, r.ID, store.RunWaiting, "")
	}
	scope := store.GrantScope(wi.KagentNamespace, wi.KagentAgent)
	body := fmt.Sprintf(
		"The factory stopped %s: a %s failure needs a person before it can continue (%s). "+
			"Work items using kagent agent %s/%s wait until an operator fixes the grant "+
			"and runs `connector unblock %s`.",
		wi.IssueKey, class, PublicFailure(class), wi.KagentNamespace, wi.KagentAgent, scope)
	if class == ClassQuota && e.cfg.Quota != nil {
		body = fmt.Sprintf(
			"The factory paused %s: %s. Work items using kagent agent %s/%s resume when the "+
				"broker reports the quota usable again, or when an operator runs `connector unblock %s`.",
			wi.IssueKey, PublicFailure(class), wi.KagentNamespace, wi.KagentAgent, scope)
	}
	body += fmt.Sprintf(" The full error is in the connector's logs and on run %d attempt %d.", r.ID, a.Seq)
	if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/blocked/%d/%d", r.ID, a.Seq), r.ID, "",
		commentPayload{IssueID: wi.IssueID, Body: body}); err != nil {
		return err
	}
	return e.st.SetRunState(ctx, tx, r.ID, store.RunWaiting, "")
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
	// Exhaustion needs no new input. A fresh-session retry keeps the failed
	// attempt's context, including any replacement's PR, branch and threads.
	var brief string
	if wi.EngineeringRetries < budget && wi.SessionID == "" {
		brief = a.Context
		if !hasIssueBrief(brief, wi.IssueKey) {
			var err error
			brief, err = e.issueBrief(ctx, r, wi)
			if err != nil {
				return err
			}
		}
	}
	var next *store.Attempt
	err := e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
		// A grant failure was already finished while blocked. Preserve its
		// class and evidence when taking the deferred retry after unblock.
		if a.Open() {
			if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, ClassEngineering, msg); err != nil {
				return err
			}
		}
		n, ok, err := e.st.TakeEngineeringRetry(ctx, tx, wi.ID, budget)
		if err != nil {
			return err
		}
		if ok && b != nil {
			next, err = e.recordNext(ctx, tx, r, wi, n, brief)
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/retries/%d/%d", r.ID, a.Seq), r.ID, "",
			commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
				"The factory gave up on %s after %d engineering retries: %s. The full error is in the "+
					"connector's logs and on run %d attempt %d.",
				wi.IssueKey, budget, PublicFailure(ClassEngineering), r.ID, a.Seq)}); err != nil {
			return err
		}
		e.log.WarnContext(ctx, "engineering retries exhausted", "run", r.ID, "attempt", a.Seq,
			"work_item", wi.ID, "retries", budget, "status", msg)
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

	if !a.Open() {
		return e.confirmCancel(ctx, c, a, a.State)
	}
	r, err := e.st.GetRun(ctx, c.RunID)
	if err != nil {
		return err
	}
	wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
	if err != nil {
		return err
	}
	ctx, err = e.guardCaller(ctx, wi, a)
	if err != nil {
		return e.recoverHeld(ctx, r, wi, a, "caller ownership unproven")
	}
	if reason, err := e.recoveryReason(ctx, a); err != nil {
		return err
	} else if reason == recoveryLifecycle {
		return e.recoverHeld(ctx, r, wi, a, reason)
	}
	if a.State == store.AttemptSending {
		return e.confirmCancel(ctx, c, a, "not_accepted")
	}
	ref := kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}
	if a.TaskID == "" {
		// The send's outcome is unknown. A cancellation never sends: that
		// could start the turn it is meant to stop. After the reservation
		// window, a missing task still cannot prove its runtime stopped.
		if e.cfg.Now().Sub(a.SentAt) < e.cfg.DispatchWindow {
			return nil
		}
		// Only the task holding this attempt's message is a target. The
		// all-state listing covers the original caller's sessions, including
		// a first send whose session the work item has not recorded yet.
		t, err := e.taskWithMessage(ctx, ref, a.ContextID, a.MessageID)
		if err != nil || t == nil {
			if holdErr := e.holdUncertain(ctx, r, wi, a); holdErr != nil {
				return holdErr
			}
			return err
		}
		if err := e.attach(ctx, a, wi, t); err != nil {
			return err
		}
		a.TaskID, a.ContextID = t.ID, t.ContextID
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	t, err := e.tasks.GetTask(ctx, ref, a.TaskID)
	if errors.Is(err, kagent.ErrTaskNotFound) {
		return e.missingTask(ctx, r, wi, a)
	}
	if err != nil {
		return err
	}
	if _, err := e.originalVisible(ctx, a); err != nil {
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
	return e.w.Tx(ctx, func(tx *sql.Tx) error { return e.confirmCancelTx(ctx, tx, c, a, outcome) })
}

func (e *Engine) confirmCancelTx(ctx context.Context, tx *sql.Tx, c *store.Cancellation, a *store.Attempt, outcome string) error {
	if err := e.st.InvalidateRecovery(ctx, tx, a.ID); err != nil {
		return err
	}
	if err := e.st.ConfirmCancellation(ctx, tx, c.ID, outcome); err != nil {
		return err
	}
	r := &store.Run{ID: c.RunID}
	var stopped string
	if err := tx.QueryRowContext(ctx, `
			SELECT r.agent_key, r.state, r.origin, coalesce(r.work_item_id, 0), coalesce(r.stopped_reason, w.stopped_reason, '')
			FROM run r LEFT JOIN work_item w ON w.id = r.work_item_id WHERE r.id = $1`,
		c.RunID).Scan(&r.AgentKey, &r.State, &r.Origin, &r.WorkItemID, &stopped); err != nil {
		return err
	}
	if c.Reason == store.CancelQuotaStall && r.State != store.RunCanceling && r.State != store.RunClosed {
		return e.confirmQuotaStall(ctx, tx, r, a, outcome)
	}
	if a.Open() {
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, ClassCanceled, c.Reason+": "+outcome); err != nil {
			return err
		}
	}
	if r.State == store.RunClosed {
		return nil
	}
	// A run itsaplan ended closes whatever reason the cancellation had:
	// it may have started as a reassignment and then gone canceling.
	if (c.Reason != store.CancelReassignment && c.Reason != store.CancelRoundStopped) || r.State == store.RunCanceling {
		if err := abandonResult(ctx, tx, c.RunID); err != nil {
			return err
		}
		return e.st.SetRunState(ctx, tx, c.RunID, store.RunClosed, "canceled ("+c.Reason+"); kagent task "+outcome)
	}
	if c.Reason == store.CancelRoundStopped {
		return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: clip("delivery stopped: "+stopped, maxErrorText)})
	}
	// Reassignment: the superseded run reports that another agent took over.
	return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: "superseded: the work item was reassigned to another agent"})
}

// confirmQuotaStall pauses a turn canceled for going quiet on an exhausted
// quota as a quota failure: the run waits without a slot and resumes in the
// same session when the quota is usable. A turn that ended on its own
// meanwhile keeps its attempt open, and progress reads its outcome.
func (e *Engine) confirmQuotaStall(ctx context.Context, tx *sql.Tx, r *store.Run, a *store.Attempt, outcome string) error {
	switch {
	case r.State != store.RunDispatched || !a.Open():
		return nil
	case outcome == "completed" || outcome == "failed" || outcome == "rejected":
		return nil
	}
	wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
	if err != nil {
		return err
	}
	return e.blockTurn(ctx, tx, r, wi, a, ClassQuota, fmt.Sprintf(
		"canceled after %s without a task event while the broker read the quota exhausted (kagent: %s)", e.cfg.QuotaStall, outcome))
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
				e.countRefusal(roleAgent, "claim", err)
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
// known run is a transport redelivery: its open attempt resumes, preserving
// any ambiguity until a read-only lookup finds the original task.
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
			e.countRefusal(roleCoordinator, lookupRun, err)
			e.log.WarnContext(ctx, "claimed run not visible to the coordinator", "run", r.ID, "error", err)
		}
	}
	return e.step(ctx, r)
}

// Outbox delivery limits. An itsaplan 401 or 403 gets its own, shorter
// bound: the connector reads its keys on every call, so a request sent with a
// key the bootstrap has just rotated, or before the kubelet refreshes the
// projected file, succeeds on a later try. Twelve tries back off for about
// 27 minutes in all, well past a rotation, before the item is abandoned as a
// credential problem.
const (
	outboxBatch        = 100
	outboxMaxTries     = 20
	outboxAuthMaxTries = 12
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
				if aerr := e.abandonItem(ctx, it, err); aerr != nil {
					e.log.ErrorContext(ctx, "abandoning outbox item failed", "outbox", it.ID, "kind", it.Kind, "error", aerr)
				}
			} else {
				backoff := min(time.Duration(1<<min(it.Tries, 10))*time.Second, 10*time.Minute)
				_ = e.st.OutboxFailed(ctx, it.ID, clip(err.Error(), maxErrorText), e.cfg.Now().Add(backoff))
			}
			errs = append(errs, fmt.Errorf("outbox %d: %w", it.ID, err))
		}
	}
	return errors.Join(errs...)
}

// terminalHTTP reports an itsaplan answer that a retry cannot change. A 401
// or 403 is not one: see authHTTP.
func terminalHTTP(err error) bool {
	var h *itsaplan.HTTPError
	if errors.Is(err, itsaplan.ErrNotFound) || errors.Is(err, itsaplan.ErrConflict) {
		return true
	}
	return errors.As(err, &h) && h.Status >= 400 && h.Status < 500 &&
		h.Status != 408 && h.Status != 429 && !authHTTP(err)
}

// authHTTP reports an itsaplan 401 or 403: the key sent was refused, which a
// rotated key read on a later call can fix.
func authHTTP(err error) bool {
	var h *itsaplan.HTTPError
	return errors.As(err, &h) && (h.Status == 401 || h.Status == 403)
}

// Key roles for factory_connector_credential_refusals_total: an agent's
// key claims, heartbeats and reports runs; the coordinator's key reads runs
// and issues, comments and moves issues.
const (
	roleAgent       = "agent"
	roleCoordinator = "coordinator"
)

// countRefusal counts err if it is an itsaplan 401 or 403 and reports
// whether it was.
func (e *Engine) countRefusal(role, kind string, err error) bool {
	if !authHTTP(err) {
		return false
	}
	e.m.CredentialRefusals.WithLabelValues(role, kind).Inc()
	return true
}

// authRefusalLimit bounds how long a coordinator lookup that holds a run's
// slot retries an itsaplan 401 or 403. Like outboxAuthMaxTries, it outlasts
// a key rotation and the kubelet's refresh of the projected file. Past it
// a dispatched run whose attempt itsaplan stopped accepting is closed.
// New sessions and fenced recovery require issue text regardless of this bound.
// Lookups that hold no slot (the issue check, a stale run) retry without a
// bound. The clock lives in memory, as the session check's does; a restart
// starts it again.
const authRefusalLimit = 30 * time.Minute

// Coordinator lookups, each a metric kind.
const (
	lookupReplacement = "replacement" // recovery input is required; retry until available
	lookupBrief       = "brief"       // a new session's required issue; retry until available
	lookupIssue       = "issue_check" // whether the issue still wants delivery; unbounded
	lookupRun         = "run_check"   // a run's state; bounded per run attempt in runGone
)

// refusalKey names one bounded lookup: its kind, the run or attempt row it
// serves and, for a run, the itsaplan attempt, so a redelivery starts a new
// clock.
type refusalKey struct {
	lookup  string
	id      int64
	attempt int
}

// refusal is a run of 401 and 403 answers to one lookup.
type refusal struct{ first, last time.Time }

// lookupRefused records a bounded coordinator lookup's outcome. Any answer
// but a 401 or 403, an error included, resets its clock and returns "", as
// does a refusal within authRefusalLimit: the caller handles err as before
// and tries again next pass. Past the limit it returns why the lookup gave
// up.
func (e *Engine) lookupRefused(ctx context.Context, k refusalKey, err error) string {
	refused := e.countRefusal(roleCoordinator, k.lookup, err)
	now := e.cfg.Now()
	e.refusalsMu.Lock()
	if !refused {
		delete(e.refusals, k)
		e.refusalsMu.Unlock()
		return ""
	}
	if e.refusals == nil {
		e.refusals = map[refusalKey]refusal{}
	}
	rf := refusal{first: now, last: now}
	if prev, ok := e.refusals[k]; ok && now.Sub(prev.last) < authRefusalLimit {
		rf.first = prev.first
	}
	e.refusals[k] = rf
	e.refusalsMu.Unlock()
	if now.Sub(rf.first) < authRefusalLimit {
		return ""
	}
	e.log.ErrorContext(ctx, "itsaplan keeps refusing the coordinator key; giving up on the lookup. "+
		"Check the itsaplan agent key Secrets and the bootstrap",
		"lookup", k.lookup, "id", k.id, "attempt", k.attempt, "since", rf.first, "error", err)
	return fmt.Sprintf("itsaplan has refused the coordinator key since %s (%s)",
		rf.first.UTC().Format(time.RFC3339), clip(err.Error(), 200))
}

// refused reports that the outbox item must be abandoned after err: a
// terminal answer, or a 401 or 403 that has used up outboxAuthMaxTries. Below
// that bound a 401 or 403 backs off like any other failure.
func (e *Engine) refused(ctx context.Context, it *store.OutboxItem, err error) bool {
	role := roleCoordinator
	if it.Kind == store.OutboxResult {
		role = roleAgent
	}
	if !e.countRefusal(role, it.Kind, err) {
		return terminalHTTP(err)
	}
	if it.Tries+1 < outboxAuthMaxTries {
		return false
	}
	e.log.ErrorContext(ctx, "itsaplan keeps refusing the connector's API key; abandoning the outbox item. "+
		"Check the itsaplan agent key Secrets and the bootstrap",
		"outbox", it.ID, "kind", it.Kind, "agent", it.AgentKey, "tries", it.Tries+1, "error", err)
	return true
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
		if p.Once {
			if it.SendStarted {
				return e.st.OutboxAbandon(ctx, e.st.DB, it.ID,
					"an earlier send's outcome is unknown; not resent, so the comment posts at most once")
			}
			if err := e.st.OutboxSendStarted(ctx, it.ID); err != nil {
				return err
			}
			if err := e.guard(ctx); err != nil {
				return err
			}
		}
		err := e.its.Comment(ctx, p.IssueID, p.Body)
		switch {
		case err == nil:
			return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
		case e.refused(ctx, it, err):
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, clip(err.Error(), maxErrorText))
		case p.Once && notCreated(err):
			// Retry under the usual bounds, including the credential-refusal
			// policy above; nothing was posted, so a resend is safe.
			if cerr := e.st.OutboxSendCleared(ctx, it.ID); cerr != nil {
				return cerr
			}
		case p.Once:
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID,
				clip("send outcome unknown; not resent: "+err.Error(), maxErrorText))
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
		case e.refused(ctx, it, err):
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
