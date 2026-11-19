package itsaplan

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"testing"
	"time"
)

func TestMajorIssueWritesUseCoordinatorAndRealDelegateUserID(t *testing.T) {
	var got seen
	c := server(t, 200, `{"permissions":{"work_items":{"read":true,"create":true,"edit":true}},"columns":[{"id":2,"name":"Todo","stateType":"unstarted"}],"assignees":[{"userId":"user-coder","username":"homelab-codex","kind":"agent"}]}`, &got)
	p, err := c.GetProject(context.Background(), "HOME")
	if err != nil || !p.Permissions.WorkItems.Create || len(p.Assignees) != 1 || p.Assignees[0].UserID != "user-coder" || got.path != "/projects/HOME" || got.key != "k-coord" {
		t.Fatalf("project = %+v, %v; request = %+v", p, err, got)
	}
	c = server(t, 201, `{"id":62,"identifier":"HOME-62","delegateUserId":null}`, &got)
	is, err := c.CreateIssue(context.Background(), "HOME", NewIssue{Title: "Land Renovate major", Description: "brief", ColumnID: 2})
	if err != nil || is.ID != 62 || is.Identifier != "HOME-62" || is.DelegateUserID != nil || got.path != "/projects/HOME/issues" || got.key != "k-coord" {
		t.Fatalf("create = %+v, %v; request = %+v", is, err, got)
	}
	if _, ok := got.body["delegateUserId"]; ok {
		t.Fatal("create delegated before the issue key was known")
	}
	description, delegate := "Put `Refs HOME-62` in the commit message.\n\nbrief", p.Assignees[0].UserID
	c = server(t, 200, `{}`, &got)
	if err := c.UpdateIssue(context.Background(), 62, IssueUpdate{Description: &description, DelegateUserID: &delegate}); err != nil {
		t.Fatal(err)
	}
	if got.method != "PATCH" || got.path != "/issues/62" || got.key != "k-coord" || got.body["description"] != description || got.body["delegateUserId"] != "user-coder" {
		t.Fatalf("delegate request = %+v", got)
	}
	if _, ok := got.body["columnId"]; ok {
		t.Fatal("delegation unexpectedly moved the issue")
	}
}

func TestMajorIssueSearchAndIncompleteCreation(t *testing.T) {
	var got seen
	c := server(t, 200, `[{"id":62,"identifier":"HOME-62","archived":true}]`, &got)
	marker := "<!-- factory-renovate:abc -->"
	hits, err := c.SearchIssues(context.Background(), "HOME", marker)
	q, _ := url.ParseQuery(got.query)
	if err != nil || len(hits) != 1 || hits[0].ID != 62 || got.path != "/projects/HOME/issues/search" || q.Get("q") != marker || q.Get("limit") != "500" || got.key != "k-coord" {
		t.Fatalf("search = %+v, %v; request = %+v", hits, err, got)
	}
	for _, reply := range []string{`{}`, `null`, `{"id":62}`} {
		c = server(t, 201, reply, &got)
		if _, err := c.CreateIssue(context.Background(), "HOME", NewIssue{}); err == nil {
			t.Fatalf("incomplete create acknowledged: %s", reply)
		}
	}
	c = server(t, 200, `null`, &got)
	if _, err := c.SearchIssues(context.Background(), "HOME", marker); err == nil {
		t.Fatal("null search treated as evidence of absence")
	}
}

func TestGetIssueDecodesNullableArchivedAt(t *testing.T) {
	for _, archived := range []bool{false, true} {
		t.Run(fmt.Sprint(archived), func(t *testing.T) {
			var got seen
			stamp := "null"
			if archived {
				stamp = `"2026-11-15T12:34:56.000Z"`
			}
			// GET /issues/{id} has archivedAt; only search hits have archived.
			c := server(t, 200, `{"id":62,"identifier":"HOME-62","columnId":1,"archivedAt":`+stamp+`}`, &got)
			is, err := c.GetIssue(context.Background(), 62)
			if err != nil || is.ID != 62 || (is.ArchivedAt != nil) != archived || got.path != "/issues/62" || got.key != "k-coord" {
				t.Fatalf("issue = %+v, %v; request = %+v", is, err, got)
			}
			if archived && is.ArchivedAt.Format(time.RFC3339) != "2026-11-15T12:34:56Z" {
				t.Fatalf("archivedAt = %v", is.ArchivedAt)
			}
		})
	}
}

type seen struct {
	method, path, query, key string
	body                     map[string]any
}

func TestDeferredWIPConflictIsDistinct(t *testing.T) {
	for _, tc := range []struct {
		body string
		wip  bool
	}{
		{`{"error":"Column is full","code":"wip_limit_exceeded"}`, true},
		{`{"error":"Issue is no longer eligible"}`, false},
		{`{"error":"wip_limit_exceeded"}`, false},
		{`{"code":"other_conflict"}`, false},
		{`not json`, false},
	} {
		t.Run(tc.body, func(t *testing.T) {
			var got seen
			c := server(t, 409, tc.body, &got)
			_, err := c.StartDeferred(context.Background(), 118, DeferredRequest{})
			if !errors.Is(err, ErrConflict) || errors.Is(err, ErrWIPLimit) != tc.wip {
				t.Fatalf("conflict = %v, want WIP = %v", err, tc.wip)
			}
		})
	}
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

func TestActivationIssueRequestsUseCoordinatorAndSeparateDelegation(t *testing.T) {
	ctx := context.Background()
	var got seen
	c := server(t, 200, `{"columns":[{"id":2,"stateType":"started","position":1}],"assignees":[{"userId":"coder-id","username":"coder","kind":"agent"}]}`, &got)
	project, err := c.GetProject(ctx, "HL")
	if err != nil || len(project.Assignees) != 1 || project.Assignees[0].UserID != "coder-id" || got.key != "k-coord" || got.path != "/projects/HL" {
		t.Fatalf("project=%+v request=%+v err=%v", project, got, err)
	}
	c = server(t, 201, `{"id":42,"identifier":"HL-4","title":"Activate HL-3: feature","columnId":1}`, &got)
	created, err := c.CreateIssue(ctx, "HL", NewIssue{Title: "Activate HL-3: feature", Description: "step\n\n<!-- factory-activation work-item=7 -->", ColumnID: 1})
	if err != nil || created.ID != 42 || created.Identifier != "HL-4" || got.key != "k-coord" || got.path != "/projects/HL/issues" || got.method != "POST" {
		t.Fatalf("created=%+v request=%+v err=%v", created, got, err)
	}
	if _, ok := got.body["delegateUserId"]; ok {
		t.Fatal("creation must not delegate an unfinished description")
	}

	c = server(t, 200, `{}`, &got)
	description := "Put `Refs HL-4` in the commit message.\n\nEnable after migration."
	if err := c.UpdateIssue(ctx, 42, IssueUpdate{Description: &description}); err != nil {
		t.Fatal(err)
	}
	if got.method != "PATCH" || got.path != "/issues/42" || got.key != "k-coord" || len(got.body) != 1 || got.body["description"] != description {
		t.Fatalf("description request=%+v", got)
	}
	if err := c.BlockIssue(ctx, 42, 41); err != nil {
		t.Fatal(err)
	}
	if got.method != "POST" || got.path != "/issues/42/links" || got.key != "k-coord" || got.body["kind"] != "blocks" || got.body["targetIssueId"] != float64(41) {
		t.Fatalf("link request=%+v", got)
	}
	delegate := "coder-id"
	if err := c.UpdateIssue(ctx, 42, IssueUpdate{DelegateUserID: &delegate}); err != nil {
		t.Fatal(err)
	}
	if len(got.body) != 1 || got.body["delegateUserId"] != delegate || got.key != "k-coord" {
		t.Fatalf("delegate request=%+v", got)
	}

	c = server(t, 200, `[{"id":42,"identifier":"HL-4"}]`, &got)
	marker := "<!-- factory-activation work-item=7 -->"
	hits, err := c.SearchIssues(ctx, "HL", marker)
	query, _ := url.ParseQuery(got.query)
	if err != nil || len(hits) != 1 || hits[0].ID != 42 || got.path != "/projects/HL/issues/search" || got.key != "k-coord" || query.Get("q") != marker || query.Get("limit") != "500" {
		t.Fatalf("search hits=%+v request=%+v err=%v", hits, got, err)
	}
	c = server(t, 200, `{"id":42,"identifier":"HL-4","assigneeUserId":"human-id","archivedAt":"2026-11-15T00:00:00Z","delegateUserId":"coder-id","links":[{"kind":"blocks","direction":"outward","issue":{"id":41}}]}`, &got)
	is, err := c.GetIssue(ctx, 42)
	if err != nil || is.AssigneeUserID != "human-id" || (is.ArchivedAt == nil || is.ArchivedAt.Format(time.RFC3339) != "2026-11-15T00:00:00Z") || (is.DelegateUserID == nil || *is.DelegateUserID != delegate) || len(is.Links) != 1 || is.Links[0].Direction != "outward" || is.Links[0].Issue.ID != 41 {
		t.Fatalf("issue=%+v err=%v", is, err)
	}
}

func TestActivationRequiresEveryEffectiveCoordinatorPermission(t *testing.T) {
	for _, missing := range []string{"", "read", "create", "edit", "ai_agents"} {
		work := map[string]bool{"read": true, "create": true, "edit": true}
		if missing != "" && missing != "ai_agents" {
			work[missing] = false
		}
		body, _ := json.Marshal(map[string]any{"permissions": map[string]any{
			"work_items": work, "ai_agents": map[string]bool{"read": missing != "ai_agents"},
		}})
		var got seen
		c := server(t, 200, string(body), &got)
		p, err := c.GetProject(context.Background(), "HOME")
		if err != nil || p.CanActivate() != (missing == "") {
			t.Fatalf("missing %q: project=%+v, err=%v", missing, p, err)
		}
	}
	if (&Project{}).CanActivate() {
		t.Fatal("missing permissions granted activation")
	}
}

func TestIssueRunHistoryUsesCoordinatorAndCursor(t *testing.T) {
	var got seen
	c := server(t, 200, `{"items":[{"id":12,"username":"coder","trigger":"delegation","status":"success","prompt":"not decoded"}],"nextCursor":12}`, &got)
	page, err := c.IssueRuns(context.Background(), 42, 20)
	query, _ := url.ParseQuery(got.query)
	if err != nil || len(page.Items) != 1 || page.Items[0].ID != 12 || page.Items[0].Username != "coder" || page.Items[0].Trigger != "delegation" || page.NextCursor == nil || *page.NextCursor != 12 ||
		got.method != "GET" || got.path != "/issues/42/agent-runs" || got.key != "k-coord" || query.Get("before") != "20" || query.Get("limit") != "50" {
		t.Fatalf("page=%+v request=%+v err=%v", page, got, err)
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

// Wire shape verified against croffasia/itsaplan v1.10.0 issues/links.ts:
// GET /issues/:id includes all relations, including archived other ends.
func TestIssueDependencyDirections(t *testing.T) {
	var got seen
	c := server(t, 200, `{"id":41,"identifier":"HL-3","description":"Brief","links":[
		{"id":1,"kind":"blocks","direction":"inward","issue":{"id":42,"identifier":"HL-4","columnId":9,"archived":true}},
		{"id":2,"kind":"blocks","direction":"outward","issue":{"id":43}},
		{"id":3,"kind":"blocks","issue":{"id":44}},
		{"id":4,"kind":"relates","direction":"inward","issue":{"id":45}}
	]}`, &got)
	is, err := c.GetIssue(context.Background(), 41)
	if err != nil {
		t.Fatal(err)
	}
	if got.method != "GET" || got.path != "/issues/41" || got.key != "k-coord" {
		t.Fatalf("request = %+v", got)
	}
	if len(is.Links) != 4 || is.Links[0].Issue.ID != 42 || is.Links[0].Issue.Identifier != "HL-4" || is.Links[0].Issue.ColumnID != 9 {
		t.Fatalf("links = %+v", is.Links)
	}
	for i, link := range is.Links {
		if link.BlockedBy() != (i == 0) {
			t.Fatalf("wrong dependency direction: %+v", link)
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
