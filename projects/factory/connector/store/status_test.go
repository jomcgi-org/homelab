package store_test

import (
	"context"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestStatusItemsScopeStatesAndLastTenMerges(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	for _, name := range []string{"homelab", "other"} {
		if err := st.UpsertBinding(ctx, store.Binding{Name: name, Owner: "org", Repo: name,
			BaseBranch: "main", ItsaplanProject: name, KagentNamespace: "ns", KagentAgent: "coder"}); err != nil {
			t.Fatal(err)
		}
	}
	_, err := st.DB.Exec(`
		INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent, state, review_rounds)
		SELECT 'homelab', n, 'HOME-' || n, 'factory/HOME-' || n, 'coder', 'ns', 'coder',
			CASE WHEN n <= 12 THEN 'done' WHEN n = 13 THEN 'stopped' WHEN n = 14 THEN 'blocked'
				WHEN n = 15 THEN 'review' WHEN n = 16 THEN 'merging' ELSE 'active' END, 2
		FROM generate_series(1, 17) n;
		INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent)
		VALUES ('other', 99, 'OTHER-99', 'other', 'coder', 'ns', 'coder');
		INSERT INTO pull_request (work_item_id, number, node_id, head_sha, state, url, merged_at)
		SELECT id, issue_id, 'PR-' || issue_id, 'head', 'merged', 'https://github.com/org/repo/pull/' || issue_id,
			'2026-11-14T12:00:00Z'::timestamptz + issue_id * interval '1 second'
		FROM work_item WHERE issue_id <= 12 OR issue_id = 99;
	`)
	if err != nil { t.Fatal(err) }
	items, err := st.StatusItems(ctx, []string{"homelab"})
	if err != nil || len(items) != 14 { t.Fatalf("items=%+v err=%v", items, err) }
	for i, item := range items[:10] {
		if item.BindingName != "homelab" || item.IssueID != int64(12-i) || item.MergedAt == nil || item.PRNumber != 12-i {
			t.Fatalf("merge %d = %+v", i, item)
		}
	}
	states := map[string]bool{}
	for _, item := range items[10:] {
		states[item.State] = true
		if item.PRNumber != 0 || item.MergedAt != nil || item.ReviewRounds != 2 { t.Fatal(item) }
	}
	for _, state := range []string{"active", "blocked", "review", "merging"} {
		if !states[state] { t.Fatalf("missing %s", state) }
	}
	_, err = st.DB.Exec(`INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent)
		SELECT 'homelab', n, 'HOME-' || n, 'factory/HOME-' || n, 'coder', 'ns', 'coder'
		FROM generate_series(100, 210) n`)
	if err != nil { t.Fatal(err) }
	if _, err := st.StatusItems(ctx, []string{"homelab"}); err == nil { t.Fatal("oversized active listing accepted") }
}
