package dispatch_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func (f *fakeItsaplan) Project(context.Context, string) (*itsaplan.Project, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.deferredProject == nil {
		return &itsaplan.Project{}, nil
	}
	return f.deferredProject, nil
}

func (f *fakeItsaplan) ProjectIssues(context.Context, string) ([]itsaplan.Issue, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var issues []itsaplan.Issue
	for _, i := range f.issues {
		issues = append(issues, *i)
	}
	return issues, nil
}

// The adapter tests exercise rollback. This fake models its committed receipt
// and network outcomes for the connector outbox tests.
func (f *fakeItsaplan) StartDeferred(_ context.Context, id int64, r itsaplan.DeferredRequest) (*itsaplan.DeferredReceipt, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.deferredErr != nil {
		return nil, f.deferredErr
	}
	if f.deferredReply != nil {
		return f.deferredReply, nil
	}
	if f.deferredReceipt != nil {
		return f.deferredReceipt, nil
	}
	i := f.issues[id]
	if i.DelegateUserID != nil || i.ArchivedAt != nil || i.ColumnID != 1 ||
		i.StartDate != r.StartDate || len(i.LabelIDs) == 0 {
		return nil, &itsaplan.HTTPError{Status: 409, Body: "no longer eligible"}
	}
	key := fmt.Sprintf("deferred/%d/%s", id, r.StartDate)
	f.comments = append(f.comments, fakeComment{id, fmt.Sprintf("Starting deferred work scheduled for %s (UTC).\n\nFactory start: %s.", r.StartDate, key)})
	i.ColumnID = 2
	i.DelegateUserID = &r.DelegateUserID
	f.deferredWrites = append(f.deferredWrites, "comment", "move", "delegate")
	f.deferredReceipt = &itsaplan.DeferredReceipt{Key: key, RunID: 42}
	if f.deferredFail != "" {
		f.deferredFail = ""
		return nil, errors.New("accepted, response lost")
	}
	return f.deferredReceipt, nil
}

func deferredEnv(t *testing.T) *env {
	t.Helper()
	v := newEnv(t)
	v.its.deferredProject = &itsaplan.Project{}
	err := json.Unmarshal([]byte(`{"project":{"id":7},"columns":[{"id":1,"name":"Backlog","stateType":"backlog"},{"id":2,"name":"Todo","stateType":"unstarted"}],"labels":[{"id":8,"name":"deferred"}],"assignees":[{"userId":"coder-id","username":"coder","kind":"agent"}]}`), v.its.deferredProject)
	if err != nil {
		t.Fatal(err)
	}
	v.its.issues = map[int64]*itsaplan.Issue{}
	v.its.issues[118] = &itsaplan.Issue{ID: 118, ProjectID: 7, Identifier: "HL-118", ColumnID: 1, StartDate: "2026-11-14", LabelIDs: []int64{8}}
	return v
}

func assertDeferred(t *testing.T, v *env) {
	t.Helper()
	if !reflect.DeepEqual(v.its.deferredWrites, []string{"comment", "move", "delegate"}) {
		t.Fatalf("writes = %v", v.its.deferredWrites)
	}
	if got := v.its.Comments(); len(got) != 1 || got[0].body != "Starting deferred work scheduled for 2026-11-14 (UTC).\n\nFactory start: deferred/118/2026-11-14." {
		t.Fatalf("comments = %v", got)
	}
	i := v.its.issues[118]
	if i.ColumnID != 2 || i.DelegateUserID == nil || *i.DelegateUserID != "coder-id" {
		t.Fatalf("issue = %+v", i)
	}
	if got := testutil.ToFloat64(v.m.DeferredStarted); got != 1 {
		t.Fatalf("starts = %v", got)
	}
}

func TestDeferredStartOrderAndRestart(t *testing.T) {
	v := deferredEnv(t)
	v.tick(v.e)
	assertDeferred(t, v)
	v.e.Release()
	v.e = v.engine("restarted")
	v.tick(v.e)
	assertDeferred(t, v)
	// A stale board or a later delegate removal cannot replay this outbox.
	v.its.issues[118].ColumnID = 1
	v.its.issues[118].DelegateUserID = nil
	v.tick(v.e)
	if len(v.its.deferredWrites) != 3 || testutil.ToFloat64(v.m.DeferredStarted) != 1 {
		t.Fatal("completed deferred start was replayed")
	}
}

func TestDeferredEligibility(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*itsaplan.Issue)
	}{
		{"future", func(i *itsaplan.Issue) { i.StartDate = "2026-11-15" }},
		{"missing date", func(i *itsaplan.Issue) { i.StartDate = "" }},
		{"invalid date", func(i *itsaplan.Issue) { i.StartDate = "2026-02-30" }},
		{"no label", func(i *itsaplan.Issue) { i.LabelIDs = nil }},
		{"delegated", func(i *itsaplan.Issue) { s := "another-agent"; i.DelegateUserID = &s }},
		{"outside Backlog", func(i *itsaplan.Issue) { i.ColumnID = 2 }},
		{"archived", func(i *itsaplan.Issue) { at := time.Date(2026, 11, 13, 0, 0, 0, 0, time.UTC); i.ArchivedAt = &at }},
		{"other project", func(i *itsaplan.Issue) { i.ProjectID = 9 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := deferredEnv(t)
			tc.change(v.its.issues[118])
			v.tick(v.e)
			if len(v.its.deferredWrites) != 0 || testutil.ToFloat64(v.m.DeferredStarted) != 0 {
				t.Fatalf("unexpected start: %v", v.its.deferredWrites)
			}
		})
	}
}

func TestDeferredUsesUTCDateAndCatchesOverdue(t *testing.T) {
	v := deferredEnv(t)
	v.clk.t = time.Date(2026, 11, 13, 23, 0, 0, 0, time.FixedZone("west", -2*3600))
	v.tick(v.e)
	assertDeferred(t, v)
	v = deferredEnv(t)
	v.clk.Advance(7 * 24 * time.Hour)
	v.tick(v.e)
	assertDeferred(t, v)
}

func TestDeferredLostResponseResumesWithoutDuplicate(t *testing.T) {
	v := deferredEnv(t)
	v.its.deferredFail = "response"
	v.tick(v.e)
	v.e.Release()
	v.e = v.engine("restarted")
	v.clk.Advance(time.Minute)
	v.tick(v.e)
	v.tick(v.e)
	assertDeferred(t, v)
}

func TestDeferredRefusalRetries(t *testing.T) {
	for _, status := range []int{401, 403} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			v := deferredEnv(t)
			v.its.deferredErr = &itsaplan.HTTPError{Status: status, Body: "rotating key"}
			v.tick(v.e)
			v.its.deferredErr = nil
			v.clk.Advance(time.Minute)
			v.tick(v.e)
			assertDeferred(t, v)
		})
	}
}

func TestDeferredWaitsForWIPCapacityAcrossRestart(t *testing.T) {
	v := deferredEnv(t)
	v.its.deferredErr = itsaplan.ErrWIPLimit
	// Exceed the ordinary outbox retry count and keep the column full for days.
	for i := 0; i < 100; i++ {
		v.tick(v.e)
		v.clk.Advance(time.Hour)
	}
	var pending, tries int
	if err := v.st.DB.QueryRow(`SELECT count(*), max(tries) FROM outbox WHERE kind = 'deferred_start' AND delivered_at IS NULL AND abandoned_at IS NULL`).Scan(&pending, &tries); err != nil || pending != 1 || tries != 0 {
		t.Fatalf("pending = %d, tries = %d, err = %v", pending, tries, err)
	}
	if len(v.its.deferredWrites) != 0 || testutil.ToFloat64(v.m.DeferredStarted) != 0 {
		t.Fatal("full column started work")
	}
	v.e.Release()
	v.e = v.engine("restarted")
	v.its.deferredErr = nil
	v.tick(v.e)
	v.tick(v.e)
	assertDeferred(t, v)
}

// Reproduce the pre-HOME-118 connector's unknown-kind delivery branch, after
// releasing the new writer and before the upgraded writer takes over again.
func rollbackDeferred(t *testing.T, v *env) int64 {
	t.Helper()
	var id int64
	if err := v.st.DB.QueryRow(`SELECT id FROM outbox WHERE kind = 'deferred_start'`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	v.e.Release()
	if err := v.st.OutboxAbandon(v.ctx, v.st.DB, id, "unknown kind "+store.OutboxDeferredStart); err != nil {
		t.Fatal(err)
	}
	v.e = v.engine("upgraded")
	return id
}

func TestDeferredWIPWaitSurvivesChartRollback(t *testing.T) {
	v := deferredEnv(t)
	v.its.deferredErr = itsaplan.ErrWIPLimit
	v.tick(v.e)
	var id int64
	for i := 0; i < 2; i++ {
		id = rollbackDeferred(t, v)
		v.clk.Advance(24 * time.Hour)
		v.tick(v.e) // Todo is still full after the upgrade.
		var pending bool
		if err := v.st.DB.QueryRow(`SELECT abandoned_at IS NULL AND delivered_at IS NULL FROM outbox WHERE id = $1`, id).Scan(&pending); err != nil || !pending {
			t.Fatalf("resumed = %v, err = %v", pending, err)
		}
		if len(v.its.deferredWrites) != 0 {
			t.Fatal("full column started work after rollback")
		}
	}
	v.its.deferredErr = nil
	v.clk.Advance(time.Minute)
	v.tick(v.e)
	v.tick(v.e)
	assertDeferred(t, v)
	var delivered bool
	if err := v.st.DB.QueryRow(`SELECT delivered_at IS NOT NULL FROM outbox WHERE id = $1`, id).Scan(&delivered); err != nil || !delivered {
		t.Fatalf("original outbox delivered = %v, err = %v", delivered, err)
	}
}

func TestDeferredRollbackRechecksEligibility(t *testing.T) {
	for _, change := range []string{"label", "date", "delegate", "column", "archive"} {
		t.Run(change, func(t *testing.T) {
			v := deferredEnv(t)
			v.its.deferredErr = itsaplan.ErrWIPLimit
			v.tick(v.e)
			id := rollbackDeferred(t, v)
			i := v.its.issues[118]
			switch change {
			case "label":
				i.LabelIDs = nil
			case "date":
				i.StartDate = "2999-01-01"
			case "delegate":
				s := "person-chosen"
				i.DelegateUserID = &s
			case "column":
				i.ColumnID = 5
			case "archive":
				at := time.Date(2026, 11, 14, 0, 0, 0, 0, time.UTC)
				i.ArchivedAt = &at
			}
			v.its.deferredErr = nil
			v.clk.Advance(time.Minute)
			v.tick(v.e)
			var abandoned bool
			if err := v.st.DB.QueryRow(`SELECT abandoned_at IS NOT NULL FROM outbox WHERE id = $1`, id).Scan(&abandoned); err != nil || !abandoned {
				t.Fatalf("ineligible start revived after %s: %v", change, err)
			}
			if len(v.its.deferredWrites) != 0 {
				t.Fatalf("writes after %s: %v", change, v.its.deferredWrites)
			}
		})
	}
}

func TestDeferredWIPWaitStillChecksEligibility(t *testing.T) {
	v := deferredEnv(t)
	v.its.deferredErr = itsaplan.ErrWIPLimit
	v.tick(v.e)
	v.its.issues[118].LabelIDs = nil
	v.its.deferredErr = nil
	v.clk.Advance(time.Minute)
	v.tick(v.e)
	var abandoned bool
	if err := v.st.DB.QueryRow(`SELECT abandoned_at IS NOT NULL FROM outbox WHERE kind = 'deferred_start'`).Scan(&abandoned); err != nil || !abandoned {
		t.Fatalf("abandoned = %v, err = %v", abandoned, err)
	}
	if len(v.its.deferredWrites) != 0 {
		t.Fatal("removed opt-in started work")
	}
}

func TestDeferredChangedIssueStopsPendingStart(t *testing.T) {
	for _, change := range []string{"delegate", "date", "label", "archive", "cancel"} {
		t.Run(change, func(t *testing.T) {
			v := deferredEnv(t)
			v.its.deferredErr = errors.New("connection failed before send")
			v.tick(v.e)
			v.its.deferredErr = nil
			i := v.its.issues[118]
			switch change {
			case "delegate":
				s := "person-chosen"
				i.DelegateUserID = &s
			case "date":
				i.StartDate = "2026-12-01"
			case "label":
				i.LabelIDs = nil
			case "archive":
				at := time.Date(2026, 11, 14, 0, 0, 0, 0, time.UTC)
				i.ArchivedAt = &at
			case "cancel":
				i.ColumnID = 5
			}
			v.clk.Advance(time.Minute)
			v.tick(v.e)
			if len(v.its.deferredWrites) != 0 {
				t.Fatalf("writes after %s: %v", change, v.its.deferredWrites)
			}
		})
	}
}

func TestDeferredRestartBeforeRequestResends(t *testing.T) {
	v := deferredEnv(t)
	v.its.deferredErr = errors.New("connection failed before send")
	v.tick(v.e)
	// The durable marker exists, but no server-side write happened. This is
	// also the state after stopping between the receipt write and HTTP send.
	var started bool
	if err := v.st.DB.QueryRow(`SELECT send_started_at IS NOT NULL FROM outbox WHERE kind = 'deferred_start'`).Scan(&started); err != nil || !started {
		t.Fatalf("send marker = %v, err = %v", started, err)
	}
	v.its.deferredErr = nil
	v.e.Release()
	v.e = v.engine("restarted")
	v.clk.Advance(time.Minute)
	v.tick(v.e)
	assertDeferred(t, v)
}

func TestDeferredDelegateAloneIsNotSuccess(t *testing.T) {
	v := deferredEnv(t)
	v.its.deferredErr = errors.New("connection failed before send")
	v.tick(v.e)
	s := "coder-id"
	v.its.issues[118].DelegateUserID = &s
	v.its.deferredErr = nil
	v.clk.Advance(time.Minute)
	v.tick(v.e)
	if testutil.ToFloat64(v.m.DeferredStarted) != 0 {
		t.Fatal("delegate without a run marked delivered")
	}
}

func TestDeferredRejectsInvalidReceipt(t *testing.T) {
	for _, receipt := range []*itsaplan.DeferredReceipt{
		{Key: "deferred/118/2026-11-14"},
		{Key: "another-start", RunID: 42},
	} {
		v := deferredEnv(t)
		v.its.deferredReply = receipt
		v.tick(v.e)
		if testutil.ToFloat64(v.m.DeferredStarted) != 0 {
			t.Fatal("invalid receipt marked delivered")
		}
		v.its.deferredReply = nil
		v.clk.Advance(time.Minute)
		v.tick(v.e)
		assertDeferred(t, v)
	}
}

func TestDeferredPreservesStoredStop(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 118, "HL-118", "Original work")
	v.tick(v.e)
	if _, err := v.st.StopWork(v.ctx, v.work(118).ID, "operator stop", "operator"); err != nil {
		t.Fatal(err)
	}
	// Install the same deferred scaffold after the stop. The issue still has
	// a due date and Backlog status, but the stored stop must stand.
	fixture := deferredEnv(t)
	v.its.deferredProject = fixture.its.deferredProject
	v.its.issues[118] = fixture.its.issues[118]
	v.tick(v.e)
	if len(v.its.Moves()) != 0 || v.its.issues[118].DelegateUserID != nil || testutil.ToFloat64(v.m.DeferredStarted) != 0 {
		t.Fatal("deferred sweep ignored a stored stop")
	}
}

func TestDeferredWIPWaitHonorsStoppedUnlinkedAdmission(t *testing.T) {
	v := deferredEnv(t)
	v.its.add(1, "coder", 118, "HL-118", "Original work")
	i := v.its.issues[118]
	delegate := "coder-id"
	i.DelegateUserID = &delegate
	i.ColumnID = 2
	v.dependency(118, 119, "HL-119", 2)
	v.tick(v.e)
	v.held(1)

	// Reschedule the issue while its original delegation is still held.
	i.ColumnID, i.DelegateUserID = 1, nil
	v.its.deferredErr = itsaplan.ErrWIPLimit
	v.tick(v.e)
	var waiting bool
	if err := v.st.DB.QueryRow(`SELECT abandoned_at IS NULL AND delivered_at IS NULL
		AND last_error = 'wip_limit_exceeded' FROM outbox WHERE kind = 'deferred_start'`).Scan(&waiting); err != nil || !waiting {
		t.Fatalf("deferred start waiting for WIP = %v, %v", waiting, err)
	}
	if stopped, err := v.st.StopIssue(v.ctx, "HL-118", "operator stop", "operator"); err != nil || stopped != (store.IssueStop{HeldRuns: 1}) {
		t.Fatalf("cancel = %+v, %v; want only the unlinked run stopped", stopped, err)
	}
	if _, err := v.st.WorkItemByIssue(v.ctx, 118); !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("held issue has a work item: %v", err)
	}

	// Capacity and dependencies clear while the connector is down. The old
	// outbox request must not create a new delegation that escapes the stop.
	v.e.Release()
	v.its.deferredErr = nil
	v.its.moveTo(119, 3, "")
	v.clk.Advance(time.Minute)
	v.e = v.engine("after-cancel")
	v.tick(v.e)
	v.tick(v.e)
	if len(v.its.deferredWrites) != 0 || v.its.deferredReceipt != nil ||
		i.ColumnID != 1 || i.DelegateUserID != nil || testutil.ToFloat64(v.m.DeferredStarted) != 0 || v.kg.Calls() != 0 {
		t.Fatal("deferred start escaped the unlinked admission stop")
	}
	var abandoned bool
	if err := v.st.DB.QueryRow(`SELECT abandoned_at IS NOT NULL AND delivered_at IS NULL
		AND last_error = 'deferred issue stopped' FROM outbox WHERE kind = 'deferred_start'`).Scan(&abandoned); err != nil || !abandoned {
		t.Fatalf("stopped deferred start abandoned = %v, %v", abandoned, err)
	}
	if r := v.run(1); r.State != store.RunClosed || !r.StopRequested {
		t.Fatalf("original stopped run = %+v", r)
	}
}

func TestDeferredReadmissionAllowsNewScheduleButFencesOldRequest(t *testing.T) {
	v := deferredEnv(t)
	v.its.add(1, "coder", 118, "HL-118", "Original work")
	i := v.its.issues[118]
	i.Description = "Deliver the deferred work."
	i.StartDate = "2026-11-13"
	delegate := "coder-id"
	i.ColumnID, i.DelegateUserID = 2, &delegate
	v.dependency(118, 119, "HL-119", 2)
	v.tick(v.e)
	v.held(1)

	i.ColumnID, i.DelegateUserID = 1, nil
	v.its.deferredErr = itsaplan.ErrWIPLimit
	v.tick(v.e)
	// Delay the original request until after the explicit re-admission.
	if _, err := v.st.DB.Exec(`UPDATE outbox SET next_attempt_at = $1 WHERE kind = 'deferred_start'`, v.clk.Now().Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if stopped, err := v.st.StopIssue(v.ctx, "HL-118", "operator stop", "operator"); err != nil || stopped.HeldRuns != 1 {
		t.Fatalf("cancel = %+v, %v", stopped, err)
	}
	i.ColumnID, i.DelegateUserID = 2, &delegate
	v.its.moveTo(119, 3, "")
	v.its.add(2, "coder", 118, "HL-118", "Explicit re-admission")
	v.tick(v.e)
	if r := v.run(2); r.WorkItemID == 0 || r.StopRequested {
		t.Fatalf("new delegation was not admitted: %+v", r)
	}
	if r := v.run(1); r.WorkItemID != 0 || !r.StopRequested || r.State != store.RunClosed {
		t.Fatalf("old admission lost its stop: %+v", r)
	}

	// Even after re-admission and restart, the pre-stop date cannot run.
	i.ColumnID, i.DelegateUserID = 1, nil
	v.its.deferredErr = nil
	v.e.Release()
	v.e = v.engine("after-readmission")
	v.clk.Advance(time.Hour)
	v.tick(v.e)
	if len(v.its.deferredWrites) != 0 {
		t.Fatal("old request borrowed the new admission")
	}
	var abandoned bool
	if err := v.st.DB.QueryRow(`SELECT abandoned_at IS NOT NULL AND last_error = 'deferred issue stopped'
		FROM outbox WHERE dedupe_key = 'deferred/118/2026-11-13'`).Scan(&abandoned); err != nil || !abandoned {
		t.Fatalf("old request abandoned = %v, %v", abandoned, err)
	}

	i.StartDate = "2026-11-14"
	v.tick(v.e)
	if len(v.its.deferredWrites) != 3 || v.its.deferredReceipt == nil ||
		v.its.deferredReceipt.Key != "deferred/118/2026-11-14" || testutil.ToFloat64(v.m.DeferredStarted) != 1 {
		t.Fatal("old stopped run blocked the new schedule")
	}
}
