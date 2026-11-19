package dispatch_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func (f *fakeItsaplan) GetProject(_ context.Context, project string) (*itsaplan.Project, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.beforeGateRead != nil {
		f.beforeGateRead("project", project)
	}
	p := &itsaplan.Project{
		Columns:   append(append([]itsaplan.Column(nil), f.columns...), itsaplan.Column{ID: 5, Name: "Backlog", StateType: "backlog"}),
		Assignees: []itsaplan.Assignee{{UserID: "joe-id", Username: "joe", Kind: "member"}, {UserID: "coder-user", Username: "coder", Kind: "agent"}},
	}
	p.Permissions.WorkItems.Read = !f.activationMissingPermissions
	p.Permissions.WorkItems.Create = !f.activationMissingPermissions
	p.Permissions.WorkItems.Edit = !f.activationMissingPermissions
	p.Permissions.AIAgents.Read = !f.activationMissingPermissions
	return p, f.columnsErr
}

func (f *fakeItsaplan) SearchGateIssues(_ context.Context, _, marker string) ([]itsaplan.Issue, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.beforeGateRead != nil {
		f.beforeGateRead("search", marker)
	}
	var hits []itsaplan.Issue
	for _, is := range f.issues {
		if strings.Contains(strings.ToLower(is.Description), strings.ToLower(marker)) {
			hits = append(hits, *is)
		}
	}
	return hits, nil
}

func (f *fakeItsaplan) CreateGateIssue(_ context.Context, _ string, body itsaplan.GateIssueWrite) (*itsaplan.Issue, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.beforeGateWrite != nil {
		f.beforeGateWrite()
	}
	f.gateCreates = append(f.gateCreates, body)
	if len(utf16.Encode([]rune(body.Description))) > 50000 {
		return nil, &itsaplan.HTTPError{Status: 422, Body: "description exceeds 50000 characters"}
	}
	if f.gateCreateErr != nil {
		return nil, f.gateCreateErr
	}
	id := int64(200 + len(f.gateCreates))
	is := &itsaplan.Issue{
		ID: id, Identifier: fmt.Sprintf("HL-%d", id), Title: body.Title, CreatedAt: f.now().UTC().Format(time.RFC3339Nano),
		Description: body.Description, ColumnID: body.ColumnID, AssigneeUserID: body.AssigneeUserID, DelegateUserID: body.DelegateUserID,
	}
	f.issues[id] = is
	if f.loseGateCreate {
		f.loseGateCreate = false
		return nil, errors.New("response lost after create")
	}
	copy := *is
	return &copy, nil
}

func (f *fakeItsaplan) UpdateGateIssue(_ context.Context, id int64, body itsaplan.GateIssueWrite) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.beforeGateWrite != nil {
		f.beforeGateWrite()
	}
	f.gateUpdates = append(f.gateUpdates, body)
	if len(utf16.Encode([]rune(body.Description))) > 50000 {
		return &itsaplan.HTTPError{Status: 422, Body: "description exceeds 50000 characters"}
	}
	is := f.issues[id]
	is.Title, is.Description, is.ColumnID = body.Title, body.Description, body.ColumnID
	is.AssigneeUserID, is.DelegateUserID = body.AssigneeUserID, body.DelegateUserID
	return f.gateUpdateErr
}

const factoryApproval = "To approve: an APPROVED review of this head by `jomcgi` clears the gate"

func newHumanGateEnv(t *testing.T) *env {
	t.Helper()
	v := newDeliveryEnv(t)
	v.cfg.Bindings[0].HumanGateAssignee = "joe"
	v.cfg.Bindings[0].Review.RequiredChecks = []dispatch.Check{{Name: "pr-checks", StatusCreator: "buildbuddy-io[bot]"}}
	v.cfg.Bindings[0].Review.ReviewerCheck = &dispatch.Check{Name: "factory review", AppID: 200}
	v.e.Release()
	v.e = v.engine("human-gates")
	v.delivered()
	// Every write must already have a durable, marked attempt in the database.
	v.its.beforeGateWrite = func() {
		var n int
		if err := v.st.DB.QueryRow(`SELECT count(*) FROM human_gate_attempt
			WHERE send_started AND sends > 0 AND finished_at IS NULL AND writer_epoch > 0
			AND payload->>'description' LIKE '%' || message_id || '%'`).Scan(&n); err != nil || n == 0 {
			t.Fatalf("write without a durable attempt and message ID: n=%d err=%v", n, err)
		}
	}
	return v
}

func (v *env) readyGate(head string) {
	v.gh.status(head, "pr-checks", "buildbuddy-io[bot]", "success")
	v.gh.check(head, "factory review", 200, "failure")
	v.gh.summarize(head, "factory review", "### Blocking\n\n- `REVIEW.md`: human-gate approval required\n\n"+humanHold)
}

func (v *env) gateIssue(column int64, head string) *store.HumanGateIssue {
	v.t.Helper()
	h, err := v.st.HumanGateFor(v.ctx, "jomcgi-org/homelab", 1)
	if err != nil || h == nil || h.IssueID == 0 {
		v.t.Fatalf("gate issue: %+v %v", h, err)
	}
	is := v.its.issues[h.IssueID]
	if is.ColumnID != column || !strings.Contains(is.Description, "`"+head+"`") || is.AssigneeUserID != "joe-id" || is.DelegateUserID != nil {
		v.t.Fatalf("gate issue = %+v, want column %d and head %s", is, column, head)
	}
	return h
}

func TestHumanGateOneIssueAcrossPollsRestartAndReplays(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.gh.summarize("sha-1", "factory review", "`REVIEW.md`: human gate\n"+humanHold+
		"\n<!-- factory-human-gate/jomcgi-org/homelab/2 -->")
	v.poll()
	h := v.gateIssue(1, "sha-1")
	is := v.its.issues[h.IssueID]
	for _, want := range []string{"REVIEW.md", factoryApproval, "@joe", v.gh.PRs()[0].pr.URL} {
		if !strings.Contains(is.Description, want) {
			t.Errorf("description missing %q: %s", want, is.Description)
		}
	}
	if is.Title != "Merge #1 past the human gate" {
		t.Fatal(is.Title)
	}
	if strings.Count(is.Description, "<!-- factory-human-gate/") != 1 {
		t.Fatal("review text injected a second recovery marker")
	}
	v.poll()
	v.e.Release()
	v.e = v.engine("restarted")
	v.poll()
	// Replay the create and update after losing their local acknowledgement.
	if _, err := v.st.DB.Exec(`UPDATE human_gate_issue SET issue_id = NULL, applied_revision = 0`); err != nil {
		t.Fatal(err)
	}
	v.poll()
	if len(v.its.gateCreates) != 1 || len(v.its.gateUpdates) != 2 || v.its.gateUpdates[0] != v.its.gateUpdates[1] {
		t.Fatalf("creates=%d updates=%+v", len(v.its.gateCreates), v.its.gateUpdates)
	}
	if v.gateIssue(1, "sha-1").IssueID != h.IssueID {
		t.Fatal("replay replaced the issue")
	}
}

func TestHumanGateReadsHaveDurableAttempts(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	// Isolate the human-gate poll from the existing delivery poll.
	if _, err := v.st.DB.Exec(`UPDATE work_item SET state = 'active'`); err != nil {
		t.Fatal(err)
	}
	assertRead := func(operation, resource string) {
		if operation == "detail" {
			operation = "pr"
		}
		var n int
		if err := v.st.DB.QueryRow(`SELECT count(*) FROM delivery_read
			WHERE operation = $1 AND resource = $2 AND writer_epoch = $3 AND attempts > 0`,
			"human-gate/"+operation, resource, v.e.Epoch()).Scan(&n); err != nil || n == 0 {
			t.Fatalf("read %s %s lacked a durable attempt: n=%d err=%v", operation, resource, n, err)
		}
	}
	v.gh.onRead = assertRead
	v.its.beforeGateRead = assertRead
	v.its.loseGateCreate = true
	v.poll()
	v.e.Release()
	v.e = v.engine("read-recovery")
	v.poll()
	v.gateIssue(1, "sha-1")
}

func TestHumanGateHeadChangedDuringChecksCreatesNothing(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	if _, err := v.st.DB.Exec(`UPDATE work_item SET state = 'active'`); err != nil {
		t.Fatal(err)
	}
	v.gh.onChecks = func() {
		// CheckRuns holds the fake's mutex.
		v.gh.branches["factory/HL-7"].head = "sha-2"
	}
	v.poll()
	if len(v.its.gateCreates) != 0 {
		t.Fatal("a stale ready head created an issue")
	}
}

func TestHumanGateObsoleteUpdateDoesNotReplayOverNewHead(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.poll()
	v.gh.push("factory/HL-7", "sha-2", "pending head")
	v.its.gateUpdateErr = errors.New("lost update response")
	v.poll()
	v.gateIssue(5, "sha-2")
	v.gh.push("factory/HL-7", "sha-3", "ready head")
	v.readyGate("sha-3")
	v.its.gateUpdateErr = nil
	v.poll()
	v.poll()
	v.gateIssue(1, "sha-3")
	if len(v.its.gateUpdates) != 3 || len(v.its.gateCreates) != 1 {
		t.Fatalf("obsolete write replayed: updates=%d creates=%d", len(v.its.gateUpdates), len(v.its.gateCreates))
	}
}

func TestHumanGateSearchRequiresExactMarkerAndReadableOrder(t *testing.T) {
	for _, duplicates := range []bool{false, true} {
		t.Run(fmt.Sprint(duplicates), func(t *testing.T) {
			v := newHumanGateEnv(t)
			v.readyGate("sha-1")
			marker := "<!-- factory-human-gate/jomcgi-org/homelab/1 -->"
			if duplicates {
				// Copies with no createdAt have no defined earliest issue.
				v.its.issues[301] = &itsaplan.Issue{ID: 301, Description: marker}
				v.its.issues[302] = &itsaplan.Issue{ID: 302, Description: marker}
			} else {
				v.its.issues[301] = &itsaplan.Issue{ID: 301, Description: strings.ToUpper(marker)}
			}
			v.poll()
			if duplicates {
				if len(v.its.gateCreates)+len(v.its.gateUpdates)+len(v.its.Moves()) != 0 {
					t.Fatal("unordered copies caused a write")
				}
			} else {
				if len(v.its.gateCreates) != 1 {
					t.Fatal("inexact marker was adopted")
				}
				v.gateIssue(1, "sha-1")
			}
		})
	}
}

func TestHumanGateHeadsMoveBothWaysDuringActiveAndStoppedWork(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.poll()
	id := v.gateIssue(1, "sha-1").IssueID
	// Delivery does not poll active work items. The human issue still follows it.
	if _, err := v.st.DB.Exec(`UPDATE work_item SET state = 'active'`); err != nil {
		t.Fatal(err)
	}
	v.gh.push("factory/HL-7", "sha-2", "new head")
	v.poll()
	v.gateIssue(5, "sha-2")
	v.readyGate("sha-2")
	v.poll()
	v.gateIssue(1, "sha-2")
	// Readiness can also change in both directions on the same head.
	v.gh.status("sha-2", "pr-checks", "buildbuddy-io[bot]", "pending")
	v.poll()
	v.gateIssue(5, "sha-2")
	v.readyGate("sha-2")
	v.poll()
	v.gateIssue(1, "sha-2")
	if _, err := v.st.StopWork(v.ctx, v.work(101).ID, "human owns delivery", "operator"); err != nil {
		t.Fatal(err)
	}
	v.gh.push("factory/HL-7", "sha-3", "another head")
	v.poll()
	v.gateIssue(5, "sha-3")
	v.readyGate("sha-3")
	v.poll()
	if v.gateIssue(1, "sha-3").IssueID != id || len(v.its.gateCreates) != 1 {
		t.Fatal("a new head created another issue")
	}
}

func TestHumanGateTerminalStates(t *testing.T) {
	for _, merged := range []bool{false, true} {
		t.Run(fmt.Sprint(merged), func(t *testing.T) {
			v := newHumanGateEnv(t)
			v.readyGate("sha-1")
			v.poll()
			column := int64(4)
			if merged {
				v.gh.merge(1, "merged-sha")
				column = 3
			} else {
				v.gh.close(1)
			}
			// A lost update response must not strand a closed/merged PR's issue.
			v.its.gateUpdateErr = errors.New("lost update response")
			v.poll()
			v.its.gateUpdateErr = nil
			v.e.Release()
			v.e = v.engine("terminal-recovery")
			v.poll()
			h := v.gateIssue(column, "sha-1")
			if h.AppliedRevision != h.Revision {
				t.Fatalf("terminal move not acknowledged: %+v", h)
			}
		})
	}
}

func TestHumanGateReopenedPRKeepsItsIssueAfterRestart(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.poll()
	id := v.gateIssue(1, "sha-1").IssueID
	v.gh.close(1)
	v.poll()
	h := v.gateIssue(4, "sha-1")
	if h.AppliedRevision != h.Revision || v.work(101).State != store.WorkStopped {
		t.Fatal("close did not stop delivery and acknowledge Canceled")
	}
	v.e.Release()
	v.e = v.engine("reopened-gate")
	v.gh.byNumber(1).pr.State = "open"
	v.poll()
	v.gateIssue(1, "sha-1")
	v.gh.close(1)
	v.poll()
	v.gateIssue(4, "sha-1")
	v.gh.byNumber(1).pr.State = "open"
	v.gh.push("factory/HL-7", "sha-2", "not ready on reopening")
	v.poll()
	v.gateIssue(5, "sha-2")
	v.readyGate("sha-2")
	v.poll()
	v.gateIssue(1, "sha-2")
	v.gh.merge(1, "merged-sha")
	v.poll()
	if v.gateIssue(3, "sha-2").IssueID != id || len(v.its.gateCreates) != 1 {
		t.Fatal("reopening replaced the gate issue")
	}
	if v.work(101).State != store.WorkStopped {
		t.Fatal("gate polling resumed stopped delivery")
	}
	if work, err := v.st.HumanGateWork(v.ctx); err != nil || len(work) != 0 {
		t.Fatalf("acknowledged merge still eligible: %+v, %v", work, err)
	}
}

func TestHumanGateWriterLossBeforeCreateCanRetry(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	// Lose authority after the last recovery read, before starting the send.
	v.its.beforeGateRead = func(operation, _ string) {
		if operation == "search" {
			if _, err := v.st.DB.Exec(`UPDATE writer SET epoch = epoch + 1`); err != nil {
				t.Fatal(err)
			}
		}
	}
	v.clk.Advance(61 * time.Second)
	if err := v.e.Tick(v.ctx); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("tick = %v, want writer loss", err)
	}
	if len(v.its.gateCreates) != 0 {
		t.Fatal("lost writer reached POST")
	}
	var started bool
	var sends int
	if err := v.st.DB.QueryRow(`SELECT send_started, sends FROM human_gate_attempt WHERE operation = 'create'`).Scan(&started, &sends); err != nil || started || sends != 0 {
		t.Fatalf("unsent attempt: started=%t sends=%d err=%v", started, sends, err)
	}
	v.its.beforeGateRead = nil
	v.e.Release()
	v.e = v.engine("writer-after-gate-loss")
	v.poll()
	v.gateIssue(1, "sha-1")
	if len(v.its.gateCreates) != 1 {
		t.Fatal("new writer did not send the proven unsent create once")
	}
}

func TestHumanGateDatabaseFailureBeforeCreateNeedsNoCleanup(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	// Disconnect the writer exactly when the send is marked, and reject any
	// compensating cleanup. With separate mark/guard connections, the mark
	// commits and cleanup fails, stranding the create. On the writer session,
	// losing the connection rolls the mark back automatically. The sequence
	// survives rollback so a replacement writer can recover on the next poll.
	if _, err := v.st.DB.Exec(`
		CREATE SEQUENCE gate_disconnects;
		CREATE FUNCTION disconnect_gate_writer() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF NEW.operation = 'create' AND NEW.send_started AND nextval('gate_disconnects') = 1 THEN
				PERFORM pg_terminate_backend(pid, 5000) FROM pg_locks
					WHERE locktype = 'advisory' AND granted
					AND database = (SELECT oid FROM pg_database WHERE datname = current_database());
			END IF;
			RETURN NEW;
		END $$;
		CREATE TRIGGER disconnect_gate_writer AFTER UPDATE OF send_started ON human_gate_attempt
			FOR EACH ROW EXECUTE FUNCTION disconnect_gate_writer();
		CREATE FUNCTION refuse_gate_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF NEW.operation = 'create' AND NOT NEW.send_started THEN
				RAISE EXCEPTION 'injected cleanup unavailable' USING ERRCODE = '08006';
			END IF;
			RETURN NEW;
		END $$;
		CREATE TRIGGER refuse_gate_cleanup BEFORE UPDATE OF send_started ON human_gate_attempt
			FOR EACH ROW EXECUTE FUNCTION refuse_gate_cleanup();`); err != nil {
		t.Fatal(err)
	}
	v.clk.Advance(61 * time.Second)
	if err := v.e.Tick(v.ctx); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("tick = %v, want writer loss", err)
	}
	if len(v.its.gateCreates) != 0 {
		t.Fatal("lost writer reached POST")
	}
	var started bool
	var sends int
	if err := v.st.DB.QueryRow(`SELECT send_started, sends FROM human_gate_attempt WHERE operation = 'create'`).Scan(&started, &sends); err != nil || started || sends != 0 {
		t.Fatalf("failed transaction stranded the create: started=%t sends=%d err=%v", started, sends, err)
	}
	const messageID = "factory-human-gate/jomcgi-org/homelab/1/create"
	if err := v.st.ClearHumanGateSend(v.ctx, messageID, v.e.Epoch(), sends); err == nil || !strings.Contains(err.Error(), "cleanup unavailable") {
		t.Fatalf("cleanup failure was not injected: %v", err)
	}
	v.e.Release()
	v.e = v.engine("writer-after-gate-outage")
	v.poll()
	v.gateIssue(1, "sha-1")
	v.poll()
	if len(v.its.gateCreates) != 1 {
		t.Fatal("new writer did not create exactly one gate issue after the outage")
	}
}

func TestHumanGateUnsentCleanupHonorsCancellationAndIsFenced(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.its.gateCreateErr = errors.New("timeout; outcome unknown")
	v.poll()
	const messageID = "factory-human-gate/jomcgi-org/homelab/1/create"
	epoch := v.e.Epoch()
	// Model a later send before a stale cleanup from the first send arrives.
	var send int
	if err := v.st.DB.QueryRow(`UPDATE human_gate_attempt SET sends = sends + 1
		WHERE message_id = $1 RETURNING sends`, messageID).Scan(&send); err != nil {
		t.Fatal(err)
	}
	canceled, cancel := context.WithCancel(v.ctx)
	cancel()
	if err := v.st.ClearHumanGateSend(canceled, messageID, epoch, send); !errors.Is(err, context.Canceled) {
		t.Fatalf("cleanup ignored cancellation: %v", err)
	}
	clearAndCheck := func(clearEpoch int64, clearSend int, wantStarted bool) {
		t.Helper()
		if err := v.st.ClearHumanGateSend(v.ctx, messageID, clearEpoch, clearSend); err != nil {
			t.Fatal(err)
		}
		var started bool
		if err := v.st.DB.QueryRow(`SELECT send_started FROM human_gate_attempt WHERE message_id = $1`, messageID).Scan(&started); err != nil || started != wantStarted {
			t.Fatalf("cleanup: started=%t want=%t err=%v", started, wantStarted, err)
		}
	}
	clearAndCheck(epoch, send-1, true) // cannot clear a later send in this epoch
	clearAndCheck(epoch-1, send, true) // cannot clear a later writer's send
	clearAndCheck(epoch, send, false)  // retry can record the matching proof
}

func TestHumanGateLostCreateResponseReconcilesCurrentHead(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.its.loseGateCreate = true
	v.poll()
	v.e.Release()
	v.e = v.engine("lost-create")
	v.gh.push("factory/HL-7", "sha-2", "not ready")
	v.poll()
	v.gateIssue(5, "sha-2")
	if len(v.its.gateCreates) != 1 {
		t.Fatal("lost response repeated POST")
	}
}

// settleGateSends moves every started send past the settle window on the
// database clock, which the connector uses to age a send.
func (v *env) settleGateSends() {
	v.t.Helper()
	if _, err := v.st.DB.Exec(`UPDATE human_gate_attempt
		SET attempted_at = attempted_at - interval '11 minutes' WHERE send_started`); err != nil {
		v.t.Fatal(err)
	}
}

// gateIssues counts the itsaplan issues that carry PR 1's marker.
func (v *env) gateIssues() int {
	n := 0
	for _, is := range v.its.issues {
		if strings.Contains(is.Description, "<!-- factory-human-gate/jomcgi-org/homelab/1 -->") {
			n++
		}
	}
	return n
}

type gateCrash struct{}

func TestHumanGateCrashOrUnknownCreateMakesOneIssueAfterSettling(t *testing.T) {
	for _, tc := range []struct {
		name  string
		posts int // POSTs that reach itsaplan
	}{
		{name: "crash before POST", posts: 1},
		{name: "crash after accepted POST", posts: 1},
		{name: "unknown answer, nothing created", posts: 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := newHumanGateEnv(t)
			v.readyGate("sha-1")
			assertAttempt := v.its.beforeGateWrite
			switch tc.name {
			case "crash before POST":
				// The send marker has committed. The process exits before
				// the request leaves, so no proof that it is unsent remains.
				v.its.beforeGateWrite = func() {
					assertAttempt()
					v.its.beforeGateWrite = assertAttempt
					panic(gateCrash{})
				}
				crashed := func() (crashed bool) {
					defer func() {
						if r := recover(); r != nil {
							if r != (gateCrash{}) {
								panic(r)
							}
							crashed = true
						}
					}()
					v.clk.Advance(61 * time.Second)
					_ = v.e.Tick(v.ctx)
					return false
				}()
				if !crashed || len(v.its.gateCreates) != 0 {
					t.Fatal("tick did not stop between the send marker and the POST")
				}
			case "crash after accepted POST":
				v.its.loseGateCreate = true // itsaplan creates the issue; the answer is lost
				v.poll()
			default:
				v.its.gateCreateErr = errors.New("timeout; outcome unknown")
				v.poll()
				v.its.gateCreateErr = nil
			}
			var original string
			if err := v.st.DB.QueryRow(`SELECT payload::text FROM human_gate_attempt WHERE operation = 'create'`).Scan(&original); err != nil {
				t.Fatal(err)
			}
			v.e.Release()
			v.e = v.engine("after-" + strings.ReplaceAll(tc.name, " ", "-"))
			// A new ready head cannot unfreeze the payload of an unsettled send.
			v.gh.push("factory/HL-7", "sha-2", "new ready head")
			v.readyGate("sha-2")
			v.poll()
			v.poll()
			var current string
			if err := v.st.DB.QueryRow(`SELECT payload::text FROM human_gate_attempt WHERE operation = 'create'`).Scan(&current); err != nil || current != original {
				t.Fatalf("unsettled create payload changed: %v", err)
			}
			if tc.name == "crash after accepted POST" {
				v.gateIssue(1, "sha-2") // adopted at once from its marker
			} else if len(v.its.gateCreates) != tc.posts-1 {
				t.Fatal("an unsettled send was repeated")
			}
			v.settleGateSends()
			v.poll()
			v.poll()
			v.gateIssue(1, "sha-2")
			if len(v.its.gateCreates) != tc.posts || v.gateIssues() != 1 {
				t.Fatalf("posts=%d issues=%d, want %d POSTs and one issue", len(v.its.gateCreates), v.gateIssues(), tc.posts)
			}
		})
	}
}

// lateGateCreate models itsaplan committing a create after the connector
// stopped waiting for it: a marked issue appears without another POST.
func (v *env) lateGateCreate(id int64, createdAt time.Time) {
	v.t.Helper()
	var body itsaplan.GateIssueWrite
	var payload []byte
	if err := v.st.DB.QueryRow(`SELECT payload FROM human_gate_attempt WHERE operation = 'create'`).Scan(&payload); err != nil {
		v.t.Fatal(err)
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		v.t.Fatal(err)
	}
	v.its.issues[id] = &itsaplan.Issue{
		ID: id, Identifier: fmt.Sprintf("HL-%d", id), Title: body.Title, Description: body.Description,
		ColumnID: body.ColumnID, AssigneeUserID: body.AssigneeUserID, CreatedAt: createdAt.UTC().Format(time.RFC3339Nano),
	}
}

// recordCancels fails any move that has no started, unfinished cancel
// attempt under that duplicate's message ID.
func (v *env) recordCancels() {
	v.its.beforeMove = func(issueID int64) {
		var n int
		if err := v.st.DB.QueryRow(`SELECT count(*) FROM human_gate_attempt WHERE message_id = $1
			AND operation = 'cancel' AND send_started AND finished_at IS NULL AND writer_epoch > 0
			AND (payload->>'issueId')::bigint = $2`,
			fmt.Sprintf("factory-human-gate/jomcgi-org/homelab/1/duplicate/%d", issueID), issueID).Scan(&n); err != nil || n != 1 {
			v.t.Fatalf("move of %d without a durable cancel attempt: n=%d err=%v", issueID, n, err)
		}
	}
}

// gateCancels counts moves to Canceled (column 4) and duplicate comments by issue.
func (v *env) gateCancels() (moves, comments map[int64]int) {
	moves, comments = map[int64]int{}, map[int64]int{}
	for _, m := range v.its.Moves() {
		if m.columnID == 4 {
			moves[m.issueID]++
		}
	}
	for _, c := range v.its.Comments() {
		if strings.HasPrefix(c.body, "Duplicate of ") {
			comments[c.issueID]++
		}
	}
	return moves, comments
}

// Both timed-out creates commit after the retry. The first poll that sees
// them keeps the earliest created and cancels the other once.
func TestHumanGateDuplicateCreatesResolveToOneOpenIssue(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.recordCancels()
	v.its.gateCreateErr = errors.New("timeout; outcome unknown")
	v.poll()
	sent := v.clk.Now()
	v.settleGateSends()
	v.poll()
	v.its.gateCreateErr = nil
	// The later row has the lower ID: createdAt, not ID, decides.
	v.lateGateCreate(301, sent.Add(time.Minute))
	v.lateGateCreate(302, sent)
	v.poll()
	v.poll()
	if id := v.gateIssue(1, "sha-1").IssueID; id != 302 {
		t.Fatalf("kept issue %d, want the earliest created, 302", id)
	}
	moves, comments := v.gateCancels()
	if v.its.issues[301].ColumnID != 4 || len(moves) != 1 || moves[301] != 1 || len(comments) != 1 || comments[301] != 1 {
		t.Fatalf("moves=%v comments=%v, want one move and one comment for 301", moves, comments)
	}
	if c := v.its.Comments(); !strings.Contains(c[len(c)-1].body, "Duplicate of HL-302, the human-gate issue for [pull request #1]") {
		t.Fatalf("comment = %q", c[len(c)-1].body)
	}
	if len(v.its.gateCreates) != 2 {
		t.Fatalf("creates=%d, want 2", len(v.its.gateCreates))
	}
}

// A copy that commits after adoption is canceled on a later poll, also after
// a lost move response and a new writer. The adopted issue stays, though
// each copy has an earlier createdAt.
func TestHumanGateLateDuplicatesCancelAcrossRetryAndTakeover(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.recordCancels()
	v.its.gateCreateErr = errors.New("timeout; outcome unknown")
	v.poll()
	sent := v.clk.Now()
	v.its.gateCreateErr = nil
	v.settleGateSends()
	v.poll()
	kept := v.gateIssue(1, "sha-1").IssueID
	v.lateGateCreate(301, sent)
	v.its.moveErr = []error{errors.New("lost move response")}
	v.poll()
	v.poll()
	v.e.Release()
	v.e = v.engine("duplicate-takeover")
	v.lateGateCreate(302, sent.Add(-time.Minute))
	v.poll()
	v.poll()
	moves, comments := v.gateCancels()
	if v.gateIssue(1, "sha-1").IssueID != kept || moves[kept] != 0 || len(moves) != 2 || moves[301] != 1 || moves[302] != 1 ||
		comments[301] != 1 || comments[302] != 1 || v.its.issues[302].ColumnID != 4 {
		t.Fatalf("kept=%d moves=%v comments=%v", kept, moves, comments)
	}
	var sends int
	if err := v.st.DB.QueryRow(`SELECT sends FROM human_gate_attempt WHERE message_id = $1`,
		"factory-human-gate/jomcgi-org/homelab/1/duplicate/301").Scan(&sends); err != nil || sends != 2 {
		t.Fatalf("retried move: sends=%d err=%v, want 2 under one message ID", sends, err)
	}
}

func TestHumanGateApprovalNamesTheRuleForThePR(t *testing.T) {
	for _, tc := range []struct {
		name, author, label, want string
	}{
		{name: "factory", author: "jomcgi-factory", want: factoryApproval + " ([rule](https://github.com/jomcgi-org/homelab/blob/main/REVIEW.md#joe-decides))"},
		{name: "joe", author: "jomcgi", want: "To approve: the PR's `factory review` check says which approval it needs. " +
			"For a PR authored by `jomcgi`, the reviewer's verified authorship waiver may apply " +
			"([decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6891640540), [#8049 scope](https://github.com/jomcgi-org/homelab/issues/8049)). " +
			"Holds such as a shared head or reviewer recovery always need an APPROVED review of this head by `jomcgi` " +
			"([rule](https://github.com/jomcgi-org/homelab/blob/main/REVIEW.md#joe-decides))."},
		{name: "held", author: "jomcgi", label: "Needs-Human", want: "explicit hold (`human` or `needs-human`), so only an APPROVED review of this head by `jomcgi` clears the gate"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := newHumanGateEnv(t)
			v.readyGate("sha-1")
			pr := v.gh.byNumber(1)
			pr.pr.Author = tc.author
			if tc.label != "" {
				pr.pr.Labels = []string{tc.label}
			}
			v.poll()
			is := v.its.issues[v.gateIssue(1, "sha-1").IssueID]
			// No push condition is presented as enough to clear the gate.
			if !strings.Contains(is.Description, tc.want) || strings.Contains(is.Description, "merge is the approval") ||
				strings.Contains(is.Description, "every push") {
				t.Fatalf("description lacks %q: %s", tc.want, is.Description)
			}
		})
	}
}

func TestHumanGateRefusedCreateCanRetry(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.its.gateCreateErr = &itsaplan.HTTPError{Status: 403, Body: "refused"}
	v.poll()
	v.its.gateCreateErr = nil
	v.poll()
	v.gateIssue(1, "sha-1")
	if len(v.its.gateCreates) != 2 || v.its.gateCreates[0] != v.its.gateCreates[1] {
		t.Fatal("refused create changed despite unchanged evidence")
	}
}

func TestHumanGateLongReviewDescriptionsFitOnCreateAndUpdate(t *testing.T) {
	for _, text := range []string{"x", "界🙂"} {
		t.Run(text, func(t *testing.T) {
			v := newHumanGateEnv(t)
			for _, head := range []string{"sha-1", "sha-2"} {
				v.gh.push("factory/HL-7", head, "long review")
				v.readyGate(head)
				v.gh.summarize(head, "factory review", "`REVIEW.md`: human gate\n"+strings.Repeat(text, 59900/len(text))+humanHold)
				v.poll()
				h := v.gateIssue(1, head)
				if h.AppliedRevision != h.Revision {
					t.Fatal("long review update was rejected")
				}
			}
			if len(v.its.gateCreates) != 1 || len(v.its.gateUpdates) != 2 {
				t.Fatalf("creates=%d updates=%d", len(v.its.gateCreates), len(v.its.gateUpdates))
			}
			for _, writes := range [][]itsaplan.GateIssueWrite{v.its.gateCreates, v.its.gateUpdates} {
				for _, body := range writes {
					if len(body.Description) > 50000 || !utf8.ValidString(body.Description) {
						t.Fatal("description exceeds its budget or splits UTF-8")
					}
					for _, want := range []string{"REVIEW.md", "Head SHA:", factoryApproval, "Review summary truncated", v.gh.PRs()[0].pr.URL + "/checks", "<!-- factory-human-gate/jomcgi-org/homelab/1 -->", "<!-- message-id: factory-human-gate/jomcgi-org/homelab/1/"} {
						if !strings.Contains(body.Description, want) {
							t.Errorf("truncation dropped %q", want)
						}
					}
				}
			}
		})
	}
}

func TestHumanGateRejectedOversizedCreateRefreshesPayloadAfterRestart(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.its.gateCreateErr = &itsaplan.HTTPError{Status: 422, Body: "invalid description"}
	v.poll()
	// Model the durable payload left by the old connector after a validation
	// refusal. Its permanent create ID must not trap the oversized description.
	old := v.its.gateCreates[0]
	old.Description += strings.Repeat("x", 50001)
	payload, err := json.Marshal(old)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := v.st.DB.Exec(`UPDATE human_gate_attempt SET payload = $1 WHERE operation = 'create'`, payload); err != nil {
		t.Fatal(err)
	}
	v.its.gateCreateErr = nil
	v.e.Release()
	v.e = v.engine("rejected-create-recovery")
	v.gh.push("factory/HL-7", "sha-2", "new ready head")
	v.readyGate("sha-2")
	v.poll()
	v.gateIssue(1, "sha-2")
	if len(v.its.gateCreates) != 2 || !strings.Contains(v.its.gateCreates[1].Description, "`sha-2`") {
		t.Fatal("refused create did not use the current head's bounded payload")
	}
	var attempts, sends int
	if err := v.st.DB.QueryRow(`SELECT count(*), sum(sends) FROM human_gate_attempt WHERE operation = 'create'`).Scan(&attempts, &sends); err != nil || attempts != 1 || sends != 2 {
		t.Fatalf("create identity changed: attempts=%d sends=%d err=%v", attempts, sends, err)
	}
}

func TestHumanGateUnreadyEvidenceCreatesNothing(t *testing.T) {
	for _, reason := range []string{"mixed", "pending CI", "failed CI", "foreign CI", "foreign review", "stale review", "pending review", "duplicate review", "draft", "wrong base"} {
		t.Run(reason, func(t *testing.T) {
			v := newHumanGateEnv(t)
			v.readyGate("sha-1")
			switch reason {
			case "mixed":
				v.gh.summarize("sha-1", "factory review", "human gate and non-gate findings")
			case "pending CI":
				v.gh.status("sha-1", "pr-checks", "buildbuddy-io[bot]", "pending")
			case "failed CI":
				v.gh.status("sha-1", "pr-checks", "buildbuddy-io[bot]", "failure")
			case "foreign CI":
				v.gh.status("sha-1", "pr-checks", "stranger", "success")
			case "foreign review":
				v.gh.check("sha-1", "factory review", 201, "failure")
				v.gh.summarize("sha-1", "factory review", humanHold)
			case "stale review":
				v.gh.checks["sha-1"][0].HeadSHA = "old"
			case "pending review":
				v.gh.check("sha-1", "factory review", 200, "")
				v.gh.summarize("sha-1", "factory review", humanHold)
			case "duplicate review":
				v.gh.checks["sha-1"] = append(v.gh.checks["sha-1"], github.CheckRun{
					Name: "factory review", AppID: 200, HeadSHA: "sha-1", Status: "completed", Conclusion: "success",
				})
			case "draft":
				v.gh.byNumber(1).pr.Draft = true
			case "wrong base":
				v.gh.byNumber(1).pr.BaseRef = "other"
			}
			v.poll()
			if len(v.its.gateCreates) != 0 {
				t.Fatalf("created an issue for %s", reason)
			}
		})
	}
}
