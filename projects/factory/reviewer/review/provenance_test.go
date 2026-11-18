package review

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

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
	h.cfg.GoalAudit = auditFunc(func(GoalAuditRecord) error { return errors.New("disk unavailable") })
	pr := github.PullRequest{Number: 7, HeadSHA: shaA}
	in := &inputs{issues: &IssueContext{Issue: &IssueEvidence{Key: "HOME-245"}, taskClass: `["old"]`}}
	e := h.engine()
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
	e = h.engine() // restart has no cached verdict or runtime snapshot
	if g := e.goalCall(context.Background(), &e.cfg.Bindings[0], pr, in, "same goal text", "initial", e.log); g == nil || g.Overall != goalMet {
		t.Fatal("existing goal did not recover")
	}
	if len(recorder.runs) != 1 || recorder.runs[0] != before || len(h.k.sends) != 1 || audit.MessageID != before.MessageID {
		t.Fatal("recovery rewrote the original receipt or sent another task")
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
