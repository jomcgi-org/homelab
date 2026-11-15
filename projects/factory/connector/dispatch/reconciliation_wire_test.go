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
	mu             sync.Mutex
	pendingCleanup bool
	cleanupTaskID  string
	listResult     json.RawMessage
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
		if w.listResult != nil {
			result = w.listResult
			break
		}
		var tasks []*kagent.Task
		var next string
		tasks, next, err = w.kg.ListTasks(req.Context(), ref, call.Params.ContextID, call.Params.State, call.Params.PageToken)
		if tasks == nil {
			tasks = []*kagent.Task{}
		}
		for _, task := range tasks {
			w.public(task)
		}
		result = map[string]any{"tasks": tasks, "nextPageToken": next, "pageSize": 100, "totalSize": len(tasks)}
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
	v.e.Release()
	v.cfg.Bindings[0].MaxEngineeringRetries = retries
	v.e = dispatch.New(v.cfg, v.st, v.its, &kagent.A2A{BaseURL: server.URL, HTTP: server.Client()}, v.kg, nil, v.m, nil)
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
	return v, w
}

func TestPendingNativeCleanupCannotProveMessageAbsent(t *testing.T) {
	for _, state := range []string{kagent.StateCompleted, kagent.StateFailed, kagent.StateCanceled,
		kagent.StateRejected, kagent.StateInputRequired, kagent.StateAuthRequired} {
		for _, cancel := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/cancel=%t", state, cancel), func(t *testing.T) {
				retries := 0
				if state == kagent.StateCompleted {
					retries = 1 // the old absence path starts a second execution
				}
				v, wire := newReconciliationWire(t, retries)
				wire.pendingCleanup = true
				v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
				v.its.add(1, "coder", 101, "HL-1", "Do it.")
				v.tick(v.e)
				taskID := v.kg.lastTask()
				// The runtime has durably saved this outcome, but its native
				// cleanup still owns effects. Public state/history stay hidden.
				v.kg.finish(taskID, state, "Original saved outcome", nil)
				v.exhaustOriginalResponses()
				if cancel {
					if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
						t.Fatal(err)
					}
				}
				for range 3 {
					v.clk.Advance(v.cfg.DispatchWindow)
					v.tick(v.e)
				}
				if a := v.latest(1); a.State != store.AttemptAmbiguous || v.work(101).EngineeringRetries != 0 || v.kg.Executions() != 1 || len(v.kg.Sends()) != 23 {
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

func TestNullTaskListCannotProveMessageAbsent(t *testing.T) {
	for _, cancel := range []bool{false, true} {
		t.Run(fmt.Sprintf("cancel=%t", cancel), func(t *testing.T) {
			v, wire := newReconciliationWire(t, 1)
			wire.listResult = json.RawMessage("null")
			v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			v.exhaustOriginalResponses()
			if cancel {
				if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
					t.Fatal(err)
				}
			}
			v.clk.Advance(v.cfg.DispatchWindow)
			v.tick(v.e)
			if v.latest(1).State != store.AttemptAmbiguous || v.work(101).EngineeringRetries != 0 || v.kg.Executions() != 1 {
				t.Fatalf("null response became absence: attempt=%+v", v.latest(1))
			}
			if cancel && len(v.pendingCancellations()) != 1 {
				t.Fatal("null task list confirmed cancellation")
			}
			wire.mu.Lock()
			wire.listResult = nil
			wire.mu.Unlock()
			v.tick(v.e)
			if v.latest(1).TaskID == "" || v.kg.Executions() != 1 {
				t.Fatal("valid task list did not reconcile original")
			}
		})
	}
}

func TestValidEmptyTaskListAllowsBoundedRetry(t *testing.T) {
	v, _ := newReconciliationWire(t, 1)
	v.kg.dropFor = map[string]bool{"itsaplan-run/1/initial": true}
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	v.exhaustOriginalResponses()
	v.clk.Advance(v.cfg.DispatchWindow)
	v.tick(v.e)
	if v.work(101).EngineeringRetries != 1 || v.kg.Executions() != 1 || v.latest(1).DecisionID != "replacement-0-1" {
		t.Fatalf("valid empty public page did not permit one retry: %+v", v.latest(1))
	}
}

func TestContinuationOpensNewTaskWhileItsInputIsUnpublished(t *testing.T) {
	v, wire := newReconciliationWire(t, 1)
	v.its.add(1, "coder", 101, "HL-1", "First turn.")
	v.tick(v.e)
	first := v.latest(1)
	v.kg.finish(first.TaskID, kagent.StateCompleted, "First complete", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	v.kg.loseFor = map[string]bool{"itsaplan-run/2/revision": true}
	v.its.add(2, "coder", 101, "HL-1", "Next turn.")
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
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
	for v.latest(2).Issues < 23 {
		v.clk.Advance(v.cfg.DispatchWindow)
		v.tick(v.e)
	}
	v.clk.Advance(v.cfg.DispatchWindow)
	v.tick(v.e)
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
