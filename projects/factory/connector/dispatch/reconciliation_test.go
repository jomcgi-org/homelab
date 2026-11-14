package dispatch_test

import (
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestAcceptedInitialSendWithEveryResponseLostNeverStartsSecondWriter(t *testing.T) {
	v := newEnv(t)
	v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	for i := 0; i < 30; i++ {
		v.clk.Advance(v.cfg.DispatchWindow)
		v.tick(v.e)
	}
	if got := v.kg.Executions(); got != 1 {
		t.Fatalf("executions = %d; accepted initial send is still running, want one writer", got)
	}
	if retries := v.work(101).EngineeringRetries; retries != 0 {
		t.Fatalf("engineering retries = %d, want no retry of accepted execution", retries)
	}
	// Keep the first attempt attached instead of replacing its identity.
	if a := v.latest(1); a.MessageID != "itsaplan-run/1/initial" || a.TaskID == "" {
		t.Fatalf("attempt = %+v, want original accepted execution", a)
	}
}

func (v *env) exhaustOriginalResponses() {
	v.t.Helper()
	for v.latest(1).Issues < 23 {
		v.clk.Advance(v.cfg.DispatchWindow)
		v.tick(v.e)
	}
}

func TestOriginalMessageReconciliationAdoptsEveryTaskState(t *testing.T) {
	for _, state := range []string{kagent.StateSubmitted, kagent.StateWorking, kagent.StateCompleted,
		kagent.StateFailed, kagent.StateCanceled, kagent.StateRejected, kagent.StateInputRequired, kagent.StateAuthRequired} {
		t.Run(state, func(t *testing.T) {
			v := newEnv(t)
			v.cfg.Bindings[0].MaxEngineeringRetries = 0
			v.e.Release()
			v.e = v.engine("reconcile-every-state")
			v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			v.exhaustOriginalResponses()
			taskID := v.kg.lastTask()
			v.kg.finish(taskID, state, "Original outcome", nil)
			v.clk.Advance(v.cfg.DispatchWindow)
			v.tick(v.e)
			if v.kg.Executions() != 1 || v.latest(1).TaskID != taskID {
				t.Fatalf("executions=%d attempt=%+v; original task was not adopted", v.kg.Executions(), v.latest(1))
			}
			if v.work(101).EngineeringRetries != 0 || len(v.kg.listedStates) == 0 || v.kg.listedStates[0] != "" {
				t.Fatalf("retry or filtered lookup: work=%+v states=%v", v.work(101), v.kg.listedStates)
			}
			if state == kagent.StateCompleted {
				if results := v.its.Run(1).results; len(results) != 1 || results[0].Output != "Original outcome" {
					t.Fatalf("results = %+v; want original terminal outcome", results)
				}
			}
		})
	}
}

func TestOriginalMessageLookupReadsPastTenPages(t *testing.T) {
	v := newEnv(t)
	for i := 0; i < 12; i++ {
		_, err := v.kg.Send(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"},
			kagent.Message{MessageID: fmt.Sprintf("unrelated-%d", i)})
		if err != nil {
			t.Fatal(err)
		}
	}
	v.kg.listPageSize = 1
	v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	original := v.kg.lastTask()
	v.exhaustOriginalResponses()
	v.clk.Advance(v.cfg.DispatchWindow)
	v.tick(v.e)
	if v.kg.Executions() != 13 || v.latest(1).TaskID != original || len(v.kg.listedStates) != 13 {
		t.Fatalf("executions=%d task=%s pages=%d; want original on page 13", v.kg.Executions(), v.latest(1).TaskID, len(v.kg.listedStates))
	}
}

func TestDeletedPageBoundaryCannotProveOriginalAbsent(t *testing.T) {
	v := newEnv(t)
	_, err := v.kg.Send(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, kagent.Message{MessageID: "cursor-task"})
	if err != nil {
		t.Fatal(err)
	}
	v.kg.listPageSize, v.kg.vanishCursor = 1, true
	v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	v.exhaustOriginalResponses()
	v.clk.Advance(v.cfg.DispatchWindow)
	v.tick(v.e)
	if v.kg.Executions() != 2 || v.work(101).EngineeringRetries != 0 || v.latest(1).State != store.AttemptAmbiguous {
		t.Fatal("deleted cursor caused false absence and retry")
	}
	// A fresh scan starts before the vanished boundary and finds the original.
	v.kg.vanishCursor = false
	v.tick(v.e)
	if v.latest(1).TaskID == "" || v.kg.Executions() != 2 {
		t.Fatal("fresh complete scan did not reconcile the original")
	}
}

func TestIncompleteOriginalMessageLookupKeepsUncertainty(t *testing.T) {
	for _, failure := range []string{"list", "not-found", "history", "cursor"} {
		t.Run(failure, func(t *testing.T) {
			v := newEnv(t)
			// The first page has no match, so a repeated cursor cannot be
			// mistaken for the original task or complete absence.
			_, err := v.kg.Send(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, kagent.Message{MessageID: "unrelated"})
			if err != nil {
				t.Fatal(err)
			}
			v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			v.exhaustOriginalResponses()
			switch failure {
			case "list":
				v.kg.listErr = errors.New("HTTP 503")
			case "not-found":
				v.kg.listErr = kagent.ErrTaskNotFound
			case "history":
				v.kg.taskReadErr = errors.New("HTTP 503")
			case "cursor":
				v.kg.listPageSize, v.kg.repeatPageToken = 1, true
			}
			for i := 0; i < 3; i++ {
				v.clk.Advance(v.cfg.DispatchWindow)
				v.tick(v.e)
			}
			if v.kg.Executions() != 2 || v.work(101).EngineeringRetries != 0 || v.latest(1).State != store.AttemptAmbiguous {
				t.Fatalf("uncertainty retried: executions=%d work=%+v attempt=%+v", v.kg.Executions(), v.work(101), v.latest(1))
			}
			v.kg.listErr, v.kg.taskReadErr, v.kg.repeatPageToken = nil, nil, false
			v.tick(v.e)
			if v.latest(1).TaskID == "" || v.kg.Executions() != 2 {
				t.Fatal("recovered lookup did not adopt original")
			}
		})
	}
}

func TestCallerChangeHoldsOriginalSendUntilIdentityRestored(t *testing.T) {
	for _, field := range []string{"uid", "issuer", "authority", "endpoint", "grpc", "unreadable"} {
		t.Run(field, func(t *testing.T) {
			v := newEnv(t)
			v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			original := v.kg.identity
			switch field {
			case "uid":
				v.kg.identity.ServiceAccountUID = "recreated-uid"
			case "issuer":
				v.kg.identity.Issuer = "https://other.example"
			case "authority":
				v.kg.identity.Authority = "other"
			case "endpoint":
				v.kg.identity.A2AEndpoint = "https://other.example"
			case "grpc":
				v.kg.identity.GRPCTarget = "other.example:80"
			case "unreadable":
				v.kg.identityErr = errors.New("cannot read token")
			}
			v.e.Release()
			v.e = v.engine("restarted-different-caller")
			for i := 0; i < 30; i++ {
				v.clk.Advance(v.cfg.DispatchWindow)
				v.tick(v.e)
			}
			if len(v.kg.Sends()) != 1 || v.kg.Executions() != 1 || v.work(101).EngineeringRetries != 0 || v.latest(1).State != store.AttemptAmbiguous {
				t.Fatalf("caller change escaped hold: sends=%d work=%+v attempt=%+v", len(v.kg.Sends()), v.work(101), v.latest(1))
			}
			v.kg.identity, v.kg.identityErr = original, nil
			v.exhaustOriginalResponses()
			v.e.Release()
			v.e = v.engine("restarted-original-caller")
			v.clk.Advance(v.cfg.DispatchWindow)
			v.tick(v.e)
			if v.kg.Executions() != 1 || v.latest(1).TaskID == "" {
				t.Fatal("restored caller did not reconcile original")
			}
		})
	}
}

func TestLegacyCallerRequiresPositiveSessionOwnership(t *testing.T) {
	for _, evidence := range []string{"accepted", "ambiguous", "wrong-owner", "missing", "unsent-retry", "known-unsent-retry"} {
		t.Run(evidence, func(t *testing.T) {
			v := newEnv(t)
			if evidence != "accepted" && evidence != "known-unsent-retry" {
				v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
			}
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			if _, err := v.st.DB.Exec(`UPDATE work_item SET caller_identity = NULL`); err != nil {
				t.Fatal(err)
			}
			session := v.kg.tasks[v.kg.lastTask()].task.ContextID
			switch evidence {
			case "wrong-owner":
				v.kg.sessions[session].Creator = "another-principal"
			case "missing":
				v.kg.expire(session)
			case "unsent-retry", "known-unsent-retry":
				// An existing pre-migration send is still alive. A newly
				// recorded retry cannot claim this is never-sent work.
				if _, err := v.st.DB.Exec(`UPDATE delivery_attempt SET state = 'failed'`); err != nil {
					t.Fatal(err)
				}
				_, err := v.st.RecordAttempt(v.ctx, v.st.DB, store.Attempt{RunID: 1, RunAttempt: 1, WorkItemEpoch: 1, DecisionID: "replacement-0-1", Kind: "initial", MessageID: "itsaplan-run/1/replacement-0-1", KagentNamespace: "factory-homelab", KagentAgent: "coder", WriterEpoch: v.e.Epoch()})
				if err != nil {
					t.Fatal(err)
				}
			}
			before := len(v.kg.Sends())
			v.clk.Advance(v.cfg.DispatchWindow)
			v.tick(v.e)
			positive := evidence == "accepted" || evidence == "ambiguous"
			if positive {
				if v.work(101).CallerIdentity != v.kg.identity.Key() {
					t.Fatal("readable matching owner was not adopted")
				}
			} else if v.work(101).CallerIdentity != "" || len(v.kg.Sends()) != before || v.work(101).EngineeringRetries != 0 {
				t.Fatalf("unproven legacy work rebound or retried: %+v", v.work(101))
			}
		})
	}
}

func TestLegacyAdoptionChecksOlderUncertainSendsBeforeNewRetry(t *testing.T) {
	v := newEnv(t)
	v.kg.loseResponse = true
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	original := v.kg.lastTask()
	wi := v.work(101)
	if _, err := v.st.DB.Exec(`UPDATE delivery_attempt SET state = 'failed'; UPDATE work_item SET caller_identity = NULL`); err != nil {
		t.Fatal(err)
	}
	// Reproduce pre-migration history: A's response was lost and A still
	// runs, while a later replacement B has already completed.
	older := store.Attempt{RunID: 1, RunAttempt: 1, WorkItemEpoch: wi.Epoch, Kind: "initial",
		DecisionID: "replacement-0-1", MessageID: "itsaplan-run/1/replacement-0-1",
		KagentNamespace: "factory-homelab", KagentAgent: "coder", WriterEpoch: v.e.Epoch()}
	b, err := v.st.RecordAttempt(v.ctx, v.st.DB, older)
	if err != nil {
		t.Fatal(err)
	}
	if err := v.st.MarkIssued(v.ctx, b, v.clk.Now()); err != nil {
		t.Fatal(err)
	}
	task, err := v.kg.Send(v.ctx, kagent.AgentRef{Namespace: b.KagentNamespace, Name: b.KagentAgent}, kagent.Message{MessageID: b.MessageID})
	if err != nil {
		t.Fatal(err)
	}
	v.kg.finish(task.ID, kagent.StateCompleted, "B completed", nil)
	if err := v.st.MarkAccepted(v.ctx, v.st.DB, b.ID, task.ContextID, task.ID); err != nil {
		t.Fatal(err)
	}
	if err := v.st.SetSession(v.ctx, v.st.DB, wi.ID, task.ContextID); err != nil {
		t.Fatal(err)
	}
	if err := v.st.FinishAttempt(v.ctx, v.st.DB, b.ID, store.AttemptCompleted, "", ""); err != nil {
		t.Fatal(err)
	}
	older.DecisionID, older.MessageID = "replacement-1-2", "itsaplan-run/1/replacement-1-2"
	if _, err := v.st.RecordAttempt(v.ctx, v.st.DB, older); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	if v.kg.Executions() != 2 || v.work(101).CallerIdentity != "" || v.latest(1).State != store.AttemptSending {
		t.Fatal("newest completed send concealed an older running predecessor")
	}
	// Positive evidence for every uncertain predecessor permits adoption.
	v.kg.finish(original, kagent.StateCompleted, "A completed", nil)
	v.tick(v.e)
	if v.kg.Executions() != 3 || v.work(101).CallerIdentity != v.kg.identity.Key() {
		t.Fatal("legacy adoption did not resume after every old writer stopped")
	}
}

func TestLegacyMissingOriginalCannotEstablishCaller(t *testing.T) {
	v := newEnv(t)
	v.kg.dropFor = map[string]bool{"itsaplan-run/1/initial": true}
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	if _, err := v.st.DB.Exec(`UPDATE work_item SET caller_identity = NULL`); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 30; i++ {
		v.clk.Advance(v.cfg.DispatchWindow)
		v.tick(v.e)
	}
	if v.work(101).CallerIdentity != "" || v.work(101).EngineeringRetries != 0 || len(v.kg.Sends()) != 1 || v.latest(1).State != store.AttemptAmbiguous {
		t.Fatal("missing legacy message was treated as new unsent work")
	}
}

func TestLegacyAttemptCannotBorrowNewerSessionsOwnership(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	original := v.latest(1)
	v.kg.sessions[original.ContextID].Creator = "old-owner"
	newer, err := v.kg.Send(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, kagent.Message{MessageID: "newer-session"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := v.st.DB.Exec(`UPDATE work_item SET caller_identity = NULL, session_id = $1`, newer.ContextID); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	if v.work(101).CallerIdentity != "" || v.latest(1).ID != original.ID || v.work(101).EngineeringRetries != 0 {
		t.Fatal("old attempt adopted another session's readable ownership")
	}
}

func TestLaterSendRefusalCannotDiscardAnAcceptedMessage(t *testing.T) {
	for _, refusal := range []error{kagent.ErrTaskNotFound, kagent.ErrRejected} {
		t.Run(refusal.Error(), func(t *testing.T) {
			v := newEnv(t)
			v.kg.loseResponse = true
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			original := v.kg.lastTask()
			for i := 0; i < 22; i++ {
				v.kg.sendErr = []error{refusal}
				v.clk.Advance(v.cfg.DispatchWindow)
				v.tick(v.e)
				if v.latest(1).State != store.AttemptAmbiguous || v.run(1).State != store.RunDispatched {
					t.Fatalf("later refusal closed original: run=%+v attempt=%+v", v.run(1), v.latest(1))
				}
			}
			v.clk.Advance(v.cfg.DispatchWindow)
			v.tick(v.e)
			if v.latest(1).TaskID != original || v.kg.Executions() != 1 || v.work(101).EngineeringRetries != 0 {
				t.Fatal("later refusal did not preserve/reconcile original")
			}
		})
	}
}

func TestCallerWitnessCannotBeRebound(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	wi := v.work(101)
	if wi.CallerIdentity != v.kg.identity.Key() {
		t.Fatal("first send did not persist identity")
	}
	if ok, err := v.st.BindCaller(v.ctx, wi.ID, "different identity"); err != nil || ok {
		t.Fatalf("rebind = %v, %v", ok, err)
	}
	if ok, err := v.st.BindCaller(v.ctx, wi.ID, wi.CallerIdentity); err != nil || !ok {
		t.Fatalf("same witness = %v, %v", ok, err)
	}
	if v.work(101).CallerIdentity != wi.CallerIdentity {
		t.Fatal("witness changed")
	}
}

func TestDeletingSessionCannotStartReplacement(t *testing.T) {
	for _, phase := range []string{"working", "continuation"} {
		t.Run(phase, func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			if phase == "continuation" {
				v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "done", nil)
				v.clk.Advance(5 * time.Second)
				v.tick(v.e)
				v.its.add(2, "coder", 101, "HL-1", "Continue.")
			}
			v.kg.sessions[v.work(101).SessionID].State = kagent.RuntimeDeleting
			for i := 0; i < 5; i++ {
				v.clk.Advance(time.Minute)
				v.tick(v.e)
			}
			if len(v.kg.Sends()) != 1 || v.work(101).EngineeringRetries != 0 {
				t.Fatal("replacement started while actor deletion was in progress")
			}
		})
	}
}

func TestCallerChangeHoldsCancellationAndContinuation(t *testing.T) {
	for _, phase := range []string{"cancel", "continuation", "crashed"} {
		t.Run(phase, func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			switch phase {
			case "cancel":
				if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
					t.Fatal(err)
				}
			case "continuation":
				v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "done", nil)
				v.clk.Advance(5 * time.Second)
				v.tick(v.e)
				v.its.add(2, "coder", 101, "HL-1", "Continue.")
			case "crashed":
				v.kg.crash(v.work(101).SessionID)
			}
			v.kg.identity.ServiceAccountUID = "new-uid"
			for i := 0; i < 5; i++ {
				v.clk.Advance(time.Minute)
				v.tick(v.e)
			}
			if len(v.kg.Sends()) != 1 || v.kg.reverts != 0 || v.work(101).EngineeringRetries != 0 {
				t.Fatal("identity drift allowed external action or retry")
			}
			if phase == "cancel" && (len(v.pendingCancellations()) != 1 || v.pendingCancellations()[0].Requests != 0) {
				t.Fatal("identity drift allowed cancellation")
			}
		})
	}
}

func TestCompleteAbsenceWaitsForDispatchWindow(t *testing.T) {
	v := newEnv(t)
	v.kg.dropFor = map[string]bool{"itsaplan-run/1/initial": true}
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	v.exhaustOriginalResponses()
	v.clk.Advance(v.cfg.DispatchWindow - time.Nanosecond)
	v.tick(v.e)
	if v.work(101).EngineeringRetries != 0 || len(v.kg.listedStates) != 0 {
		t.Fatal("absence was used inside dispatch window")
	}
	v.clk.Advance(time.Nanosecond)
	v.tick(v.e)
	if v.work(101).EngineeringRetries != 1 || v.kg.Executions() != 1 || v.latest(1).DecisionID != "replacement-0-1" {
		t.Fatalf("complete absence did not take one bounded retry: %+v", v.latest(1))
	}
}
