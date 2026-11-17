package store_test

import (
	"database/sql"
	_ "embed"
	"reflect"
	"testing"
)

// Execute the same query documented for operators, so filtering and totals
// cannot silently drift away from the tested report.
//
//go:embed delivered_issue_usage.sql
var deliveredIssueUsage string

func TestDeliveredIssueUsage(t *testing.T) {
	st := open(t)
	_, err := st.DB.Exec(`
		INSERT INTO repo_binding (name, owner, repo, base_branch, itsaplan_project,
			kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
		VALUES ('homelab', 'org', 'homelab', 'main', 'HOME', 'factory', 'coder', 'factory/', 2);
		INSERT INTO work_item (id, binding_name, issue_id, issue_key, branch, agent_key,
			kagent_namespace, kagent_agent, state)
		VALUES (1, 'homelab', 101, 'HOME-101', 'factory/HOME-101', 'coder', 'factory', 'coder', 'done'),
		       (2, 'homelab', 102, 'HOME-102', 'factory/HOME-102', 'coder', 'factory', 'coder', 'done'),
		       (3, 'homelab', 103, 'HOME-103', 'factory/HOME-103', 'coder', 'factory', 'coder', 'active'),
		       (4, 'homelab', 104, 'HOME-104', 'factory/HOME-104', 'coder', 'factory', 'coder', 'review'),
		       (5, 'homelab', 105, 'HOME-105', 'factory/HOME-105', 'coder', 'factory', 'coder', 'stopped'),
		       (6, 'homelab', 106, 'HOME-106', 'factory/HOME-106', 'coder', 'factory', 'coder', 'done'),
		       (7, 'homelab', 107, 'HOME-107', 'factory/HOME-107', 'coder', 'factory', 'coder', 'done'),
		       (8, 'homelab', 108, 'HOME-108', 'factory/HOME-108', 'coder', 'factory', 'coder', 'done'),
		       (9, 'homelab', 109, 'HOME-109', 'factory/HOME-109', 'coder', 'factory', 'coder', 'done');
		INSERT INTO run (id, work_item_id, agent_key, trigger, prompt, system_prompt, attempt,
			state, closed_reason, input_tokens, cached_input_tokens, output_tokens)
		VALUES (1, 1, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 100, 50, 10),
		       (2, 1, 'coder', 'issue_delegated', '', '', 1, 'closed', 'failed', 200, 100, 20),
		       (-1, 1, 'coder', 'review', '', '', 1, 'closed', 'success', 300, 150, 30),
		       (3, 1, 'coder', 'issue_delegated', '', '', 1, 'closed', 'canceled', NULL, NULL, NULL),
		       (4, 2, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 400, 200, 40),
		       (5, 3, 'coder', 'issue_delegated', '', '', 1, 'dispatched', NULL, 9000, 0, 900),
		       (6, 4, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900),
		       (7, 5, 'coder', 'issue_delegated', '', '', 1, 'closed', 'canceled', 9000, 0, 900),
		       (8, 6, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', NULL, NULL, NULL),
		       (9, 7, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900),
		       (10, 8, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900),
		       (11, 9, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900);
		UPDATE run SET origin = 'connector' WHERE id = -1;
		INSERT INTO outbox (kind, dedupe_key, payload, delivered_at, abandoned_at)
		VALUES ('issue_done', 'issue-done/1', '{"issueId":101}', now(), NULL),
		       ('issue_done', 'issue-done/2', '{"issueId":102}', now(), NULL),
		       ('issue_done', 'issue-done/6', '{"issueId":106}', now(), NULL),
		       ('issue_done', 'issue-done/7', '{"issueId":107}', NULL, NULL),
		       ('issue_done', 'issue-done/8', '{"issueId":108}', NULL, now()),
		       ('comment', 'comment/merged/9', '{"issueId":109}', now(), NULL);
	`)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := st.DB.Query(deliveredIssueUsage)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	type report struct {
		issue                        string
		runs, unmeasured             int64
		input, cached, output, total sql.NullInt64
	}
	var got []report
	for rows.Next() {
		var r report
		if err := rows.Scan(&r.issue, &r.runs, &r.unmeasured, &r.input, &r.cached, &r.output, &r.total); err != nil {
			t.Fatal(err)
		}
		got = append(got, r)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	measured := func(n int64) sql.NullInt64 { return sql.NullInt64{Int64: n, Valid: true} }
	want := []report{
		{issue: "HOME-101", runs: 4, unmeasured: 1, input: measured(600), cached: measured(300), output: measured(60), total: measured(660)},
		{issue: "HOME-102", runs: 1, input: measured(400), cached: measured(200), output: measured(40), total: measured(440)},
		{issue: "HOME-106", runs: 1, unmeasured: 1},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("delivered-issue report = %+v, want %+v", got, want)
	}
}
