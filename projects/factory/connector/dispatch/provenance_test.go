// invariant: FACT-17
package dispatch_test

import (
	"context"
	"errors"
	"fmt"
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

func (k *fakeKagent) PromptRevision(_ context.Context, _ kagent.AgentRef, prompt string) (string, error) {
	if k.onPromptRead != nil {
		k.onPromptRead()
	}
	if k.servedPrompt != "" && k.servedPrompt != prompt {
		return "", errors.New("prompt rollout pending")
	}
	return "current", nil
}

func (k *fakeKagent) CreateSession(_ context.Context, agent kagent.AgentRef, id string) (*kagent.Session, error) {
	ctxID := "ctx-" + id
	if k.onPromptCreate != nil {
		k.onPromptCreate(id)
	}
	if sess := k.sessions[ctxID]; sess != nil {
		return sess, nil
	}
	revision := "current"
	if k.createdRevision != "" {
		revision = k.createdRevision
	}

	k.contexts[ctxID] = agent
	k.sessions[ctxID] = &kagent.Session{ID: ctxID, ContextID: ctxID, AgentName: agent.Name, AgentNamespace: agent.Namespace, PreparedRevision: revision, Creator: k.identity.PrincipalID(), State: kagent.RuntimeReady}
	return k.sessions[ctxID], nil
}

func TestWriterLostDuringPromptReadCannotCreateSession(t *testing.T) {
	v := provenanceEnv(t)
	v.kg.onPromptRead = func() {
		var terminated bool
		if err := v.st.DB.QueryRow(`SELECT pg_terminate_backend(pid, 5000) FROM pg_locks
 WHERE locktype='advisory' AND granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())`).Scan(&terminated); err != nil || !terminated {
			t.Fatalf("terminate writer: %v, %v", terminated, err)
		}
	}
	v.kg.onPromptCreate = func(string) { t.Fatal("fenced writer created a session") }
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	if err := v.e.Tick(v.ctx); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("tick: %v", err)
	}
}

func TestPromptMismatchRetiresJournaledCreation(t *testing.T) {
	v := provenanceEnv(t)
	var keys []string
	v.kg.onPromptCreate = func(key string) {
		var count int
		if err := v.st.DB.QueryRow("SELECT count(*) FROM admission_read WHERE message_id=$1 AND operation='create-prompt-session'", key).Scan(&count); err != nil || count != 1 {
			t.Fatalf("creation journal count=%d err=%v", count, err)
		}
		keys = append(keys, key)
	}
	v.kg.createdRevision = "other"
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	if len(keys) == 0 || len(v.kg.Sends()) != 0 {
		t.Fatal("mismatched session crossed dispatch gate")
	}
	v.e.Release()
	v.e = v.engine("after-rollback")
	v.kg.createdRevision = "current"
	v.tick(v.e)
	if len(v.kg.Sends()) != 1 || keys[len(keys)-1] == keys[0] {
		t.Fatal("replacement reused the retired creation request")
	}
}

func TestDispatchWaitsForServedPrompt(t *testing.T) {
	v := provenanceEnv(t)
	v.kg.servedPrompt = "previous system guidance"
	v.its.add(1, "coder", 101, "HL-7", "Fix it.")
	v.tick(v.e)
	if len(v.kg.Sends()) != 0 || v.run(1).Provenance.PromptHash != "" {
		t.Fatal("dispatch crossed the rollout gap")
	}
	v.kg.servedPrompt = v.cfg.Bindings[0].Runtime.SystemPrompt
	v.clk.Advance(time.Minute)
	v.tick(v.e)
	sends := v.kg.Sends()
	if len(sends) != 1 {
		t.Fatalf("sends=%d", len(sends))
	}
	want := provenance.New(*v.cfg.Bindings[0].Runtime, strings.Repeat("a", 40), sends[0].msg.Parts[0].Text, "unclassified")
	if v.run(1).Provenance != want {
		t.Fatal("receipt differs from served prompt")
	}
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

func TestCoderProvenanceRetryBeforeFirstPush(t *testing.T) {
	for _, status := range []int{0, 404, 403, 503} {
		t.Run(fmt.Sprintf("branch-status-%d", status), func(t *testing.T) {
			v := provenanceEnv(t)
			v.its.add(1, "coder", 101, "HL-7", "Fix it.")
			v.tick(v.e)
			first := v.latest(1)
			session := v.work(101).SessionID
			if first.State != store.AttemptAccepted || session == "" {
				t.Fatal("initial task was not attached")
			}
			// The task fails before publishing its branch. Main and the runtime
			// for new sessions advance while this session keeps its own inputs.
			v.kg.finish(first.TaskID, kagent.StateFailed, "test command failed", nil)
			v.gh.push("main", strings.Repeat("c", 40), "new base")
			v.cfg.Bindings[0].Runtime = &provenance.Runtime{Model: "new-model", ReasoningEffort: "high", Arm: "trial", SystemPrompt: "new guidance"}
			v.e.Release()
			v.e = v.engine("retry-before-first-push")
			if status != 0 {
				v.gh.baseErr = &github.HTTPError{Status: status}
			}
			v.clk.Advance(5 * time.Second)
			v.tick(v.e)
			if status == 403 || status == 503 {
				if len(v.kg.Sends()) != 1 || v.latest(1).Issues != 0 {
					t.Fatal("branch read failure permitted a send")
				}
				v.gh.baseErr = nil
				v.tick(v.e)
			}
			next, sends := v.latest(1), v.kg.Sends()
			if len(sends) != 2 || next.State != store.AttemptAccepted || next.ContextID != session || next.MessageID == first.MessageID {
				t.Fatalf("unpublished branch blocked the continuation: %+v sends=%d", next, len(sends))
			}
			runtime := provenance.Runtime{Model: "coder-model", ReasoningEffort: "high", Arm: "control", SystemPrompt: "rendered system guidance"}
			want := provenance.New(runtime, first.Provenance.BaseSHA, sends[1].msg.Parts[0].Text, "unclassified")
			if next.Provenance != want || !strings.Contains(next.RenderedPrompt, "Worktree starting commit: "+first.Provenance.BaseSHA) {
				t.Fatal("retry did not retain its session's starting commit and pinned runtime")
			}
			if v.run(1).Provenance != first.Provenance {
				t.Fatal("retry changed the first execution receipt")
			}
		})
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

func TestLegacyRunRetryRecordsOnlyAttemptProvenance(t *testing.T) {
	for _, workBranch := range []bool{true, false} {
		t.Run(fmt.Sprintf("work-branch-present-%t", workBranch), func(t *testing.T) {
			v := newDeliveryEnv(t)
			v.its.add(1, "coder", 101, "HL-7", "Fix it.")
			v.tick(v.e)
			first := v.latest(1)
			if first.State != store.AttemptAccepted || first.Provenance != (provenance.Record{}) {
				t.Fatalf("expected a legacy attempt: %+v", first)
			}
			if _, err := v.st.DB.Exec(`UPDATE run SET provenance_required=false WHERE id=1`); err != nil {
				t.Fatal(err)
			}
			session := v.work(101).SessionID
			v.kg.sessions[session].PreparedRevision = "current"
			head := strings.Repeat("b", 40)
			if workBranch {
				v.gh.push("factory/HL-7", head, "partial fix")
			} else {
				v.gh.push("main", head, "current base")
			}
			v.kg.finish(first.TaskID, kagent.StateFailed, "test command failed", nil)
			runtime := provenance.Runtime{Model: "coder-model", ReasoningEffort: "high", Arm: "control"}
			v.cfg.Bindings[0].Runtime = &runtime
			v.e.Release()
			v.e = v.engine("legacy-retry")
			v.clk.Advance(5 * time.Second)
			v.tick(v.e)
			if !workBranch {
				if len(v.kg.Sends()) != 1 || v.latest(1).Issues != 0 {
					t.Fatal("issued legacy continuation before fencing")
				}
				v.attestRecovery("retry")
				v.tick(v.e)
			}
			next, sends := v.latest(1), v.kg.Sends()
			if len(sends) != 2 || next.ID == first.ID || next.State != store.AttemptAccepted {
				t.Fatalf("retry attempt=%+v sends=%d", next, len(sends))
			}
			if !workBranch && (next.ContextID == "" || next.ContextID == session) {
				t.Fatal("replacement did not start a fresh session")
			}
			text := sends[1].msg.Parts[0].Text
			if next.RenderedPrompt != text || next.Provenance != provenance.New(runtime, head, text, "unclassified") {
				t.Fatalf("incorrect retry receipt: %+v", next)
			}
			if run := v.run(1); run.ProvenanceRequired || run.Provenance != (provenance.Record{}) {
				t.Fatalf("retry attributed provenance to the legacy run: %+v", run)
			}
		})
	}
}

func legacyProvenanceEnv(t *testing.T) (*env, string, string) {
	t.Helper()
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
	return v, head, session
}

func TestLegacyPinnedSessionReadIsJournaled(t *testing.T) {
	v, head, session := legacyProvenanceEnv(t)
	checked := false
	v.kg.onPromptRead = func() {
		v.kg.onSessionRead = func(id string) {
			var exists bool
			err := v.st.DB.QueryRow("SELECT EXISTS (SELECT 1 FROM admission_read WHERE operation='provenance-pinned-session' AND resource=$1)", id).Scan(&exists)
			if err != nil || !exists || id != session {
				t.Errorf("pin read %s journaled=%v error=%v", id, exists, err)
			}
			checked = true
			v.kg.onSessionRead = nil
		}
	}
	v.gh.review(1, "CHANGES_REQUESTED", "reviewer", head, "Handle timeout.")
	v.poll()
	if !checked || len(v.kg.Sends()) != 2 {
		t.Fatal("legacy continuation did not dispatch through the journaled read")
	}
}

func TestLegacySessionProvenanceUsesFencedReplacement(t *testing.T) {
	v, head, session := legacyProvenanceEnv(t)
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

func TestTrialArmRounds(t *testing.T) {
	// Each case lists the round kinds and the round that escalates. Review
	// and CI fix rounds count against separate thresholds.
	for _, tc := range []struct {
		name  string
		kinds []string
		limit int
	}{
		{"review", []string{"review", "review", "review"}, 3},
		{"ci", []string{"ci", "ci"}, 2},
		{"two reviews and a CI fix", []string{"review", "review", "ci", "ci"}, 4},
		{"a CI fix and three reviews", []string{"ci", "review", "review", "review"}, 4},
		// A required check that fails in the merge group ejects the PR. That
		// round is a CI fix too, not a review round.
		{"queue CI", []string{"queue", "queue"}, 2},
		{"two reviews and a queue CI fix", []string{"review", "review", "queue", "queue"}, 4},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := provenanceEnv(t)
			b := &v.cfg.Bindings[0]
			b.Review.MaxReviewRounds = 6
			b.Arms = []provenance.Arm{{Name: "luna", Model: "gpt-6-luna", ReasoningEffort: "high", Weight: 1}}
			v.e.Release()
			v.e = v.engine("trial")
			v.its.add(1, "coder", 101, "HL-7", "Fix the issue.")
			v.tick(v.e)
			initial := v.run(1).Provenance
			if initial.Arm != "luna" || initial.Model != "gpt-6-luna" {
				t.Fatal(initial)
			}
			session := v.work(101).SessionID
			// Drain the trial and restart before rework. Re-selecting by weight
			// would now send the item to control one or two rounds too early.
			b.Arms[0].Weight = 0
			b.Arms = append(b.Arms, provenance.Arm{Name: "control", Model: b.Runtime.Model, ReasoningEffort: b.Runtime.ReasoningEffort, Weight: 1})
			v.e.Release()
			v.e = v.engine("draining-trial")
			for i := 1; i <= tc.limit; i++ {
				head := fmt.Sprintf("%040x", i)
				v.gh.push("factory/HL-7", head, "fix: issue")
				v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
				v.clk.Advance(5 * time.Second)
				v.tick(v.e)
				switch tc.kinds[i-1] {
				case "ci":
					v.gh.check(head, "test", 100, "failure")
				case "queue":
					group := fmt.Sprintf("group-%d", i)
					v.gh.green(head)
					v.gh.review(1, "APPROVED", "reviewer", head, "")
					v.poll()
					v.gh.mergeGroup(1, group)
					v.poll()
					v.gh.check(group, "test", 100, "failure")
					v.gh.eject(1)
				default:
					v.gh.review(1, "CHANGES_REQUESTED", "reviewer", head, "Fix the timeout.")
				}
				v.poll()
				rounds := v.rounds()
				if len(rounds) != i {
					t.Fatalf("rounds=%d want %d", len(rounds), i)
				}
				wi := v.work(101)
				want := "luna"
				if i == tc.limit {
					want = "control"
				}
				if rounds[i-1].Provenance.Arm != want || wi.FirstArm != "luna" || wi.Escalated != (i == tc.limit) || (wi.SessionID != session) != (i == tc.limit) {
					t.Fatalf("round %d: work=%+v run=%+v", i, wi, rounds[i-1])
				}
			}
		})
	}
}

// These issue keys exercise all three buckets in the Homelab allocation.
// Read each run back from Postgres and compare it with the actual Send target.
func TestWeightedCoderRunColumns(t *testing.T) {
	arms := []provenance.Arm{
		{Name: "control", Model: "gpt-6.2-astra", ReasoningEffort: "xhigh", Weight: 70},
		{Name: "astra-high", Model: "gpt-6.2-astra", ReasoningEffort: "high", Weight: 15},
		{Name: "luna", Model: "gpt-6-luna", ReasoningEffort: "xhigh", Weight: 15},
	}
	for i, key := range []string{"HL-1", "HL-3", "HL-9"} {
		t.Run(key, func(t *testing.T) {
			v := provenanceEnv(t)
			b := &v.cfg.Bindings[0]
			b.Arms = arms
			b.Runtime.Model, b.Runtime.ReasoningEffort = arms[0].Model, arms[0].ReasoningEffort
			v.e.Release()
			v.e = v.engine("weighted")
			v.its.add(1, "coder", 101, key, "Fix the issue.")
			v.tick(v.e)
			got := v.run(1).Provenance
			want := arms[i]
			agent := b.KagentAgent
			if i != 0 {
				agent += "-" + want.Name
			}
			sends := v.kg.Sends()
			if len(sends) != 1 || sends[0].agent.Name != agent || got.Arm != want.Name || got.Model != want.Model || got.ReasoningEffort != want.ReasoningEffort {
				t.Fatalf("sends=%v run=%+v want agent=%s arm=%+v", sends, got, agent, want)
			}
		})
	}
}
