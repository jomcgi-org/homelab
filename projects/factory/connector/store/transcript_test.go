package store_test

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestTranscriptByteMigrationPreservesExistingJSON(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	if _, _, err := st.RecordClaim(ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", Trigger: "mention", Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	// Recreate the previous schema with a persisted journal, then migrate it.
	// That schema had no storyboard view over the journal.
	if _, err := st.DB.ExecContext(ctx, `
		DROP VIEW public.storyboard_task_messages, public.storyboard_messages;
		ALTER TABLE task_transcript ALTER COLUMN state TYPE jsonb USING convert_from(state, 'UTF8')::jsonb;
		DELETE FROM schema_migrations WHERE version = '20261115210000_transcript_bytes';
		INSERT INTO task_transcript (run_id, attempt, task_id, base_offset, state)
		VALUES (1, 1, 'task', 3, '{"events":[{"delta":"saved"}]}')`); err != nil {
		t.Fatal(err)
	}
	before, err := st.GetTranscript(ctx, st.DB, 1, 1, "task")
	if err != nil || before == nil {
		t.Fatalf("read previous journal: %v", err)
	}
	if err := migrations.Apply(ctx, st.DB); err != nil {
		t.Fatal(err)
	}
	after, err := st.GetTranscript(ctx, st.DB, 1, 1, "task")
	if err != nil || after == nil || after.Base != before.Base || string(after.State) != string(before.State) {
		t.Fatalf("migration changed existing journal: %+v, %v", after, err)
	}
	// U+0000 is valid JSON but cannot be represented in PostgreSQL JSONB.
	raw, err := json.Marshal(map[string]string{"delta": "before\x00after"})
	if err != nil {
		t.Fatal(err)
	}
	after.State = raw
	if err := st.SaveTranscript(ctx, st.DB, 1, 1, "task", after); err != nil {
		t.Fatal(err)
	}
	got, err := st.GetTranscript(ctx, st.DB, 1, 1, "task")
	if err != nil || got == nil || string(got.State) != string(raw) || got.Base != 3 {
		t.Fatalf("NUL journal did not round trip: %+v, %v", got, err)
	}
	if err := migrations.Apply(ctx, st.DB); err != nil {
		t.Fatalf("migration is not idempotent: %v", err)
	}
}
