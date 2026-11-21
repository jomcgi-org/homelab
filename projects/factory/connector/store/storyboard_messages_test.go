package store_test

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

// storyboardJournal encodes a journal as the connector stores it.
func storyboardJournal(t *testing.T, events ...map[string]string) []byte {
	t.Helper()
	b, err := json.Marshal(map[string]any{"events": events, "historyEvents": len(events)})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func storyboardContent(id, delta string) map[string]string {
	return map[string]string{"type": "TEXT_MESSAGE_CONTENT", "messageId": id, "delta": delta}
}

func storyboardRow(project string, issue int64, session, task, finished string, attempt, first int, text string) string {
	return fmt.Sprintf("%s %d %s %s %s %d %d %q", project, issue, session, task, finished, attempt, first, text)
}

// storyboardMessages reads the view as the publisher login, in the
// publisher's order.
func storyboardMessages(t *testing.T, reader *sql.DB) []string {
	t.Helper()
	rows, err := reader.Query(`SELECT itsaplan_project, issue_id, session_id, task_id,
			finished_at, journal_attempt, first_event, text
		FROM public.storyboard_messages
		ORDER BY session_id, finished_at, task_id, journal_attempt, first_event`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var project, session, task, text string
		var issue int64
		var attempt, first int
		var finished time.Time
		if err := rows.Scan(&project, &issue, &session, &task, &finished, &attempt, &first, &text); err != nil {
			t.Fatal(err)
		}
		out = append(out, storyboardRow(project, issue, session, task,
			finished.UTC().Format(time.RFC3339), attempt, first, text))
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

// TestStoryboardMessages checks which journal text the publisher login can
// read: assistant messages of completed tasks that the HOME binding's own
// agent ran, and nothing from another project or the security agent.
func TestStoryboardMessages(t *testing.T) {
	st := open(t)
	if _, err := st.DB.Exec(`
		INSERT INTO repo_binding (name, owner, repo, base_branch, itsaplan_project,
			kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
		VALUES ('homelab', 'org', 'homelab', 'main', 'HOME', 'factory-r-homelab', 'homelab-codex', 'factory/', 2),
			('private', 'org', 'private', 'main', 'PRIV', 'factory-r-homelab', 'homelab-codex', 'factory/', 2);
		INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key,
			kagent_namespace, kagent_agent, session_id)
		VALUES ('homelab', 213, 'HOME-213', 'factory/HOME-213', 'homelab-codex', 'factory-r-homelab', 'homelab-codex', 'session'),
			('private', 300, 'PRIV-300', 'factory/PRIV-300', 'homelab-codex', 'factory-r-homelab', 'homelab-codex', 'session');
		INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
		SELECT CASE issue_id WHEN 213 THEN 414 ELSE 415 END, 'homelab-codex', id, 'initial', '', '', 1, 'closed'
		FROM work_item;
		INSERT INTO delivery_attempt (run_id, seq, run_attempt, work_item_epoch, decision_id, kind,
			message_id, kagent_namespace, kagent_agent, context_id, task_id, state, writer_epoch,
			kagent_outcome, finished_at)
		SELECT run, n, 1, 1, 'decision-' || run || '-' || n, 'initial', 'message-' || run || '-' || n, namespace, agent, session,
			task, state, 1, outcome, finished::timestamptz
		FROM (VALUES
			(414, 1, 'factory-r-homelab', 'homelab-codex', 'session', 'finished', 'completed', 'TASK_STATE_COMPLETED', '2026-11-02T12:00:00Z'),
			-- A later attempt for the same task must not repeat its text.
			(414, 2, 'factory-r-homelab', 'homelab-codex', 'session', 'finished', 'completed', 'TASK_STATE_COMPLETED', '2026-11-02T13:00:00Z'),
			(414, 3, 'factory-r-homelab', 'homelab-codex', 'session', 'waiting', 'completed', 'TASK_STATE_INPUT_REQUIRED', '2026-11-02T12:00:00Z'),
			(414, 4, 'factory-r-homelab', 'homelab-codex', 'old', 'stale', 'completed', 'TASK_STATE_COMPLETED', '2026-11-02T10:00:00Z'),
			(414, 5, 'factory-r-homelab', 'homelab-codex', 'session', 'running', 'accepted', NULL, NULL),
			(414, 6, 'factory-r-homelab', 'homelab-codex', 'session', 'legacy', 'completed', NULL, '2026-11-02T12:00:00Z'),
			-- The security agent (HOME-235) and another agent in the binding's namespace.
			(414, 7, 'factory-intsec', 'audit', 'session', 'audit', 'completed', 'TASK_STATE_COMPLETED', '2026-11-02T12:00:00Z'),
			(414, 8, 'factory-r-homelab', 'homelab-other', 'session', 'other', 'completed', 'TASK_STATE_COMPLETED', '2026-11-02T12:00:00Z'),
			-- A private project's task with the same session and task IDs.
			(415, 1, 'factory-r-homelab', 'homelab-codex', 'session', 'finished', 'completed', 'TASK_STATE_COMPLETED', '2026-11-02T12:00:00Z')
		) AS cases(run, n, namespace, agent, session, task, state, outcome, finished)`); err != nil {
		t.Fatal(err)
	}
	reader := pgtest.AsRole(t, st.DB, "factory_storyboard")
	if got := storyboardMessages(t, reader); len(got) != 0 {
		t.Fatalf("messages without journals: %v", got)
	}
	// A completed prose segment must match the cursor for this task.
	artifactMessage := func(id string) string {
		return fmt.Sprintf("artifact/%x", sha256.Sum256([]byte("finished\x00"+id)))
	}
	finished, err := json.Marshal(map[string]any{
		"artifactText": true,
		"artifacts": []map[string]any{
			{"id": "prose", "closed": true},
			{"id": "still-open", "closed": false},
		},
		"events": []map[string]string{
			{"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
			storyboardContent("m1", "Coded "),
			storyboardContent(artifactMessage("missing"), "Unmatched text"),
			storyboardContent("m2", "a\x00b\\u0000"),
			storyboardContent("m3", "Second reply"),
			storyboardContent("m1", "it"),
			{"type": "TEXT_MESSAGE_END", "messageId": "m1"},
			{"type": "TEXT_MESSAGE_START", "messageId": artifactMessage("prose"), "role": "assistant"},
			storyboardContent(artifactMessage("prose"), "Codex prose "),
			storyboardContent(artifactMessage("still-open"), "Unfinished text"),
			{"type": "TOOL_CALL_RESULT", "messageId": artifactMessage("prose"), "delta": "Tool data"},
			storyboardContent(artifactMessage("prose"), "segment"),
			{"type": "TEXT_MESSAGE_END", "messageId": artifactMessage("prose")},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, j := range []struct {
		run     int
		attempt int
		task    string
		state   []byte
	}{
		{414, 1, "finished", finished},
		{414, 2, "finished", []byte(`{"events":null}`)},
		{414, 1, "waiting", storyboardJournal(t, storyboardContent("w", "Waiting reply"))},
		{414, 1, "stale", storyboardJournal(t, storyboardContent("s", "Old session reply"))},
		{414, 1, "running", storyboardJournal(t, storyboardContent("r", "Running reply"))},
		{414, 1, "legacy", storyboardJournal(t, storyboardContent("l", "Legacy reply"))},
		{414, 1, "audit", storyboardJournal(t, storyboardContent("a", "Security finding"))},
		{414, 1, "other", storyboardJournal(t, storyboardContent("o", "Other agent reply"))},
		{415, 1, "finished", storyboardJournal(t, storyboardContent("p", "Private project reply"))},
	} {
		if _, err := st.DB.Exec(`INSERT INTO task_transcript (run_id, attempt, task_id, base_offset, state)
			VALUES ($1, $2, $3, 0, $4)`, j.run, j.attempt, j.task, j.state); err != nil {
			t.Fatal(err)
		}
	}
	const done = "2026-11-02T12:00:00Z"
	want := []string{
		storyboardRow("HOME", 213, "old", "stale", "2026-11-02T10:00:00Z", 1, 1, "Old session reply"),
		storyboardRow("HOME", 213, "session", "finished", done, 1, 2, "Coded it"),
		storyboardRow("HOME", 213, "session", "finished", done, 1, 4, "a\ufffdb\\u0000"),
		storyboardRow("HOME", 213, "session", "finished", done, 1, 5, "Second reply"),
		storyboardRow("HOME", 213, "session", "finished", done, 1, 9, "Codex prose segment"),
	}
	got := storyboardMessages(t, reader)
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("messages =\n%s\nwant\n%s", got, want)
	}

	// The migration only adds: the chart 0.1.10 publisher's approval table
	// and digest views stay through the rollback window.
	// TestStoryboardDeliveryPrivileges covers their rows and grants.
	for _, name := range []string{"storyboard_approved_message", "storyboard_tasks", "storyboard_task_messages"} {
		var exists bool
		if err := st.DB.QueryRow(`SELECT to_regclass('public.' || $1) IS NOT NULL`, name).Scan(&exists); err != nil || !exists {
			t.Fatalf("%s is gone: %t (%v)", name, exists, err)
		}
	}
	// Another login cannot read the view, and the publisher login reads
	// neither the journal nor the tables behind the view.
	var refusal interface{ SQLState() string }
	unrelated := pgtest.AsRole(t, st.DB, "factory_reaper")
	if _, err := unrelated.Exec("SELECT * FROM public.storyboard_messages"); !errors.As(err, &refusal) || refusal.SQLState() != "42501" {
		t.Fatalf("unrelated role read of storyboard_messages: %v", err)
	}
	for _, query := range []string{
		"SELECT state FROM public.task_transcript",
		"SELECT context_id FROM public.delivery_attempt",
		"SELECT prompt FROM public.run",
		"SELECT * FROM public.repo_binding",
		"INSERT INTO public.task_transcript (run_id, attempt, task_id, base_offset, state) VALUES (415, 2, 'x', 0, '\\x7b7d')",
	} {
		if _, err := reader.Exec(query); !errors.As(err, &refusal) || refusal.SQLState() != "42501" {
			t.Errorf("%s: got %v, want insufficient_privilege (42501)", query, err)
		}
	}
}

// TestStoryboardMessagesSurviveSessionRelease releases a done work item's
// session as the connector does before it deletes the session. The view
// matches the attempt's session, so the text stays published.
func TestStoryboardMessagesSurviveSessionRelease(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	if _, err := st.DB.Exec(`
		INSERT INTO repo_binding (name, owner, repo, base_branch, itsaplan_project,
			kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
		VALUES ('homelab', 'org', 'homelab', 'main', 'HOME', 'factory', 'coder', 'factory/', 2);
		INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key,
			kagent_namespace, kagent_agent, session_id, state)
		VALUES ('homelab', 4, 'HOME-4', 'factory/HOME-4', 'coder', 'factory', 'coder', 's''1', 'done');
		INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
		SELECT 4, 'coder', id, 'initial', '', '', 1, 'closed' FROM work_item WHERE issue_id = 4;
		INSERT INTO delivery_attempt (run_id, seq, run_attempt, work_item_epoch, decision_id, kind,
			message_id, kagent_namespace, kagent_agent, context_id, task_id, state, writer_epoch,
			kagent_outcome, finished_at)
		VALUES (4, 1, 1, 1, 'decision-4', 'initial', 'message-4', 'factory', 'coder', 's''1', 't',
			'completed', 1, 'TASK_STATE_COMPLETED', '2026-11-02T12:00:00Z')`); err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB.Exec(`INSERT INTO task_transcript (run_id, attempt, task_id, base_offset, state)
		VALUES (4, 1, 't', 0, $1)`, storyboardJournal(t, storyboardContent("m1", "Ca"), storyboardContent("m1", "fé"))); err != nil {
		t.Fatal(err)
	}
	var workID int64
	if err := st.DB.QueryRow(`SELECT id FROM work_item WHERE issue_id = 4`).Scan(&workID); err != nil {
		t.Fatal(err)
	}
	reader := pgtest.AsRole(t, st.DB, "factory_storyboard")
	want := fmt.Sprint([]string{storyboardRow("HOME", 4, "s'1", "t", "2026-11-02T12:00:00Z", 1, 1, "Café")})
	if got := storyboardMessages(t, reader); fmt.Sprint(got) != want {
		t.Fatalf("messages before release = %v, want %v", got, want)
	}
	released, err := st.ReleaseSession(ctx, st.DB, workID, "s'1")
	if err != nil || !released {
		t.Fatalf("release session: %t (%v)", released, err)
	}
	var cleared bool
	if err := st.DB.QueryRow(`SELECT session_id IS NULL FROM work_item WHERE id = $1`, workID).Scan(&cleared); err != nil || !cleared {
		t.Fatalf("work item still records its session: %t (%v)", cleared, err)
	}
	if got := storyboardMessages(t, reader); fmt.Sprint(got) != want {
		t.Fatalf("messages after release = %v, want %v", got, want)
	}
}
