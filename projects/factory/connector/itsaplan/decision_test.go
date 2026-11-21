package itsaplan

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func TestDecisionResumesAfterComment(t *testing.T) {
	label := strings.Repeat("東京&", 900)
	decision := "Decision: " + label
	sessionUser := "other-member"
	comments, patches := 0, 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" && r.Header.Get("x-api-key") != "coordinator" {
			t.Error("read without coordinator identity")
		}
		if r.Method == "GET" && (r.URL.Path == "/projects/HOME/issues/42" || r.URL.Path == "/issues/99") {
			json.NewEncoder(w).Encode(Issue{ID: 99, Identifier: "HOME-42", AssigneeUserID: "joe", ColumnID: 1, Description: "## Choices\n1. Ship (Recommended)\n2. " + label})
			return
		}
		switch r.URL.Path {
		case "/projects/HOME":
			w.Write([]byte(`{"columns":[{"id":1,"stateType":"started"}],"assignees":[{"username":"joesessions","kind":"member","userId":"sessions"},{"username":"joe","kind":"member","userId":"joe"},{"kind":"agent","userId":"coder"}]}`))
		case "/projects/HOME/issues":
			w.Write([]byte(`[{"id":99,"identifier":"HOME-42","assigneeUserId":"joe","columnId":1}]`))
		case "/issues/99/feed":
			w.Write([]byte(`{"items":[`))
			if comments > 0 {
				json.NewEncoder(w).Encode(map[string]any{"issueId": 99, "kind": "comment", "actorUserId": "sessions", "editedAt": nil, "body": decision})
				w.Write([]byte(","))
			}
			w.Write([]byte(`{"issueId":99,"kind":"activity","action":"delegate","payload":{"from":{"id":"coder","value":"Coder"}}}]}`))
		case "/api/auth/sign-in/email":
			var credentials map[string]string
			json.NewDecoder(r.Body).Decode(&credentials)
			if credentials["email"] != "joe-sessions@jomcgi.dev" || credentials["password"] != "test-password" {
				t.Error("incorrect decision login")
			}
			http.SetCookie(w, &http.Cookie{Name: "session", Value: "test"})
			json.NewEncoder(w).Encode(map[string]any{"user": map[string]string{"id": sessionUser}})
		case "/api/auth/sign-out":
		case "/issues/99/comments", "/issues/99":
			cookie, err := r.Cookie("session")
			if err != nil || cookie.Value != "test" || r.Header.Get("x-api-key") != "" {
				t.Error("incorrect write identity")
			}
			var body map[string]string
			json.NewDecoder(r.Body).Decode(&body)
			if r.Method == "POST" && body["body"] == decision {
				comments++
				return
			}
			if r.Method != "PATCH" || body["delegateUserId"] != "coder" {
				t.Error("incorrect delegation")
			}
			patches++
			if patches == 1 {
				w.WriteHeader(503)
			}
		default:
			t.Errorf("unexpected request %s", r.URL.Path)
		}
	}))
	defer srv.Close()
	stopped := true
	handler := DecisionHandler{WithFence: func(_ context.Context, id int64, write func() error) (bool, error) {
		if stopped {
			return false, nil
		}
		return true, write()
	}, Plan: &Client{BaseURL: srv.URL, Coordinator: "coord", Keys: map[string]string{"coord": "coordinator"}, HTTP: srv.Client()}, Projects: []string{"HOME"}, Password: func() (string, error) { return "test-password", nil }}
	issues, err := handler.Plan.NeedsYou(context.Background(), "HOME", "joe")
	if err != nil || len(issues) != 1 || len(issues[0].Choices) != 2 || issues[0].Choices[1] != label || len(issues[0].ChoiceRevision) != 64 {
		t.Fatal("missing displayed choice or revision", err)
	}
	for i, want := range []int{409, 409, 502, 502, 204} {
		stopped = i == 1
		if i >= 3 {
			sessionUser = "sessions"
		}
		revision := issues[0].ChoiceRevision
		if i == 0 {
			revision = choiceRevision([]string{"Changed", label})
		}
		rec := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/decide", strings.NewReader(`{"key":"HOME-42","choice":2}`))
		req.Header.Set("If-Match", strconv.Quote(revision))
		handler.ServeHTTP(rec, req)
		if rec.Code != want {
			t.Fatalf("got %d, want %d", rec.Code, want)
		}
	}
	if comments != 1 || patches != 2 {
		t.Errorf("comments=%d patches=%d", comments, patches)
	}
}

// An edit after the initial read must still return 409, both before the
// decision comment and between the comment and the delegate restore. A
// fresh pick from the edited list then supersedes the stale comment.
func TestDecisionRejectsEditAfterInitialRead(t *testing.T) {
	original := "## Choices\n1. Ship (Recommended)\n2. Hold"
	reordered := "## Choices\n1. Hold (Recommended)\n2. Ship"
	for _, editAfter := range []int{1, 2} {
		reads, patches := 0, 0
		var comments []string
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch {
			case r.Method == "GET" && r.URL.Path == "/projects/HOME/issues/42":
				desc := original
				if reads >= editAfter {
					desc = reordered
				}
				reads++
				json.NewEncoder(w).Encode(Issue{ID: 99, Identifier: "HOME-42", AssigneeUserID: "joe", ColumnID: 1, Description: desc})
			case r.URL.Path == "/projects/HOME":
				w.Write([]byte(`{"columns":[{"id":1,"stateType":"started"}],"assignees":[{"username":"joesessions","kind":"member","userId":"sessions"},{"username":"joe","kind":"member","userId":"joe"},{"kind":"agent","userId":"coder"}]}`))
			case r.URL.Path == "/issues/99/feed":
				w.Write([]byte(`{"items":[`))
				for i := len(comments) - 1; i >= 0; i-- {
					json.NewEncoder(w).Encode(map[string]any{"issueId": 99, "kind": "comment", "actorUserId": "sessions", "editedAt": nil, "body": comments[i]})
					w.Write([]byte(","))
				}
				w.Write([]byte(`{"issueId":99,"kind":"activity","action":"delegate","payload":{"from":{"id":"coder","value":"Coder"}}}]}`))
			case r.URL.Path == "/api/auth/sign-in/email":
				http.SetCookie(w, &http.Cookie{Name: "session", Value: "test"})
				json.NewEncoder(w).Encode(map[string]any{"user": map[string]string{"id": "sessions"}})
			case r.URL.Path == "/api/auth/sign-out":
			case r.URL.Path == "/issues/99/comments":
				var body map[string]string
				json.NewDecoder(r.Body).Decode(&body)
				comments = append(comments, body["body"])
			case r.URL.Path == "/issues/99":
				patches++
			default:
				t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			}
		}))
		handler := DecisionHandler{WithFence: func(_ context.Context, _ int64, write func() error) (bool, error) {
			return true, write()
		}, Plan: &Client{BaseURL: srv.URL, Coordinator: "coord", Keys: map[string]string{"coord": "coordinator"}, HTTP: srv.Client()}, Projects: []string{"HOME"}, Password: func() (string, error) { return "test-password", nil }}
		decide := func(list string) int {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("POST", "/decide", strings.NewReader(`{"key":"HOME-42","choice":1}`))
			req.Header.Set("If-Match", strconv.Quote(choiceRevision(parseChoices(list))))
			handler.ServeHTTP(rec, req)
			return rec.Code
		}
		if code := decide(original); code != 409 || patches != 0 || len(comments) != editAfter-1 {
			t.Fatalf("edit after read %d: code=%d comments=%q patches=%d", editAfter, code, comments, patches)
		}
		// The refreshed list completes the handoff with a newer decision.
		if code := decide(reordered); code != 204 || patches != 1 || comments[len(comments)-1] != "Decision: Hold" {
			t.Fatalf("refreshed pick after edit %d: code=%d comments=%q patches=%d", editAfter, code, comments, patches)
		}
		srv.Close()
	}
}
