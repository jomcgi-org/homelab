package store_test

import (
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
