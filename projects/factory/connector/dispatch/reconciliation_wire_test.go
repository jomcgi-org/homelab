package dispatch_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// reconciliationWire exercises the real JSON-RPC client against kagent's
// public view. A first terminal/waiting save retains a SUBMITTED projection
// and hides its input until native cleanup publishes both atomically:
// kagent v1.0.1 database/session_tasks.go:218-252, task_finalization.go:60-109.
type reconciliationWire struct {
	kg             *fakeKagent
	client         *kagent.A2A
	mu             sync.Mutex
	pendingCleanup bool
	cleanupTaskID  string
	listResult     json.RawMessage
	listPages      map[string]json.RawMessage
	messages       []map[string]json.RawMessage
}

func (w *reconciliationWire) public(task *kagent.Task) *kagent.Task {
	if task != nil && w.pendingCleanup && (w.cleanupTaskID == "" || w.cleanupTaskID == task.ID) {
		task.Status = kagent.TaskStatus{State: kagent.StateSubmitted}
		task.History = nil
	}
	return task
}

func (w *reconciliationWire) serveHTTP(out http.ResponseWriter, req *http.Request) {
	w.mu.Lock()
	defer w.mu.Unlock()
	var call struct {
		ID     string `json:"id"`
		Method string `json:"method"`
		Params struct {
			ID        string          `json:"id"`
			Message   json.RawMessage `json:"message"`
			ContextID string          `json:"contextId"`
			State     string          `json:"status"`
			PageToken string          `json:"pageToken"`
		} `json:"params"`
	}
	if err := json.NewDecoder(req.Body).Decode(&call); err != nil {
		http.Error(out, err.Error(), http.StatusBadRequest)
		return
	}
	ref := kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}
	var result any
	var err error
	switch call.Method {
	case "SendMessage":
		var task *kagent.Task
		var message kagent.Message
		var raw map[string]json.RawMessage
		if err = json.Unmarshal(call.Params.Message, &message); err == nil {
			err = json.Unmarshal(call.Params.Message, &raw)
		}
		if err == nil {
			w.messages = append(w.messages, raw)
			task, err = w.kg.Send(req.Context(), ref, message)
		}
		result = map[string]any{"task": w.public(task)}
	case "GetTask":
		var task *kagent.Task
		task, err = w.kg.GetTask(req.Context(), ref, call.Params.ID)
		result = w.public(task)
	case "ListTasks":
		if w.listPages != nil {
			result = w.listPages[call.Params.PageToken]
			break
		}
		if w.listResult != nil {
			result = w.listResult
			break
		}
		var page *kagent.TaskPage
		page, err = w.kg.ListTasks(req.Context(), ref, call.Params.ContextID, call.Params.State, call.Params.PageToken)
		if page != nil {
			if page.Tasks == nil {
				page.Tasks = []*kagent.Task{}
			}
			for _, task := range page.Tasks {
				w.public(task)
			}
		}
		result = page

	case "CancelTask":
		result, err = w.kg.CancelTask(req.Context(), ref, call.Params.ID)
	default:
		err = fmt.Errorf("unexpected method %s", call.Method)
	}
	if err != nil {
		http.Error(out, "response unavailable", http.StatusServiceUnavailable)
		return
	}
	out.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(out).Encode(map[string]any{"jsonrpc": "2.0", "id": call.ID, "result": result})
}

func newReconciliationWire(t *testing.T, retries int) (*env, *reconciliationWire) {
	t.Helper()
	v := newEnv(t)
	w := &reconciliationWire{kg: v.kg}
	server := httptest.NewServer(http.HandlerFunc(w.serveHTTP))
	t.Cleanup(server.Close)
	v.cfg.Bindings[0].MaxEngineeringRetries = retries
	w.client = &kagent.A2A{BaseURL: server.URL, HTTP: server.Client()}
	v.restartWire(t, w)
	return v, w
}

func (v *env) restartWire(t *testing.T, w *reconciliationWire) {
	t.Helper()
	v.e.Release()
	v.e = dispatch.New(v.cfg, v.st, v.its, w.client, v.kg, nil, v.m, nil)
	for deadline := time.Now().Add(5 * time.Second); ; {
		ok, err := v.e.Acquire(v.ctx)
		if err != nil {
			t.Fatal(err)
		}
		if ok {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("wire engine did not acquire writer lock")
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Cleanup(v.e.Release)
}

func TestPendingNativeCleanupCannotProveMessageAbsent(t *testing.T) {
	for _, state := range []string{
		kagent.StateCompleted, kagent.StateFailed, kagent.StateCanceled,
		kagent.StateRejected, kagent.StateInputRequired, kagent.StateAuthRequired,
	} {
		for _, cancel := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/cancel=%t", state, cancel), func(t *testing.T) {
				retries := 0
				if state == kagent.StateCompleted {
					retries = 1 // the old absence path starts a second execution
				}
				v, wire := newReconciliationWire(t, retries)
				wire.pendingCleanup = true
				v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
				v.dispatchReconciliationInitial()
				taskID := v.kg.lastTask()
				// The runtime has durably saved this outcome, but its native
				// cleanup still owns effects. Public state/history stay hidden.
				v.kg.finish(taskID, state, "Original saved outcome", nil)
				v.advanceDispatchWindow()
				if cancel {
					if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
						t.Fatal(err)
					}
				}
				v.tickReconciliation(3, v.cfg.DispatchWindow)
				if a := v.latest(1); a.State != store.AttemptAmbiguous || v.work(101).EngineeringRetries != 0 || v.kg.Executions() != 1 || len(v.kg.Sends()) != 1 {
					t.Fatalf("unpublished input became absence: attempt=%+v sends=%d executions=%d", a, len(v.kg.Sends()), v.kg.Executions())
				}
				if cancel && (len(v.pendingCancellations()) != 1 || v.pendingCancellations()[0].Requests != 0) {
					t.Fatal("pending native cleanup was confirmed not_dispatched or canceled without message identity")
				}
				wire.mu.Lock()
				wire.pendingCleanup = false // native cleanup publishes input and outcome together
				wire.mu.Unlock()
				v.tick(v.e)
				if v.latest(1).TaskID != taskID || v.kg.Executions() != 1 {
					t.Fatal("published original input was not reconciled")
				}
				if !cancel && state == kagent.StateCompleted {
					if results := v.its.Run(1).results; len(results) != 1 || results[0].Output != "Original saved outcome" {
						t.Fatalf("published outcome was lost: %+v", results)
					}
				}
				if cancel {
					v.tick(v.e)
					if len(v.pendingCancellations()) != 0 || v.run(1).State != store.RunClosed {
						t.Fatal("published task did not finish cancellation")
					}
				}
			})
		}
	}
}

func TestNullAndEmptyTaskListsKeepUnknownSendHeld(t *testing.T) {
	for _, accepted := range []bool{false, true} {
		for _, cancel := range []bool{false, true} {
			t.Run(fmt.Sprintf("accepted=%t/cancel=%t", accepted, cancel), func(t *testing.T) {
				v, wire := newReconciliationWire(t, 1)
				if accepted {
					wire.listResult = json.RawMessage("null")
				}
				v.checkListHold(wire, accepted, cancel)
			})
		}
	}
}

// checkListHold verifies the same safety contract for each distinct wire
// response, including operator cancellation and a later healthy listing.
func (v *env) checkListHold(wire *reconciliationWire, accepted, cancel bool) {
	v.t.Helper()
	executions := v.kg.Executions()
	if accepted {
		v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
		executions++
	} else {
		v.kg.dropFor = map[string]bool{"itsaplan-run/1/initial": true}
	}
	v.dispatchReconciliationInitial()
	original, sends := v.latest(1).ID, len(v.kg.Sends())
	taskID := ""
	if accepted {
		taskID = v.kg.lastTask()
	}
	if cancel {
		if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
			v.t.Fatal(err)
		}
	}
	assertHeld := func() {
		v.t.Helper()
		a := v.latest(1)
		if a.ID != original || a.State != store.AttemptAmbiguous || a.Error == "" || v.work(101).EngineeringRetries != 0 || v.kg.Executions() != executions || len(v.kg.Sends()) != sends {
			v.t.Fatalf("list response released unknown work: attempt=%+v executions=%d sends=%d", a, v.kg.Executions(), len(v.kg.Sends()))
		}
		if cancel && (len(v.pendingCancellations()) != 1 || v.pendingCancellations()[0].Requests != 0) {
			v.t.Fatal("unknown work cancellation was confirmed or sent")
		}
	}
	v.tickReconciliation(3, v.cfg.DispatchWindow)
	assertHeld()
	wire.mu.Lock()
	wire.listResult, wire.listPages = nil, nil
	wire.mu.Unlock()
	v.tick(v.e)
	if !accepted {
		assertHeld()
		return
	}
	if a := v.latest(1); a.TaskID != taskID || a.Error != "" || v.kg.Executions() != executions || len(v.kg.Sends()) != sends {
		v.t.Fatalf("healthy listing did not adopt original: %+v", a)
	}
}

func (v *env) wireTasks(n int) []*kagent.Task {
	v.t.Helper()
	var tasks []*kagent.Task
	for i := range n {
		task, err := v.kg.Send(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, kagent.Message{MessageID: fmt.Sprintf("unrelated-%d", i)})
		if err != nil {
			v.t.Fatal(err)
		}
		tasks = append(tasks, task)
	}
	return tasks
}

func TestContinuationOpensNewTaskWhileItsInputIsUnpublished(t *testing.T) {
	v, wire := newReconciliationWire(t, 1)
	v.its.add(1, "coder", 101, "HL-1", "First turn.")
	v.tick(v.e)
	first := v.latest(1)
	v.kg.finish(first.TaskID, kagent.StateCompleted, "First complete", nil)
	v.tickReconciliation(1, 5*time.Second)
	v.kg.loseFor = map[string]bool{"itsaplan-run/2/revision": true}
	v.its.add(2, "coder", 101, "HL-1", "Next turn.")
	v.tickReconciliation(1, 5*time.Second)
	secondTask := v.kg.lastTask()
	if secondTask == first.TaskID || v.kg.tasks[secondTask].task.ContextID != first.ContextID {
		t.Fatal("continuation did not open a distinct task in the original session")
	}
	wire.mu.Lock()
	if len(wire.messages) != 2 || wire.messages[1]["contextId"] == nil || wire.messages[1]["taskId"] != nil {
		t.Errorf("continuation must send contextId and omit taskId: %+v", wire.messages)
	}
	wire.pendingCleanup, wire.cleanupTaskID = true, secondTask
	wire.mu.Unlock()
	v.kg.finish(secondTask, kagent.StateCompleted, "Second saved outcome", nil)
	v.clk.Advance(v.cfg.DispatchWindow)
	v.tickReconciliation(1, v.cfg.DispatchWindow)
	if v.latest(2).State != store.AttemptAmbiguous || v.work(101).EngineeringRetries != 0 || v.kg.Executions() != 2 {
		t.Fatal("older published input concealed the continuation's unpublished task")
	}
	wire.mu.Lock()
	wire.pendingCleanup = false
	wire.mu.Unlock()
	v.tick(v.e)
	if v.latest(2).TaskID != secondTask || v.kg.Executions() != 2 {
		t.Fatal("published continuation input was not adopted")
	}
}

func TestInconsistentTaskListMetadataCannotProveAbsence(t *testing.T) {
	for _, kind := range []string{"empty positive total", "short first page", "truncated later page", "falling total", "duplicate rows"} {
		for _, cancel := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/cancel=%t", kind, cancel), func(t *testing.T) {
				v, wire := newReconciliationWire(t, 1)
				tasks := v.wireTasks(2)
				first, second := tasks[0], tasks[1]

				switch kind {
				case "empty positive total":
					wire.listResult = wirePage(t, []*kagent.Task{}, 1, 100, "")
				case "short first page":
					wire.listResult = wirePage(t, []*kagent.Task{first}, 2, 100, "")
				case "truncated later page":
					wire.listPages = map[string]json.RawMessage{"": wirePage(t, []*kagent.Task{first}, 3, 1, "next"), "next": wirePage(t, []*kagent.Task{second}, 3, 1, "")}
				case "falling total":
					wire.listPages = map[string]json.RawMessage{"": wirePage(t, []*kagent.Task{first}, 3, 1, "next"), "next": wirePage(t, []*kagent.Task{second}, 2, 1, "")}
				case "duplicate rows":
					wire.listPages = map[string]json.RawMessage{"": wirePage(t, []*kagent.Task{first}, 2, 1, "next"), "next": wirePage(t, []*kagent.Task{first}, 2, 1, "")}
				}
				v.checkListHold(wire, true, cancel)
			})
		}
	}
}

func wirePage(t *testing.T, tasks []*kagent.Task, total, size int, next string) json.RawMessage {
	t.Helper()
	b, err := json.Marshal(kagent.TaskPage{Tasks: tasks, TotalSize: total, PageSize: size, NextPageToken: next})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestTaskListCountsCannotReleaseUnknownSend(t *testing.T) {
	for _, kind := range []string{"count before insert", "count before delete", "changed between pages", "changed and restored", "stable pages", "empty final page"} {
		t.Run(kind, func(t *testing.T) {
			v, wire := newReconciliationWire(t, 1)
			n := 2
			if kind == "changed and restored" {
				n = 3
			}
			tasks := v.wireTasks(n)
			switch kind {
			case "count before insert":
				wire.listResult = wirePage(t, tasks[:2], 1, 100, "")
			case "count before delete":
				wire.listResult = wirePage(t, tasks[:1], 2, 100, "")
			case "changed between pages":
				wire.listPages = map[string]json.RawMessage{
					"": wirePage(t, tasks[:1], 1, 1, "second"), "second": wirePage(t, tasks[1:2], 2, 1, ""),
				}
			case "changed and restored":
				wire.listPages = map[string]json.RawMessage{
					"": wirePage(t, tasks[:1], 3, 1, "second"), "second": wirePage(t, tasks[1:2], 2, 1, "third"), "third": wirePage(t, tasks[2:], 3, 1, ""),
				}
			case "stable pages", "empty final page":
				wire.listPages = map[string]json.RawMessage{
					"": wirePage(t, tasks[:1], 2, 1, "second"), "second": wirePage(t, tasks[1:], 2, 1, ""),
				}
				if kind == "empty final page" {
					wire.listPages["second"] = wirePage(t, tasks[1:], 2, 1, "end")
					wire.listPages["end"] = wirePage(t, []*kagent.Task{}, 2, 1, "")
				}

			}
			v.checkListHold(wire, false, false)
		})
	}
}

func TestTaskListCountDriftStillAdoptsPositiveOriginal(t *testing.T) {
	for _, kind := range []string{"smaller total", "larger total", "changing total", "duplicate rows"} {
		t.Run(kind, func(t *testing.T) {
			v, wire := newReconciliationWire(t, 1)
			unrelated := v.wireTasks(1)[0]
			v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
			v.dispatchReconciliationInitial()
			v.advanceDispatchWindow()
			original, err := v.kg.GetTask(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, v.kg.lastTask())
			if err != nil {
				t.Fatal(err)
			}
			wire.mu.Lock()
			switch kind {
			case "smaller total":
				wire.listResult = wirePage(t, []*kagent.Task{original}, 0, 100, "")
			case "larger total":
				wire.listResult = wirePage(t, []*kagent.Task{original}, 2, 100, "")
			case "changing total":
				wire.listPages = map[string]json.RawMessage{
					"": wirePage(t, []*kagent.Task{unrelated}, 3, 1, "second"), "second": wirePage(t, []*kagent.Task{original}, 2, 1, ""),
				}
			case "duplicate rows":
				wire.listPages = map[string]json.RawMessage{
					"": wirePage(t, []*kagent.Task{unrelated}, 2, 1, "second"), "second": wirePage(t, []*kagent.Task{unrelated}, 2, 1, "third"), "third": wirePage(t, []*kagent.Task{original}, 2, 1, ""),
				}
			}
			wire.mu.Unlock()
			v.tickReconciliation(1, v.cfg.DispatchWindow)
			if v.latest(1).TaskID != original.ID || v.kg.Executions() != 2 || v.work(101).EngineeringRetries != 0 {
				t.Fatal("count race hid the positive original-message match")
			}
		})
	}
}

func TestInconsistentSessionTaskCountBlocksContinuation(t *testing.T) {
	v, wire := newReconciliationWire(t, 1)
	v.its.add(1, "coder", 101, "HL-1", "First turn.")
	v.tick(v.e)
	v.kg.finish(v.latest(1).TaskID, kagent.StateCompleted, "First complete", nil)
	v.tickReconciliation(1, 5*time.Second)
	wire.mu.Lock()
	wire.listResult = wirePage(t, []*kagent.Task{}, 1, 100, "")
	wire.mu.Unlock()
	v.its.add(2, "coder", 101, "HL-1", "Next turn.")
	v.tickReconciliation(1, 5*time.Second)
	if v.kg.Executions() != 1 {
		t.Fatal("incomplete task list opened another turn")
	}
	wire.mu.Lock()
	wire.listResult = nil
	wire.mu.Unlock()
	v.tick(v.e)
	if v.kg.Executions() != 2 {
		t.Fatal("valid idle-session listing did not resume continuation")
	}
}

func TestPrunedCreationReceiptNeverResendsUnknownExecution(t *testing.T) {
	for _, continuation := range []bool{false, true} {
		t.Run(fmt.Sprintf("continuation=%t", continuation), func(t *testing.T) {
			v, wire := newReconciliationWire(t, 1)
			runID := int64(1)
			messageID := "itsaplan-run/1/initial"
			if continuation {
				v.its.add(1, "coder", 101, "HL-1", "First turn.")
				v.tick(v.e)
				v.kg.finish(v.latest(1).TaskID, kagent.StateCompleted, "First complete", nil)
				v.tickReconciliation(1, 5*time.Second)
				runID, messageID = 2, "itsaplan-run/2/revision"
			}
			v.kg.loseFor = map[string]bool{messageID: true}
			v.its.add(runID, "coder", 101, "HL-1", "Uncertain turn.")
			v.tickReconciliation(1, 5*time.Second)
			original, executions, sends := v.latest(runID), v.kg.Executions(), len(v.kg.Sends())
			// Idle expiry can remove session/tasks AND the creation receipt.
			// Model that pruned view without independent proof that the old
			// execution stopped; its execution count remains. This does not
			// model explicit deletion's retained request tombstone.
			taskID := v.kg.lastTask()
			v.kg.mu.Lock()
			session := v.kg.tasks[taskID].task.ContextID
			for id, task := range v.kg.tasks {
				if task.task.ContextID == session {
					delete(v.kg.tasks, id)
				}
			}
			delete(v.kg.contexts, session)
			delete(v.kg.sessions, session)
			delete(v.kg.initial, messageID)
			v.kg.mu.Unlock()
			// Restart after arbitrary downtime: neither the receipt nor a task can
			// be assumed retained for the dispatch window measured from this tick.
			v.restartWire(t, wire)
			v.clk.Advance(24 * time.Hour)
			for range 3 {
				v.tick(v.e)
				v.clk.Advance(v.cfg.DispatchWindow)
			}
			a := v.latest(runID)
			active, err := v.st.CountActiveRuns(v.ctx, "coder")
			if err != nil {
				t.Fatal(err)
			}
			if a.ID != original.ID || a.State != store.AttemptAmbiguous || a.Error == "" || v.kg.Executions() != executions || len(v.kg.Sends()) != sends || v.work(101).EngineeringRetries != 0 || active != 1 {
				t.Fatalf("unknown execution escaped hold after receipt pruning: attempt=%+v executions=%d sends=%d active=%d", a, v.kg.Executions(), len(v.kg.Sends()), active)
			}
		})
	}
}
