package dispatch_test

import (
	"errors"
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
	// The first tick admits in HandleClaim and then polls all open runs.
	if err := v.st.DB.QueryRowContext(v.ctx, `SELECT count(*), min(attempts) FROM admission_read WHERE run_id = 1`).Scan(&n, &attempts); err != nil || n != 3 || attempts != 3 {
		t.Fatalf("read replay identities: %d rows, %d attempts, %v", n, attempts, err)
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
