package dispatch_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func (g *fakeGitHub) BranchHead(_ context.Context, _ string, branch string) (string, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.baseErr != nil {
		return "", g.baseErr
	}
	if b := g.branches[branch]; b != nil {
		return b.head, nil
	}
	return "", github.ErrNotFound
}

func provenanceEnv(t *testing.T) *env {
	v := newDeliveryEnv(t)
	v.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "coder-model", ReasoningEffort: "xhigh", Arm: "control", SystemPrompt: "rendered system guidance"}
	v.gh.push("main", strings.Repeat("a", 40), "base")
	v.e.Release()
	v.e = v.engine("provenance")
	return v
}

func TestCoderAndReworkRunProvenance(t *testing.T) {
	v := provenanceEnv(t)
	v.its.add(1, "coder", 101, "HL-7", "Fix the issue.")
	v.tick(v.e)
	sends := v.kg.Sends()
	if len(sends) != 1 {
		t.Fatalf("sends=%d", len(sends))
	}
	initial := v.run(1).Provenance
	want := provenance.New(*v.cfg.Bindings[0].Runtime, strings.Repeat("a", 40), sends[0].msg.Parts[0].Text, "unclassified")
	if initial != want {
		t.Fatalf("coder provenance=%+v want %+v", initial, want)
	}
	head := strings.Repeat("b", 40)
	v.gh.push("factory/HL-7", head, "fix: issue")
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	// Rework retains the model and system prompt pinned to the existing session,
	// even after the configuration for new sessions changes.
	original := *v.cfg.Bindings[0].Runtime
	v.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "new-model", ReasoningEffort: "high", Arm: "control", SystemPrompt: "new system guidance"}
	v.e.Release()
	v.e = v.engine("provenance-restart")
	v.gh.review(1, "CHANGES_REQUESTED", "reviewer", head, "Handle the timeout.")
	v.poll()
	rounds := v.rounds()
	if len(rounds) != 1 {
		t.Fatalf("rounds=%d", len(rounds))
	}
	sends = v.kg.Sends()
	if len(sends) != 2 {
		t.Fatalf("sends=%d", len(sends))
	}
	original.ReasoningEffort = "high"
	want = provenance.New(original, head, sends[1].msg.Parts[0].Text, "unclassified")
	if rounds[0].Provenance != want {
		t.Fatalf("rework provenance=%+v want %+v", rounds[0].Provenance, want)
	}
	if v.run(1).Provenance != initial {
		t.Fatal("rework changed the original run")
	}
}

func TestProvenanceFailurePreventsCoderSend(t *testing.T) {
	v := provenanceEnv(t)
	v.gh.baseErr = errors.New("branch read unavailable")
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	if len(v.kg.Sends()) != 0 {
		t.Fatal("sent work without provenance")
	}
	v.gh.baseErr = nil
	v.tick(v.e)
	if len(v.kg.Sends()) != 1 {
		t.Fatal("did not resume after provenance became readable")
	}
}

func TestCoderProvenanceAndRenderedPromptSurviveResend(t *testing.T) {
	v := provenanceEnv(t)
	v.kg.notAccepted = 1
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	before := v.run(1).Provenance
	first := v.kg.Sends()[0].msg.Parts[0].Text
	v.gh.push("main", strings.Repeat("c", 40), "new base")
	v.its.issues[101].Description = "changed issue after first send"
	v.e.Release()
	v.e = v.engine("resend")
	v.clk.Advance(20 * time.Second)
	v.tick(v.e)
	sends := v.kg.Sends()
	if len(sends) != 2 || sends[1].msg.Parts[0].Text != first {
		t.Fatalf("resend changed input: sends=%d", len(sends))
	}
	if v.run(1).Provenance != before {
		t.Fatal("resend rewrote the run snapshot")
	}
}

func (k *fakeKagent) AgentRevision(context.Context, kagent.AgentRef) (string, error) {
	return "current", nil
}

func TestLegacySessionProvenanceUsesFencedReplacement(t *testing.T) {
	v := provenanceEnv(t)
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	head := strings.Repeat("b", 40)
	v.gh.push("factory/HL-7", head, "fix: issue")
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	// An existing session from before rollout has no model snapshot.
	if _, err := v.st.DB.Exec(`UPDATE run SET model=NULL,reasoning_effort=NULL,arm=NULL,
 base_sha=NULL,prompt_hash=NULL,task_class=NULL,guidance_system_prompt=NULL,provenance_required=false WHERE id=1;
 UPDATE delivery_attempt SET model=NULL,reasoning_effort=NULL,arm=NULL,
 base_sha=NULL,prompt_hash=NULL,task_class=NULL,guidance_system_prompt=NULL,rendered_prompt=NULL WHERE run_id=1`); err != nil {
		t.Fatal(err)
	}
	session := v.work(101).SessionID
	v.kg.sessions[session].PreparedRevision = "older"
	v.gh.review(1, "CHANGES_REQUESTED", "reviewer", head, "Handle timeout.")
	v.poll()
	if len(v.kg.Sends()) != 1 {
		t.Fatal("attributed an unknown legacy model to current settings")
	}
	old := v.latest(v.rounds()[0].ID)
	if old.Issues != 0 {
		t.Fatal("unverified legacy continuation was sent")
	}
	v.attestRecovery("retry")
	v.e.Release()
	v.e = v.engine("legacy-recovery-restart")
	v.tick(v.e)
	next := v.latest(old.RunID)
	if len(v.kg.Sends()) != 2 || next.ContextID == session || next.State != store.AttemptAccepted || next.Provenance.Model != "coder-model" {
		t.Fatalf("legacy session did not recover with verified fresh provenance: %+v", next)
	}
	if v.run(1).Provenance.Model != "" {
		t.Fatal("backfilled historical run")
	}
}

func TestRefusedCoderSendGetsNewReceiptAfterRuntimeChange(t *testing.T) {
	for _, field := range []string{"model", "effort", "arm", "system"} {
		t.Run(field, func(t *testing.T) {
			v := provenanceEnv(t)
			v.kg.notAccepted = 1
			v.its.add(1, "coder", 101, "HL-7", "Fix it.")
			v.tick(v.e)
			old := v.latest(1)
			runtime := *v.cfg.Bindings[0].Runtime
			switch field {
			case "model":
				runtime.Model = "new-model"
			case "effort":
				runtime.ReasoningEffort = "high"
			case "arm":
				runtime.Arm = "trial"
			case "system":
				runtime.SystemPrompt = "new guidance"
			}
			v.cfg.Bindings[0].Runtime = &runtime
			v.e.Release()
			v.e = v.engine("runtime-restart")
			v.clk.Advance(20 * time.Second)
			v.tick(v.e)
			next := v.latest(1)
			want := provenance.New(runtime, strings.Repeat("a", 40), next.RenderedPrompt, "unclassified")
			if next.MessageID == old.MessageID || next.State != store.AttemptAccepted || next.Provenance != want || v.run(1).Provenance != want {
				t.Fatalf("accepted dispatch has stale provenance: run=%+v attempt=%+v", v.run(1).Provenance, next)
			}
			retired, err := v.st.GetAttempt(v.ctx, old.ID)
			if err != nil || retired.Provenance != old.Provenance || retired.ErrorClass != "runtime_changed" || len(v.kg.Sends()) != 2 {
				t.Fatal("lost refusal receipt or duplicate send")
			}
		})
	}
}

func TestAmbiguousCoderSendDoesNotReplaceReceiptAfterRuntimeChange(t *testing.T) {
	v := provenanceEnv(t)
	v.kg.loseFor = map[string]bool{"itsaplan-run/1/initial": true}
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	old := v.latest(1)
	v.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "new", ReasoningEffort: "high", Arm: "trial", SystemPrompt: "new"}
	v.e.Release()
	v.e = v.engine("ambiguous-runtime-restart")
	v.clk.Advance(20 * time.Second)
	v.tick(v.e)
	if next := v.latest(1); next.ID != old.ID || next.Provenance != old.Provenance || len(v.kg.Sends()) != 1 {
		t.Fatal("ambiguous execution was replaced")
	}
}

func TestExecutedRunKeepsFirstProvenanceOnRuntimeRetry(t *testing.T) {
	v := provenanceEnv(t)
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	first := v.run(1).Provenance
	session := v.work(101).SessionID
	v.gh.push("factory/HL-7", strings.Repeat("b", 40), "fix: first attempt")
	v.kg.finish(v.kg.lastTask(), kagent.StateFailed, "test command failed", nil)
	v.kg.notAccepted = 1
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	refused := v.latest(1)
	if refused.State != store.AttemptSending || refused.Issues != 1 {
		t.Fatalf("expected refused engineering retry: %+v", refused)
	}
	v.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "rollout-model", ReasoningEffort: "high", Arm: "trial", SystemPrompt: "rollout guidance"}
	v.e.Release()
	v.e = v.engine("engineering-runtime-restart")
	v.clk.Advance(20 * time.Second)
	v.tick(v.e)
	next := v.latest(1)
	if next.MessageID == refused.MessageID || next.State != store.AttemptAccepted || next.ContextID != session || next.Provenance.Model != first.Model || next.Provenance.ReasoningEffort != "high" {
		t.Fatalf("retry did not retain the pinned session runtime: %+v", next)
	}
	if v.run(1).Provenance != first {
		t.Fatal("rewrote an already executed run's snapshot")
	}
}

func TestRefusedCoderRetryBudgetKeepsFirstAcceptedProvenance(t *testing.T) {
	v := provenanceEnv(t)
	v.cfg.MaxIssues, v.cfg.FinalLookups = 1, 1
	v.e.Release()
	v.e = v.engine("bounded-refusals")
	v.kg.notAccepted = 2
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	v.clk.Advance(v.cfg.DispatchWindow)
	v.tick(v.e)
	old := v.latest(1)
	if old.Issues != 2 || old.State != store.AttemptSending {
		t.Fatal("expected two definitive refusals")
	}
	v.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "new", ReasoningEffort: "high", Arm: "trial", SystemPrompt: "new guidance"}
	v.e.Release()
	v.e = v.engine("bounded-refusals-rollout")
	v.clk.Advance(v.cfg.DispatchWindow)
	v.tick(v.e)
	next := v.latest(1)
	if next.ID == old.ID || next.State != store.AttemptAccepted || v.run(1).Provenance != next.Provenance || next.Provenance.Model != "new" {
		t.Fatalf("exhausted refused attempt retained a stale run snapshot: %+v", next)
	}
}
