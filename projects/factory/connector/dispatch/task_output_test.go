package dispatch_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// oversizedTask reproduces #8012 with PostgreSQL and the real HTTP client:
// valid task JSON with 1,024 artifacts of 32 KiB each, even with no history.
// Twelve completed tasks precede the live task in the same session.
func oversizedTask(t *testing.T) (*env, *reconciliationWire, *store.Attempt) {
	t.Helper()
	v, wire := newReconciliationWire(t, 2)
	v.dispatchReconciliationInitial()
	a := v.latest(1)
	v.kg.mu.Lock()
	task := v.kg.tasks[a.TaskID]
	text := strings.Repeat("x", 32<<10)
	for i := range 1024 {
		task.task.Artifacts = append(task.task.Artifacts, &kagent.Artifact{
			ArtifactID: fmt.Sprintf("artifact-%d", i), Parts: []kagent.Part{{Text: text}},
		})
	}
	var order []string
	for i := range 12 {
		id := fmt.Sprintf("older-%d", i)
		v.kg.tasks[id] = &fakeTask{agent: task.agent, task: kagent.Task{
			ID: id, ContextID: a.ContextID, Status: kagent.TaskStatus{State: kagent.StateCompleted},
			History: []*kagent.Message{{MessageID: id + "-input", Role: "ROLE_USER"}},
		}}
		order = append(order, id)
	}
	v.kg.order = append(order, a.TaskID)
	v.kg.listPageSize = 1
	v.kg.mu.Unlock()
	return v, wire, a
}

func TestOversizedOutputRemainsCancellableUntilCleanupFinishes(t *testing.T) {
	v, wire, a := oversizedTask(t)
	// The production client must detect the size condition explicitly.
	if _, err := wire.client.GetTask(v.ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.TaskID); !errors.Is(err, kagent.ErrResponseTooLarge) {
		t.Fatalf("oversized task: %v", err)
	}
	v.tick(v.e)
	if got := v.latest(1); got.State != store.AttemptAccepted || !strings.Contains(got.Error, "32 MiB") {
		t.Fatalf("oversized live task lost ownership or its visible condition: %+v", got)
	}
	if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
		t.Fatal(err)
	}
	// kagent's public projection remains SUBMITTED during native cleanup,
	// even after CancelTask took effect. Its full response is still oversized.
	wire.mu.Lock()
	wire.pendingCleanup, wire.cleanupTaskID = true, a.TaskID
	wire.mu.Unlock()
	v.tick(v.e)
	cs := v.pendingCancellations()
	if len(cs) != 1 || cs[0].Requests != 1 || !v.latest(1).Open() {
		t.Fatalf("cancellation was not sent or was confirmed from its oversized response: %+v", cs)
	}
	v.kg.mu.Lock()
	requested := v.kg.tasks[a.TaskID].cancelRequested
	v.kg.mu.Unlock()
	if !requested {
		t.Fatal("CancelTask did not reach the exact accepted task")
	}
	v.restartWire(t, wire)
	v.tickReconciliation(3, time.Second)
	active, err := v.st.CountActiveRuns(v.ctx, "coder")
	if err != nil {
		t.Fatal(err)
	}
	if len(v.pendingCancellations()) != 1 || !v.latest(1).Open() || active != 1 {
		t.Fatal("native cleanup released the execution slot before terminal metadata")
	}
	wire.mu.Lock()
	wire.pendingCleanup = false
	wire.mu.Unlock()
	v.tick(v.e)
	active, err = v.st.CountActiveRuns(v.ctx, "coder")
	if err != nil {
		t.Fatal(err)
	}
	if len(v.pendingCancellations()) != 0 || v.latest(1).State != store.AttemptCanceled || active != 0 {
		t.Fatalf("terminal cancellation did not release the slot: attempt=%+v active=%d", v.latest(1), active)
	}
	if v.kg.Executions() != 1 || v.work(101).EngineeringRetries != 0 {
		t.Fatal("oversized output caused another execution")
	}
}

func TestOversizedActivityWithdrawsQuietCancellationButKeepsStop(t *testing.T) {
	for _, reason := range []string{store.CancelQuotaStall, store.CancelLeaseExpired} {
		for _, stopped := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/stopped=%t", reason, stopped), func(t *testing.T) {
				v, wire, a := oversizedTask(t)
				messageID := "kagent/GetTask/" + a.KagentNamespace + "/" + a.KagentAgent + "/" + a.TaskID
				activityReads := 0
				wire.onTranscript = func(id, taskID string) {
					if id != messageID {
						return // progress's transcript read
					}
					activityReads++
					var attempts int
					var epoch int64
					err := v.st.DB.QueryRow(`SELECT attempts, writer_epoch FROM status_read WHERE message_id = $1`, id).Scan(&attempts, &epoch)
					if err != nil || attempts != activityReads || epoch != v.e.Epoch() || taskID != a.TaskID {
						t.Errorf("activity read without its journal: attempts=%d epoch=%d task=%s err=%v", attempts, epoch, taskID, err)
					}
				}
				// The quiet decision predates the oversized output, as it can when
				// a tool produces output between the decision and CancelTask.
				if err := v.st.RequestQuietCancel(v.ctx, v.st.DB, a.ID, a.RunID, reason, a.EventCount); err != nil {
					t.Fatal(err)
				}
				if stopped {
					if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator stop", "operator:test"); err != nil {
						t.Fatal(err)
					}
				}
				v.tick(v.e)
				if activityReads != 1 {
					t.Fatalf("activity reads = %d, want one recorded read before deciding cancellation", activityReads)
				}
				want := reason + "/withdrawn/0"
				if stopped {
					want = reason + "/pending/1"
				}
				if got := v.cancellationRows(a.ID); len(got) != 1 || got[0] != want {
					t.Fatalf("cancellations = %v, want %s", got, want)
				}
				v.tickReconciliation(3, time.Second)
				if stopped {
					results := v.its.Run(1).results
					if v.latest(1).Open() || len(v.pendingCancellations()) != 0 || len(results) != 1 ||
						!strings.Contains(results[0].Error, "delivery stopped: operator stop") {
						t.Fatalf("oversized quiet cancellation lost the stop: attempt=%+v results=%+v", v.latest(1), results)
					}
				} else if got := v.latest(1); !got.Open() || !strings.Contains(got.Error, "32 MiB") || v.kg.state(a.TaskID) != kagent.StateWorking {
					t.Fatalf("unknown task activity was canceled or lost its hold: %+v", got)
				}
			})
		}
	}
}

func TestOversizedQuiescentTaskWithdrawsUnsentLease(t *testing.T) {
	for _, state := range []string{kagent.StateCompleted, kagent.StateInputRequired, kagent.StateAuthRequired} {
		t.Run(state, func(t *testing.T) {
			v, _, a := oversizedTask(t)
			if err := v.st.RequestQuietCancel(v.ctx, v.st.DB, a.ID, a.RunID, store.CancelLeaseExpired, a.EventCount); err != nil {
				t.Fatal(err)
			}
			v.kg.finish(a.TaskID, state, "waiting or finished", nil)
			v.tick(v.e)
			if got := v.cancellationRows(a.ID); len(got) != 1 || got[0] != "lease_expired/withdrawn/0" {
				t.Fatalf("cancellations = %v, want unsent lease withdrawn", got)
			}
			class := "bounded_output"
			if state == kagent.StateAuthRequired {
				class = dispatch.ClassCredential
			}
			if got := v.latest(1); got.Open() || got.ErrorClass != class || v.work(101).EngineeringRetries != 0 {
				t.Fatalf("lease lost the oversized task's outcome: %+v", got)
			}
			if active, err := v.st.CountActiveRuns(v.ctx, "coder"); err != nil || active != 0 {
				t.Fatalf("quiescent task holds a slot: active=%d err=%v", active, err)
			}
		})
	}
}

func TestOversizedStoppedTaskUsesLivenessEscalation(t *testing.T) {
	v, _, a := oversizedTask(t)
	v.tick(v.e)
	v.kg.hung = true
	if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator stop", "operator:test"); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	if cs := v.pendingCancellations(); len(cs) != 1 || cs[0].Requests != 1 {
		t.Fatalf("missing initial cancellation: %+v", cs)
	}
	v.kg.forceErr = errors.New("session deletion unavailable")
	v.clk.Advance(11 * time.Minute)
	v.tick(v.e)
	if n, _, _, confirmed, ok := v.forced(a.ContextID); !ok || n != 1 || confirmed || !v.latest(1).Open() {
		t.Fatalf("refused deletion lost ownership: requests=%d confirmed=%t attempt=%+v", n, confirmed, v.latest(1))
	}
	v.kg.forceErr = nil
	v.clk.Advance(time.Minute)
	v.tick(v.e)
	if _, _, _, confirmed, _ := v.forced(a.ContextID); !confirmed || len(v.pendingCancellations()) != 0 || v.latest(1).Open() {
		t.Fatalf("confirmed deletion did not finish cancellation: confirmed=%t attempt=%+v", confirmed, v.latest(1))
	}
	results := v.its.Run(1).results
	if len(results) != 1 || !strings.Contains(results[0].Error, "delivery stopped: operator stop") || v.work(101).EngineeringRetries != 0 {
		t.Fatalf("escalation lost the stop: results=%+v", results)
	}
}

func TestOversizedTerminalResponseCannotOverwriteStop(t *testing.T) {
	v, wire, a := oversizedTask(t)
	v.kg.finish(a.TaskID, kagent.StateCompleted, "finished", nil)
	// Hold a stop uncommitted while progress reads terminal metadata. Its
	// cancellation is not visible yet, so AttemptTx must wait on the stop's
	// row lock and reject the stale outcome after the stop commits.
	started := make(chan func(), 1)
	wire.onMetadata = func(string, string) {
		wire.onMetadata = nil
		started <- v.stopInFlight(v.work(101).ID, "operator stop")
	}
	ticked := make(chan error, 1)
	go func() { ticked <- v.e.Tick(v.ctx) }()
	var release func()
	select {
	case release = <-started:
	case err := <-ticked:
		t.Fatalf("tick ended before the stop started: %v", err)
	}
	v.waitForLockWaits(2)
	release()
	if err := <-ticked; err != nil && !errors.Is(err, store.ErrAttemptChanged) {
		t.Fatal(err)
	}
	v.tickReconciliation(3, time.Second)
	got, results := v.latest(1), v.its.Run(1).results
	if got.ErrorClass == "bounded_output" || len(results) != 1 || !strings.Contains(results[0].Error, "delivery stopped: operator stop") {
		t.Fatalf("terminal output overwrote the stop: attempt=%+v results=%+v", got, results)
	}
}

func TestOversizedTerminalOutputReportsBoundedFailureAndReleasesWorkItem(t *testing.T) {
	for _, initiallyTerminal := range []bool{false, true} {
		t.Run(fmt.Sprintf("initially-terminal=%t", initiallyTerminal), func(t *testing.T) {
			v, wire, a := oversizedTask(t)
			if initiallyTerminal {
				v.kg.finish(a.TaskID, kagent.StateCompleted, "finished", nil)
			}
			v.tick(v.e)
			if !initiallyTerminal {
				if !v.latest(1).Open() || len(v.its.Run(1).results) != 0 {
					t.Fatal("live execution finished because its output exceeded the budget")
				}
				v.restartWire(t, wire)
				wire.mu.Lock()
				reads := wire.transcriptReads
				wire.mu.Unlock()
				v.tick(v.e)
				v.kg.finish(a.TaskID, kagent.StateCompleted, "finished", nil)
				v.tick(v.e)
				wire.mu.Lock()
				gotReads := wire.transcriptReads
				wire.mu.Unlock()
				if gotReads != reads {
					t.Fatal("transcript fetched again after reaching the budget")
				}
			}
			got := v.latest(1)
			results := v.its.Run(1).results
			if got.State != store.AttemptFailed || got.ErrorClass != "bounded_output" || len(results) != 1 || results[0].Status != "failed" || !strings.Contains(results[0].Error, "32 MiB") {
				t.Fatalf("missing bounded-output result: attempt=%+v results=%+v", got, results)
			}
			var notices int
			if err := v.st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE dedupe_key LIKE 'comment/output-limit/%' AND delivered_at IS NOT NULL`).Scan(&notices); err != nil {
				t.Fatal(err)
			}
			if notices != 1 || v.kg.Executions() != 1 || v.work(101).EngineeringRetries != 0 {
				t.Fatalf("notices=%d executions=%d retries=%d", notices, v.kg.Executions(), v.work(101).EngineeringRetries)
			}
			if open, err := v.st.OpenAttemptForWork(v.ctx, v.work(101).ID); err != nil || open != nil {
				t.Fatalf("terminal output holds the work item: open=%+v err=%v", open, err)
			}
			// A later run on the same work item can now use the existing session.
			v.its.add(2, "coder", 101, "HL-1", "Continue with bounded output.")
			v.tick(v.e)
			if v.kg.Executions() != 2 || v.latest(2).ContextID != a.ContextID {
				t.Fatal("bounded output wedged the work item's session")
			}
		})
	}
}

func TestCancellationMetadataUncertaintyRetainsOwnership(t *testing.T) {
	for _, condition := range []string{"unavailable", "missing", "changed caller"} {
		t.Run(condition, func(t *testing.T) {
			v, wire := newReconciliationWire(t, 2)
			v.dispatchReconciliationInitial()
			a := v.latest(1)
			switch condition {
			case "unavailable":
				wire.metadataUnavailable = true
			case "missing":
				v.kg.mu.Lock()
				delete(v.kg.tasks, a.TaskID)
				v.kg.mu.Unlock()
			case "changed caller":
				v.kg.identity.ServiceAccountUID = "different-uid"
			}
			if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
				t.Fatal(err)
			}
			v.tickReconciliation(3, time.Second)
			cs := v.pendingCancellations()
			active, err := v.st.CountActiveRuns(v.ctx, "coder")
			if err != nil {
				t.Fatal(err)
			}
			if len(cs) != 1 || cs[0].Requests != 0 || !v.latest(1).Open() || active != 1 {
				t.Fatalf("uncertain metadata allowed cancellation or released ownership: cancellations=%+v attempt=%+v active=%d", cs, v.latest(1), active)
			}
		})
	}
}

func TestOversizedAcceptedTaskIgnoresStalledListing(t *testing.T) {
	for _, output := range []string{"artifacts", "status"} {
		t.Run(output, func(t *testing.T) {
			v, wire, a := oversizedTask(t)
			if output == "status" {
				v.kg.mu.Lock()
				task := &v.kg.tasks[a.TaskID].task
				task.Artifacts = nil
				task.Status.Message = &kagent.Message{Parts: []kagent.Part{{Text: strings.Repeat("x", (32<<20)+1)}}}
				v.kg.mu.Unlock()
			}
			wire.mu.Lock()
			wire.stallListings = true
			wire.mu.Unlock()
			// Each tick must finish without the stalled listing. Give each
			// independent metadata stream its own deadline.
			tick := func() {
				t.Helper()
				ctx, cancel := context.WithTimeout(v.ctx, 10*time.Second)
				defer cancel()
				if err := v.e.Tick(ctx); err != nil {
					t.Fatal(err)
				}
			}
			tick()
			if got := v.latest(1); !got.Open() || !strings.Contains(got.Error, "32 MiB") {
				t.Fatalf("stalled listing blocked bounded-output reporting: %+v", got)
			}
			if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
				t.Fatal(err)
			}
			tick()
			if cs := v.pendingCancellations(); len(cs) != 1 || cs[0].Requests != 1 || !v.latest(1).Open() {
				t.Fatalf("stalled listing blocked cancellation or released ownership: %+v", cs)
			}
			// Native cleanup must still retain the slot, including after restart.
			wire.mu.Lock()
			wire.pendingCleanup, wire.cleanupTaskID = true, a.TaskID
			wire.mu.Unlock()
			v.restartWire(t, wire)
			tick()
			if len(v.pendingCancellations()) != 1 || !v.latest(1).Open() {
				t.Fatal("native cleanup released the execution slot")
			}
			wire.mu.Lock()
			wire.pendingCleanup = false
			wire.mu.Unlock()
			tick()
			if len(v.pendingCancellations()) != 0 || v.latest(1).State != store.AttemptCanceled {
				t.Fatal("stalled listing blocked terminal confirmation")
			}
			if active, err := v.st.CountActiveRuns(v.ctx, "coder"); err != nil || active != 0 {
				t.Fatalf("terminal oversized task holds a slot: active=%d err=%v", active, err)
			}
		})
	}
}

func TestOversizedStatusMessagesRemainCancellable(t *testing.T) {
	for _, oversized := range []string{"earlier task", "accepted task", "page total"} {
		t.Run(oversized, func(t *testing.T) {
			v, _ := newReconciliationWire(t, 2)
			v.dispatchReconciliationInitial()
			a := v.latest(1)
			v.kg.mu.Lock()
			target := v.kg.tasks[a.TaskID]
			older := &fakeTask{agent: target.agent, task: kagent.Task{
				ID: "older", ContextID: a.ContextID, Status: kagent.TaskStatus{State: kagent.StateCompleted},
			}}
			size := (32 << 20) + 1
			if oversized == "page total" {
				size = 17 << 20 // Each task fits separately; their listing exceeds the budget.
			}
			message := &kagent.Message{Parts: []kagent.Part{{Text: strings.Repeat("x", size)}}}
			if oversized != "accepted task" {
				older.task.Status.Message = message
			}
			if oversized != "earlier task" {
				target.task.Status.Message = message
			}
			v.kg.tasks[older.task.ID] = older
			v.kg.order = []string{older.task.ID, a.TaskID}
			v.kg.mu.Unlock()
			if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
				t.Fatal(err)
			}
			v.tick(v.e)
			cs := v.pendingCancellations()
			if len(cs) != 1 || cs[0].Requests != 1 || !v.latest(1).Open() {
				t.Fatalf("oversized listing blocked cancellation or released ownership: %+v", cs)
			}
			v.kg.mu.Lock()
			canceledTarget, canceledOther := target.cancelRequested, older.cancelRequested
			v.kg.mu.Unlock()
			if !canceledTarget || canceledOther {
				t.Fatal("cancellation did not target the accepted task")
			}
			v.tick(v.e)
			active, err := v.st.CountActiveRuns(v.ctx, "coder")
			if err != nil {
				t.Fatal(err)
			}
			if len(v.pendingCancellations()) != 0 || v.latest(1).State != store.AttemptCanceled || active != 0 {
				t.Fatal("oversized status messages blocked terminal confirmation")
			}
		})
	}
}

func TestStalledUnrelatedStatusDoesNotBlockAcceptedTask(t *testing.T) {
	for _, cancelTask := range []bool{false, true} {
		t.Run(fmt.Sprintf("cancel=%t", cancelTask), func(t *testing.T) {
			v, wire := newReconciliationWire(t, 2)
			v.dispatchReconciliationInitial()
			a := v.latest(1)
			wire.mu.Lock()
			wire.stallListings = true
			wire.mu.Unlock()
			if cancelTask {
				if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
					t.Fatal(err)
				}
			} else {
				v.kg.finish(a.TaskID, kagent.StateCompleted, "finished", nil)
			}
			// The listing would consume this deadline. Both progress and
			// cancellation must use the exact task without starting that read.
			ctx, cancel := context.WithTimeout(v.ctx, 2*time.Second)
			defer cancel()
			if err := v.e.Tick(ctx); err != nil {
				t.Fatal(err)
			}
			if cancelTask {
				if cs := v.pendingCancellations(); len(cs) != 1 || cs[0].Requests != 1 || !v.latest(1).Open() {
					t.Fatalf("cancel request blocked or prematurely confirmed: %+v", cs)
				}
				if err := v.e.Tick(ctx); err != nil {
					t.Fatal(err)
				}
				if len(v.pendingCancellations()) != 0 || v.latest(1).State != store.AttemptCanceled {
					t.Fatal("unrelated status blocked terminal confirmation")
				}
			} else if results := v.its.Run(1).results; len(results) != 1 || results[0].Output != "finished" {
				t.Fatalf("unrelated status blocked progress: %+v", results)
			}
			if active, err := v.st.CountActiveRuns(v.ctx, "coder"); err != nil || active != 0 {
				t.Fatalf("quiescent task still holds a slot: active=%d err=%v", active, err)
			}
		})
	}
}

func TestDirectMetadataMustMatchAcceptedTask(t *testing.T) {
	for _, result := range []string{
		`null`,
		`{"id":"unrelated","contextId":"<context>","status":{"state":"TASK_STATE_COMPLETED"}}`,
		`{"id":"<task>","contextId":"other","status":{"state":"TASK_STATE_COMPLETED"}}`,
		`{"id":"<task>","contextId":"<context>","status":{}}`,
	} {
		t.Run(result, func(t *testing.T) {
			v, wire := newReconciliationWire(t, 2)
			v.dispatchReconciliationInitial()
			a := v.latest(1)
			result = strings.NewReplacer("<task>", a.TaskID, "<context>", a.ContextID).Replace(result)
			wire.mu.Lock()
			wire.metadataResult = []byte(result)
			wire.mu.Unlock()
			if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
				t.Fatal(err)
			}
			v.tickReconciliation(2, time.Second)
			if cs := v.pendingCancellations(); len(cs) != 1 || cs[0].Requests != 0 || !v.latest(1).Open() {
				t.Fatalf("invalid direct metadata changed ownership: %+v", cs)
			}
		})
	}
}

func TestOversizedWaitingOutputReleasesExecutionSlotAfterCleanup(t *testing.T) {
	for _, state := range []string{kagent.StateInputRequired, kagent.StateAuthRequired} {
		t.Run(state, func(t *testing.T) {
			v, wire, a := oversizedTask(t)
			wire.mu.Lock()
			wire.pendingCleanup, wire.cleanupTaskID = true, a.TaskID
			wire.mu.Unlock()
			v.kg.finish(a.TaskID, state, "waiting", nil)
			v.tick(v.e)
			if !v.latest(1).Open() {
				t.Fatal("unpublished waiting state released execution ownership")
			}
			wire.mu.Lock()
			wire.pendingCleanup = false
			wire.mu.Unlock()
			v.tick(v.e)
			if open, err := v.st.OpenAttemptForWork(v.ctx, v.work(101).ID); err != nil || open != nil {
				t.Fatalf("quiescent oversized task holds the work item: open=%+v err=%v", open, err)
			}
			if active, err := v.st.CountActiveRuns(v.ctx, "coder"); err != nil || active != 0 {
				t.Fatalf("quiescent oversized task holds a slot: active=%d err=%v", active, err)
			}
			if v.kg.Executions() != 1 || v.work(101).EngineeringRetries != 0 {
				t.Fatal("waiting oversized task consumed an engineering retry")
			}
			if state == kagent.StateAuthRequired {
				if v.latest(1).ErrorClass != dispatch.ClassCredential || v.run(1).State != store.RunWaiting || v.work(101).State != store.WorkBlocked {
					t.Fatal("oversized authorization failure lost the credential block")
				}
			} else if got := v.latest(1); got.State != store.AttemptFailed || got.ErrorClass != "bounded_output" || len(v.its.Run(1).results) != 1 {
				t.Fatalf("waiting oversized output did not report bounded failure: %+v", got)
			}
		})
	}
}

func TestOversizedFailedStatusPreservesGrantBlock(t *testing.T) {
	for _, tc := range []struct{ status, class string }{
		{"HTTP 401", dispatch.ClassCredential}, {"HTTP 429", dispatch.ClassQuota},
	} {
		for _, initiallyFailed := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/initially-failed=%t", tc.class, initiallyFailed), func(t *testing.T) {
				v, wire := newReconciliationWire(t, 2)
				v.dispatchReconciliationInitial()
				a := v.latest(1)
				text := strings.Repeat("x", (32<<20)+1) + " " + tc.status
				state := kagent.StateWorking
				if initiallyFailed {
					state = kagent.StateFailed
				}
				v.kg.finish(a.TaskID, state, text, nil)
				v.tick(v.e)
				if !initiallyFailed {
					if !v.latest(1).Open() || v.work(101).State == store.WorkBlocked {
						t.Fatal("working task released ownership or blocked before quiescence")
					}
					v.restartWire(t, wire)
					v.kg.finish(a.TaskID, kagent.StateFailed, text, nil)
					v.tick(v.e)
				}
				got := v.latest(1)
				if got.State != store.AttemptFailed || got.ErrorClass != tc.class || v.work(101).State != store.WorkBlocked || v.run(1).State != store.RunWaiting {
					t.Fatalf("oversized grant failure did not block: attempt=%+v work=%+v run=%+v", got, v.work(101), v.run(1))
				}
				if active, err := v.st.CountActiveRuns(v.ctx, "coder"); err != nil || active != 0 {
					t.Fatalf("failed task holds its slot: active=%d error=%v", active, err)
				}
				if len(got.Error) > 512 || v.work(101).EngineeringRetries != 0 || v.kg.Executions() != 1 {
					t.Fatal("oversized grant failure retained output or spent an engineering retry")
				}
				v.its.add(2, "coder", 102, "HL-2", "Use the same grant.")
				v.tick(v.e)
				if v.kg.Executions() != 1 || v.run(2).State != store.RunWaiting {
					t.Fatal("another work item used the blocked grant")
				}
			})
		}
	}
}
