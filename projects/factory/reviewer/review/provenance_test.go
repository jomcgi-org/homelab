package review

import (
	"context"
	"errors"
	"strings"
	"testing"

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
	return k.Tasks.ListTasks(ctx, agent, session, state, token)
}

type recordedRuns struct {
	runs []provenance.ReviewerRun
	err  error
}

func (r *recordedRuns) RecordReview(_ context.Context, run provenance.ReviewerRun) error {
	for _, old := range r.runs {
		if old.MessageID == run.MessageID && old != run {
			return errors.New("receipt conflict")
		}
	}
	r.runs = append(r.runs, run)
	return r.err
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
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g != nil {
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
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g == nil || g.Overall != goalMet {
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
	if g := e.goalCall(ctx, &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g != nil {
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
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g == nil {
		t.Fatal("lost send did not recover by its own message ID")
	}
	if len(recorder.runs) != 1 || h.k.executions != 1 || len(h.k.sends) != 2 || h.k.sends[0].MessageID != h.k.sends[1].MessageID || tasks.lists != 0 {
		t.Fatal("recovery changed the receipt, executed again, or scanned other tasks")
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
			if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g != nil {
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
			if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g == nil || h.k.executions != 1 {
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
			e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log)
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
			if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g == nil {
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
	msg, err := e.message(context.Background(), b, pr, &github.CheckRun{ID: 1}, in, 1, false, e.log)
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
	if _, err := e.message(context.Background(), b, pr, &github.CheckRun{ID: 2}, in, 1, false, e.log); err == nil {
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
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, &inputs{}, "goal prompt", "initial", e.log); g != nil {
		t.Fatal("goal call succeeded without provenance")
	}
	if len(recorder.runs) != 1 || !strings.Contains(recorder.runs[0].MessageID, "/goal/") {
		t.Fatalf("goal record=%+v", recorder.runs)
	}
	if len(h.k.sends) != 0 {
		t.Fatal("goal sent before provenance persisted")
	}
}
