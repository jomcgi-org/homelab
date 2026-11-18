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
	r.runs = append(r.runs, run)
	return r.err
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
