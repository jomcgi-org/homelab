package itsaplan

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

type seen struct {
	method, path, query, key string
	body                     map[string]any
}

func TestGateIssueWritesUseCoordinatorAndExplicitNullDelegate(t *testing.T) {
	var got seen
	c := server(t, 201, `{"id":42,"identifier":"HOME-42"}`, &got)
	body := GateIssueWrite{Title: "Merge #1 past the human gate", Description: "<!-- message-id: gate/1/create -->", ColumnID: 1, AssigneeUserID: "joe-id"}
	is, err := c.CreateGateIssue(context.Background(), "HOME", body)
	if err != nil || is.ID != 42 || got.path != "/projects/HOME/issues" || got.method != "POST" || got.key != "k-coord" {
		t.Fatalf("create = %+v %v request=%+v", is, err, got)
	}
	if delegate, ok := got.body["delegateUserId"]; !ok || delegate != nil || got.body["assigneeUserId"] != "joe-id" {
		t.Fatalf("assignment = %+v", got.body)
	}
	body.ColumnID = 5
	if err := c.UpdateGateIssue(context.Background(), 42, body); err != nil {
		t.Fatal(err)
	}
	if got.path != "/issues/42" || got.method != "PATCH" || got.key != "k-coord" || got.body["columnId"] != float64(5) {
		t.Fatalf("update = %+v", got)
	}
}

func TestGateIssueSearchIncludesArchivedHits(t *testing.T) {
	marker := "<!-- factory-human-gate/owner/repo/1 -->"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "k-coord" || r.Method != "GET" {
			t.Errorf("unexpected request method or identity")
		}
		switch r.URL.Path {
		case "/projects/HOME/issues/search":
			if r.URL.Query().Get("q") != marker || r.URL.Query().Get("limit") != "500" {
				t.Errorf("search query = %s", r.URL.RawQuery)
			}
			_, _ = io.WriteString(w, `[{"id":1},{"id":2,"archived":true}]`)
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Coordinator: "coord", Keys: map[string]string{"coord": "k-coord"}}
	hits, err := c.SearchGateIssues(context.Background(), "HOME", marker)
	if err != nil || len(hits) != 2 || hits[1].ID != 2 {
		t.Fatalf("search = %+v %v", hits, err)
	}
}

func TestGateProjectDecodesHumanAssignees(t *testing.T) {
	var got seen
	c := server(t, 200, `{"columns":[{"id":1,"name":"Todo","stateType":"unstarted"}],"assignees":[{"userId":"joe-id","username":"joe","kind":"member"}]}`, &got)
	p, err := c.GetProject(context.Background(), "HOME")
	if err != nil || len(p.Assignees) != 1 || p.Assignees[0].UserID != "joe-id" || len(p.Columns) != 1 || got.key != "k-coord" || got.path != "/projects/HOME" {
		t.Fatalf("project = %+v %v, request=%+v", p, err, got)
	}
}

func server(t *testing.T, status int, reply string, got *seen) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.method, got.path, got.query, got.key = r.Method, r.URL.Path, r.URL.RawQuery, r.Header.Get("x-api-key")
		b, _ := io.ReadAll(r.Body)
		got.body = nil
		if len(b) > 0 {
			if err := json.Unmarshal(b, &got.body); err != nil {
				t.Errorf("body: %v", err)
			}
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, reply)
	}))
	t.Cleanup(srv.Close)
	return &Client{BaseURL: srv.URL, Keys: map[string]string{"coder": "k-coder", "coord": "k-coord"}, Coordinator: "coord"}
}

func TestClaimDecodesRun(t *testing.T) {
	var got seen
	c := server(t, 200, `{"run":{"id":7,"trigger":"mention","prompt":"p","systemPrompt":"s","attempts":2,"traceEvents":true,"issueId":41,"issueIdentifier":"HL-3"}}`, &got)
	r, err := c.Claim(context.Background(), "coder")
	if err != nil {
		t.Fatal(err)
	}
	if got.method != "POST" || got.path != "/agent-runs/claim" || got.key != "k-coder" {
		t.Fatalf("request = %+v", got)
	}
	if r.ID != 7 || r.Attempts != 2 || *r.IssueID != 41 || r.IssueIdentifier != "HL-3" {
		t.Fatalf("run = %+v", r)
	}
}

func TestClaimEmptyQueue(t *testing.T) {
	var got seen
	c := server(t, 200, `{"run":null}`, &got)
	r, err := c.Claim(context.Background(), "coder")
	if err != nil || r != nil {
		t.Fatalf("run = %v err = %v", r, err)
	}
}

func TestHeartbeatAlwaysSendsAttempt(t *testing.T) {
	var got seen
	c := server(t, 204, "", &got)
	if err := c.Heartbeat(context.Background(), "coder", 7, 3); err != nil {
		t.Fatal(err)
	}
	if got.path != "/agent-runs/7/heartbeat" || got.body["attempt"] != float64(3) {
		t.Fatalf("request = %+v", got)
	}
}

func TestHeartbeatNotFound(t *testing.T) {
	var got seen
	c := server(t, 404, `{"message":"Run not found"}`, &got)
	if err := c.Heartbeat(context.Background(), "coder", 7, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("err = %v", err)
	}
}

func TestEventsOffsetAndConflict(t *testing.T) {
	var got seen
	c := server(t, 200, `{"nextOffset":5,"truncated":false}`, &got)
	next, err := c.Events(context.Background(), "coder", 7, 2, 3, []Event{{Type: "TEXT_MESSAGE_END", MessageID: "m"}, {Type: "TEXT_MESSAGE_END", MessageID: "n"}})
	if err != nil || next != 5 {
		t.Fatalf("next = %d err = %v", next, err)
	}
	if got.body["attempt"] != float64(2) || got.body["offset"] != float64(3) {
		t.Fatalf("body = %+v", got.body)
	}
	c = server(t, 409, `{"message":"Unexpected trace offset"}`, &got)
	if _, err := c.Events(context.Background(), "coder", 7, 2, 9, []Event{{Type: "TEXT_MESSAGE_END", MessageID: "m"}}); !errors.Is(err, ErrConflict) {
		t.Fatalf("err = %v", err)
	}
}

func TestResultSendsAttemptAndUsage(t *testing.T) {
	var got seen
	c := server(t, 204, "", &got)
	err := c.Result(context.Background(), "coder", 7, Result{Attempt: 2, Status: "success", Output: "ok", Usage: &Usage{InputTokens: 10, OutputTokens: 2}})
	if err != nil {
		t.Fatal(err)
	}
	if got.path != "/agent-runs/7/result" || got.body["attempt"] != float64(2) || got.body["status"] != "success" {
		t.Fatalf("request = %+v", got)
	}
	u, _ := got.body["usage"].(map[string]any)
	if u["inputTokens"] != float64(10) || u["outputTokens"] != float64(2) {
		t.Fatalf("usage = %+v", got.body["usage"])
	}
}

func TestGetRunUsesCoordinatorAndQueue(t *testing.T) {
	var got seen
	c := server(t, 200, `{"items":[{"id":7,"status":"canceled","executionState":"canceled","attempts":2}],"total":1,"page":1,"pageSize":25,"queued":0,"running":0}`, &got)
	r, err := c.GetRun(context.Background(), 7)
	if err != nil {
		t.Fatal(err)
	}
	if got.method != "GET" || got.path != "/agent-runs/queue" || got.query != "runId=7" || got.key != "k-coord" {
		t.Fatalf("request = %+v", got)
	}
	if r.Status != "canceled" || r.Attempts != 2 {
		t.Fatalf("run = %+v", r)
	}
	c = server(t, 200, `{"items":[],"total":0,"page":1,"pageSize":25,"queued":0,"running":0}`, &got)
	if _, err := c.GetRun(context.Background(), 7); !errors.Is(err, ErrNotFound) {
		t.Fatalf("hidden run err = %v", err)
	}
}

func TestComment(t *testing.T) {
	var got seen
	c := server(t, 201, `{}`, &got)
	if err := c.Comment(context.Background(), 41, "hello"); err != nil {
		t.Fatal(err)
	}
	if got.path != "/issues/41/comments" || got.key != "k-coord" || got.body["body"] != "hello" {
		t.Fatalf("request = %+v", got)
	}
}

// These fields follow itsaplan v1.10.0's issue and attachment responses.
func TestGetIssueDecodesStructuredContext(t *testing.T) {
	var got seen
	c := server(t, 200, `{
		"id":234,"identifier":"HOME-126","title":"Show the ticket","description":"Refs HOME-126","columnId":3,
		"checklists":[{"id":1,"title":"Done when","position":0,"items":[{"id":2,"content":"Verify the brief","done":true,"position":1.5}]}],
		"subtasks":[{"id":235,"identifier":"HOME-127","title":"Add tests","columnId":2,"archived":true}],
		"parent":{"id":230,"identifier":"HOME-122","title":"Improve tickets","columnId":3},
		"links":[{"kind":"blocks","direction":"inward","issue":{"identifier":"HOME-125","title":"Define the API","columnId":4}}],
		"development":[
			{"kind":"branch","repository":"jomcgi-org/homelab","number":null,"title":"factory/HOME-126","url":null,"state":"open","draft":false},
			{"kind":"pull_request","repository":"jomcgi-org/homelab","number":42,"title":"Show the ticket","url":"https://github.com/jomcgi-org/homelab/pull/42","state":"open","draft":true}
		]
	}`, &got)
	issue, err := c.GetIssue(context.Background(), 234)
	if err != nil {
		t.Fatal(err)
	}
	if got.method != "GET" || got.path != "/issues/234" || got.key != "k-coord" {
		t.Fatalf("request = %+v", got)
	}
	want := &Issue{
		ID: 234, Identifier: "HOME-126", Title: "Show the ticket", Description: "Refs HOME-126", ColumnID: 3,
		Checklists: []Checklist{{ID: 1, Title: "Done when", Items: []ChecklistItem{{ID: 2, Content: "Verify the brief", Done: true, Position: 1.5}}}},
		Subtasks:   []IssueRef{{ID: 235, Identifier: "HOME-127", Title: "Add tests", ColumnID: 2, Archived: true}},
		Parent:     &IssueRef{ID: 230, Identifier: "HOME-122", Title: "Improve tickets", ColumnID: 3},
		Links:      []IssueLink{{Kind: "blocks", Direction: "inward", Issue: IssueRef{Identifier: "HOME-125", Title: "Define the API", ColumnID: 4}}},
		Development: []DevelopmentLink{
			{Kind: "branch", State: "open"},
			{Kind: "pull_request", Number: 42, State: "open", Draft: true},
		},
	}
	if !reflect.DeepEqual(issue, want) {
		t.Fatalf("issue = %+v, want %+v", issue, want)
	}
	c = server(t, 200, `{"id":234,"title":"Plain ticket","description":"Do it.","parent":null}`, &got)
	issue, err = c.GetIssue(context.Background(), 234)
	if err != nil || !reflect.DeepEqual(issue, &Issue{ID: 234, Title: "Plain ticket", Description: "Do it."}) {
		t.Fatalf("plain issue = %+v, %v", issue, err)
	}
}

func TestGetAttachmentsReadsOnlyNames(t *testing.T) {
	var got seen
	c := server(t, 200, `[{"id":"public-id","filename":"failure.log","contentType":"text/plain","sizeBytes":123,"url":"/attachments/public-id/raw"}]`, &got)
	attachments, err := c.GetAttachments(context.Background(), 234)
	if err != nil {
		t.Fatal(err)
	}
	if got.method != "GET" || got.path != "/issues/234/attachments" || got.key != "k-coord" {
		t.Fatalf("request = %+v", got)
	}
	if !reflect.DeepEqual(attachments, []Attachment{{Filename: "failure.log"}}) {
		t.Fatalf("attachments = %+v", attachments)
	}
	c = server(t, 200, `[]`, &got)
	if attachments, err = c.GetAttachments(context.Background(), 234); err != nil || len(attachments) != 0 {
		t.Fatalf("empty attachments = %+v, %v", attachments, err)
	}
	for _, status := range []int{403, 404, 503} {
		c = server(t, status, `{}`, &got)
		if _, err := c.GetAttachments(context.Background(), 234); err == nil {
			t.Fatalf("status %d succeeded", status)
		}
	}
}

func TestProjectColumnsAndMoveIssueUseTheCoordinator(t *testing.T) {
	var got seen
	c := server(t, 200, `{"id":1,"key":"HL","columns":[{"id":4,"name":"Doing","stateType":"started"},{"id":9,"name":"Done","stateType":"completed"}]}`, &got)
	cols, err := c.ProjectColumns(context.Background(), "HL")
	if err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodGet || got.path != "/projects/HL" || got.key != "k-coord" {
		t.Fatalf("request = %+v", got)
	}
	if len(cols) != 2 || cols[1].ID != 9 || cols[1].StateType != "completed" {
		t.Fatalf("columns = %+v", cols)
	}
	if err := c.MoveIssue(context.Background(), 41, 9); err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodPatch || got.path != "/issues/41" || got.key != "k-coord" || got.body["columnId"] != float64(9) {
		t.Fatalf("request = %+v", got)
	}
}

func TestKeyFuncIsAskedPerCall(t *testing.T) {
	var got seen
	c := server(t, http.StatusOK, `{"run":null}`, &got)
	key := "k-1"
	c.KeyFunc = func(agent string) (string, error) {
		if agent != "coder" {
			return "", errors.New("unknown agent")
		}
		return key, nil
	}
	for _, want := range []string{"k-1", "k-2"} {
		key = want
		if _, err := c.Claim(context.Background(), "coder"); err != nil {
			t.Fatal(err)
		}
		if got.key != want {
			t.Fatalf("key = %q, want %q", got.key, want)
		}
	}
	if _, err := c.Claim(context.Background(), "stranger"); err == nil {
		t.Fatal("claim without a key succeeded")
	}
}
