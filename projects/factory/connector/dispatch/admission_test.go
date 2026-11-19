package dispatch_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func (v *env) dependency(from, to int64, key string, column int64) {
	v.t.Helper()
	v.its.mu.Lock()
	defer v.its.mu.Unlock()
	if v.its.issues[to] == nil {
		v.its.issues[to] = &itsaplan.Issue{ID: to, Identifier: key, ColumnID: column, Description: "A brief.", Links: []itsaplan.IssueLink{}}
	}
	link := itsaplan.IssueLink{Kind: "blocks", Direction: "inward"}
	link.Issue.ID = to
	v.its.issues[from].Links = append(v.its.issues[from].Links, link)
}

func (v *env) held(id int64) {
	v.t.Helper()
	if r := v.run(id); r.State != store.RunWaiting || r.HoldReason == "" || r.WorkItemID != 0 {
		v.t.Fatalf("held run = %+v", r)
	}
	if a, err := v.st.LatestAttempt(v.ctx, id); err != nil || a != nil {
		v.t.Fatalf("held attempt = %+v, err = %v", a, err)
	}
}

func TestDependenciesHoldUntilLastCompletedOnNextPoll(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 2)
	v.dependency(101, 103, "HL-3", 1)
	v.tick(v.e)
	v.held(1)
	if v.kg.Calls() != 0 || len(v.its.Comments()) != 0 || len(v.its.Moves()) != 0 {
		t.Fatal("open blockers caused execution or an issue write")
	}
	if n, err := v.st.CountActiveRuns(v.ctx, "coder"); err != nil || n != 0 {
		t.Fatalf("held run occupies a concurrency slot: %d, %v", n, err)
	}
	v.clk.Advance(90 * time.Second)
	v.its.moveTo(102, 3, "")
	v.tick(v.e)
	v.held(1)
	if testutil.ToFloat64(v.m.HeldRuns) != 1 || testutil.ToFloat64(v.m.OldestHold) != 90 {
		t.Fatal("hold metrics lost the first hold time")
	}
	if v.its.Run(1).beats == 0 || len(v.its.Run(1).events[1]) == 0 {
		t.Fatal("held run has no heartbeat or events")
	}
	v.its.moveTo(103, 3, "")
	v.tick(v.e)
	if v.kg.Executions() != 1 || v.run(1).State != store.RunDispatched {
		t.Fatal("last completed blocker did not release on the next poll")
	}
	if testutil.ToFloat64(v.m.HeldRuns) != 0 || testutil.ToFloat64(v.m.OldestHold) != 0 {
		t.Fatal("released hold remains in metrics")
	}
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Delivered after the hold.", nil)
	v.tick(v.e)
	if len(v.its.Results(1)) != 1 || v.its.Results(1)[0].Status != "success" {
		t.Fatal("task transcript could not follow the admission events")
	}
}

func TestCanceledDependencyCommentsOnceAcrossRestartAndRedelivery(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 4)
	v.tick(v.e)
	v.held(1)
	comments := v.its.Comments()
	if len(comments) != 1 || !strings.Contains(comments[0].body, "HL-2 is canceled") {
		t.Fatalf("comments = %+v", comments)
	}
	v.e.Release()
	v.clk.Advance(301 * time.Second)
	// Model itsaplan reclaiming the lease while the process was stopped.
	claim, err := v.its.Claim(v.ctx, "coder")
	if err != nil || claim == nil {
		t.Fatalf("redelivery = %+v, %v", claim, err)
	}
	if _, _, err := v.st.RecordClaim(v.ctx, store.ClaimedRun{
		ID: claim.ID, AgentKey: "coder", IssueID: 101, IssueKey: "HL-1", Attempt: claim.Attempts,
	}); err != nil {
		t.Fatal(err)
	}
	e := v.engine("after-restart")
	for range 3 {
		v.tick(e)
		v.held(1)
	}
	if len(v.its.Comments()) != 1 || v.kg.Calls() != 0 || v.run(1).Attempt != 2 {
		t.Fatal("restart/redelivery duplicated a notice or started held work")
	}
	if testutil.ToFloat64(v.m.OldestHold) != 301 || len(v.its.Run(1).events[2]) != 3 {
		t.Fatal("restart lost the hold age or current-attempt transcript")
	}
	v.its.moveTo(102, 3, "")
	v.tick(e)
	if v.kg.Executions() != 1 {
		t.Fatal("explicitly completing the canceled blocker did not release the run")
	}
}

func TestDependencyCycleHoldsEveryMemberAndReportsOnce(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "One.")
	v.its.add(2, "coder", 102, "HL-2", "Two.")
	v.dependency(101, 102, "HL-2", 2)
	v.dependency(102, 103, "HL-3", 2)
	v.dependency(103, 101, "HL-1", 2)
	v.its.add(3, "coder", 103, "HL-3", "Three.")
	for range 3 {
		v.tick(v.e)
	}
	for _, id := range []int64{1, 2, 3} {
		v.held(id)
	}
	if cs := v.its.Comments(); len(cs) != 1 || cs[0].issueID != 101 || !strings.Contains(cs[0].body, "cycle involving HL-1, HL-2, HL-3") {
		t.Fatalf("cycle comments = %+v", cs)
	}
	v.e.Release()
	e := v.engine("cycle-restart")
	v.tick(e)
	if len(v.its.Comments()) != 1 || v.kg.Calls() != 0 {
		t.Fatal("cycle replay started work or reported the same cycle again")
	}
	// An independent delegation can still occupy the released slots.
	v.its.add(4, "coder", 104, "HL-4", "Independent.")
	v.tick(e)
	if v.kg.Executions() != 1 || v.run(4).State != store.RunDispatched {
		t.Fatal("held cycle starved unrelated work")
	}
}

func TestOnlyInwardBlocksRelationsHold(t *testing.T) {
	for _, link := range []itsaplan.IssueLink{
		{Kind: "blocks", Direction: "outward"},
		{Kind: "blocks"},
		{Kind: "relates", Direction: "inward"},
		{Kind: "duplicates", Direction: "inward"},
	} {
		t.Run(link.Kind+"/"+link.Direction, func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			// This nonexistent other end must never be read.
			link.Issue.ID = 999
			v.its.issues[101].Links = []itsaplan.IssueLink{link}
			v.tick(v.e)
			if v.kg.Executions() != 1 {
				t.Fatal("unrelated or undirected relation held dispatch")
			}
		})
	}
}

func TestDependencyEvidenceFailsClosed(t *testing.T) {
	for _, failure := range []string{"missing", "columns", "unknown-column", "missing-links", "empty-brief"} {
		t.Run(failure, func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.dependency(101, 102, "HL-2", 3)
			switch failure {
			case "missing":
				delete(v.its.issues, 102)
			case "columns":
				v.its.columnsErr = errors.New("unavailable")
			case "unknown-column":
				v.its.issues[102].ColumnID = 999
			case "missing-links":
				v.its.issues[101].Links = nil
			case "empty-brief":
				v.its.issues[101].Description = "  "
			}
			v.tick(v.e)
			v.held(1)
			if v.kg.Calls() != 0 {
				t.Fatal("incomplete dependency evidence started execution")
			}
		})
	}
}

func TestLostHoldEventAckReplaysBeforeDispatch(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 2)
	v.its.loseEventAck = true
	claim, err := v.its.Claim(v.ctx, "coder")
	if err != nil || claim == nil {
		t.Fatalf("claim = %+v, %v", claim, err)
	}
	if err := v.e.HandleClaim(v.ctx, "coder", claim); err == nil {
		t.Fatal("expected the lost hold event acknowledgement")
	}
	v.held(1)
	if v.run(1).EventOffset != 0 || len(v.its.Run(1).events[1]) != 3 {
		t.Fatal("test did not leave an unacknowledged journal")
	}
	v.e.Release()
	e := v.engine("event-restart")
	v.its.moveTo(102, 3, "")
	v.tick(e)
	if len(v.its.Run(1).events[1]) != 3 || v.run(1).EventOffset != 3 || v.kg.Executions() != 1 {
		t.Fatal("lost event ack duplicated the hold or prevented next-poll dispatch")
	}
}

func TestUnknownCommentOutcomeIsNotReposted(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 4)
	// An ambiguous response cannot prove whether the comment was posted.
	v.its.commentErr = errors.New("lost acknowledgement")
	v.tick(v.e)
	v.e.Release()
	v.its.commentErr = nil
	e := v.engine("comment-restart")
	v.tick(e)
	if len(v.its.Comments()) != 0 || v.kg.Calls() != 0 {
		t.Fatal("unknown comment outcome was reposted or cleared the hold")
	}
}

func TestAdmissionReadsAreRecordedBeforeExternalCalls(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 2)
	check := func(operation, resource string) {
		t.Helper()
		var n int
		err := v.st.DB.QueryRowContext(v.ctx, `SELECT count(*) FROM admission_read
			WHERE run_id = 1 AND run_attempt = 1 AND operation = $1 AND resource = $2
			AND writer_epoch = $3`, operation, resource, v.e.Epoch()).Scan(&n)
		if err != nil || n != 1 {
			t.Fatalf("read %s/%s has no durable intent: %d, %v", operation, resource, n, err)
		}
	}
	v.its.beforeIssueRead = func(id int64) { check("issue", strconv.FormatInt(id, 10)) }
	v.its.beforeColumnsRead = func() { check("columns", "HL") }
	v.tick(v.e)
	v.tick(v.e)
	var n, attempts int
	// Claims and the open-run pass share one admission poll per tick.
	if err := v.st.DB.QueryRowContext(v.ctx, `SELECT count(*), min(attempts) FROM admission_read WHERE run_id = 1`).Scan(&n, &attempts); err != nil || n != 3 || attempts != 2 {
		t.Fatalf("read replay identities: %d rows, %d attempts, %v", n, attempts, err)
	}
}

func TestHeldDelegationCannotUndoALaterStop(t *testing.T) {
	for _, resumeNewer := range []bool{false, true} {
		t.Run(fmt.Sprintf("newer-delegation=%t", resumeNewer), func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Original work.")
			v.tick(v.e)
			v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Done.", nil)
			v.tick(v.e)
			v.dependency(101, 102, "HL-2", 2)
			v.its.add(2, "coder", 101, "HL-1", "Already delegated.")
			v.tick(v.e)
			v.held(2)
			if ok, err := v.st.StopIssue(v.ctx, "HL-1", "Stop after delegation", "joe"); err != nil || !ok.Any() {
				t.Fatalf("stop = %+v, %v", ok, err)
			}
			v.its.moveTo(102, 3, "")
			// Restart and reclaim do not refresh the old run's authority.
			v.e.Release()
			v.clk.Advance(301 * time.Second)
			claim, err := v.its.Claim(v.ctx, "coder")
			if err != nil || claim == nil || claim.ID != 2 {
				t.Fatalf("redelivery = %+v, %v", claim, err)
			}
			if _, _, err := v.st.RecordClaim(v.ctx, store.ClaimedRun{ID: 2, AgentKey: "coder", IssueID: 101, IssueKey: "HL-1", Attempt: claim.Attempts}); err != nil {
				t.Fatal(err)
			}
			v.e = v.engine("stop-restart")
			if resumeNewer {
				// A genuinely new delegation may resume delivery, but must
				// not also authorize the run that predates the stop.
				v.its.add(3, "coder", 101, "HL-1", "New delegation after stop.")
			}
			v.tick(v.e)
			if a, err := v.st.LatestAttempt(v.ctx, 2); err != nil || a != nil {
				t.Fatalf("stopped delegation created an attempt: %+v, %v", a, err)
			}
			if res := v.its.Results(2); len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "stopped after") {
				t.Fatalf("old delegation result = %+v", res)
			}
			wantSends := 1
			if resumeNewer {
				wantSends = 2
				if v.run(3).AdmissionStops != 1 || v.work(101).State != store.WorkActive {
					t.Fatal("a new delegation could not resume the stopped work item")
				}
			} else if v.work(101).State != store.WorkStopped {
				t.Fatal("the stop was cleared")
			}
			if v.kg.Executions() != wantSends || v.run(2).AdmissionStops != 0 {
				t.Fatal("redelivery refreshed a stopped delegation's authority")
			}
		})
	}
}

func TestStopDuringDependencyReadFencesResolution(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Original work.")
	v.tick(v.e)
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Done.", nil)
	v.tick(v.e)
	v.dependency(101, 102, "HL-2", 3)
	v.its.add(2, "coder-b", 101, "HL-1", "Delegated before the stop.")
	v.its.beforeIssueRead = func(id int64) {
		if id == 102 {
			if ok, err := v.st.StopIssue(v.ctx, "HL-1", "Stop during admission", "joe"); err != nil || !ok.Any() {
				t.Fatalf("stop = %+v, %v", ok, err)
			}
		}
	}
	v.tick(v.e)
	if a, err := v.st.LatestAttempt(v.ctx, 2); err != nil || a != nil {
		t.Fatalf("racing stop created an attempt: %+v, %v", a, err)
	}
	if w := v.work(101); w.State != store.WorkStopped || w.AgentKey != "coder" || v.kg.Executions() != 1 {
		t.Fatalf("resolution undid the stop or reassigned work: %+v", w)
	}
}

// A held first delegation has no work item. A stop during its dependency
// read is on the run, and resolution checks it under the run's lock.
func TestStopDuringFirstDelegationReadStands(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 2)
	v.tick(v.e)
	v.held(1)
	v.its.moveTo(102, 3, "")
	v.its.issues[101].Links = []itsaplan.IssueLink{}
	v.its.beforeColumnsRead = func() {
		v.its.beforeColumnsRead = nil
		if ok, err := v.st.StopIssue(v.ctx, "HL-1", "Stop during admission", "joe"); err != nil || !ok.Any() {
			t.Errorf("stop = %+v, %v", ok, err)
		}
	}
	v.tick(v.e)
	v.tick(v.e)
	if _, err := v.st.WorkItemByIssue(v.ctx, 101); !errors.Is(err, sql.ErrNoRows) || v.kg.Calls() != 0 {
		t.Fatalf("stopped first delegation resolved a work item or started: %v", err)
	}
	if res := v.its.Results(1); len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "Stop during admission") {
		t.Fatalf("results = %+v, want the operator's stop", res)
	}
}

// A run linked before its hold has no attempt. A stop during its admission
// read discards the poll and leaves no attempt for a later delegation to send.
func TestStopDuringLinkedRunReadStands(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "First turn.")
	v.tick(v.e)
	v.its.add(2, "coder", 101, "HL-1", "Second turn.")
	v.tick(v.e)
	if r := v.run(2); r.State != store.RunWaiting || r.WorkItemID == 0 {
		t.Fatalf("run 2 = %+v, want linked and waiting for the first turn", r)
	}
	v.its.beforeIssueRead = func(int64) {
		v.its.beforeIssueRead = nil
		if ok, err := v.st.StopIssue(v.ctx, "HL-1", "Stop during admission", "joe"); err != nil || !ok.Any() {
			t.Errorf("stop = %+v, %v", ok, err)
		}
	}
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Done.", nil)
	v.tickReconciliation(3, 5*time.Second)
	if a, err := v.st.LatestAttempt(v.ctx, 2); err != nil || a != nil {
		t.Fatalf("stopped run recorded an attempt: %+v, %v", a, err)
	}
	// A newer delegation resumes the work item but not the stopped run.
	v.its.add(3, "coder", 101, "HL-1", "Third turn.")
	v.tickReconciliation(3, 5*time.Second)
	if a, err := v.st.LatestAttempt(v.ctx, 2); err != nil || a != nil || v.run(2).State != store.RunClosed {
		t.Fatalf("newer delegation revived the stopped run: %+v, %v", a, err)
	}
	for _, send := range v.kg.Sends() {
		if strings.HasPrefix(send.msg.MessageID, "itsaplan-run/2/") {
			t.Fatalf("stopped run sent %s", send.msg.MessageID)
		}
	}
	if res := v.its.Results(2); len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "Stop during admission") {
		t.Fatalf("results = %+v, want the operator's stop", res)
	}
}

// A delegation held after its work item stopped or finished is not linked,
// and no stop counter moves. `connector cancel` stops it on its run, and it
// never starts when its blockers complete.
func TestCancelStopsHeldDelegationAfterStoppedOrDoneWork(t *testing.T) {
	for _, prior := range []string{store.WorkStopped, store.WorkDone} {
		t.Run(prior, func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Original work.")
			v.tick(v.e)
			v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Done.", nil)
			v.tick(v.e)
			if prior == store.WorkStopped {
				if ok, err := v.st.StopIssue(v.ctx, "HL-1", "First stop", "joe"); err != nil || !ok.WorkItem {
					t.Fatalf("first stop = %+v, %v", ok, err)
				}
			} else {
				if _, err := v.st.DB.ExecContext(v.ctx, `UPDATE work_item SET state = 'done' WHERE issue_id = 101`); err != nil {
					t.Fatal(err)
				}
				v.its.moveTo(101, 3, "")
			}
			v.dependency(101, 102, "HL-2", 2)
			v.its.add(2, "coder", 101, "HL-1", "Held delegation.")
			v.tick(v.e)
			v.held(2)
			if ok, err := v.st.StopIssue(v.ctx, "HL-1", "Cancel the held delegation", "joe"); err != nil || ok != (store.IssueStop{HeldRuns: 1}) {
				t.Fatalf("cancel = %+v, %v; want the held run stopped", ok, err)
			}
			v.its.moveTo(102, 3, "")
			for range 3 {
				v.tick(v.e)
			}
			if a, err := v.st.LatestAttempt(v.ctx, 2); err != nil || a != nil || v.kg.Executions() != 1 {
				t.Fatalf("canceled delegation started: %+v, %v", a, err)
			}
			if res := v.its.Results(2); len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "Cancel the held delegation") {
				t.Fatalf("results = %+v, want the operator's cancel", res)
			}
			if w := v.work(101); w.State != prior {
				t.Fatalf("work item = %s, want %s", w.State, prior)
			}
		})
	}
}

// rollbackHolds is the statement in ARCHITECTURE.md "Admission rollback".
const rollbackHolds = `UPDATE run SET stopped_reason = 'the connector was rolled back while this run was held for admission', updated_at = now()
WHERE work_item_id IS NULL AND state = 'waiting' AND stopped_reason IS NULL`

// main's connector resumes a waiting run through its work item. The rollback
// statement stops each unlinked held run, leaves a linked waiting run, and
// changes nothing when it runs again.
func TestRollbackStatementStopsOnlyUnlinkedHolds(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "First turn.")
	v.tick(v.e)
	v.its.add(2, "coder", 101, "HL-1", "Second turn.")
	v.its.add(3, "coder-b", 103, "HL-3", "Held.")
	v.dependency(103, 104, "HL-4", 2)
	v.tick(v.e)
	v.held(3)
	if r := v.run(2); r.State != store.RunWaiting || r.WorkItemID == 0 {
		t.Fatalf("run 2 = %+v, want linked and waiting", r)
	}
	for _, want := range []int64{1, 0} {
		res, err := v.st.DB.ExecContext(v.ctx, rollbackHolds)
		if err != nil {
			t.Fatal(err)
		}
		if n, err := res.RowsAffected(); err != nil || n != want {
			t.Fatalf("rollback stopped %d runs, %v; want %d", n, err, want)
		}
	}
	v.tick(v.e)
	if res := v.its.Results(3); len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "rolled back") {
		t.Fatalf("results = %+v, want the rollback stop", res)
	}
	if v.run(2).StopRequested || v.kg.Executions() != 1 {
		t.Fatal("rollback stopped a linked run or started held work")
	}
}

func TestTerminalRootIssueStopsHeldFirstRun(t *testing.T) {
	for _, column := range []int64{3, 4} {
		for _, removeRelation := range []bool{false, true} {
			t.Run(fmt.Sprintf("column=%d/removed=%t", column, removeRelation), func(t *testing.T) {
				v := newEnv(t)
				v.its.add(1, "coder", 101, "HL-1", "Do it.")
				v.dependency(101, 102, "HL-2", 2)
				v.tick(v.e)
				v.held(1)
				v.its.moveTo(101, column, "")
				v.e.Release()
				v.e = v.engine("root-state-restart")
				if removeRelation {
					v.its.issues[101].Links = []itsaplan.IssueLink{}
				} else {
					// Stopping the root must not depend on its blocker being
					// complete or even readable.
					delete(v.its.issues, 102)
				}
				v.tick(v.e)
				if r := v.run(1); r.State != store.RunClosed || r.WorkItemID != 0 || v.kg.Calls() != 0 {
					t.Fatalf("terminal root started work: %+v", r)
				}
				if res := v.its.Results(1); len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "issue is in a") {
					t.Fatalf("terminal root result = %+v", res)
				}
				// Reopening does not revive the stopped delegation.
				v.its.moveTo(101, 2, "")
				v.its.issues[101].Links = []itsaplan.IssueLink{}
				v.tick(v.e)
				if v.kg.Calls() != 0 {
					t.Fatal("reopening revived the stopped run")
				}
			})
		}
	}
}

func TestSlowAdmissionsDoNotStallDelivery(t *testing.T) {
	v := newDeliveryEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Deliver this.")
	v.its.add(2, "coder-b", 102, "HL-2", "Cancel this.")
	v.tick(v.e)
	first, err := v.st.LatestAttempt(v.ctx, 1)
	if err != nil || first == nil {
		t.Fatalf("first attempt = %+v, %v", first, err)
	}
	second, err := v.st.LatestAttempt(v.ctx, 2)
	if err != nil || second == nil {
		t.Fatalf("second attempt = %+v, %v", second, err)
	}
	for id := int64(10); id < 20; id++ {
		v.its.add(id, "coder", id+100, fmt.Sprintf("HL-%d", id), "Held work.")
		v.dependency(id+100, id+1000, fmt.Sprintf("HL-%d", id+1000), 2)
	}
	for range 4 {
		v.tick(v.e)
	}
	for id := int64(10); id < 20; id++ {
		v.held(id)
	}
	v.e.Release()
	v.cfg.AdmissionBudget = 200 * time.Millisecond
	v.e = v.engine("bounded-admission")
	reads := make(chan int64, 20)
	unblock := make(chan struct{})
	v.its.waitIssueRead = func(ctx context.Context, id int64) error {
		if id < 1000 {
			return nil
		}
		reads <- id
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-unblock:
			return errors.New("dependency unavailable")
		}
	}
	v.gh.push("factory/HL-1", "sha-1", "fix: delivered")
	v.kg.finish(first.TaskID, kagent.StateCompleted, "Pushed the fix.", nil)
	v.its.cancel(2, false)
	v.clk.Advance(61 * time.Second)
	for round := range 2 {
		ctx, cancel := context.WithTimeout(v.ctx, 4*time.Second)
		started := time.Now()
		err := v.e.Tick(ctx)
		cancel()
		if err != nil || time.Since(started) > 1500*time.Millisecond {
			t.Fatalf("admission stalled delivery: %v, elapsed %v", err, time.Since(started))
		}
		if len(reads) != 1 {
			t.Fatalf("a tick restarted or duplicated the in-flight poll: reads = %d", len(reads))
		}
		if round == 0 {
			if len(v.gh.PRs()) != 1 || len(v.its.Results(1)) != 1 {
				t.Fatal("delivery or result outbox did not progress behind slow holds")
			}
			v.kg.mu.Lock()
			canceled := v.kg.tasks[second.TaskID].cancelRequested
			v.kg.mu.Unlock()
			if !canceled {
				t.Fatal("cancellations did not progress behind slow holds")
			}
		}
	}
	close(unblock)
	v.e.Release() // joins all background reads before fixture cleanup
}

func TestNewSlowClaimsCannotStarveAnExistingHold(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Existing hold.")
	v.dependency(101, 102, "HL-2", 2)
	v.tick(v.e)
	v.held(1)
	v.its.moveTo(102, 3, "")
	reads := make(chan int64, 10)
	v.its.waitIssueRead = func(ctx context.Context, id int64) error {
		if id < 103 {
			return nil
		}
		reads <- id
		<-ctx.Done()
		return ctx.Err()
	}
	// Keep the original engine: its scheduling history must put the old hold
	// ahead of each newly arriving run, even though claims happen first.
	v.its.add(2, "coder", 103, "HL-3", "Slow new claim.")
	v.tick(v.e)
	if v.kg.Executions() != 1 || v.run(1).State != store.RunDispatched {
		t.Fatal("a slow new claim starved the existing hold after its blocker completed")
	}
	v.its.add(3, "coder", 104, "HL-4", "Another slow claim.")
	v.tick(v.e)
	if len(reads) != 1 {
		t.Fatal("new claims displaced or duplicated the current admission poll")
	}
	v.e.Release()
}

func TestSuccessfulGraphCanOutlastTheTickWait(t *testing.T) {
	for _, terminalRoot := range []bool{false, true} {
		t.Run(fmt.Sprintf("terminal-root=%t", terminalRoot), func(t *testing.T) {
			v := newEnv(t)
			v.e.Release()
			v.cfg.AdmissionBudget = 50 * time.Millisecond
			v.e = v.engine("slow-successful-graph")
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.dependency(101, 102, "HL-2", 3)
			v.dependency(101, 103, "HL-3", 3)
			reads := make(chan int64, 4)
			advance := make(chan struct{})
			v.its.waitIssueRead = func(ctx context.Context, id int64) error {
				if id == 101 {
					return nil
				}
				reads <- id
				select {
				case <-advance:
					return nil
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			// Each successful dependency read takes longer than the configured
			// tick wait. Neither the tick nor the second read restarts the graph.
			for _, id := range []int64{102, 103} {
				v.tick(v.e)
				v.held(1)
				select {
				case got := <-reads:
					if got != id {
						t.Fatalf("read %d, want %d without restarting the traversal", got, id)
					}
				case <-time.After(5 * time.Second):
					t.Fatal("dependency read did not start")
				}
				if id == 103 && terminalRoot {
					v.its.moveTo(101, 4, "")
				}
				select {
				case advance <- struct{}{}:
				case <-time.After(5 * time.Second):
					t.Fatal("dependency read was canceled by the tick wait")
				}
			}
			// Finish the original poll, including its fresh root read and result.
			for range 20 {
				v.tick(v.e)
				if v.kg.Executions() == 1 || v.run(1).State == store.RunClosed {
					break
				}
			}
			if terminalRoot {
				if v.run(1).State != store.RunClosed || v.kg.Executions() != 0 {
					t.Fatal("a root canceled during the slow graph was dispatched")
				}
			} else if v.run(1).State != store.RunDispatched || v.kg.Executions() != 1 {
				t.Fatal("a successful graph exceeding the tick wait never dispatched")
			}
			if len(reads) != 0 {
				t.Fatal("a completed part of the in-flight graph was read again")
			}
		})
	}
}

func TestSuccessfulGraphOutlastsReadTimeoutAndKeepsLease(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.AdmissionBudget = 20 * time.Millisecond
	v.cfg.AdmissionReadTimeout = 2 * time.Second
	v.e = v.engine("long-graph-with-heartbeats")
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	for id := int64(102); id <= 104; id++ {
		v.dependency(101, id, fmt.Sprintf("HL-%d", id-100), 3)
	}
	type readDeadline struct {
		id        int64
		remaining time.Duration
	}
	reads := make(chan readDeadline, 16)
	v.its.waitIssueRead = func(ctx context.Context, id int64) error {
		if id == 101 {
			return nil
		}
		deadline, _ := ctx.Deadline()
		reads <- readDeadline{id, time.Until(deadline)}
		// Three individually successful requests exceed the request timeout
		// in aggregate. The later requests must get their own full deadlines.
		timer := time.NewTimer(800 * time.Millisecond)
		defer timer.Stop()
		select {
		case <-timer.C:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	v.tick(v.e)
	v.held(1)
	select {
	case first := <-reads:
		if first.id != 102 || first.remaining <= time.Second || first.remaining > v.cfg.AdmissionReadTimeout {
			t.Fatalf("first read deadline = %+v", first)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("dependency read did not start")
	}
	v.clk.Advance(61 * time.Second)
	if err := v.e.Heartbeats(v.ctx); err != nil {
		t.Fatal(err)
	}
	if v.its.Run(1).beats == 0 {
		t.Fatal("a dependency read prevented the held run's lease renewal")
	}
	deadline := time.Now().Add(10 * time.Second)
	for v.kg.Executions() == 0 && time.Now().Before(deadline) {
		v.tick(v.e)
	}
	if v.kg.Executions() != 1 || v.run(1).State != store.RunDispatched {
		t.Fatal("a successful traversal exceeding the request timeout never dispatched")
	}
	for _, id := range []int64{103, 104} {
		select {
		case read := <-reads:
			if read.id != id || read.remaining <= time.Second || read.remaining > v.cfg.AdmissionReadTimeout {
				t.Fatalf("read = %+v, want %d with a fresh deadline", read, id)
			}
		default:
			t.Fatalf("missing read of %d", id)
		}
	}
	if len(reads) != 0 {
		t.Fatal("the long graph restarted an already successful read")
	}
}

func TestReleaseRechecksAllBlockersAfterSlowRead(t *testing.T) {
	for _, column := range []int64{2, 4} {
		t.Run(fmt.Sprintf("reopened-column=%d", column), func(t *testing.T) {
			v := newEnv(t)
			v.e.Release()
			v.cfg.AdmissionBudget = 20 * time.Millisecond
			v.e = v.engine("blocker-changes-during-read")
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.dependency(101, 102, "HL-2", 3)
			v.dependency(101, 103, "HL-3", 3)
			entered := make(chan struct{}, 1)
			proceed := make(chan struct{})
			v.its.waitIssueRead = func(ctx context.Context, id int64) error {
				if id != 103 {
					return nil
				}
				select {
				case entered <- struct{}{}:
				default:
				}
				select {
				case <-proceed:
					return nil
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			v.tick(v.e)
			select {
			case <-entered:
			case <-time.After(5 * time.Second):
				t.Fatal("later blocker read did not start")
			}
			// A was read as completed, then changes while the graph reads B.
			// The relation IDs stay the same throughout the poll.
			v.its.moveTo(102, column, "")
			close(proceed)
			deadline := time.Now().Add(5 * time.Second)
			for !strings.Contains(v.run(1).HoldReason, "HL-2") && time.Now().Before(deadline) {
				v.tick(v.e)
				if v.kg.Executions() != 0 {
					t.Fatal("stale completion evidence released admission")
				}
			}
			v.held(1)
			if !strings.Contains(v.run(1).HoldReason, "HL-2") {
				t.Fatal("fresh blocker state did not become the hold reason")
			}
			v.e.Release()
			v.its.waitIssueRead = nil
			v.e = v.engine("reopened-blocker-restart")
			for range 2 {
				v.tick(v.e)
				v.held(1)
			}
			wantComments := 0
			if column == 4 {
				wantComments = 1
			}
			if len(v.its.Comments()) != wantComments || v.kg.Calls() != 0 {
				t.Fatal("replaying the fresh blocker state duplicated a comment or started work")
			}
			v.its.moveTo(102, 3, "")
			deadline = time.Now().Add(5 * time.Second)
			for v.kg.Executions() == 0 && time.Now().Before(deadline) {
				v.tick(v.e)
			}
			if v.kg.Executions() != 1 {
				t.Fatal("completing the reopened blocker did not release admission")
			}
		})
	}
}

func TestCancellationDuringAdmissionReadDiscardsThePoll(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.AdmissionBudget = 20 * time.Millisecond
	v.e = v.engine("cancellation-during-read")
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 3)
	entered := make(chan struct{}, 1)
	proceed := make(chan struct{})
	v.its.waitIssueRead = func(ctx context.Context, id int64) error {
		if id != 102 {
			return nil
		}
		entered <- struct{}{}
		select {
		case <-proceed:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	v.tick(v.e)
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("dependency read did not start")
	}
	v.its.cancel(1, false)
	v.clk.Advance(61 * time.Second)
	if err := v.e.Heartbeats(v.ctx); err != nil {
		t.Fatal(err)
	}
	if v.run(1).State != store.RunClosed {
		t.Fatal("a pending dependency read prevented cancellation")
	}
	close(proceed)
	v.tick(v.e)
	v.e.Release()
	if v.run(1).State != store.RunClosed || v.run(1).WorkItemID != 0 || v.kg.Calls() != 0 {
		t.Fatal("the completed read resurrected a canceled run")
	}
	if a, err := v.st.LatestAttempt(v.ctx, 1); err != nil || a != nil {
		t.Fatalf("canceled admission created an attempt: %+v, %v", a, err)
	}
}

func TestRedeliveryDuringAdmissionReadDiscardsThePoll(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.AdmissionBudget = 20 * time.Millisecond
	v.e = v.engine("redelivery-during-read")
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 3)
	entered := make(chan struct{}, 1)
	proceed := make(chan struct{})
	v.its.waitIssueRead = func(ctx context.Context, id int64) error {
		if id != 102 {
			return nil
		}
		select {
		case entered <- struct{}{}:
		default:
		}
		select {
		case <-proceed:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	v.tick(v.e)
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("dependency read did not start")
	}
	// Model a reclaimed lease being recorded while the old attempt's read
	// is still in flight. Its successful response cannot authorize a send.
	v.clk.Advance(301 * time.Second)
	claim, err := v.its.Claim(v.ctx, "coder")
	if err != nil || claim == nil {
		t.Fatalf("redelivery = %+v, %v", claim, err)
	}
	if _, _, err := v.st.RecordClaim(v.ctx, store.ClaimedRun{
		ID: claim.ID, AgentKey: "coder", IssueID: 101, IssueKey: "HL-1", Attempt: claim.Attempts,
	}); err != nil {
		t.Fatal(err)
	}
	close(proceed)
	deadline := time.Now().Add(5 * time.Second)
	for v.kg.Executions() == 0 && time.Now().Before(deadline) {
		v.tick(v.e)
	}
	if v.run(1).Attempt != 2 || v.kg.Executions() != 1 {
		t.Fatal("the fresh attempt did not run its own admission poll")
	}
	var rootReads int
	if err := v.st.DB.QueryRowContext(v.ctx, `SELECT attempts FROM admission_read
		WHERE run_id = 1 AND run_attempt = 1 AND operation = 'issue' AND resource = '101'`).Scan(&rootReads); err != nil || rootReads != 1 {
		t.Fatalf("the stale poll reached its final release read: %d root reads, %v", rootReads, err)
	}
	v.e.Release()
}

func TestRestartDiscardsAnInFlightAdmission(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.AdmissionBudget = 50 * time.Millisecond
	v.e = v.engine("before-admission-restart")
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 3)
	v.its.waitIssueRead = func(ctx context.Context, id int64) error {
		if id == 102 {
			<-ctx.Done()
			return ctx.Err()
		}
		return nil
	}
	v.tick(v.e)
	v.held(1)
	v.e.Release()
	v.its.waitIssueRead = nil
	v.its.moveTo(102, 2, "")
	v.e = v.engine("after-admission-restart")
	v.tick(v.e)
	v.held(1)
	if v.kg.Calls() != 0 {
		t.Fatal("a prior process's partial admission survived restart")
	}
}

type admissionCaller struct {
	*fakeKagent
	entered chan struct{}
	proceed chan struct{}
}

func (c *admissionCaller) Pin(ctx context.Context) (context.Context, kagent.CallerIdentity, error) {
	select {
	case c.entered <- struct{}{}:
	default:
	}
	select {
	case <-c.proceed:
		return c.fakeKagent.Pin(ctx)
	case <-ctx.Done():
		return ctx, kagent.CallerIdentity{}, ctx.Err()
	}
}

func TestAdmissionAndGrantRetryShareConcurrency(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Original work.")
	v.tick(v.e)
	v.kg.finish(v.kg.lastTask(), kagent.StateFailed, "codex_login_required", nil)
	v.tick(v.e)
	v.its.add(2, "coder", 102, "HL-2", "Dependent work.")
	v.dependency(102, 103, "HL-3", 2)
	v.tick(v.e)
	v.held(2)
	v.e.Release()
	v.cfg.Agents[0].Concurrency = 1
	v.cfg.AdmissionBudget = 50 * time.Millisecond
	caller := &admissionCaller{fakeKagent: v.kg, entered: make(chan struct{}, 1), proceed: make(chan struct{})}
	v.cfg.Caller = caller
	v.e = v.engine("concurrent-admission-and-retry")
	v.its.moveTo(103, 3, "")
	if ok, err := v.st.ClearGrantBlock(v.ctx, store.GrantScope("factory-homelab", "coder")); err != nil || !ok {
		t.Fatalf("unblock = %t, %v", ok, err)
	}
	// Pause admission at its caller lookup, with a slot reserved but no send.
	// The delivery loop must defer the older grant retry without waiting on
	// that same slow lookup or exceeding the configured concurrency.
	ctx, cancel := context.WithTimeout(v.ctx, time.Second)
	defer cancel()
	if err := v.e.Tick(ctx); err != nil {
		t.Fatalf("admission's start blocked the delivery loop: %v", err)
	}
	select {
	case <-caller.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the admission did not reach its caller lookup")
	}
	if v.run(1).State != store.RunWaiting || len(v.kg.Sends()) != 1 {
		t.Fatal("the grant retry raced admission for the same slot")
	}
	close(caller.proceed)
	for range 20 {
		v.tick(v.e)
		if len(v.kg.Sends()) == 2 {
			break
		}
	}
	if v.run(2).State != store.RunDispatched || v.run(1).State != store.RunWaiting || len(v.kg.Sends()) != 2 {
		t.Fatal("admission and the grant retry exceeded concurrency one")
	}
}

// A held run whose lease lapsed returns dispatched with no delivery attempt.
// When its admission passes while another run holds the agent's last slot,
// it waits with its admission kept and starts only once the slot frees.
func TestReclaimedHeldRunWaitsForAFreeSlot(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.Agents[0].Concurrency = 1
	v.e = v.engine("reclaimed-held-run")
	v.its.add(1, "coder", 101, "HL-1", "Dependent work.")
	v.dependency(101, 102, "HL-2", 2)
	v.tick(v.e)
	v.held(1)
	v.its.add(2, "coder", 103, "HL-3", "Independent work.")
	v.tick(v.e)
	if v.kg.Executions() != 1 || v.run(2).State != store.RunDispatched {
		t.Fatal("the independent run did not take the only slot")
	}
	task := v.kg.lastTask()

	v.its.reclaimElsewhere(1)
	v.clk.Advance(61 * time.Second)
	v.tick(v.e)
	if v.run(1).State != store.RunStale {
		t.Fatalf("state = %s, want stale", v.run(1).State)
	}
	v.its.moveTo(102, 3, "")
	fr := v.its.Run(1)
	redelivery := fr.run
	redelivery.Attempts = fr.attempts
	if err := v.e.HandleClaim(v.ctx, "coder", &redelivery); err != nil {
		t.Fatal(err)
	}
	for range 3 {
		if r := v.run(1); r.State != store.RunWaiting || r.HoldReason != "" || r.WorkItemID != 0 || v.latest(1) != nil {
			t.Fatalf("admitted run with no free slot = %+v", r)
		}
		if v.kg.Executions() != 1 {
			t.Fatal("the reclaimed run started past the agent's concurrency")
		}
		v.tick(v.e)
	}
	v.kg.finish(task, kagent.StateCompleted, "Done.", nil)
	v.tickUntil(time.Second, 10, func() bool { return v.kg.Executions() == 2 })
	if v.run(1).State != store.RunDispatched || v.latest(1) == nil {
		t.Fatal("the reclaimed run did not start when the slot freed")
	}
}

func TestCrashAfterHoldCommentAcceptanceDoesNotDuplicate(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.dependency(101, 102, "HL-2", 4)
	v.tick(v.e)
	if len(v.its.Comments()) != 1 {
		t.Fatal("comment was not accepted")
	}
	// Restore the DB state at the crash boundary: send_started_at committed,
	// itsaplan accepted, but OutboxDelivered had not committed yet.
	if _, err := v.st.DB.ExecContext(v.ctx, `UPDATE outbox SET delivered_at = NULL
		WHERE dedupe_key = 'comment/admission/canceled/1/102'`); err != nil {
		t.Fatal(err)
	}
	v.e.Release()
	e := v.engine("accepted-comment-restart")
	v.tick(e)
	v.held(1)
	if len(v.its.Comments()) != 1 || v.kg.Calls() != 0 {
		t.Fatal("comment acceptance replay duplicated the comment or started work")
	}
}

func TestAdmissionSweepSharesBlockerReads(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "First")
	v.its.add(2, "coder-b", 102, "HL-2", "Second")
	v.dependency(101, 103, "HL-3", 1)
	v.dependency(102, 103, "HL-3", 1)
	for i := 0; i < 2; i++ {
		before := v.its.issueCalls
		v.tick(v.e)
		v.held(1)
		v.held(2)
		if got := v.its.issueCalls - before; got != 3 {
			t.Fatalf("sweep %d read %d issues, want two roots and one shared blocker", i, got)
		}
	}
}
