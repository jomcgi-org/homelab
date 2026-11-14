package itsaplan

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNeedsYouUsesCoordinatorAndFiltersJoeWithoutDelegate(t *testing.T) {
	var reads int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reads++
		if r.Method != "GET" || r.Header.Get("x-api-key") != "coordinator-key" {
			t.Errorf("unexpected method or identity")
		}
		switch r.URL.Path {
		case "/projects/HOME":
			_, _ = w.Write([]byte(`{"columns":[{"id":1,"stateType":"unstarted"},{"id":2,"stateType":"completed"},{"id":3,"stateType":"canceled"},{"id":4,"stateType":"backlog"},{"id":5,"stateType":"started"}],"assignees":[{"userId":"joe-id","username":"joe","kind":"member"},{"userId":"bot-id","username":"joe","kind":"agent"}]}`))
		case "/projects/HOME/issues":
			q := r.URL.Query()
			if q.Get("assigneeUserId") != "joe-id" || q.Get("limit") != "500" || q.Get("includeArchived") != "false" || q.Has("delegateUserId") {
				t.Errorf("query = %v", q)
			}
			_, _ = w.Write([]byte(`[
				{"id":20,"identifier":"HOME-20","title":"Decision","priority":"high","createdAt":"2026-11-14T12:00:00Z","columnId":1,"assigneeUserId":"joe-id","delegateUserId":null,"description":"must not survive"},
				{"id":21,"columnId":1,"assigneeUserId":"joe-id","delegateUserId":"coder"},
				{"id":22,"columnId":1,"assigneeUserId":"other"},
				{"id":23,"columnId":2,"assigneeUserId":"joe-id"},
				{"id":24,"columnId":3,"assigneeUserId":"joe-id"},
				{"id":25,"columnId":1,"assigneeUserId":"joe-id","archived":true},
				{"id":26,"columnId":99,"assigneeUserId":"joe-id"},
				{"id":27,"columnId":1,"assigneeUserId":null},
				{"id":28,"columnId":4,"assigneeUserId":"joe-id","delegateUserId":null},
				{"id":29,"columnId":5,"assigneeUserId":"joe-id","delegateUserId":null}
			]`))
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
		}
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Coordinator: "coord", Keys: map[string]string{"coord": "coordinator-key"}}
	issues, err := c.NeedsYou(context.Background(), "HOME", "joe")
	if err != nil || len(issues) != 3 || reads != 2 {
		t.Fatalf("issues = %+v, reads = %d, err = %v", issues, reads, err)
	}
	if issues[0].ID != 20 || issues[1].ID != 28 || issues[2].ID != 29 {
		t.Fatalf("wrong issue filter: %+v", issues)
	}
	data, _ := json.Marshal(issues[0])
	var fields map[string]any
	_ = json.Unmarshal(data, &fields)
	if _, ok := fields["description"]; ok { t.Fatal("description retained") }
}

func TestNeedsYouRejectsMissingMemberAndFullListing(t *testing.T) {
	for _, full := range []bool{false, true} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/projects/HOME" {
				if !full { _, _ = w.Write([]byte(`{"assignees":[]}`)); return }
				_, _ = w.Write([]byte(`{"assignees":[{"userId":"joe-id","username":"joe","kind":"member"}]}`))
				return
			}
			_ = json.NewEncoder(w).Encode(make([]IssueSummary, 500))
		}))
		c := &Client{BaseURL: srv.URL, Coordinator: "coord", Keys: map[string]string{"coord": "key"}}
		_, err := c.NeedsYou(context.Background(), "HOME", "joe")
		srv.Close()
		if err == nil { t.Fatalf("full=%v: expected an error", full) }
	}
}
