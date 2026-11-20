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

	"github.com/jomcgi/homelab/projects/factory/connector/internal/tracing"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/metrics"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Itsaplan is the subset of the itsaplan client dispatch uses.
type Itsaplan interface {
	Project(context.Context, string) (*itsaplan.Project, error)
	ProjectIssues(context.Context, string) ([]itsaplan.Issue, error)
	StartDeferred(context.Context, int64, itsaplan.DeferredRequest) (*itsaplan.DeferredReceipt, error)
	Claim(ctx context.Context, agent string) (*itsaplan.Run, error)
	Heartbeat(ctx context.Context, agent string, runID int64, attempt int) error
	Events(ctx context.Context, agent string, runID int64, attempt, offset int, events []itsaplan.Event) (int, error)
	Result(ctx context.Context, agent string, runID int64, r itsaplan.Result) error
	GetRun(ctx context.Context, runID int64) (*itsaplan.RunStatus, error)
	Comment(ctx context.Context, issueID int64, body string) error
	GetIssue(ctx context.Context, issueID int64) (*itsaplan.Issue, error)
	GetAttachments(ctx context.Context, issueID int64) ([]itsaplan.Attachment, error)
	ProjectColumns(ctx context.Context, projectKey string) ([]itsaplan.Column, error)
	MoveIssue(ctx context.Context, issueID, columnID int64) error
	GetProject(ctx context.Context, key string) (*itsaplan.Project, error)
	SearchIssues(ctx context.Context, project, query string) ([]itsaplan.Issue, error)
	CreateIssue(ctx context.Context, project string, issue itsaplan.NewIssue) (*itsaplan.Issue, error)
	UpdateIssue(ctx context.Context, id int64, patch itsaplan.IssueUpdate) error
	IssueRuns(ctx context.Context, issueID, before int64) (*itsaplan.IssueRunPage, error)
	BlockIssue(ctx context.Context, source, target int64) error
	SearchGateIssues(ctx context.Context, project, marker string) ([]itsaplan.Issue, error)
	CreateGateIssue(ctx context.Context, project string, body itsaplan.GateIssueWrite) (*itsaplan.Issue, error)
	UpdateGateIssue(ctx context.Context, id int64, body itsaplan.GateIssueWrite) error
}

// Tasks is the subset of the kagent A2A client dispatch uses.
type Tasks interface {
	Send(ctx context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error)
	GetTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
	GetTaskMetadata(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
	ListTasks(ctx context.Context, agent kagent.AgentRef, contextID, state, pageToken string) (*kagent.TaskPage, error)
	CancelTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
}

// Sessions is the subset of the kagent session client dispatch uses.
type Sessions interface {
	GetSession(ctx context.Context, id string) (*kagent.Session, error)
	RevertSession(ctx context.Context, id string) (*kagent.Session, error)
	ListSessions(ctx context.Context, agent kagent.AgentRef, pageToken string) ([]*kagent.Session, string, error)
	DeleteSession(ctx context.Context, id string) (*kagent.Session, error)
	ForceDeleteSession(ctx context.Context, id string) (*kagent.Session, error)
}

// Agent is an itsaplan agent key the connector claims runs for.
type Agent struct {
	Key         string
	Concurrency int
}

// Binding is a repository binding, the agent keys that may deliver it and
// its review policy.
type Binding struct {
	Arms    []provenance.Arm
	Runtime *provenance.Runtime
	store.Binding
	Agents            []string
	Review            ReviewPolicy
	HumanGateAssignee string
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
	// CILogs is optional; its candidates must pass base-tree validation.
	CILogs CILogs
	// GoalReasons is optional: the reviewer's goal check reasons for a
	// head, posted on the issue and quoted in the round (goal_reasons.go).
	GoalReasons GoalReasonsSource
	// The liveness lease (HOME-141, liveness.go): a send must show a task
	// within TaskLease (5 min), and a running turn a task event every
	// ProgressLease (20 min), or every ToolLease (60 min) while a tool call
	// is open (HOME-159). A cancellation kagent has not confirmed within
	// ForceDelete (10 min) deletes the session with force.
	TaskLease     time.Duration
	ProgressLease time.Duration
	ToolLease     time.Duration
	ForceDelete   time.Duration
	Links         Links
	Now           func() time.Time
	// AdmissionBudget bounds how long a tick waits for the admission worker.
	// Reads continue independently when it expires. Zero selects two seconds.
	AdmissionBudget time.Duration
	// AdmissionReadTimeout bounds one request, not a whole dependency graph.
	AdmissionReadTimeout time.Duration

	PlannerSchedules []planner.Schedule
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
	NotifyOutage  func(context.Context, string) error
	phaseFailures map[string]time.Time
	cfg           Config
	st            *store.Store
	its           Itsaplan
	tasks         Tasks
	sessions      Sessions
	gh            GitHub
	m             *metrics.Metrics
	log           *slog.Logger
	w             *store.Writer
	locks         sync.Map   // run ID -> *sync.Mutex
	starts        sync.Mutex // serialize slot checks and starts across admission and delivery
	// checked holds when each running attempt's session was last checked.
	checked         sync.Map // attempt ID -> sessionCheck
	admissionPolled sync.Map // run ID -> wall time; oldest admission is polled first
	admissionCtx    context.Context
	admissionCancel context.CancelFunc
	admissionDone   chan error // one worker, owned by Tick and joined by Release
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
	if cfg.TaskLease == 0 {
		cfg.TaskLease = 5 * time.Minute
	}
	if cfg.ProgressLease == 0 {
		cfg.ProgressLease = 20 * time.Minute
	}
	if cfg.ToolLease == 0 {
		cfg.ToolLease = 60 * time.Minute
	}
	if cfg.ForceDelete == 0 {
		cfg.ForceDelete = 10 * time.Minute
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.AdmissionBudget <= 0 {
		cfg.AdmissionBudget = 2 * time.Second
	}
	if cfg.AdmissionReadTimeout <= 0 {
		cfg.AdmissionReadTimeout = 30 * time.Second
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
	if err := w.SyncPlannerSchedules(ctx, e.cfg.PlannerSchedules); err != nil {
		w.Release()
		return false, fmt.Errorf("record planner schedules: %w", err)
	}
	e.w = w
	e.admissionCtx, e.admissionCancel = context.WithCancel(ctx)
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
	if e.admissionCancel != nil {
		e.admissionCancel()
	}
	if e.admissionDone != nil {
		<-e.admissionDone
		e.admissionDone = nil
	}
	e.w.Release()
	e.m.WriterEpoch.Set(0)
}

// guard checks the writer epoch before external side effects that do not use
// a writer transaction to check authority and record the send together.
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
	e.admissionPolled.Range(func(k, v any) bool {
		if !open[k.(int64)] {
			e.admissionPolled.Delete(k)
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
func (e *Engine) Tick(ctx context.Context) (result error) {
	ctx, span := tracing.StartPoll(ctx)
	var phaseErr error
	defer func() { tracing.FinishPoll(ctx, span, errors.Join(result, phaseErr)) }()

	if err := e.guard(ctx); err != nil {
		return err
	}
	ctx = context.WithValue(ctx, deferAdmissionKey{}, true)
	phases := []struct {
		name string
		fn   func(context.Context) error
	}{
		{"heartbeats", e.Heartbeats},
		{"deferred", e.sweepDeferred},
		{"claims", e.claim},
		{"quota", e.checkQuota},
		{"admissions", e.stepAdmissions},
		{"runs", e.stepRuns},
		{"leases", e.checkLeases},
		{"cancellations", e.stepCancellations},
		{"delivery", e.stepDelivery},
		{"delivery_reverts", e.checkDeliveryReverts},
		{"activation recovery", e.st.RecoverActivation},
		{"human_gates", e.stepHumanGates},
		{"planner", e.stepPlanner},
		{"outbox", e.flushOutbox},
		{"sessions", e.sweepSessions},
		{"admission_metrics", e.admissionMetrics},
	}
	for _, p := range phases {
		err := p.fn(ctx)
		if p.name != "admissions" && p.name != "outbox" {
			e.observePhase(ctx, p.name, err)
		}
		if err != nil {
			phaseErr = err
			span.AddEvent("phase failed", trace.WithAttributes(attribute.String("phase", p.name)))
			if fatal(err) {
				return err
			}
			e.m.TickErrors.WithLabelValues(p.name).Inc()
			e.log.ErrorContext(ctx, "dispatch phase failed", "phase", p.name, "error", err)
		}
	}
	return nil
}

func (e *Engine) observePhase(ctx context.Context, phase string, err error) {
	if !itsaplan.IsUnavailable(err) {
		e.m.ItsaplanUnavailable.DeleteLabelValues(phase)
		delete(e.phaseFailures, phase)
		return
	}
	now := e.cfg.Now()
	if e.phaseFailures == nil {
		e.phaseFailures = map[string]time.Time{}
	}
	since, ok := e.phaseFailures[phase]
	if !ok {
		e.m.ItsaplanUnavailable.WithLabelValues(phase).Set(1)
		e.phaseFailures[phase] = now
		return
	}
	e.notifyPhase(ctx, phase, since)
}

func (e *Engine) notifyPhase(ctx context.Context, phase string, since time.Time) {
	if e.cfg.Now().Sub(since) >= 10*time.Minute && e.NotifyOutage != nil {
		if err := e.NotifyOutage(ctx, phase); err != nil {
			e.log.ErrorContext(ctx, "itsaplan outage notification failed", "phase", phase, "error", err)
		}
	}
}

// checkQuota reads the broker's quota while a quota block stands or a
// running turn has had no task event for QuotaStall (HOME-127). An
// exhausted reading cancels each such quiet turn that a fresh read of its
// task still shows quiet; kagent's confirmation pauses it as a quota
// failure. A quiet turn with a tool call that has no result yet, such as a
// 30-minute ci run, is busy and never quiet. A usable reading cancels
// nothing.
//
// A usable reading clears quota blocks. Only a reading received after a
// block began clears it: the failed turn's own readings predate the block,
// so a grant that keeps failing pauses again until the broker's next
// reading, about every 30 minutes, instead of on every poll.
//
// The read is recorded under the writer's epoch before the call, as
// quotaReadIDs names it.
func (e *Engine) checkQuota(ctx context.Context) error {
	if e.cfg.Quota == nil || e.cfg.Now().Sub(e.quotaRead) < e.cfg.QuotaPoll {
		return nil
	}
	blocks, err := e.st.QuotaBlocks(ctx)
	if err != nil {
		return err
	}
	quiet, err := e.st.QuietAttempts(ctx, e.cfg.Now().Add(-e.cfg.QuotaStall))
	if err != nil || (len(blocks) == 0 && len(quiet) == 0) {
		return err
	}
	if err := e.st.RecordQuotaReads(ctx, e.w, quotaReadIDs(blocks, quiet)); err != nil {
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
	if len(blocks) == 0 || q.ReceivedAt.IsZero() {
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

// quotaReadIDs names a broker quota read once for each quota block and
// quiet turn it serves. The next poll, or a new writer after a takeover,
// finds the same block or turn and reuses its ID. A block's ID includes
// when it began, so a later block on the grant gets a new one.
func quotaReadIDs(blocks []store.QuotaBlock, quiet []*store.Attempt) []string {
	ids := make([]string, 0, len(blocks)+len(quiet))
	for _, b := range blocks {
		ids = append(ids, fmt.Sprintf("quota/block/%s/%d", b.Scope, b.CreatedAt.UnixMicro()))
	}
	for _, a := range quiet {
		ids = append(ids, fmt.Sprintf("quota/stall/%d", a.ID))
	}
	return ids
}

// cancelQuiet cancels each quiet turn that is still quiet when its task is
// read again. The stored activity can predate a tool call or event since
// the last progress read. A turn whose read fails keeps running this tick.
func (e *Engine) cancelQuiet(ctx context.Context, quiet []*store.Attempt) error {
	var errs []error
	for _, a := range quiet {
		if err := e.cancelIfQuiet(ctx, a, store.CancelQuotaStall); err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("attempt %d: %w", a.ID, err))
		}
	}
	return errors.Join(errs...)
}

// cancelIfQuiet reads the turn's task as progress does, with the read
// recorded first (BeginStatusRead). A changed task records its activity
// and restarts the quiet period. The turn is canceled, for reason
// quota_stall or lease_expired, only when its task is unchanged and has
// not finished. A tool call without a result always exempts the turn from
// quota_stall (QuietAttempts says why), and from lease_expired only until
// ToolLease (toolExpired). The cancellation records the task size it was
// decided on and stays pending until kagent confirms it, as every
// cancellation does. stepCancellation sends
// CancelTask, or withdraws the cancellation when the task changed first
// (withdrawQuiet). A lease also expires on a task kagent no longer has:
// its cancellation deletes the session.
func (e *Engine) cancelIfQuiet(ctx context.Context, a *store.Attempt, reason string) error {
	defer e.lockRun(a.RunID)()
	r, err := e.st.GetRun(ctx, a.RunID)
	if err != nil || r.State != store.RunDispatched {
		return err
	}
	wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
	if err != nil {
		return err
	}
	ctx, err = e.guardCaller(ctx, wi, a)
	if err != nil {
		return err
	}
	if err := e.guardAttempt(ctx, a, wi); err != nil {
		return err
	}
	if err := e.BeginStatusRead(ctx, taskReadID(a)); err != nil {
		return err
	}
	t, err := e.tasks.GetTask(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.TaskID)
	n, running := a.EventCount, false
	switch {
	case errors.Is(err, kagent.ErrTaskNotFound) && reason == store.CancelLeaseExpired:
	case errors.Is(err, kagent.ErrTaskNotFound):
		// progress handles a missing task.
		return nil
	case err != nil:
		return err
	default:
		n, running = taskSize(t), t.ToolRunning()
		if n != a.EventCount || a.EventAt.IsZero() {
			return e.st.SetAttemptEvents(ctx, e.st.DB, a.ID, n, running, e.cfg.Now())
		}
		if kagent.Quiescent(t.Status.State) || (running && (reason != store.CancelLeaseExpired || !e.toolExpired(a))) {
			return nil
		}
	}
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		return e.st.RequestQuietCancel(ctx, tx, a.ID, a.RunID, reason, n)
	}); err != nil {
		return err
	}
	if reason == store.CancelLeaseExpired {
		why := "no task event for %s"
		if running {
			why = "a tool call open with no task event for %s"
		}
		e.leaseRequested(ctx, a, fmt.Sprintf(why, e.cfg.Now().Sub(a.EventAt)))
		return nil
	}
	e.m.Cancellations.WithLabelValues(store.CancelQuotaStall, "requested").Inc()
	e.log.WarnContext(ctx, "canceling a quiet turn: the broker reads the quota exhausted", "run", a.RunID,
		"attempt", a.Seq, "task", a.TaskID, "quiet_for", e.cfg.Now().Sub(a.EventAt))
	return nil
}

// toolExpired reports whether a turn with an open tool call is past
// ToolLease: its task has not changed for that long (HOME-159). kagent keeps
// a tool call open when the runtime that ran it dies, so an open call alone
// is no proof of progress.
func (e *Engine) toolExpired(a *store.Attempt) bool {
	return !a.EventAt.IsZero() && e.cfg.Now().Sub(a.EventAt) >= e.cfg.ToolLease
}

// taskReadID is the read journal's message ID of a quiet turn's task read.
// Each poll and a new writer after a takeover reuse it.
func taskReadID(a *store.Attempt) string {
	return "kagent/GetTask/" + a.KagentNamespace + "/" + a.KagentAgent + "/" + a.TaskID
}

// RunHeartbeats keeps leases alive independently of the tick, so a slow
// kagent or session call cannot let itsaplan expire a run.
func (e *Engine) RunHeartbeats(ctx context.Context, every time.Duration) error {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		if err := e.HeartbeatPass(ctx); err != nil {
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

// HeartbeatPass is one pass of the heartbeat loop under its own root span,
// so the runs and PRs it checks, and its failures, are on a trace.
func (e *Engine) HeartbeatPass(ctx context.Context) (result error) {
	ctx, span := tracing.StartHeartbeat(ctx)
	defer func() { tracing.FinishPoll(ctx, span, result) }()
	return e.Heartbeats(ctx)
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
func (e *Engine) heartbeatRun(ctx context.Context, id int64) (result error) {
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
	if trace.SpanFromContext(ctx).IsRecording() {
		run, issue, state, workID := r.ID, r.IssueKey, r.State, r.WorkItemID
		defer func() {
			attrs := []attribute.KeyValue{attribute.Int64("run.id", run), attribute.String("issue.key", issue), attribute.String("state.from", state)}
			attrs = append(attrs, e.workAttrs(ctx, workID)...)
			outcome := "heartbeat"
			if result != nil {
				outcome = "error"
			}
			tracing.Observe(ctx, "run heartbeat", append(attrs, attribute.String("outcome", outcome))...)
		}()
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
		if err := e.tryStep(ctx, r.ID); err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("run %d: %w", r.ID, err))
		}
	}
	return errors.Join(errs...)
}

// A slow admission owns only its run's lock. The ordinary delivery loop must
// never wait for that lock; the worker will advance the run itself.
func (e *Engine) tryStep(ctx context.Context, id int64) error {
	unlock, ok := e.tryLockRun(id)
	if !ok {
		return nil
	}
	defer unlock()
	r, err := e.st.GetRun(ctx, id)
	if err != nil {
		return err
	}
	return e.step(ctx, r)
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
func (e *Engine) step(ctx context.Context, r *store.Run) (result error) {
	if trace.SpanFromContext(ctx).IsRecording() {
		id, issue, source, workID := r.ID, r.IssueKey, r.State, r.WorkItemID
		defer func() {
			attrs := []attribute.KeyValue{attribute.Int64("run.id", id), attribute.String("issue.key", issue), attribute.String("state.from", source)}
			outcome := "unchanged"
			current, err := e.st.GetRun(ctx, id)
			if err == nil {
				// Dispatch can link the run to a work item during this step.
				if current.WorkItemID != 0 {
					workID = current.WorkItemID
				}
				attrs = append(attrs, attribute.String("state.to", current.State))
				if current.State != source {
					outcome = "advanced"
				}
			}
			if result != nil || err != nil {
				outcome = "error"
			}
			attrs = append(attrs, e.workAttrs(ctx, workID)...)
			tracing.Observe(ctx, "run observed", append(attrs, attribute.String("outcome", outcome))...)
		}()
	}
	// Only an unlinked itsaplan run is new work that may resume delivery.
	// A linked run, held for admission or waiting for its brief, has already
	// been admitted and must honor any later stop. A stop of an unlinked run
	// (`connector cancel` with no live work item) is on the run itself.
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
		// A run claimed before its work item's PR merged has nothing left to
		// deliver: it carries a stop, as StopWork's runs do (HOME-141).
		if wi.State == store.WorkDone {
			stopped, err := e.st.StopMergedRun(ctx, r.ID)
			if err != nil {
				return err
			}
			if stopped {
				if r, err = e.st.GetRun(ctx, r.ID); err != nil {
					return err
				}
				return e.stopRun(ctx, r, r.StoppedReason)
			}
		}
	}
	if !r.Connector() && (r.State == store.RunClaimed || r.State == store.RunWaiting || r.State == store.RunDispatched) {
		a, err := e.st.LatestAttempt(ctx, r.ID)
		if err != nil {
			return err
		}
		if a == nil {
			if deferred, _ := ctx.Value(deferAdmissionKey{}).(bool); deferred {
				return nil
			}
			// A linked run's stop is on the run itself (StopWork); the stop
			// counter fences only a delegation not yet linked to its work item.
			if r.WorkItemID == 0 {
				if stopped, err := e.st.AdmissionStopped(ctx, r); err != nil {
					return err
				} else if stopped {
					return e.failRun(ctx, e.st.DB, r, store.ErrAdmissionStopped.Error())
				}
				if superseded, err := e.st.Superseded(ctx, e.st.DB, r.ID); err != nil {
					return err
				} else if superseded {
					return e.failRun(ctx, e.st.DB, r, store.ErrSuperseded.Error())
				}
			}
			if ready, err := e.admit(ctx, r); err != nil || !ready {
				return err
			}
			if r.WorkItemID == 0 {
				return e.dispatch(ctx, r)
			}
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
	usage, err := e.st.RunUsage(ctx, q, r.ID)
	if err != nil {
		return err
	}
	res.Usage = usage
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
	if !e.starts.TryLock() {
		return nil
	}
	defer e.starts.Unlock()
	if full, err := e.slotsFull(ctx, r); err != nil || full {
		return err
	}
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
			Binding: b.Binding, FirstArm: provenance.SelectArm(r.IssueKey, b.Arms), IssueID: r.IssueID, IssueKey: r.IssueKey, AgentKey: r.AgentKey,
		})
	}
	if errors.Is(err, store.ErrAdmissionStopped) {
		if current, readErr := e.st.GetRun(ctx, r.ID); readErr != nil {
			return readErr
		} else if current.StopRequested {
			return e.stopRun(ctx, current, current.StoppedReason)
		}
		return e.failRun(ctx, e.st.DB, r, err.Error())
	}
	if errors.Is(err, store.ErrSuperseded) {
		return e.failRun(ctx, e.st.DB, r, err.Error())
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

// slotsFull reports that a first admission finds no free slot for its agent
// key. It checks a waiting run and every itsaplan run with no delivery
// attempt, whatever its state: a held run whose lease lapsed returns
// dispatched with no attempt. The run is not counted as an occupant. A full
// run waits, as other runs over the limit do, and keeps its admission result.
func (e *Engine) slotsFull(ctx context.Context, r *store.Run) (bool, error) {
	if r.State != store.RunWaiting {
		if r.Connector() {
			return false, nil
		}
		if a, err := e.st.LatestAttempt(ctx, r.ID); err != nil || a != nil {
			return false, err
		}
	}
	busy, err := e.st.CountBusyRuns(ctx, r.AgentKey, r.ID)
	if err != nil || busy < e.concurrency(r.AgentKey) {
		return false, err
	}
	if r.State != store.RunWaiting {
		if err := e.st.SetRunState(ctx, e.st.DB, r.ID, store.RunWaiting, ""); err != nil {
			return true, err
		}
		r.State = store.RunWaiting
	}
	return true, nil
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
	if !e.starts.TryLock() {
		return nil
	}
	defer e.starts.Unlock()
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
	busy, err := e.st.CountBusyRuns(ctx, r.AgentKey, r.ID)
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
// has its own message ID. Like an engineering retry, a resume without a
// session keeps the failed attempt's context when it holds the issue brief.
func (e *Engine) resumeQuotaFailure(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	kind, brief := "continuation", ""
	if wi.SessionID == "" {
		kind, brief = "initial", a.Context
		if !hasIssueBrief(brief, wi.IssueKey) {
			var err error
			if brief, err = e.issueBrief(ctx, r, wi); err != nil {
				return err
			}
		}
	}
	var next *store.Attempt
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
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
	// Admission's last issue read supplies this poll's first attempt. Only
	// attachment names are read here then, as for any new session.
	issue := r.AdmissionIssue
	var err error
	if issue == nil {
		if err := e.beginAdmissionRead(ctx, r, "brief", strconv.FormatInt(wi.IssueID, 10)); err != nil {
			return "", err
		}
		issue, err = e.its.GetIssue(ctx, wi.IssueID)
		e.countRefusal(roleCoordinator, lookupBrief, err)
	}
	if err == nil && (issue == nil || issue.ID != wi.IssueID || strings.TrimSpace(issue.Title+issue.Description) == "") {
		err = errors.New("missing authoritative issue text")
	}
	var attachments []itsaplan.Attachment
	if err == nil {
		if err = e.beginAdmissionRead(ctx, r, "attachments", strconv.FormatInt(wi.IssueID, 10)); err != nil {
			return "", err
		}
		attachments, err = e.its.GetAttachments(ctx, wi.IssueID)
		e.countRefusal(roleCoordinator, lookupBrief, err)
	}
	if err != nil {
		// Required input never expires into permission to start without it.
		// Announce the hold once per run attempt, without exposing response bodies.
		noticeErr := e.w.Tx(ctx, func(tx *sql.Tx) error {
			return e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/brief-held/%d/%d", r.ID, r.Attempt), r.ID, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"The factory is holding %s run %d until its issue and attachment names can be read. No new agent session will start without the issue text. Check the issue and the coordinator's itsaplan access; the lookup will retry automatically.",
					wi.IssueKey, r.ID)})
		})
		return "", errors.Join(fmt.Errorf("issue brief required before opening a session: %w", err), noticeErr)
	}
	return renderIssueBrief(issueBriefIntro, wi.IssueKey, issue, attachments), nil
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
		if !r.Connector() {
			a, lookupErr := e.st.LatestAttempt(ctx, r.ID)
			if lookupErr != nil {
				return lookupErr
			}
			if a == nil {
				return e.admissionFailure(ctx, r, "Waiting for the issue brief to be readable.", err)
			}
		}
		return err
	}
	var a *store.Attempt
	var stopped bool
	err = e.st.Tx(ctx, func(tx *sql.Tx) error {
		// Stops stand: a stop that committed during admission or the brief
		// read leaves no attempt. The next step stops the run with its reason.
		var err error
		if stopped, err = e.st.LockStop(ctx, tx, wi.ID, r.ID); err != nil || stopped {
			return err
		}
		a, err = e.recordNext(ctx, tx, r, wi, retry, brief)
		return err
	})
	if err != nil || stopped {
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
	text, err := e.prepareProvenance(ctx, r, wi, b, a)
	if errors.Is(err, errLegacyRuntime) {
		return e.recoverHeld(ctx, r, wi, a, recoveryLifecycle)
	}
	if errors.Is(err, errRuntimeChanged) {
		return e.replaceRuntimeAttempt(ctx, r, wi, b, a)
	}
	if err != nil {
		return err
	}
	if issued, err := e.st.MarkIssued(ctx, e.w, a, r.Attempt, now); err != nil || !issued {
		return err
	}
	task, err := e.tasks.Send(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, kagent.Message{
		MessageID: a.MessageID,
		ContextID: a.ContextID,
		Parts:     []kagent.Part{{Text: text}},
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

// unresolved only adopts positive evidence of the original message. It
// never resends: NodeGone deletion may hide an orphan actor, and idle expiry
// can remove even its initial-message creation receipt. Past TaskLease the
// lease's cancellation decides what absence means (stepCancellation) and
// holds the attempt for an operator when it cannot.
func (e *Engine) unresolved(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	t, err := e.taskWithMessage(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.ContextID, a.MessageID)
	if err == nil && t != nil {
		return e.attach(ctx, a, wi, t)
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
	if errors.Is(err, itsaplan.ErrBackoff) {
		return true
	}
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
	if a.State == store.AttemptAccepted || a.State == store.AttemptAmbiguous {
		gone, err := e.st.WorkerGone(ctx, a)
		if err != nil {
			return err
		}
		if gone {
			return e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
				return e.st.RequestQuietCancel(ctx, tx, a.ID, r.ID, store.CancelLeaseExpired, a.EventCount)
			})
		}
	}
	if a.State == store.AttemptFailed && a.ErrorClass == ClassAbandoned {
		// The liveness lease ended the turn; retry within the budget.
		return e.engineeringFailure(ctx, r, wi, a, a.Error)
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
	if pending, err := e.st.CancelPending(ctx, a.ID); err != nil {
		return err
	} else if pending {
		// The cancellation that deletes the session decides the turn.
		if deleting, err := e.st.ForceDeleting(ctx, a.ContextID); err != nil || deleting {
			return err
		}
	}
	if err := e.guardAttempt(ctx, a, wi); err != nil {
		return err
	}
	metadata, err := e.taskMetadata(ctx, a)
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
	if a.Error == boundedOutput {
		return e.outputLimited(ctx, r, wi, a, metadata)
	}
	if err := e.guardAttempt(ctx, a, wi); err != nil {
		return err
	}
	t, err := e.tasks.GetTask(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.TaskID)
	if errors.Is(err, kagent.ErrResponseTooLarge) {
		return e.outputLimited(ctx, r, wi, a, metadata)
	}
	if err != nil {
		return err
	}
	if t == nil || t.ID != a.TaskID || t.ContextID != a.ContextID {
		return errors.New("task transcript: invalid task identity")
	}
	// Lifecycle decisions use the bounded metadata observation. A newer
	// transcript state will be observed by the next metadata poll.
	t.Status.State = metadata.Status.State
	if gone, err := e.postEvents(ctx, r, t); err != nil || gone {
		return err
	}
	// The first read records the task even when it is empty: a turn is
	// quiet only after a read (QuietAttempts).
	if n := taskSize(t); n != a.EventCount || a.EventAt.IsZero() {
		if err := e.st.SetAttemptEvents(ctx, e.st.DB, a.ID, n, t.ToolRunning(), e.cfg.Now()); err != nil {
			return err
		}
	}
	if kagent.Quiescent(t.Status.State) {
		// A pending cancellation reports the turn once kagent confirms it.
		// A lease_expired one that sent no CancelTask yields to a turn that
		// ended on its own.
		if pending, err := e.st.CancelPending(ctx, a.ID); err != nil {
			return err
		} else if pending {
			if ended, err := e.leaseYields(ctx, r, wi, a); err != nil || !ended {
				return err
			}
		}
		e.checked.Delete(a.ID)
		return e.complete(ctx, r, wi, a, t)
	}
	return e.watchSession(ctx, r, wi, a)
}

// taskSize grows with every task event the connector reads: history
// messages, artifacts and their parts, and artifact text. A tool call and
// its result are each an artifact with one data part.
func taskSize(t *kagent.Task) int {
	n := len(t.History)
	for _, a := range t.Artifacts {
		if a != nil {
			n++
			for _, p := range a.Parts {
				n += 1 + len(p.Text)
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
	return e.sendEvents(ctx, r, t.ID, events, base)
}

// sendEvents posts a persisted journal with itsaplan's attempt and offset.
// Admission and task journals share one transcript without sharing boundaries.
func (e *Engine) sendEvents(ctx context.Context, r *store.Run, taskID string, events []itsaplan.Event, base int) (bool, error) {
	offset := r.EventOffset
	if r.EventAttempt != r.Attempt {
		offset = 0
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
			if err := e.guard(ctx); err != nil {
				return false, err
			}
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
		if err := e.st.SetEventOffset(ctx, r.ID, r.Attempt, offset, taskID, base); err != nil {
			return false, err
		}
		r.EventAttempt, r.EventOffset, r.EventTaskID, r.EventTaskBase = r.Attempt, offset, taskID, base
	}
	return false, nil
}

// complete handles a quiescent task: the turn finished, failed or waits.
func (e *Engine) complete(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt, t *kagent.Task) error {
	state := t.Status.State
	switch state {
	case kagent.StateCompleted, kagent.StateInputRequired:
		res := itsaplan.Result{Status: "success", Output: Output(t)}
		return e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
			if err := e.st.CompleteTask(ctx, tx, a.ID, state); err != nil {
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
// announces the block once and leaves the run waiting without a slot. A
// stop of the run or work item wins: blockTurn locks both first and does
// nothing when either is stopped, so step's stopRun reports the stop.
func (e *Engine) blockTurn(ctx context.Context, tx *sql.Tx, r *store.Run, wi *store.WorkItem, a *store.Attempt, class, msg string) error {
	if stopped, err := e.st.LockStop(ctx, tx, wi.ID, r.ID); err != nil || stopped {
		return err
	}
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
		var err error
		if !hasIssueBrief(brief, wi.IssueKey) && a.ErrorClass == ClassAbandoned {
			// An abandoned continuation recorded no brief; its session did.
			if brief, err = e.recordedBrief(ctx, wi); err != nil {
				return err
			}
		}
		if !hasIssueBrief(brief, wi.IssueKey) {
			brief, err = e.issueBrief(ctx, r, wi)
			if err != nil {
				return err
			}
		}
		if a.ErrorClass == ClassAbandoned {
			brief += replacementNote
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
	// kagent has not confirmed the cancellation in time: deleting the
	// session with force ends the turn, and settles a lifecycle hold too.
	escalate, err := e.escalating(ctx, c, a)
	if err != nil {
		return err
	}
	if reason, err := e.recoveryReason(ctx, a); err != nil {
		return err
	} else if reason == recoveryLifecycle && !escalate {
		return e.recoverHeld(ctx, r, wi, a, reason)
	}
	if a.State == store.AttemptSending {
		return e.confirmCancel(ctx, c, a, "not_accepted")
	}
	ref := kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}
	if a.TaskID == "" {
		// The send's outcome is unknown. A cancellation never sends: that
		// could start the turn it is meant to stop.
		if e.cfg.Now().Sub(a.SentAt) < e.cfg.DispatchWindow {
			return nil
		}
		// Only the task holding this attempt's message is a target. The
		// all-state listing covers the original caller's sessions, including
		// a first send whose session the work item has not recorded yet.
		t, err := e.taskWithMessage(ctx, ref, a.ContextID, a.MessageID)
		if err != nil && escalate {
			// A failed lookup can hide a completed send. Retry reconciliation
			// before using worker-loss evidence to delete this session.
			if gone, checkErr := e.st.WorkerGone(ctx, a); gone || checkErr != nil {
				return errors.Join(err, checkErr)
			}
		}
		switch {
		case err == nil && t == nil && e.cfg.Now().Sub(a.SentAt) < notStartedWithin:
			// A complete scan finds no task with the message after the
			// dispatch window. kagent rejects a first save after its
			// two-minute dispatch reservation, before native execution
			// (https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/core/internal/database/session_tasks.go#L29-L32,
			// https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/core/internal/database/session_tasks.go#L192-L203),
			// so the send never started and never will (HOME-141).
			e.m.StuckTurns.WithLabelValues(stepNotStarted).Inc()
			return e.confirmCancel(ctx, c, a, stepNotStarted)
		case (err != nil || t == nil) && escalate:
			return e.forceDelete(ctx, c, r, wi, a)
		case (err != nil || t == nil) && a.ContextID != "" && e.sessions != nil:
			// Deleting the named session settles it after ForceDelete.
			return err
		case err != nil || t == nil:
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
	t, err := e.taskMetadata(ctx, a)
	switch {
	case errors.Is(err, kagent.ErrTaskNotFound) && escalate:
		return e.forceDelete(ctx, c, r, wi, a)
	case errors.Is(err, kagent.ErrTaskNotFound):
		return e.missingTask(ctx, r, wi, a)
	case err != nil:
		return err
	}
	if _, err := e.originalVisible(ctx, a); err != nil {
		return err
	}
	if kagent.Terminal(t.Status.State) {
		return e.confirmCancel(ctx, c, a, strings.ToLower(strings.TrimPrefix(t.Status.State, "TASK_STATE_")))
	}
	if escalate {
		return e.forceDelete(ctx, c, r, wi, a)
	}
	if (c.Reason == store.CancelQuotaStall || c.Reason == store.CancelLeaseExpired) && c.Requests == 0 {
		if withdrawn, err := e.withdrawQuiet(ctx, c, r, wi, a); err != nil || withdrawn {
			return err
		}
	}
	if c.Requests > 0 && e.cfg.Now().Sub(c.LastAt) < e.cfg.CancelInterval {
		return nil
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	if err := e.st.CountCancelRequest(ctx, e.st.DB, c.ID, e.cfg.Now()); err != nil {
		return err
	}
	if _, err := e.tasks.CancelTask(ctx, ref, a.TaskID); err != nil {
		// The task may have finished meanwhile; the next read decides.
		e.log.WarnContext(ctx, "cancel request failed", "task", a.TaskID, "error", err)
	}
	return nil
}

// withdrawQuiet withdraws a quota_stall or lease_expired cancellation,
// before its first CancelTask, whose task changed after the quiet decision:
// the task grew past the size the decision recorded, or has a tool call
// without a result. A lease_expired cancellation decided on an open tool
// call past ToolLease stands while the task has not grown: the same open
// call is no proof of progress (toolExpired).
// The turn runs on, and the change restarts its quiet period. It reports
// whether it withdrew the cancellation. A stop's or itsaplan's request
// joins a pending cancellation (RequestCancellation), so a stopped run or
// work item, or a run no longer dispatched, keeps it. Once CancelTask is
// out the cancellation stands: kagent may still apply it, and its
// confirmation then pauses the turn as a quota failure, or abandons it.
func (e *Engine) withdrawQuiet(ctx context.Context, c *store.Cancellation, r *store.Run, wi *store.WorkItem, a *store.Attempt) (bool, error) {
	// Lifecycle metadata omits artifacts and cannot prove that the turn is
	// still quiet. Read activity separately before this automatic cancel.
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	messageID := taskReadID(a)
	if err := e.BeginStatusRead(ctx, messageID); err != nil {
		return false, err
	}
	t, err := e.tasks.GetTask(kagent.WithRequestID(ctx, messageID), kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.TaskID)
	limited := errors.Is(err, kagent.ErrResponseTooLarge)
	if err != nil && !limited {
		return false, err
	}
	var n int
	var running bool
	if !limited {
		if t == nil || t.ID != a.TaskID || t.ContextID != a.ContextID {
			return false, errors.New("quiet cancellation activity: invalid task identity")
		}
		n, running = taskSize(t), t.ToolRunning()
	}
	if !limited && n == c.EventCount && (!running || (c.Reason == store.CancelLeaseExpired && e.toolExpired(a))) {
		return false, nil
	}
	why := "withdrawn: the task changed before CancelTask"
	if limited {
		why = "withdrawn: task activity exceeds the transcript budget"
	} else if running {
		why = "withdrawn: a tool call started before CancelTask"
	}
	var withdrawn bool
	err = e.w.Tx(ctx, func(tx *sql.Tx) error {
		withdrawn = false
		if stopped, err := e.st.LockStop(ctx, tx, wi.ID, r.ID); err != nil || stopped {
			return err
		}
		var err error
		if withdrawn, err = e.st.WithdrawQuietCancel(ctx, tx, c.ID, why); err != nil || !withdrawn || limited || n == a.EventCount {
			return err
		}
		return e.st.SetAttemptEvents(ctx, tx, a.ID, n, running, e.cfg.Now())
	})
	if err != nil || !withdrawn {
		return false, err
	}
	e.m.Cancellations.WithLabelValues(c.Reason, store.CancelWithdrawn).Inc()
	e.log.InfoContext(ctx, "quiet turn's cancellation withdrawn", "reason", c.Reason, "run", r.ID, "attempt", a.Seq,
		"task", a.TaskID, "outcome", why)
	return true, nil
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
	if c.Reason == store.CancelLeaseExpired && r.State == store.RunDispatched {
		return e.abandonTx(ctx, tx, r, a, outcome)
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
// meanwhile keeps its attempt open, and progress reads its outcome. So does
// a turn whose run or work item was stopped, as blockTurn checks under lock.
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
	if deferred, _ := ctx.Value(deferAdmissionKey{}).(bool); deferred {
		// Claims join the same queue as existing holds, without spending
		// their read time first or taking an execution slot while queued.
		e.admissionPolled.LoadOrStore(r.ID, time.Now())
		if r.WorkItemID == 0 && r.HoldReason == "" {
			return e.st.SetAdmissionHold(ctx, e.st.DB, r.ID, "Waiting for admission.", e.cfg.Now())
		}
		return nil
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
				now := e.cfg.Now()
				var since *time.Time
				if itsaplan.IsUnavailable(err) {
					since = &now
				}
				if saveErr := e.st.OutboxFailed(ctx, it.ID, clip(err.Error(), maxErrorText), now.Add(backoff), since); saveErr != nil {
					return errors.Join(err, saveErr)
				}
			}
			errs = append(errs, fmt.Errorf("outbox %d: %w", it.ID, err))
		}
	}
	since, readErr := e.st.PendingOutage(ctx)
	if readErr != nil {
		errs = append(errs, readErr)
	} else if since.Valid {
		e.m.ItsaplanUnavailable.WithLabelValues("outbox").Set(1)
		e.notifyPhase(ctx, "outbox", since.Time)
	} else {
		e.m.ItsaplanUnavailable.DeleteLabelValues("outbox")
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
// Admission, new sessions and fenced recovery require issue text regardless
// of this bound and retry without consuming a new delivery attempt.
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
	case store.OutboxDeferredStart:
		return e.deliverDeferred(ctx, it)
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
				return e.st.OutboxFailed(ctx, it.ID, "result not accepted; run still pending", e.cfg.Now().Add(e.cfg.Heartbeat), nil)
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
	if it.Kind == store.OutboxActivation {
		return e.deliverActivation(ctx, it)
	}
	return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "unknown kind "+it.Kind)
}

// workAttrs returns the PR number, head SHA and review round of a run's work
// item, so a poll that only advances a run can still be followed to its PR.
func (e *Engine) workAttrs(ctx context.Context, workID int64) []attribute.KeyValue {
	if workID == 0 {
		return nil
	}
	var attrs []attribute.KeyValue
	if wi, err := e.st.GetWorkItem(ctx, workID); err == nil {
		attrs = append(attrs, attribute.Int("round", wi.ReviewRounds))
	}
	if pr, err := e.st.PullRequestFor(ctx, workID); err == nil && pr != nil {
		attrs = append(attrs, attribute.Int("pr.number", pr.Number), attribute.String("head.sha", pr.HeadSHA))
	}
	return attrs
}
