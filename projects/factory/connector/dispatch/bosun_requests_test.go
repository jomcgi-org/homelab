package dispatch_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

// FileRequest models the keyed create: one issue per key.
func (f *fakeItsaplan) FileRequest(_ context.Context, project, key string, n itsaplan.NewIssue) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.requestErr != nil {
		return 0, f.requestErr
	}
	if id, ok := f.requestKeys[key]; ok {
		return id, nil
	}
	if f.requestKeys == nil {
		f.requestKeys = map[string]int64{}
	}
	id := int64(900 + len(f.requestKeys))
	f.issues[id] = &itsaplan.Issue{ID: id, Identifier: fmt.Sprintf("%s-%d", project, id), Title: n.Title, Description: n.Description, ColumnID: n.ColumnID}
	f.requestKeys[key] = id
	return id, nil
}

type bosunEnv struct{ *env }

func newBosunEnv(t *testing.T) *bosunEnv {
	t.Helper()
	v := newEnv(t)
	v.cfg.BosunRequests = "homelab"
	v.its.deferredProject = &itsaplan.Project{}
	if err := json.Unmarshal([]byte(`{"project":{"id":7},"columns":[{"id":1,"name":"Todo","stateType":"unstarted"},{"id":2,"name":"In Progress","stateType":"started"},{"id":3,"name":"Done","stateType":"completed"},{"id":4,"name":"Canceled","stateType":"canceled"}],"assignees":[{"userId":"joe-id","username":"coder","kind":"member"},{"userId":"coder-id","username":"coder","kind":"agent"}]}`), v.its.deferredProject); err != nil {
		t.Fatal(err)
	}
	v.e.Release()
	v.e = v.engine("connector-a")
	return &bosunEnv{v}
}

func (v *bosunEnv) request(message string) int64 {
	v.t.Helper()
	var id int64
	// next_check_at is on the test clock, which is behind the database's.
	err := v.st.DB.QueryRowContext(v.ctx, `INSERT INTO bosun_request (guild_id, channel_id, message_id, author_id, title, description, next_check_at)
		VALUES ('1501965852042330302', '1575140752776757368', $1, '42', 'Add a status page', 'Joe asked for a status page.', $2) RETURNING id`, message, v.clk.Now()).Scan(&id)
	if err != nil {
		v.t.Fatal(err)
	}
	return id
}

func (v *bosunEnv) state(id int64) (state, key, column string) {
	v.t.Helper()
	if err := v.st.DB.QueryRowContext(v.ctx, `SELECT state, coalesce(issue_key, ''), coalesce(column_name, '') FROM bosun_request WHERE id = $1`, id).Scan(&state, &key, &column); err != nil {
		v.t.Fatal(err)
	}
	return state, key, column
}

func (v *bosunEnv) issue(id int64) *itsaplan.Issue {
	return v.its.issues[v.its.requestKeys[fmt.Sprintf("bosun-request/%d", id)]]
}

func TestBosunRequestFilesDelegatesAndFollows(t *testing.T) {
	v := newBosunEnv(t)
	id := v.request("1600000000000000001")
	v.tick(v.e)

	state, key, column := v.state(id)
	if state != "filed" || key == "" || column != "Todo" {
		t.Fatalf("state=%s key=%s column=%s", state, key, column)
	}
	issue := v.issue(id)
	if issue.DelegateUserID == nil || *issue.DelegateUserID != "coder-id" || issue.ColumnID != 1 {
		t.Fatalf("issue not delegated in Todo: %+v", issue)
	}
	if !strings.HasPrefix(issue.Description, "Put `Refs "+key+"` in the commit message.") ||
		!strings.Contains(issue.Description, "https://discord.com/channels/1501965852042330302/1575140752776757368/1600000000000000001") {
		t.Fatalf("description = %q", issue.Description)
	}

	// A restart files nothing new.
	v.e.Release()
	v.e = v.engine("connector-b")
	issue.ColumnID = 2
	v.clk.Advance(3 * time.Minute)
	v.tick(v.e)
	if state, _, column := v.state(id); len(v.its.requestKeys) != 1 || state != "filed" || column != "In Progress" {
		t.Fatalf("creates=%d state=%s column=%s", len(v.its.requestKeys), state, column)
	}

	issue.ColumnID = 3
	v.clk.Advance(3 * time.Minute)
	v.tick(v.e)
	if state, _, column := v.state(id); state != "done" || column != "Done" {
		t.Fatalf("state=%s column=%s", state, column)
	}
}

func TestBosunRequestCanceled(t *testing.T) {
	v := newBosunEnv(t)
	id := v.request("1600000000000000002")
	v.tick(v.e)
	v.issue(id).ColumnID = 4
	v.clk.Advance(3 * time.Minute)
	v.tick(v.e)
	if state, _, _ := v.state(id); state != "canceled" {
		t.Fatalf("state = %s", state)
	}
}

// A lost response after the create replays the same key and delegates the
// first issue once.
func TestBosunRequestReplayAfterCreate(t *testing.T) {
	v := newBosunEnv(t)
	id := v.request("1600000000000000004")
	if _, err := v.its.FileRequest(v.ctx, "HL", fmt.Sprintf("bosun-request/%d", id), itsaplan.NewIssue{Title: "t", Description: "d", ColumnID: 1}); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	if state, _, _ := v.state(id); state != "filed" || len(v.its.requestKeys) != 1 || v.issue(id).DelegateUserID == nil {
		t.Fatalf("state=%s creates=%d", state, len(v.its.requestKeys))
	}
}

func TestBosunRequestRetriesThenFails(t *testing.T) {
	v := newBosunEnv(t)
	id := v.request("1600000000000000003")
	v.its.requestErr = errors.New("itsaplan down")
	for range 8 {
		_ = v.e.Tick(v.ctx)
		v.clk.Advance(2 * time.Hour)
	}
	if state, _, _ := v.state(id); state != "failed" {
		t.Fatalf("state = %s", state)
	}
}

func TestBosunRequestsOffByDefault(t *testing.T) {
	v := newEnv(t)
	var id int64
	if err := v.st.DB.QueryRowContext(v.ctx, `INSERT INTO bosun_request (guild_id, channel_id, message_id, author_id, title, description)
		VALUES ('1', '2', '3', '4', 't', 'd') RETURNING id`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	v.clk.Advance(30 * 24 * time.Hour)
	v.tick(v.e)
	var state string
	if err := v.st.DB.QueryRowContext(v.ctx, `SELECT state FROM bosun_request WHERE id = $1`, id).Scan(&state); err != nil || state != "pending" {
		t.Fatalf("state=%s err=%v", state, err)
	}
}

// The bosun-requests login may add a request and read status, nothing else.
func TestBosunRequestsLoginGrants(t *testing.T) {
	v := newEnv(t)
	db := pgtest.AsRole(t, v.st.DB, "factory_bosun_requests")
	var id int64
	if err := db.QueryRowContext(v.ctx, `INSERT INTO bosun_request (guild_id, channel_id, message_id, author_id, title, description)
		VALUES ('1', '2', '3', '4', 't', 'd') ON CONFLICT (message_id) DO NOTHING RETURNING id`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(v.ctx, `SELECT state, issue_key, pr_number FROM bosun_request_status WHERE id = $1`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(v.ctx, `SELECT id FROM bosun_request WHERE message_id = '3'`); err != nil {
		t.Fatal(err)
	}
	// A reply is marked only on a finished request.
	if _, err := db.ExecContext(v.ctx, `UPDATE bosun_request SET replied_at = now() WHERE id = $1 AND replied_at IS NULL`, id); err == nil {
		t.Fatal("a pending request was marked replied")
	}
	if _, err := v.st.DB.ExecContext(v.ctx, `UPDATE bosun_request SET state = 'filed', issue_id = 1 WHERE id = $1`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := v.st.DB.ExecContext(v.ctx, `UPDATE bosun_request SET state = 'done' WHERE id = $1`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(v.ctx, `UPDATE bosun_request SET replied_at = now() WHERE id = $1 AND replied_at IS NULL`, id); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{
		`UPDATE bosun_request SET state = 'done'`,
		`SELECT description FROM bosun_request`,
		`SELECT id FROM work_item`,
		`DELETE FROM bosun_request`,
		`INSERT INTO bosun_request (guild_id, channel_id, message_id, author_id, title, description, state)
			VALUES ('1', '2', '5', '4', 't', 'd', 'done')`,
	} {
		if _, err := db.ExecContext(v.ctx, q); err == nil {
			t.Fatalf("allowed: %s", q)
		}
	}
}
