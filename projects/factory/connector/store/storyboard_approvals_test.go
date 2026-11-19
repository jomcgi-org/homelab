package store_test

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

// approvalsSQL reads SQL that projects/factory-storyboard/approvals.py
// prints; approvals_test.py keeps these files equal to its output.
func approvalsSQL(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "..", "factory-storyboard", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// TestStoryboardApprovalsImport runs the owner's approval SQL as psql does:
// one connection per input, closed at its end, so an unfinished
// transaction rolls back.
func TestStoryboardApprovalsImport(t *testing.T) {
	st := open(t)
	run := func(sql string) error {
		t.Helper()
		owner := pgtest.AsRole(t, st.DB, "factory_connector")
		defer owner.Close()
		_, err := owner.Exec(sql)
		return err
	}
	rows := func() []string {
		t.Helper()
		got, err := st.DB.Query(`SELECT itsaplan_project, issue_key, session_id, task_id, text_sha256
			FROM storyboard_approved_message`)
		if err != nil {
			t.Fatal(err)
		}
		defer got.Close()
		var out []string
		for got.Next() {
			var project, issue, session, task, digest string
			if err := got.Scan(&project, &issue, &session, &task, &digest); err != nil {
				t.Fatal(err)
			}
			out = append(out, strings.Join([]string{project, issue, session, task, digest}, " "))
		}
		if err := got.Err(); err != nil {
			t.Fatal(err)
		}
		slices.Sort(out)
		return out
	}
	mismatch := func(err error) bool {
		return err != nil && strings.Contains(err.Error(), "storyboard approvals for HOME do not match")
	}
	sum := func(text string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(text))) }
	stale := "HOME HOME-9 old old " + strings.Repeat("0", 64)
	other := "PRIV PRIV-1 s t " + strings.Repeat("1", 64)
	if _, err := st.DB.Exec(`INSERT INTO storyboard_approved_message VALUES
		('HOME', 'HOME-9', 'old', 'old', repeat('0', 64)),
		('PRIV', 'PRIV-1', 's', 't', repeat('1', 64))`); err != nil {
		t.Fatal(err)
	}
	before := []string{stale, other}
	approved := []string{
		"HOME HOME-4 s'1 t " + sum("Café"),
		"HOME HOME-4 s'1 t " + sum("Done"),
		"HOME HOME-5 s2 review " + sum("Reviewed"),
		other,
	}
	slices.Sort(approved)

	full := approvalsSQL(t, "approvals.sql")
	verify := approvalsSQL(t, "approvals_verify.sql")
	// A lost INSERT leaves the table short of the expected set; the check
	// raises before COMMIT.
	lines := strings.Split(full, "\n")
	first := slices.IndexFunc(lines, func(l string) bool { return strings.HasPrefix(l, "INSERT") })
	short := strings.Join(slices.Delete(slices.Clone(lines), first, first+1), "\n")
	if err := run(short); !mismatch(err) {
		t.Fatalf("import without one row: %v", err)
	}
	// Input cut before its COMMIT never commits.
	truncated, found := strings.CutSuffix(full, "COMMIT;\n")
	if !found {
		t.Fatal("approvals.sql must end with COMMIT")
	}
	if err := run(truncated); err != nil {
		t.Fatal(err)
	}
	if got := rows(); !slices.Equal(got, before) {
		t.Fatalf("failed imports changed approvals: %v", got)
	}
	if err := run(verify); !mismatch(err) {
		t.Fatalf("verify before import: %v", err)
	}

	if err := run(full); err != nil {
		t.Fatal(err)
	}
	if got := rows(); !slices.Equal(got, approved) {
		t.Fatalf("approvals = %v, want %v", got, approved)
	}
	if err := run(verify); err != nil {
		t.Fatal(err)
	}
	// The read-only check notices a later change.
	if _, err := st.DB.Exec(`DELETE FROM storyboard_approved_message WHERE issue_key = 'HOME-5'`); err != nil {
		t.Fatal(err)
	}
	if err := run(verify); !mismatch(err) {
		t.Fatalf("verify after a lost row: %v", err)
	}

	// --allow-empty withdraws every approval of the project only.
	if err := run(approvalsSQL(t, "approvals_empty.sql")); err != nil {
		t.Fatal(err)
	}
	if got := rows(); !slices.Equal(got, []string{other}) {
		t.Fatalf("approvals after withdrawal = %v", got)
	}
}

// TestStoryboardApprovalSurvivesSessionRelease approves a done work item's
// messages, then releases its session as the connector does before it
// deletes the session. The views match the session and task in the approval,
// so they still confirm the approved digests until the owner withdraws them.
func TestStoryboardApprovalSurvivesSessionRelease(t *testing.T) {
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
			kagent_outcome)
		VALUES (4, 1, 1, 1, 'decision-4', 'initial', 'message-4', 'factory', 'coder', 's''1', 't',
			'completed', 1, 'TASK_STATE_COMPLETED');
		INSERT INTO task_transcript (run_id, attempt, task_id, base_offset, state)
		VALUES (4, 1, 't', 0, convert_to('{"events": [
			{"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Ca"},
			{"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "fé"},
			{"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "Done"},
			{"type": "TEXT_MESSAGE_CONTENT", "messageId": "m3", "delta": "Private reply"}]}', 'UTF8'))`); err != nil {
		t.Fatal(err)
	}
	var workID int64
	if err := st.DB.QueryRow(`SELECT id FROM work_item WHERE issue_id = 4`).Scan(&workID); err != nil {
		t.Fatal(err)
	}
	importSQL := func(name string) {
		t.Helper()
		owner := pgtest.AsRole(t, st.DB, "factory_connector")
		defer owner.Close()
		if _, err := owner.Exec(approvalsSQL(t, name)); err != nil {
			t.Fatal(err)
		}
	}
	reader := pgtest.AsRole(t, st.DB, "factory_storyboard")
	views := func() []string {
		t.Helper()
		rows, err := reader.Query(`
			SELECT 'task', issue_id, session_id, task_id, '' FROM public.storyboard_tasks
			UNION ALL
			SELECT 'message', issue_id, session_id, task_id, text_sha256 FROM public.storyboard_task_messages`)
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		var out []string
		for rows.Next() {
			var kind, session, task, digest string
			var issue int
			if err := rows.Scan(&kind, &issue, &session, &task, &digest); err != nil {
				t.Fatal(err)
			}
			out = append(out, strings.TrimSpace(fmt.Sprintf("%s %d %s %s %s", kind, issue, session, task, digest)))
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		slices.Sort(out)
		return out
	}
	sum := func(text string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(text))) }
	// The unapproved reply stays invisible throughout.
	want := []string{
		"message 4 s'1 t " + sum("Café"),
		"message 4 s'1 t " + sum("Done"),
		"task 4 s'1 t",
	}
	slices.Sort(want)

	importSQL("approvals.sql")
	if got := views(); !slices.Equal(got, want) {
		t.Fatalf("views before release = %v, want %v", got, want)
	}
	released, err := st.ReleaseSession(ctx, st.DB, workID, "s'1")
	if err != nil || !released {
		t.Fatalf("release session: %t (%v)", released, err)
	}
	var cleared bool
	if err := st.DB.QueryRow(`SELECT session_id IS NULL FROM work_item WHERE id = $1`, workID).Scan(&cleared); err != nil || !cleared {
		t.Fatalf("work item still records its session: %t (%v)", cleared, err)
	}
	if got := views(); !slices.Equal(got, want) {
		t.Fatalf("views after release = %v, want %v", got, want)
	}
	// Withdrawal through approvals.py removes the task from both views.
	importSQL("approvals_empty.sql")
	if got := views(); len(got) != 0 {
		t.Fatalf("views after withdrawal = %v", got)
	}
}
