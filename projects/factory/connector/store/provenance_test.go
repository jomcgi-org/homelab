package store_test

import (
	"context"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestProvenanceMigrationLeavesHistoricalRunsNull(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	if _, _, err := st.RecordClaim(ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	// Restore the pre-migration shape with a real historical row, then apply it.
	_, err := st.DB.Exec(`DROP FUNCTION public.record_reviewer_run(text,text,text,text,text,text,text,text,text);
 ALTER TABLE run DROP COLUMN model,DROP COLUMN reasoning_effort,DROP COLUMN arm,DROP COLUMN base_sha,
 DROP COLUMN prompt_hash,DROP COLUMN task_class,DROP COLUMN reviewer_message_id,DROP COLUMN guidance_system_prompt,DROP COLUMN provenance_required;
 ALTER TABLE delivery_attempt DROP COLUMN rendered_prompt,DROP COLUMN model,DROP COLUMN reasoning_effort,
 DROP COLUMN arm,DROP COLUMN base_sha,DROP COLUMN prompt_hash,DROP COLUMN task_class,DROP COLUMN guidance_system_prompt,DROP COLUMN execution_possible;
 DELETE FROM schema_migrations WHERE version IN ('20261119040000_run_provenance','20261120000000_run_provenance_opt_in')`)
	if err != nil {
		t.Fatal(err)
	}
	if err := migrations.Apply(ctx, st.DB); err != nil {
		t.Fatal(err)
	}
	old, err := st.GetRun(ctx, 1)
	if err != nil || old.Provenance != (provenance.Record{}) || old.ProvenanceRequired {
		t.Fatalf("legacy run=%+v err=%v", old, err)
	}
	var nulls bool
	if err := st.DB.QueryRow(`SELECT model IS NULL AND reasoning_effort IS NULL AND arm IS NULL
 AND base_sha IS NULL AND prompt_hash IS NULL AND task_class IS NULL FROM run WHERE id=1`).Scan(&nulls); err != nil || !nulls {
		t.Fatalf("historical values changed: %v %v", nulls, err)
	}
	fresh, _, err := st.RecordClaim(ctx, store.ClaimedRun{ID: 2, AgentKey: "coder", Attempt: 1})
	if err != nil || !fresh.ProvenanceRequired {
		t.Fatalf("new run=%+v err=%v", fresh, err)
	}
}

func TestReviewerProvenanceIsAppendOnlyAndIdempotent(t *testing.T) {
	st := open(t)
	p := provenance.New(provenance.Runtime{Model: "review-model", ReasoningEffort: "xhigh", Arm: "control"}, strings.Repeat("a", 40), "review prompt", "unclassified")
	args := []any{"factory-review/org/repo/1/head", "review/reviewer", "HOME-245", p.Model, p.ReasoningEffort, p.Arm, p.BaseSHA, p.PromptHash, p.TaskClass}
	var first int64
	for i := 0; i < 2; i++ {
		var id int64
		err := st.DB.QueryRow(`SELECT public.record_reviewer_run($1,$2,$3,$4,$5,$6,$7,$8,$9)`, args...).Scan(&id)
		if err != nil {
			t.Fatal(err)
		}
		if i == 0 {
			first = id
		} else if id != first {
			t.Fatal("redelivery created another run")
		}
	}
	got, err := st.GetRun(context.Background(), first)
	if err != nil || got.Origin != "reviewer" || got.Provenance != p {
		t.Fatalf("review=%+v err=%v", got, err)
	}
	args[3] = "other-model"
	if _, err := st.DB.Exec(`SELECT public.record_reviewer_run($1,$2,$3,$4,$5,$6,$7,$8,$9)`, args...); err == nil {
		t.Fatal("rewrote a historical snapshot")
	}
}

func TestPreProvenanceClaimRetryLeavesRunReceiptEmpty(t *testing.T) {
	st := open(t)
	ctx := t.Context()
	_, err := st.DB.Exec(`INSERT INTO run (id, agent_key, trigger, prompt, system_prompt, attempt, state)
 VALUES (1, 'coder', 'issue', 'original', '', 1, 'dispatched')`)
	if err != nil {
		t.Fatal(err)
	}
	_, outcome, err := st.RecordClaim(ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", Attempt: 2})
	if err != nil || outcome != store.ClaimRedelivery {
		t.Fatalf("retry outcome=%v err=%v", outcome, err)
	}
	w, err := st.TryAcquireWriter(ctx, "retry")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.Release)
	a, err := st.RecordAttempt(ctx, st.DB, store.Attempt{RunID: 1, RunAttempt: 2, DecisionID: "retry", MessageID: "retry", Kind: "initial", KagentNamespace: "ns", KagentAgent: "coder", WriterEpoch: w.Epoch})
	if err != nil {
		t.Fatal(err)
	}
	receipt := provenance.New(provenance.Runtime{Model: "model", ReasoningEffort: "high", Arm: "control"}, strings.Repeat("a", 40), "retry", "unclassified")
	if err := st.RecordProvenance(ctx, w, a, receipt, "", "retry"); err != nil {
		t.Fatal(err)
	}
	run, err := st.GetRun(ctx, 1)
	if err != nil || run.ProvenanceRequired || run.Provenance != (provenance.Record{}) || a.Provenance != receipt {
		t.Fatalf("retry run=%+v attempt=%+v err=%v", run, a, err)
	}
}
