package review

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

// Unrelated tasks can have histories larger than GetTask's bounded reader.
// They must not participate in either starting or recovering this goal.
type isolatedGoalTasks struct {
	Tasks
	reads       []string
	lists       int
	sessions    []string
	nextReadErr error
}

func (k *isolatedGoalTasks) GetTask(ctx context.Context, agent kagent.AgentRef, id string) (*kagent.Task, error) {
	k.reads = append(k.reads, id)
	if id == "unrelated" {
		return nil, kagent.ErrResponseTooLarge
	}
	if err := k.nextReadErr; err != nil {
		k.nextReadErr = nil
		return nil, err
	}
	return k.Tasks.GetTask(ctx, agent, id)
}

func (k *isolatedGoalTasks) ListTasks(ctx context.Context, agent kagent.AgentRef, session, state, token string) (*kagent.TaskPage, error) {
	k.lists++
	k.sessions = append(k.sessions, session)
	return k.Tasks.ListTasks(ctx, agent, session, state, token)
}

type recordedRuns struct {
	mu   sync.Mutex
	runs []provenance.ReviewerRun
	err  error
}

func (r *recordedRuns) RecordReview(_ context.Context, run provenance.ReviewerRun) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, old := range r.runs {
		if old.MessageID == run.MessageID && old != run {
			return errors.New("receipt conflict")
		}
	}
	r.runs = append(r.runs, run)
	return r.err
}

func (r *recordedRuns) snapshot() []provenance.ReviewerRun {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]provenance.ReviewerRun(nil), r.runs...)
}

func TestReviewerQuotaRetryRecordsSentMessage(t *testing.T) {
	for _, rollout := range []bool{false, true} {
		name := "same inputs"
		if rollout {
			name = "changed inputs"
		}
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			addPR7(h)
			h.gh.baseHead = strings.Repeat("b", 40)
			recorder := &recordedRuns{}
			h.cfg.Runs = recorder
			h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "review-model", ReasoningEffort: "xhigh", Arm: "control", SystemPrompt: "review guidance"}
			q := &fakeQuota{}
			h.cfg.Quota = q
			e, ctx, stop := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			first := recorder.snapshot()
			if len(first) != 1 || first[0].MessageID != lastSend(h).MessageID {
				t.Fatal("initial task has no matching receipt")
			}
			h.k.finish(task, kagent.StateFailed, "usage_limit_reached")
			rec := waitForQuotaCheck(t, h)
			stop()
			if rollout {
				h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "new-model", ReasoningEffort: "high", Arm: "trial", SystemPrompt: "new guidance"}
			}
			q.set(github.QuotaReading{ReceivedAt: rec.QuotaSince.Add(time.Second)}, nil)
			e, ctx, stop = h.start()
			e.Tick(ctx)
			id := h.msgID(7, shaA, 1) + "/quota-1"
			eventually(t, "quota retry accepted with provenance", func() bool {
				task = h.k.taskFor(id)
				return task != "" && RecordedTask(h.gh.check(shaA).Summary).ID == task
			})
			runs, sent := recorder.snapshot(), lastSend(h)
			if len(runs) != 2 || runs[0] != first[0] || runs[1].MessageID != sent.MessageID || sent.ContextID != "" {
				t.Fatal("quota retry changed the original receipt or sent an unrecorded message")
			}
			want := provenance.New(*h.cfg.Bindings[0].Runtime, h.gh.baseHead, sent.Parts[0].Text, "unclassified")
			if runs[1].Record != want {
				t.Fatalf("quota receipt=%+v want %+v", runs[1].Record, want)
			}
			h.k.finish(task, kagent.StateCompleted, report(t, passing))
			eventually(t, "quota retry completes review", func() bool {
				return h.gh.check(shaA).Conclusion == ConclusionSuccess
			})
			stop()
		})
	}
}

func TestGoalProvenanceRecoversOriginalTaskAfterInputsChange(t *testing.T) {
	h := newHarness(t)
	recorder := &recordedRuns{}
	h.cfg.Runs = recorder
	h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
	h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "original", ReasoningEffort: "xhigh", Arm: "control", SystemPrompt: "original guidance"}
	h.k.tasks["unrelated"] = &fakeTask{agent: testAgent, task: kagent.Task{ID: "unrelated"}}
	tasks := &isolatedGoalTasks{Tasks: h.k}
	index := h.cfg.GoalTasks.(*FileGoalAudit)
	h.cfg.GoalAudit = auditFunc(func(GoalAuditRecord) error { return errors.New("disk unavailable") })
	pr := github.PullRequest{Number: 7, HeadSHA: shaA}
	in := &inputs{issues: &IssueContext{Issue: &IssueEvidence{Key: "HOME-245"}, taskClass: `["old"]`}}
	e := h.engine()
	e.tasks = tasks
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log); g != nil {
		t.Fatal("unexpected verdict with failed audit")
	}
	if len(recorder.runs) != 1 || len(h.k.sends) != 1 {
		t.Fatal("first goal did not leave a task and receipt")
	}
	before := recorder.runs[0]
	in.issues.taskClass = `["new"]`
	h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "new", ReasoningEffort: "high", Arm: "trial", SystemPrompt: "new guidance"}
	var audit GoalAuditRecord
	h.cfg.GoalAudit = auditFunc(func(r GoalAuditRecord) error { audit = r; return nil })
	var err error
	h.cfg.GoalTasks, err = OpenGoalAudit(index.dir)
	if err != nil {
		t.Fatal(err)
	}
	e = h.engine() // restart has no cached verdict or runtime snapshot
	e.tasks = tasks
	tasks.nextReadErr = errors.New("temporary own-task read failure")
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log); g == nil || g.Overall != goalMet {
		t.Fatal("existing goal did not recover")
	}
	if len(recorder.runs) != 1 || recorder.runs[0] != before || len(h.k.sends) != 1 || audit.MessageID != before.MessageID {
		t.Fatal("recovery rewrote the original receipt or sent another task")
	}
	if tasks.lists != 0 || len(tasks.reads) != 2 || tasks.reads[0] != audit.TaskID || tasks.reads[1] != audit.TaskID {
		t.Fatalf("goal touched tasks other than its own: lists=%d reads=%v", tasks.lists, tasks.reads)
	}
}

func TestGoalProvenanceRecoversAmbiguousSendAfterRestart(t *testing.T) {
	h := newHarness(t)
	recorder := &recordedRuns{}
	h.cfg.Runs = recorder
	h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
	h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "original", ReasoningEffort: "xhigh", Arm: "control", SystemPrompt: "guidance"}
	index := h.cfg.GoalTasks.(*FileGoalAudit)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h.k.loseResponse, h.k.afterSend = true, cancel
	pr := github.PullRequest{Number: 7, HeadSHA: shaA}
	in := &inputs{issues: &IssueContext{Issue: &IssueEvidence{Key: "HOME-245"}, taskClass: "unclassified"}}
	e := h.engine()
	if g := e.goalCall(ctx, &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log); g != nil {
		t.Fatal("lost send response unexpectedly produced a verdict")
	}
	if len(recorder.runs) != 1 || h.k.executions != 1 {
		t.Fatal("expected a dispatched task and receipt")
	}
	h.k.afterSend = nil
	h.cfg.Bindings[0].Runtime.Model = "new"
	in.issues.taskClass = `["new"]`
	var err error
	h.cfg.GoalTasks, err = OpenGoalAudit(index.dir)
	if err != nil {
		t.Fatal(err)
	}
	e = h.engine()
	tasks := &isolatedGoalTasks{Tasks: h.k}
	e.tasks = tasks
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log); g == nil {
		t.Fatal("lost send did not recover by its own message ID")
	}
	if len(recorder.runs) != 1 || h.k.executions != 1 || len(h.k.sends) != 1 || tasks.lists != 1 || tasks.sessions[0] != h.k.sends[0].ContextID {
		t.Fatal("recovery changed the receipt, executed again, or scanned other tasks")
	}
}

type observedGoalIndex struct {
	GoalTasks
	afterSave func(GoalTask) error
}

func (s *observedGoalIndex) SaveGoalTask(r GoalTask) error {
	if err := s.GoalTasks.SaveGoalTask(r); err != nil {
		return err
	}
	return s.afterSave(r)
}

type goalSessionCreatorFunc func(context.Context, kagent.AgentRef, string) (*kagent.Session, error)

func (f goalSessionCreatorFunc) CreateSession(ctx context.Context, agent kagent.AgentRef, id string) (*kagent.Session, error) {
	return f(ctx, agent, id)
}

func TestPreparedGoalReusesSessionAfterLostCreationResponse(t *testing.T) {
	h := newHarness(t)
	recorder := &recordedRuns{}
	h.cfg.Runs = recorder
	h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
	h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "model", ReasoningEffort: "high", Arm: "control"}
	h.cfg.GoalSessions = goalSessionCreatorFunc(func(ctx context.Context, a kagent.AgentRef, id string) (*kagent.Session, error) {
		_, err := h.k.CreateSession(ctx, a, id)
		if err != nil {
			t.Fatal(err)
		}
		return nil, errors.New("creation completed but response was lost")
	})
	pr := github.PullRequest{Number: 7, HeadSHA: shaA}
	in := &inputs{}
	e := h.engine()
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log); g != nil || len(h.k.sends) != 0 || len(h.k.created) != 1 {
		t.Fatal("expected one prepared session without a task")
	}
	h.cfg.GoalSessions = h.k
	e = h.engine()
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log); g == nil || len(h.k.created) != 1 || h.k.executions != 1 {
		t.Fatal("retry did not recover the idempotent session creation")
	}
	if len(recorder.runs) != 2 || recorder.runs[0] != recorder.runs[1] {
		t.Fatal("unchanged prepared inputs did not reuse the same receipt")
	}
}

func TestPreparedGoalRechecksInputsAfterRestart(t *testing.T) {
	for _, checkpoint := range []string{"prepared", "session", "refused"} {
		for _, field := range []string{"model", "effort", "arm", "system", "class"} {
			t.Run(checkpoint+"/"+field, func(t *testing.T) {
				h := newHarness(t)
				recorder := &recordedRuns{}
				h.cfg.Runs = recorder
				h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
				h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "old", ReasoningEffort: "high", Arm: "control", SystemPrompt: "old guidance"}
				index := h.cfg.GoalTasks.(*FileGoalAudit)
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				var saved GoalTask
				h.cfg.GoalTasks = &observedGoalIndex{GoalTasks: index, afterSave: func(r GoalTask) error {
					saved = r
					if r.State == goalPrepared && ((checkpoint == "prepared" && r.ContextID == "") || (checkpoint == "session" && r.ContextID != "")) {
						cancel()
					}
					return nil
				}}
				if checkpoint == "refused" {
					h.k.sendErr, h.k.afterSend = []error{kagent.ErrNotAccepted}, cancel
				}
				pr := github.PullRequest{Number: 7, HeadSHA: shaA}
				in := &inputs{issues: &IssueContext{Issue: &IssueEvidence{Key: "HOME-245"}, taskClass: "unclassified"}}
				e := h.engine()
				if g := e.goalCall(ctx, &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log); g != nil || h.k.executions != 0 || saved.State != goalPrepared {
					t.Fatal("expected a prepared, unexecuted dispatch")
				}
				before := saved
				runtime := h.cfg.Bindings[0].Runtime
				switch field {
				case "model":
					runtime.Model = "new"
				case "effort":
					runtime.ReasoningEffort = "xhigh"
				case "arm":
					runtime.Arm = "trial"
				case "system":
					runtime.SystemPrompt = "new guidance"
				case "class":
					in.issues.taskClass = `["new"]`
				}
				var err error
				h.cfg.GoalTasks, err = OpenGoalAudit(index.dir)
				if err != nil {
					t.Fatal(err)
				}
				h.k.afterSend = nil
				e = h.engine()
				if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log); g == nil || h.k.executions != 1 {
					t.Fatal("prepared dispatch did not resume")
				}
				last := h.k.sends[len(h.k.sends)-1]
				want := provenance.New(*runtime, shaA, last.Parts[0].Text, in.issues.taskClass)
				got := recorder.runs[len(recorder.runs)-1]
				if last.MessageID == before.MessageID || last.ContextID == before.ContextID || got.MessageID != last.MessageID || got.Record != want {
					t.Fatal("new execution reused stale receipt or pinned session")
				}
			})
		}
	}
}

func TestIssuedGoalWithoutTaskCannotExecuteOnRestart(t *testing.T) {
	for _, legacy := range []bool{false, true} {
		h := newHarness(t)
		recorder := &recordedRuns{}
		h.cfg.Runs = recorder
		h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
		h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "old", ReasoningEffort: "high", Arm: "control"}
		index := h.cfg.GoalTasks.(*FileGoalAudit)
		var issued GoalTask
		h.cfg.GoalTasks = &observedGoalIndex{GoalTasks: index, afterSave: func(r GoalTask) error {
			if r.State == goalIssued {
				issued = r
				return errors.New("stop after persisting possible acceptance")
			}
			return nil
		}}
		pr := github.PullRequest{Number: 7, HeadSHA: shaA}
		in := &inputs{}
		e := h.engine()
		e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log)
		if issued.ContextID == "" || len(h.k.sends) != 0 || len(recorder.runs) != 1 {
			t.Fatal("expected an uncertain dispatch with no send")
		}
		if legacy {
			issued.State, issued.ContextID, issued.Nonce = "", "", ""
			if err := index.SaveGoalTask(issued); err != nil {
				t.Fatal(err)
			}
		}
		h.cfg.GoalTasks = index
		h.cfg.Bindings[0].Runtime.Model, h.cfg.Bindings[0].Runtime.ReasoningEffort = "new", "xhigh"
		e = h.engine()
		if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log); g != nil || len(h.k.sends) != 0 || len(recorder.runs) != 1 {
			t.Fatal("uncertain dispatch was treated as permission to execute")
		}
		if legacy {
			continue
		}
		h.k.tasks["foreign"] = &fakeTask{agent: testAgent, task: kagent.Task{
			ID: "foreign", ContextID: issued.ContextID,
			History: []*kagent.Message{{MessageID: "another-message"}},
		}}
		if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log); g != nil || len(h.k.sends) != 0 {
			t.Fatal("adopted a task without the original opening message")
		}
		delete(h.k.tasks, "foreign")
		// An earlier request can become visible later. Adopt its task by reads
		// of this session, retaining the old receipt despite the rollout.
		_, err := h.k.Send(context.Background(), testAgent, kagent.Message{MessageID: issued.MessageID, ContextID: issued.ContextID, Parts: []kagent.Part{{Text: "goal text"}}})
		if err != nil {
			t.Fatal(err)
		}
		if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "goal text", nil, e.log); g == nil || len(h.k.sends) != 1 || len(recorder.runs) != 1 {
			t.Fatal("late accepted task did not recover read-only")
		}
	}
}

type failingGoalIndex struct {
	GoalTasks
	operation string
}

func (s *failingGoalIndex) LoadGoalTask(key string) (*GoalTask, error) {
	if s.operation == "load" {
		return nil, errors.New("PRIVATE index failure")
	}
	return s.GoalTasks.LoadGoalTask(key)
}

func (s *failingGoalIndex) SaveGoalTask(r GoalTask) error {
	if (s.operation == "message" && r.TaskID == "") || (s.operation == "task" && r.TaskID != "") {
		return errors.New("PRIVATE index failure")
	}
	return s.GoalTasks.SaveGoalTask(r)
}

func (s *failingGoalIndex) DeleteGoalTask(key string) error {
	if s.operation == "delete" {
		return errors.New("PRIVATE index failure")
	}
	return s.GoalTasks.DeleteGoalTask(key)
}

func TestGoalIndexFailureKeepsDispatchRecoverable(t *testing.T) {
	for _, operation := range []string{"load", "message", "task", "delete"} {
		t.Run(operation, func(t *testing.T) {
			h := newHarness(t)
			h.cfg.Runs = &recordedRuns{}
			h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
			h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "model", ReasoningEffort: "high", Arm: "control"}
			index := h.cfg.GoalTasks
			h.cfg.GoalTasks = &failingGoalIndex{GoalTasks: index, operation: operation}
			cleaner := &fakeCleaner{}
			h.cfg.Cleaner = cleaner
			pr := github.PullRequest{Number: 7, HeadSHA: shaA}
			in := &inputs{}
			e := h.engine()
			if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log); g != nil {
				t.Fatal("index failure produced a verdict")
			}
			if (operation == "load" || operation == "message") && len(h.k.sends) != 0 {
				t.Fatal("sent without a durable message index")
			}
			if len(cleaner.deletions()) != 0 || strings.Contains(h.log.String(), "PRIVATE") {
				t.Fatal("index failure deleted the session or leaked its error")
			}
			h.cfg.GoalTasks = index
			e = h.engine()
			if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log); g == nil || h.k.executions != 1 {
				t.Fatal("index recovery failed or repeated execution")
			}
		})
	}
}

func TestGoalProvenanceAllocatesNewReceiptForChangedInputs(t *testing.T) {
	for _, field := range []string{"model", "effort", "arm", "system", "class"} {
		t.Run(field, func(t *testing.T) {
			h := newHarness(t)
			recorder := &recordedRuns{err: errors.New("receipt saved but response lost")}
			h.cfg.Runs = recorder
			h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
			h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "original", ReasoningEffort: "xhigh", Arm: "control", SystemPrompt: "guidance"}
			pr := github.PullRequest{Number: 7, HeadSHA: shaA}
			in := &inputs{issues: &IssueContext{Issue: &IssueEvidence{Key: "HOME-245"}, taskClass: "unclassified"}}
			e := h.engine()
			e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log)
			if len(recorder.runs) != 1 || len(h.k.sends) != 0 {
				t.Fatal("expected a receipt without a task")
			}
			runtime := h.cfg.Bindings[0].Runtime
			switch field {
			case "model":
				runtime.Model = "new"
			case "effort":
				runtime.ReasoningEffort = "high"
			case "arm":
				runtime.Arm = "trial"
			case "system":
				runtime.SystemPrompt = "new guidance"
			case "class":
				in.issues.taskClass = `["feature"]`
			}
			recorder.err = nil
			e = h.engine()
			if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", nil, e.log); g == nil {
				t.Fatal("new goal stalled on old receipt")
			}
			if len(recorder.runs) != 2 || recorder.runs[0].MessageID == recorder.runs[1].MessageID || len(h.k.sends) != 1 {
				t.Fatal("changed inputs reused an old message")
			}
		})
	}
}

func TestReviewerMessageRecordsProvenanceBeforeDispatch(t *testing.T) {
	h := newHarness(t)
	recorder := &recordedRuns{}
	h.cfg.Runs = recorder
	h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "review-model", ReasoningEffort: "xhigh", Arm: "control", SystemPrompt: "review guidance"}
	h.gh.baseHead = strings.Repeat("b", 40)
	h.gh.addPR(&fakePR{Number: 7, Author: "jomcgi", Head: shaA, Title: "Change"})
	e := h.engine()
	b := &e.cfg.Bindings[0]
	pr := github.PullRequest{Number: 7, HeadSHA: shaA, Title: "Change"}
	in := &inputs{issues: &IssueContext{Issue: &IssueEvidence{Key: "HOME-245"}, taskClass: `["feature"]`}}
	msg, err := e.message(context.Background(), b, pr, &github.CheckRun{ID: 1}, in, 1, 0, false, e.log)
	if err != nil {
		t.Fatal(err)
	}
	if len(recorder.runs) != 1 {
		t.Fatalf("records=%d", len(recorder.runs))
	}
	got := recorder.runs[0]
	want := provenance.New(*b.Runtime, h.gh.baseHead, msg.Parts[0].Text, `["feature"]`)
	if got.Record != want || got.MessageID != msg.MessageID || got.IssueKey != "HOME-245" {
		t.Fatalf("reviewer provenance=%+v", got)
	}
	recorder.err = errors.New("database unavailable")
	if _, err := e.message(context.Background(), b, pr, &github.CheckRun{ID: 2}, in, 1, 0, false, e.log); err == nil {
		t.Fatal("built dispatchable message without durable provenance")
	}
}

func TestGoalReviewRequiresProvenanceBeforeSending(t *testing.T) {
	h := newHarness(t)
	recorder := &recordedRuns{err: errors.New("database unavailable")}
	h.cfg.Runs = recorder
	h.cfg.MACSecret = func() (string, error) { return strings.Repeat("k", 32), nil }
	h.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "review-model", ReasoningEffort: "xhigh", Arm: "control", SystemPrompt: "review guidance"}
	h.gh.baseHead = strings.Repeat("b", 40)
	e := h.engine()
	pr := github.PullRequest{Number: 7, HeadSHA: shaA}
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, &inputs{}, "goal prompt", nil, e.log); g != nil {
		t.Fatal("goal call succeeded without provenance")
	}
	if len(recorder.runs) != 1 || !strings.Contains(recorder.runs[0].MessageID, "/goal/") {
		t.Fatalf("goal record=%+v", recorder.runs)
	}
	if len(h.k.sends) != 0 {
		t.Fatal("goal sent before provenance persisted")
	}
}
