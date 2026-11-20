package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/tracing"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
	"go.opentelemetry.io/otel/trace"
)

const admissionTask = "factory-admission"

type (
	deferAdmissionKey struct{}
	admissionLockKey  struct{}
)

type blockerCacheKey struct{}

type admissionLock struct{ unlock func() }

var errAdmissionChanged = errors.New("run changed while reading admission evidence")

// stepAdmissions gives one serialized worker a snapshot of the queue. New
// claims cannot jump ahead of this pass. A tick waits briefly for fast reads,
// then carries on with delivery while the worker finishes the same fresh poll.
// The worker never caches an admission decision for a later tick to consume.
func (e *Engine) stepAdmissions(ctx context.Context) error {
	var previous error
	if e.admissionDone != nil {
		select {
		case previous = <-e.admissionDone:
			e.observePhase(ctx, "admissions", previous)
			e.admissionDone = nil
			if fatal(previous) {
				return previous
			}
		default:
		}
	}
	if e.admissionDone == nil {
		runs, err := e.st.OpenRuns(ctx)
		if err != nil {
			return err
		}
		e.orderAdmissions(runs)
		done := make(chan error, 1)
		e.admissionDone = done
		go func() {
			ctx := context.WithValue(e.admissionCtx, blockerCacheKey{}, map[int64]*itsaplan.Issue{})
			// A pass that admits a run gets its own root; an empty pass none.
			var span trace.Span
			var errs []error
			defer func() {
				err := errors.Join(errs...)
				if span != nil {
					tracing.FinishPoll(ctx, span, err)
				}
				done <- err
			}()
			for _, r := range runs {
				if r.Connector() || (r.State != store.RunClaimed && r.State != store.RunWaiting && r.State != store.RunDispatched) {
					continue
				}
				// Do not wait on a lock held by normal task progression. The
				// locked step below rechecks eligibility before any mutation.
				a, err := e.st.LatestAttempt(e.admissionCtx, r.ID)
				if err != nil {
					errs = append(errs, err)
					if fatal(err) {
						return
					}
					continue
				}
				if a != nil {
					continue
				}
				if span == nil {
					ctx, span = tracing.StartAdmission(ctx)
				}
				if err := e.stepAdmission(ctx, r.ID); err != nil {
					errs = append(errs, fmt.Errorf("run %d: %w", r.ID, err))
					if fatal(err) {
						return
					}
				}
			}
		}()
	}
	timer := time.NewTimer(e.cfg.AdmissionBudget)
	defer timer.Stop()
	select {
	case err := <-e.admissionDone:
		e.observePhase(ctx, "admissions", err)
		e.admissionDone = nil
		return errors.Join(previous, err)
	case <-timer.C:
		return previous
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (e *Engine) stepAdmission(ctx context.Context, id int64) error {
	lock := &admissionLock{unlock: e.lockRun(id)}
	defer func() { lock.unlock() }()
	ctx = context.WithValue(ctx, admissionLockKey{}, lock)
	r, err := e.st.GetRun(ctx, id)
	if err != nil {
		return err
	}
	if r.Connector() || (r.State != store.RunClaimed && r.State != store.RunWaiting && r.State != store.RunDispatched) {
		return nil
	}
	a, err := e.st.LatestAttempt(ctx, id)
	if err != nil || a != nil {
		return err
	}
	return e.step(ctx, r)
}

// Rotate held runs by last poll. Claims enter at their arrival time, so older
// holds precede new claims. Restart discards all in-progress reads and decisions.
func (e *Engine) orderAdmissions(runs []*store.Run) {
	last := func(id int64) time.Time {
		if at, ok := e.admissionPolled.Load(id); ok {
			return at.(time.Time)
		}
		return time.Time{}
	}
	sort.SliceStable(runs, func(i, j int) bool { return last(runs[i].ID).Before(last(runs[j].ID)) })
}

// beginAdmissionRead follows the read-attempt protocol: stable identity,
// durable intent, then a fresh writer check immediately before the call.
func (e *Engine) beginAdmissionRead(ctx context.Context, r *store.Run, operation, resource string) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	id := fmt.Sprintf("admission/%d/%d/%s/%s", r.ID, r.Attempt, operation, resource)
	if err := e.st.RecordAdmissionRead(ctx, r, e.w.Epoch, id, operation, resource); err != nil {
		return err
	}
	return e.guard(ctx)
}

func (e *Engine) admissionIssue(ctx context.Context, r *store.Run, id int64) (*itsaplan.Issue, error) {
	var is *itsaplan.Issue
	err := e.readAdmission(ctx, r, "issue", strconv.FormatInt(id, 10), func(callCtx context.Context) error {
		var err error
		is, err = e.its.GetIssue(callCtx, id)
		return err
	})
	e.countRefusal(roleCoordinator, "admission", err)
	if err == nil && (is == nil || is.ID != id || is.Identifier == "" || is.Links == nil) {
		err = errors.New("incomplete issue or relations response")
	}
	return is, err
}

func (e *Engine) admissionColumns(ctx context.Context, r *store.Run, project string) ([]itsaplan.Column, error) {
	var columns []itsaplan.Column
	err := e.readAdmission(ctx, r, "columns", project, func(callCtx context.Context) error {
		var err error
		columns, err = e.its.ProjectColumns(callCtx, project)
		return err
	})
	e.countRefusal(roleCoordinator, "admission", err)
	return columns, err
}

// Each request has its own deadline. Successful reads accumulate within this
// poll without imposing a deadline on the entire graph. Release the run lock
// only around read-only I/O so heartbeats, cancellation and redelivery remain
// live even when a traversal outlasts the lease. Revalidate under the lock
// before using the response; a changed or stopped run discards the entire poll.
func (e *Engine) readAdmission(ctx context.Context, r *store.Run, operation, resource string, read func(context.Context) error) error {
	callCtx, cancel := context.WithTimeout(ctx, e.cfg.AdmissionReadTimeout)
	defer cancel()
	if err := e.beginAdmissionRead(callCtx, r, operation, resource); err != nil {
		return err
	}
	lock, _ := ctx.Value(admissionLockKey{}).(*admissionLock)
	err := func() error {
		if lock != nil {
			lock.unlock()
			defer func() { lock.unlock = e.lockRun(r.ID) }()
		}
		return read(callCtx)
	}()
	if lock != nil {
		current, readErr := e.st.GetRun(ctx, r.ID)
		if readErr != nil {
			return readErr
		}
		if current.Attempt != r.Attempt || current.State != r.State || current.WorkItemID != r.WorkItemID ||
			current.StopRequested {
			return errAdmissionChanged
		}
		a, readErr := e.st.LatestAttempt(ctx, r.ID)
		if readErr != nil {
			return readErr
		}
		if a != nil {
			return errAdmissionChanged
		}
		r.SeenVisible, r.LastHeartbeatAt = current.SeenVisible, current.LastHeartbeatAt
	}
	return err
}

type admissionNotice struct {
	key, body string
	issueID   int64
}

// admit checks only runs with no delivery attempt, before resolving a work
// item can reassign/cancel another task or clear a grant. Reads are never cached
// across polls or restart. Completed issues break dependency chains.
func (e *Engine) admit(ctx context.Context, r *store.Run) (bool, error) {
	b, err := e.bindingFor(r)
	if err != nil || r.IssueID == 0 {
		return true, nil // dispatch reports the invalid binding or issue
	}
	e.admissionPolled.Store(r.ID, time.Now())
	root, err := e.admissionIssue(ctx, r, r.IssueID)
	if err != nil {
		return false, e.admissionFailure(ctx, r, "Waiting for the issue brief and dependency relations to be readable.", err, briefNotice(r))
	}
	if root.Identifier != r.IssueKey {
		return false, e.holdAdmission(ctx, r, "Waiting for the issue to match the claimed repository binding.", nil)
	}
	columns, err := e.admissionColumns(ctx, r, b.ItsaplanProject)
	if err != nil {
		return false, e.admissionFailure(ctx, r, "Waiting for the issue's current workflow state.", err)
	}
	states := map[int64]string{}
	for _, c := range columns {
		states[c.ID] = c.StateType
	}
	if done, err := e.terminalRoot(ctx, r, states[root.ColumnID]); err != nil || done {
		return false, err
	}
	if states[root.ColumnID] == "" {
		return false, e.holdAdmission(ctx, r, "Waiting for the issue's current workflow state.", nil)
	}
	if strings.TrimSpace(root.Description) == "" {
		return false, e.holdAdmission(ctx, r, "Waiting for an issue brief: the description is empty.", []admissionNotice{briefNotice(r)})
	}
	issues := map[int64]*itsaplan.Issue{root.ID: root}
	graph := map[int64][]int64{}
	var waiting []string
	var notices []admissionNotice
	cache, _ := ctx.Value(blockerCacheKey{}).(map[int64]*itsaplan.Issue)
	var visit func(*itsaplan.Issue) error
	visit = func(is *itsaplan.Issue) error {
		if _, seen := graph[is.ID]; seen {
			return nil
		}
		graph[is.ID] = nil
		for _, link := range is.Links {
			if !link.BlockedBy() {
				continue
			}
			if link.Issue.ID <= 0 {
				return errors.New("blocked_by link has no issue ID")
			}
			blocker := issues[link.Issue.ID]
			if blocker == nil {
				if len(issues) >= 256 {
					return errors.New("dependency graph exceeds 256 issues")
				}
				var err error
				blocker = cache[link.Issue.ID]
				if blocker == nil {
					blocker, err = e.admissionIssue(ctx, r, link.Issue.ID)
					if err == nil && cache != nil {
						cache[blocker.ID] = blocker
					}
				}
				if err != nil {
					return err
				}
				issues[blocker.ID] = blocker
			}
			// itsaplan only permits links within a project. A changed project
			// or unknown column is incomplete evidence, never completion.
			if !strings.HasPrefix(blocker.Identifier, b.ItsaplanProject+"-") || states[blocker.ColumnID] == "" {
				return fmt.Errorf("unknown project or column for %s", blocker.Identifier)
			}
			state := states[blocker.ColumnID]
			if state == "completed" {
				continue
			}
			graph[is.ID] = append(graph[is.ID], blocker.ID)
			if is.ID == root.ID {
				waiting = append(waiting, blocker.Identifier)
			}
			if state == "canceled" {
				notices = append(notices, admissionNotice{
					key: fmt.Sprintf("comment/admission/canceled/%d/%d", r.ID, blocker.ID), issueID: r.IssueID,
					body: fmt.Sprintf("Run held: dependency %s is canceled, not completed. A person must decide whether to complete it or remove the blocked-by relation.", blocker.Identifier),
				})
			}
			if err := visit(blocker); err != nil {
				return err
			}
		}
		return nil
	}
	if err := visit(root); err != nil {
		return false, e.admissionFailure(ctx, r, "Waiting for complete dependency evidence from itsaplan.", err)
	}
	for _, cycle := range dependencyCycles(graph) {
		var ids, keys []string
		for _, id := range cycle {
			ids = append(ids, strconv.FormatInt(id, 10))
			keys = append(keys, issues[id].Identifier)
		}
		// The same component has one durable key regardless of which run
		// discovers it first. Report on its lowest-ID issue, once in total.
		notices = append(notices, admissionNotice{
			key: "comment/admission/cycle/" + strings.Join(ids, "/"), issueID: cycle[0],
			body: "Runs held by a blocked-by cycle involving " + strings.Join(keys, ", ") + ". A person must remove the cycle before these runs can start.",
		})
	}
	if len(waiting) > 0 {
		sort.Strings(waiting)
		reason := "Waiting for completed blockers: " + strings.Join(waiting, ", ") + "."
		if len(notices) > 0 {
			reason += " A canceled dependency or dependency cycle needs a person's decision."
		}
		return false, e.holdAdmission(ctx, r, reason, notices)
	}
	// Drain the frozen journal before task events can use its offsets, even
	// when the blocker completed while an event acknowledgement was lost.
	if gone, err := e.postAdmission(ctx, r); err != nil || gone {
		return false, err
	}
	// A graph can finish after the tick that started it. Refresh the workflow
	// and root before release. v1.10.0 listIssueLinks joins every direct
	// blocker's column in one SQL statement, so this last response checks all
	// completion evidence together, including a blocker reopened mid-poll.
	columns, err = e.admissionColumns(ctx, r, b.ItsaplanProject)
	if err != nil {
		return false, e.admissionFailure(ctx, r, "Waiting for the issue's current workflow state.", err)
	}
	states = map[int64]string{}
	for _, c := range columns {
		states[c.ID] = c.StateType
	}
	current, err := e.admissionIssue(ctx, r, r.IssueID)
	if err != nil {
		return false, e.admissionFailure(ctx, r, "Waiting for the issue's current workflow state.", err, briefNotice(r))
	}
	if done, err := e.terminalRoot(ctx, r, states[current.ColumnID]); err != nil || done {
		return false, err
	}
	if states[current.ColumnID] == "" {
		return false, e.holdAdmission(ctx, r, "Waiting for the issue's current workflow state.", nil)
	}
	if current.Identifier != root.Identifier || !sameBlockers(root, current) {
		return false, e.holdAdmission(ctx, r, "Waiting for a fresh poll of the changed issue dependencies.", nil)
	}
	for _, link := range current.Links {
		if !link.BlockedBy() {
			continue
		}
		if link.Issue.ID <= 0 || !strings.HasPrefix(link.Issue.Identifier, b.ItsaplanProject+"-") || states[link.Issue.ColumnID] == "" {
			return false, e.holdAdmission(ctx, r, "Waiting for complete dependency evidence from itsaplan.", nil)
		}
		if states[link.Issue.ColumnID] != "completed" {
			waiting = append(waiting, link.Issue.Identifier)
			if states[link.Issue.ColumnID] == "canceled" {
				notices = append(notices, admissionNotice{
					key: fmt.Sprintf("comment/admission/canceled/%d/%d", r.ID, link.Issue.ID), issueID: r.IssueID,
					body: fmt.Sprintf("Run held: dependency %s is canceled, not completed. A person must decide whether to complete it or remove the blocked-by relation.", link.Issue.Identifier),
				})
			}
		}
	}
	if len(waiting) > 0 {
		sort.Strings(waiting)
		return false, e.holdAdmission(ctx, r, "Waiting for completed blockers: "+strings.Join(waiting, ", ")+".", notices)
	}
	if strings.TrimSpace(current.Description) == "" {
		return false, e.holdAdmission(ctx, r, "Waiting for an issue brief: the description is empty.", []admissionNotice{briefNotice(r)})
	}
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		return e.st.SetAdmissionHold(ctx, tx, r.ID, "", e.cfg.Now())
	}); err != nil {
		return false, err
	}
	r.HoldReason = ""
	r.AdmissionIssue = current
	return true, nil
}

// terminalRoot ends a first delegation whose issue people completed or
// canceled before it started. With a work item, the factory moved the issue
// itself, and a later delegation proceeds: followUp asks for a new issue when
// the work item's PR already merged.
func (e *Engine) terminalRoot(ctx context.Context, r *store.Run, state string) (bool, error) {
	if state != "completed" && state != "canceled" {
		return false, nil
	}
	if _, err := e.st.WorkItemByIssue(ctx, r.IssueID); err == nil {
		return false, nil
	} else if !errors.Is(err, sql.ErrNoRows) {
		return false, err
	}
	return true, e.failRun(ctx, e.st.DB, r, "delivery stopped: the issue is in a "+state+" column")
}

func sameBlockers(a, b *itsaplan.Issue) bool {
	ids := func(is *itsaplan.Issue) string {
		var out []string
		for _, link := range is.Links {
			if link.BlockedBy() {
				out = append(out, strconv.FormatInt(link.Issue.ID, 10))
			}
		}
		sort.Strings(out)
		return strings.Join(out, ",")
	}
	return ids(a) == ids(b)
}

func (e *Engine) admissionFailure(ctx context.Context, r *store.Run, reason string, err error, notices ...admissionNotice) error {
	if errors.Is(err, errAdmissionChanged) {
		return nil // cancellation, a stop or redelivery owns the run now
	}
	if fatal(err) {
		return err
	}
	if itsaplan.IsUnavailable(err) {
		reason = "Waiting for itsaplan to recover from an API failure."
		for i := range notices {
			notices[i].body = fmt.Sprintf("The factory is holding %s run %d while itsaplan is failing. Admission will retry automatically.", r.IssueKey, r.ID)
		}
	}
	return errors.Join(err, e.holdAdmission(ctx, r, reason, notices))
}

// briefNotice announces once per run attempt, with issueBrief's key, that no
// session starts without the issue text. It never quotes a response body.
func briefNotice(r *store.Run) admissionNotice {
	return admissionNotice{
		key: fmt.Sprintf("comment/brief-held/%d/%d", r.ID, r.Attempt), issueID: r.IssueID,
		body: fmt.Sprintf("The factory is holding %s run %d until its issue and attachment names can be read. "+
			"No new agent session will start without the issue text. Check the issue and the coordinator's "+
			"itsaplan access; the lookup will retry automatically.", r.IssueKey, r.ID),
	}
}

type admissionJournal struct {
	Reason string           `json:"reason"`
	Events []itsaplan.Event `json:"events"`
}

func (e *Engine) holdAdmission(ctx context.Context, r *store.Run, reason string, notices []admissionNotice) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.SetAdmissionHold(ctx, tx, r.ID, reason, e.cfg.Now()); err != nil {
			return err
		}
		tr, err := e.st.GetTranscript(ctx, tx, r.ID, r.Attempt, admissionTask)
		if err != nil {
			return err
		}
		var journal admissionJournal
		if tr == nil {
			base := r.EventOffset
			if r.EventAttempt != r.Attempt {
				base = 0
			}
			tr = &store.Transcript{Base: base}
		} else if err := json.Unmarshal(tr.State, &journal); err != nil {
			return err
		}
		if journal.Reason != reason {
			id := fmt.Sprintf("hold/%d/%d/%d", r.ID, r.Attempt, len(journal.Events))
			journal.Events = append(journal.Events,
				itsaplan.Event{Type: "TEXT_MESSAGE_START", MessageID: id, Role: "assistant"})
			journal.Events = appendContent(journal.Events, id, itsaplanText(reason))
			journal.Events = append(journal.Events,
				itsaplan.Event{Type: "TEXT_MESSAGE_END", MessageID: id})
			journal.Reason = reason
			tr.State, err = json.Marshal(journal)
			if err != nil {
				return err
			}
			if err := e.st.SaveTranscript(ctx, tx, r.ID, r.Attempt, admissionTask, tr); err != nil {
				return err
			}
		}
		for _, n := range notices {
			if err := e.st.Enqueue(ctx, tx, store.OutboxComment, n.key, r.ID, "",
				commentPayload{IssueID: n.issueID, Body: n.body, Once: true}); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return err
	}
	r.HoldReason, r.State = reason, store.RunWaiting
	if ctx.Err() != nil {
		return nil // a later poll will drain the journal
	}
	_, err = e.postAdmission(ctx, r)
	return err
}

func (e *Engine) postAdmission(ctx context.Context, r *store.Run) (bool, error) {
	tr, err := e.st.GetTranscript(ctx, e.st.DB, r.ID, r.Attempt, admissionTask)
	if err != nil || tr == nil {
		return false, err
	}
	var journal admissionJournal
	if err := json.Unmarshal(tr.State, &journal); err != nil {
		return false, err
	}
	postCtx, cancel := context.WithTimeout(ctx, e.cfg.AdmissionReadTimeout)
	defer cancel()
	return e.sendEvents(postCtx, r, admissionTask, journal.Events, tr.Base)
}

// invariant: FACT-13
func (e *Engine) admissionMetrics(ctx context.Context) error {
	total := 0
	for _, agent := range e.cfg.Agents {
		dispatched, runnable, busy, err := e.st.DispatchCounts(ctx, agent.Key)
		if err != nil {
			return err
		}
		e.m.BusyRuns.WithLabelValues(agent.Key).Set(float64(busy))
		e.m.DispatchedRuns.WithLabelValues(agent.Key).Set(float64(dispatched))
		total += runnable
	}
	e.m.RunnableRuns.Set(float64(total))
	n, age, err := e.st.AdmissionHolds(ctx, e.cfg.Now())
	if err == nil {
		e.m.HeldRuns.Set(float64(n))
		e.m.OldestHold.Set(age)
	}
	return err
}

// dependencyCycles returns cyclic strongly connected components, with stable
// membership keys even when overlapping cycles are reached in a different order.
func dependencyCycles(graph map[int64][]int64) [][]int64 {
	index, low := map[int64]int{}, map[int64]int{}
	onStack := map[int64]bool{}
	var stack []int64
	var cycles [][]int64
	next := 0
	var visit func(int64)
	visit = func(v int64) {
		next++
		index[v], low[v] = next, next
		stack = append(stack, v)
		onStack[v] = true
		self := false
		for _, w := range graph[v] {
			self = self || w == v
			if index[w] == 0 {
				visit(w)
				low[v] = min(low[v], low[w])
			} else if onStack[w] {
				low[v] = min(low[v], index[w])
			}
		}
		if low[v] != index[v] {
			return
		}
		var component []int64
		for {
			w := stack[len(stack)-1]
			stack = stack[:len(stack)-1]
			onStack[w] = false
			component = append(component, w)
			if w == v {
				break
			}
		}
		if len(component) > 1 || self {
			sort.Slice(component, func(i, j int) bool { return component[i] < component[j] })
			cycles = append(cycles, component)
		}
	}
	for v := range graph {
		if index[v] == 0 {
			visit(v)
		}
	}
	return cycles
}
