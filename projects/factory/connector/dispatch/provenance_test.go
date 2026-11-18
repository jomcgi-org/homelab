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

func TestLegacySessionProvenanceRequiresKnownRevision(t *testing.T) {
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
 base_sha=NULL,prompt_hash=NULL,task_class=NULL,guidance_system_prompt=NULL,provenance_required=false WHERE id=1`); err != nil {
		t.Fatal(err)
	}
	session := v.work(101).SessionID
	v.kg.sessions[session].PreparedRevision = "older"
	v.gh.review(1, "CHANGES_REQUESTED", "reviewer", head, "Handle timeout.")
	v.poll()
	if len(v.kg.Sends()) != 1 {
		t.Fatal("attributed an unknown legacy model to current settings")
	}
	v.kg.sessions[session].PreparedRevision = "current"
	v.tick(v.e)
	if len(v.kg.Sends()) != 2 {
		t.Fatal("verified legacy session did not resume")
	}
	if v.run(1).Provenance.Model != "" {
		t.Fatal("backfilled historical run")
	}
}
