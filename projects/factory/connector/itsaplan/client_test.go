package itsaplan

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

type seen struct {
	method, path, query, key string
	body                     map[string]any
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
	for _, status := range []int{http.StatusOK, http.StatusUnauthorized, http.StatusForbidden} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			var got string
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				got = r.Header.Get("x-api-key")
				if got == "k-1" {
					w.WriteHeader(status)
				}
				_, _ = io.WriteString(w, `{"run":null}`)
			}))
			defer srv.Close()
			key := "k-1"
			c := &Client{BaseURL: srv.URL, KeyFunc: func(agent string) (string, error) {
				if agent != "coder" {
					return "", errors.New("unknown agent")
				}
				return key, nil
			}}
			for _, want := range []string{"k-1", "k-2"} {
				key = want
				_, err := c.Claim(context.Background(), "coder")
				if want == "k-1" && status != http.StatusOK {
					var refusal *HTTPError
					if !errors.As(err, &refusal) || refusal.Status != status {
						t.Fatalf("claim error = %v, want HTTP %d", err, status)
					}
				} else if err != nil {
					t.Fatal(err)
				}
				if got != want {
					t.Fatalf("key = %q, want %q", got, want)
				}
			}
			if _, err := c.Claim(context.Background(), "stranger"); err == nil {
				t.Fatal("claim without a key succeeded")
			}
		})
	}
}
