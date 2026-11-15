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

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

const admissionTask = "factory-admission"

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
	if err := e.beginAdmissionRead(ctx, r, "issue", strconv.FormatInt(id, 10)); err != nil {
		return nil, err
	}
	is, err := e.its.GetIssue(ctx, id)
	e.countRefusal(roleCoordinator, "admission", err)
	if err == nil && (is == nil || is.ID != id || is.Identifier == "" || is.Links == nil) {
		err = errors.New("incomplete issue or relations response")
	}
	return is, err
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
	// The run lock excludes its heartbeat. Bound the entire graph read so
	// an unavailable dependency cannot consume the five-minute lease.
	readCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	root, err := e.admissionIssue(readCtx, r, r.IssueID)
	if err != nil {
		return false, e.admissionFailure(ctx, r, "Waiting for the issue brief and dependency relations to be readable.", err)
	}
	if strings.TrimSpace(root.Description) == "" {
		return false, e.holdAdmission(ctx, r, "Waiting for an issue brief: the description is empty.", nil)
	}
	if root.Identifier != r.IssueKey {
		return false, e.holdAdmission(ctx, r, "Waiting for the issue to match the claimed repository binding.", nil)
	}
	var columns []itsaplan.Column
	states := map[int64]string{}
	issues := map[int64]*itsaplan.Issue{root.ID: root}
	graph := map[int64][]int64{}
	var waiting []string
	var notices []admissionNotice
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
			if columns == nil {
				if err := e.beginAdmissionRead(readCtx, r, "columns", b.ItsaplanProject); err != nil {
					return err
				}
				var err error
				columns, err = e.its.ProjectColumns(readCtx, b.ItsaplanProject)
				e.countRefusal(roleCoordinator, "admission", err)
				if err != nil {
					return err
				}
				for _, c := range columns {
					states[c.ID] = c.StateType
				}
			}
			blocker := issues[link.Issue.ID]
			if blocker == nil {
				if len(issues) >= 256 {
					return errors.New("dependency graph exceeds 256 issues")
				}
				var err error
				blocker, err = e.admissionIssue(readCtx, r, link.Issue.ID)
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
	if err := e.guard(ctx); err != nil {
		return false, err
	}
	if err := e.st.SetAdmissionHold(ctx, e.st.DB, r.ID, "", e.cfg.Now()); err != nil {
		return false, err
	}
	r.HoldReason = ""
	return true, nil
}

func (e *Engine) admissionFailure(ctx context.Context, r *store.Run, reason string, err error) error {
	if fatal(err) {
		return err
	}
	return errors.Join(err, e.holdAdmission(ctx, r, reason, nil))
}

type admissionJournal struct {
	Reason string           `json:"reason"`
	Events []itsaplan.Event `json:"events"`
}

func (e *Engine) holdAdmission(ctx context.Context, r *store.Run, reason string, notices []admissionNotice) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
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
	return e.sendEvents(ctx, r, admissionTask, journal.Events, tr.Base)
}

func (e *Engine) admissionMetrics(ctx context.Context) error {
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
