package dispatch_test

import (
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/internal/reaper"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

// forced reads a session's force-delete record: requests, RevertSession
// calls, writer epoch and whether kagent confirmed it gone. ok is false
// without one.
func (v *env) forced(id string) (requests, reverts int, epoch int64, confirmed, ok bool) {
	v.t.Helper()
	err := v.st.DB.QueryRow(`SELECT requests, reverts, writer_epoch, confirmed_at IS NOT NULL FROM session_deletion
		WHERE context_id = $1 AND force AND reason = 'stuck'`, id).Scan(&requests, &reverts, &epoch, &confirmed)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, 0, 0, false, false
	}
	if err != nil {
		v.t.Fatal(err)
	}
	return requests, reverts, epoch, confirmed, true
}

// A running turn with no task event for the 20-minute progress lease is read
// again, recorded first, and canceled. kagent confirms the CancelTask, and
// the run spends one engineering retry in the same session (HOME-141).
func TestQuietTurnPastTheProgressLeaseIsCanceledAndRetried(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	first := v.latest(1)
	v.kg.say(first.TaskID, "working")
	v.clk.Advance(30 * time.Second)
	v.tick(v.e)
	lastEvent := v.clk.Now()
	v.tickUntil(30*time.Second, 50, func() bool { return len(v.cancellationRows(first.ID)) == 1 })
	if quiet := v.clk.Now().Sub(lastEvent); quiet < 20*time.Minute || quiet > 21*time.Minute {
		t.Fatalf("canceled after %s without a task event, want the 20m lease", quiet)
	}
	if got := v.cancellationRows(first.ID); got[0] != "lease_expired/pending/1" {
		t.Fatalf("cancellation = %v, want CancelTask sent", got)
	}
	var reads int
	// The lease decision and the final activity check before CancelTask
	// each record a read; bounded lifecycle metadata cannot replace either.
	if err := v.st.DB.QueryRow(`SELECT attempts FROM status_read WHERE message_id = $1`,
		"kagent/GetTask/factory-homelab/coder/"+first.TaskID).Scan(&reads); err != nil || reads != 2 {
		t.Fatalf("recorded task read: attempts = %d, %v", reads, err)
	}
	v.tickUntil(30*time.Second, 4, func() bool { return v.latest(1).ID != first.ID })
	if a, err := v.st.GetAttempt(v.ctx, first.ID); err != nil || a.ErrorClass != dispatch.ClassAbandoned || a.Error != "lease_expired: canceled" {
		t.Fatalf("first attempt = %+v, %v; want abandoned", a, err)
	}
	if retry := v.latest(1); retry.MessageID != "itsaplan-run/1/retry-1" || retry.ContextID != first.ContextID ||
		retry.State != store.AttemptAccepted || v.work(101).EngineeringRetries != 1 {
		t.Fatalf("retry = %+v, retries %d", retry, v.work(101).EngineeringRetries)
	}
}

// An open tool call within the 60-minute tool lease, or a task event within
// the progress lease, keeps a turn's slot.
func TestBusyTurnKeepsItsSlot(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	a := v.latest(1)
	v.kg.tool(a.TaskID, "ci-1", kagent.PartToolCall)
	for range 50 {
		v.clk.Advance(61 * time.Second)
		v.tick(v.e)
	}
	v.kg.tool(a.TaskID, "ci-1", kagent.PartToolResponse)
	for i := range 60 {
		v.clk.Advance(61 * time.Second)
		if i%15 == 14 {
			v.kg.say(a.TaskID, fmt.Sprintf("step %d", i))
		}
		v.tick(v.e)
	}
	if got := v.cancellationRows(a.ID); len(got) != 0 || v.kg.state(a.TaskID) != kagent.StateWorking || v.run(1).State != store.RunDispatched {
		t.Fatalf("cancellations = %v, task %s, run %s", got, v.kg.state(a.TaskID), v.run(1).State)
	}
}

// kagent keeps a tool call open when the runtime that ran it dies: the task
// stays WORKING with no event, and CancelTask never returns. The turn keeps
// its slot for the 60-minute tool lease, then gets a lease_expired
// cancellation that the open call does not withdraw. Ten minutes later the
// session is deleted with force, and the run spends one engineering retry
// in a fresh session (HOME-159).
func TestOpenToolCallPastTheToolLeaseIsForceDeletedAndRetried(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	turn, session := v.latest(1), v.work(101).SessionID
	v.kg.tool(turn.TaskID, "ci-1", kagent.PartToolCall)
	v.clk.Advance(30 * time.Second)
	v.tick(v.e)
	opened := v.clk.Now()
	v.kg.hung = true
	v.tickUntil(30*time.Second, 130, func() bool { return len(v.cancellationRows(turn.ID)) == 1 })
	if held := v.clk.Now().Sub(opened); held <= time.Hour || held > 61*time.Minute {
		t.Fatalf("canceled after %s with a tool call open, want the 60m tool lease", held)
	}
	if got := v.cancellationRows(turn.ID); got[0] != "lease_expired/pending/1" {
		t.Fatalf("cancellation = %v, want CancelTask sent despite the open tool call", got)
	}
	requested := v.clk.Now()
	v.tickUntil(30*time.Second, 25, func() bool { _, _, _, _, ok := v.forced(session); return ok })
	if waited := v.clk.Now().Sub(requested); waited < 10*time.Minute || waited > 11*time.Minute {
		t.Fatalf("force delete %s after the cancellation, want 10m", waited)
	}
	v.kg.hung = false
	v.tickUntil(30*time.Second, 4, func() bool { return v.latest(1).ID != turn.ID })
	if f := v.kg.Forced(); !slices.Equal(f, []string{session}) {
		t.Fatalf("force-deleted %v, want %s", f, session)
	}
	if a, err := v.st.GetAttempt(v.ctx, turn.ID); err != nil || a.ErrorClass != dispatch.ClassAbandoned || a.Error != "lease_expired: session_deleted" {
		t.Fatalf("turn = %+v, %v; want abandoned", a, err)
	}
	retry, s := v.latest(1), v.kg.Sends()
	if retry.ContextID == session || retry.State != store.AttemptAccepted || v.work(101).EngineeringRetries != 1 ||
		!strings.Contains(s[len(s)-1].msg.Parts[0].Text, "## Replacement session") {
		t.Fatalf("retry = %+v, retries %d", retry, v.work(101).EngineeringRetries)
	}
}

// A task event after the lease's read and before the first CancelTask
// withdraws the lease's cancellation, and the lease starts again.
func TestActivityBeforeCancelTaskWithdrawsALeaseCancellation(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	a := v.latest(1)
	for range 19 {
		v.clk.Advance(61 * time.Second)
		v.tick(v.e)
	}
	// The tick reads the task three times: progress, the lease's decision and
	// the cancellation, which sees the new event.
	v.kg.getTaskHooks = []func(){nil, nil, func() { v.kg.say(a.TaskID, "still going") }}
	v.clk.Advance(61 * time.Second)
	v.tick(v.e)
	if got := v.cancellationRows(a.ID); len(got) != 1 || got[0] != "lease_expired/withdrawn/0" {
		t.Fatalf("cancellations = %v, want the lease's withdrawn unsent", got)
	}
	for range 19 {
		v.clk.Advance(61 * time.Second)
		v.tick(v.e)
	}
	if got := v.cancellationRows(a.ID); len(got) != 1 || v.kg.state(a.TaskID) != kagent.StateWorking {
		t.Fatalf("cancellations = %v, task %s within the restarted lease", got, v.kg.state(a.TaskID))
	}
}

// kagent's runtime hangs: CancelTask times out. Ten minutes after the
// cancellation, the connector deletes the session with force, recorded
// first. Once kagent reports it gone, the continuation is abandoned and
// retried in a fresh session with the work item's recorded brief, without
// reading the issue again (HOME-141).
func TestUnconfirmedCancellationDeletesTheSessionWithForce(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "First turn.")
	v.tick(v.e)
	v.kg.finish(v.latest(1).TaskID, kagent.StateCompleted, "done", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	v.its.add(2, "coder", 101, "HL-1", "Continue.")
	v.tick(v.e)
	turn, session, reads := v.latest(2), v.work(101).SessionID, v.its.issueCalls
	v.kg.hung = true
	v.kg.forceErr = errors.New("rpc error: code = Unavailable desc = controller restarting")
	v.tickUntil(30*time.Second, 50, func() bool { return len(v.cancellationRows(turn.ID)) == 1 })
	requested := v.clk.Now()
	v.tickUntil(30*time.Second, 25, func() bool { _, _, _, _, ok := v.forced(session); return ok })
	if waited := v.clk.Now().Sub(requested); waited < 10*time.Minute || waited > 11*time.Minute {
		t.Fatalf("force delete %s after the cancellation, want 10m", waited)
	}
	// The failed delete was recorded before it was sent, and the work item
	// no longer names the session; kagent still has it, so the run waits.
	if n, reverts, epoch, confirmed, _ := v.forced(session); n != 1 || reverts != 1 || epoch != v.e.Epoch() || confirmed {
		t.Fatalf("force delete record = %d requests, %d reverts, epoch %d, confirmed %v", n, reverts, epoch, confirmed)
	}
	if w := v.work(101); w.SessionID != "" || v.run(2).State != store.RunDispatched || !v.latest(2).Open() {
		t.Fatalf("work item %+v, run %s", w, v.run(2).State)
	}
	v.kg.forceErr, v.kg.hung = nil, false
	v.tickUntil(30*time.Second, 4, func() bool { return v.latest(2).ID != turn.ID })
	if f := v.kg.Forced(); !slices.Equal(f, []string{session}) {
		t.Fatalf("force-deleted %v, want %s", f, session)
	}
	if _, _, _, confirmed, _ := v.forced(session); !confirmed {
		t.Fatal("deletion not confirmed")
	}
	if a, err := v.st.GetAttempt(v.ctx, turn.ID); err != nil || a.ErrorClass != dispatch.ClassAbandoned || a.Error != "lease_expired: session_deleted" {
		t.Fatalf("turn = %+v, %v; want abandoned", a, err)
	}
	retry, s := v.latest(2), v.kg.Sends()
	text := s[len(s)-1].msg.Parts[0].Text
	if retry.MessageID != "itsaplan-run/2/replacement-1-1" || retry.ContextID == session || retry.State != store.AttemptAccepted ||
		!strings.Contains(text, "### Issue HL-1: Issue 101 title") || !strings.Contains(text, "## Replacement session") {
		t.Fatalf("retry = %+v, text %q", retry, text)
	}
	if v.its.issueCalls != reads || v.work(101).EngineeringRetries != 1 {
		t.Fatalf("issue reads %d (was %d), retries %d", v.its.issueCalls, reads, v.work(101).EngineeringRetries)
	}
}

// kagent refuses the force delete, as it does while a suspend holds its
// claim. The run keeps its slot and its attempt: kagent may still run the
// turn. The connector reverts the session after each refusal. Once the
// runtime crashes, such as after a person deletes its worker pod, the
// revert fails the turn and the run retries in a fresh session.
func TestRefusedForceDeleteKeepsTheSlotUntilARevertEndsTheTurn(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	turn, session := v.latest(1), v.work(101).SessionID
	v.kg.hung = true
	v.kg.forceErr = fmt.Errorf("%w: runtime cleanup or lifecycle work is not yet settled", kagent.ErrSessionUnrecoverable)
	for range 90 {
		v.clk.Advance(30 * time.Second)
		v.tick(v.e)
	}
	active, err := v.st.CountActiveRuns(v.ctx, "coder")
	if err != nil || active != 1 || v.run(1).State != store.RunDispatched || v.latest(1).ID != turn.ID || !v.latest(1).Open() {
		t.Fatalf("active %d, %v; run %s; attempt %+v", active, err, v.run(1).State, v.latest(1))
	}
	if n, reverts, _, confirmed, _ := v.forced(session); n < 20 || reverts != n || confirmed || len(v.kg.Forced()) != 0 {
		t.Fatalf("force delete record = %d requests, %d reverts, confirmed %v", n, reverts, confirmed)
	}
	v.kg.hung = false
	v.kg.crash(session)
	v.tickUntil(30*time.Second, 6, func() bool { return v.latest(1).ID != turn.ID })
	if a, err := v.st.GetAttempt(v.ctx, turn.ID); err != nil || a.State != store.AttemptFailed {
		t.Fatalf("turn = %+v, %v; want the reverted turn failed", a, err)
	}
	if retry := v.latest(1); retry.ContextID == session || retry.State != store.AttemptAccepted || v.work(101).EngineeringRetries != 1 {
		t.Fatalf("retry = %+v", retry)
	}
}

// A stop that arrives while a lease's cancellation waits on kagent stands:
// the force delete ends the turn and the run reports the stop, with no
// retry.
func TestStopDuringAnUnconfirmedCancellationStands(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	turn := v.latest(1)
	v.kg.hung = true
	v.tickUntil(30*time.Second, 50, func() bool { return len(v.cancellationRows(turn.ID)) == 1 })
	if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
		t.Fatal(err)
	}
	v.tickUntil(30*time.Second, 30, func() bool { return v.run(1).State == store.RunClosed })
	res := v.its.Run(1).results
	if len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "delivery stopped: operator canceled") {
		t.Fatalf("results = %+v", res)
	}
	if a := v.latest(1); a.ID != turn.ID || a.State != store.AttemptCanceled || len(v.kg.Forced()) != 1 ||
		v.work(101).State != store.WorkStopped || v.work(101).EngineeringRetries != 0 || len(v.kg.Sends()) != 1 {
		t.Fatalf("attempt %+v, work item %+v", a, v.work(101))
	}
}

// A round's heartbeat records its PR's merge; the round, claimed before
// it, has nothing left to deliver and stops. A run claimed after the merge,
// such as a question on the done issue, runs (HOME-141).
func TestMergedPullRequestStopsARunningRound(t *testing.T) {
	v := newDeliveryEnv(t)
	round, task := v.startRound()
	v.gh.merge(1, "merge-sha-1")
	v.tickUntil(61*time.Second, 4, func() bool { return v.run(round.ID).State == store.RunClosed })
	if r := v.run(round.ID); !strings.Contains(r.ClosedReason, "delivery stopped: its pull request #1 merged") ||
		v.kg.state(task) != kagent.StateCanceled || v.work(101).State != store.WorkDone {
		t.Fatalf("round %s (%s), task %s, work item %s", r.State, r.ClosedReason, v.kg.state(task), v.work(101).State)
	}
	v.its.add(2, "coder", 101, "HL-7", "Why did the flake happen?")
	v.tick(v.e)
	v.poll()
	if r := v.run(2); r.State != store.RunDispatched || r.StopRequested {
		t.Fatalf("run 2 = %+v, want it running", r)
	}
}

// Invariant: once two turns are out, kagent never answers sends, cancels,
// task listings or deletes without force. A quiet turn and a continuation
// whose send went unanswered both free their slots within the lease bound,
// while the other phases keep running: heartbeats, results and claims
// (HOME-141).
func TestHungKagentFreesEverySlotWithinTheBound(t *testing.T) {
	v := newEnv(t)
	v.cfg.Bindings[0].MaxEngineeringRetries = 0
	v.e.Release()
	v.e = v.engine("connector-hung")
	// Issue 102's first turn finishes, so its next run continues the session.
	v.its.add(2, "coder", 102, "HL-2", "First turn.")
	v.tick(v.e)
	v.kg.finish(v.latest(2).TaskID, kagent.StateCompleted, "done", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	start, sessions := v.clk.Now(), []string{v.work(101).SessionID, v.work(102).SessionID}
	v.kg.hung = true
	v.its.add(3, "coder", 102, "HL-2", "Continue.")
	v.its.add(4, "coder", 104, "HL-4", "Queued behind the two held slots.")
	v.tick(v.e)
	if a := v.latest(3); a == nil || a.State != store.AttemptAmbiguous || a.ContextID != sessions[1] {
		t.Fatalf("continuation = %+v, want its send unanswered", a)
	}
	v.kg.listErr = errHung
	freed := map[int64]time.Duration{}
	for range 70 {
		v.clk.Advance(30 * time.Second)
		v.tick(v.e)
		for _, id := range []int64{1, 3} {
			if _, ok := freed[id]; !ok && v.run(id).State == store.RunClosed {
				freed[id] = v.clk.Now().Sub(start)
			}
		}
	}
	if freed[1] == 0 || freed[1] > 31*time.Minute || freed[3] == 0 || freed[3] > 16*time.Minute {
		t.Fatalf("freed after %v, want the quiet turn by 31m and the unanswered send by 16m", freed)
	}
	if f := v.kg.Forced(); !slices.Equal(f, []string{sessions[1], sessions[0]}) {
		t.Fatalf("force-deleted %v, want %v", f, sessions)
	}
	for _, id := range []int64{1, 3} {
		res := v.its.Run(id).results
		if len(res) != 1 || res[0].Status != "failed" || !strings.Contains(res[0].Error, "engineering retries exhausted") {
			t.Fatalf("run %d results = %+v", id, res)
		}
	}
	// Run 4 was claimed once a slot freed. Its first send names no session
	// and kagent lists no tasks, so nothing can show it never started: it
	// waits for an operator, as before.
	if r := v.run(4); r.State != store.RunDispatched || v.latest(4).Error == "" {
		t.Fatalf("run 4 = %s, attempt %+v", r.State, v.latest(4))
	}
	if beats := v.its.Run(1).beats; beats < 25 {
		t.Fatalf("run 1 heartbeats = %d while kagent hung", beats)
	}
}

func TestGoneWorkerRetriesBusyTurnWithinFiveMinutes(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	a := v.latest(1)
	v.kg.tool(a.TaskID, "ci-1", kagent.PartToolCall)
	// Snapshot a running Substrate actor (revision 18) on a real connector
	// attempt, then clear its assignment as Substrate does on crash (revision 19).
	// The fixtures encode ateapi.v1.Actor metadata, state and worker_assignment.
	_, err := v.st.DB.Exec(`CREATE TABLE actor_fixture(name text, proto bytea);
		CREATE FUNCTION public.reaper_actors() RETURNS TABLE(atespace text, name text, proto bytea)
		LANGUAGE sql AS $$ SELECT 'factory-homelab'::text, name, proto FROM actor_fixture $$`)
	if err != nil {
		t.Fatal(err)
	}
	_, err = v.st.DB.Exec(`INSERT INTO actor_fixture VALUES ($1, decode($2,'hex'))`,
		"session-"+a.ContextID, "0a151a116163746f722d696e6361726e6174696f6e20122a37080212330a11666163746f72792d722d686f6d656c61621a14686f6d656c61622d636f6465782d776f726b657222086f726967696e616c")
	if err != nil {
		t.Fatal(err)
	}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: "factory-r-homelab", Name: "homelab-codex-worker", UID: "original",
	}}
	pods := fake.NewSimpleClientset(pod).CoreV1()
	lease, err := reaper.TryLease(v.ctx, v.st.DB)
	if err != nil || lease == nil {
		t.Fatalf("lease=%v err=%v", lease, err)
	}
	t.Cleanup(func() { lease.Close() })
	scan := func() {
		t.Helper()
		if err := reaper.Scan(v.ctx, v.st.DB, v.st.DB, pods, lease); err != nil {
			t.Fatal(err)
		}
	}
	scan()
	if err := pods.Pods(pod.Namespace).Delete(v.ctx, pod.Name, metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	start := v.clk.Now()
	if _, err := v.st.DB.Exec(`UPDATE actor_fixture SET proto=decode($1,'hex')`, "0a151a116163746f722d696e6361726e6174696f6e20132a020805"); err != nil {
		t.Fatal(err)
	}
	v.clk.Advance(15 * time.Second)
	scan()
	// A replacement is still running after the observed revision. Evidence
	// alone cannot authorize a second send while its session refuses deletion.
	v.kg.forceErr = errors.New("session still running")
	v.clk.Advance(30 * time.Second)
	v.tick(v.e)
	if requests, _, _, confirmed, ok := v.forced(a.ContextID); !ok || requests != 1 || confirmed {
		t.Fatal("expected a pending journaled fence for the original session")
	}
	if !v.latest(1).Open() || v.latest(1).ID != a.ID {
		t.Fatal("abandoned before authoritative session fence")
	}
	v.kg.forceErr = nil
	v.tickUntil(30*time.Second, 9, func() bool {
		next := v.latest(1)
		return next.ID != a.ID && next.State == store.AttemptAccepted && next.ContextID != a.ContextID
	})
	if elapsed := v.clk.Now().Sub(start); elapsed > 5*time.Minute {
		t.Fatalf("retry took %s after pod deletion", elapsed)
	}
	var held bool
	if err := v.st.DB.QueryRow(`SELECT EXISTS(SELECT 1 FROM recovery_hold WHERE target_attempt_id=$1)`, a.ID).Scan(&held); err != nil || held {
		t.Fatalf("worker-loss recovery created an operator hold: held=%t err=%v", held, err)
	}
	if _, _, _, confirmed, _ := v.forced(a.ContextID); !confirmed {
		t.Fatal("retry started before session deletion was confirmed")
	}
}

func (v *env) recordWorkerLoss(a *store.Attempt) {
	v.t.Helper()
	_, err := v.st.DB.Exec(`INSERT INTO scoped_worker_fence(attempt_id,session_id,namespace,pod,uid,gone_at,actor_uid,actor_version)
		VALUES ($1,$2,'factory-r-homelab','homelab-codex-a','original',now(),'actor',19)`, a.ID, a.ContextID)
	if err != nil {
		v.t.Fatal(err)
	}
}

func TestCompletedTurnSurvivesWorkerLoss(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	a := v.latest(1)
	v.kg.finish(a.TaskID, kagent.StateCompleted, "Pushed the fix.", nil)
	v.recordWorkerLoss(a)
	v.tickUntil(5*time.Second, 4, func() bool { return len(v.its.Run(1).results) > 0 })
	got := v.its.Run(1).results
	if len(got) != 1 || got[0].Status != "success" || got[0].Output != "Pushed the fix." ||
		v.latest(1).ID != a.ID || len(v.kg.Forced()) != 0 || v.work(101).EngineeringRetries != 0 {
		t.Fatalf("worker loss discarded the completed turn: results=%+v forced=%v", got, v.kg.Forced())
	}
}
