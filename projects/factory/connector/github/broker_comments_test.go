package github

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/synctest"
	"time"
)

type delayedCommentTransport struct{ delay time.Duration }

func (d delayedCommentTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	select {
	case <-r.Context().Done():
		return nil, r.Context().Err()
	case <-time.After(d.delay):
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader(`{"comment":null,"complete":true}`)),
		}, nil
	}
}

func TestBrokerCommentsDefaultTimeouts(t *testing.T) {
	for _, tc := range []struct {
		name, method string
		delay, limit time.Duration
	}{
		// 100 pages at 400 ms each exceed the former 30-second limit.
		{"slow lookup", http.MethodGet, 40 * time.Second, 0},
		{"bounded lookup", http.MethodGet, 6 * time.Minute, CommentLookupTimeout},
		{"bounded create", http.MethodPost, 40 * time.Second, 30 * time.Second},
		{"bounded update", http.MethodPatch, 40 * time.Second, 30 * time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				// Leave HTTP nil to exercise the deployed client's defaults.
				original := http.DefaultTransport
				http.DefaultTransport = delayedCommentTransport{delay: tc.delay}
				defer func() { http.DefaultTransport = original }()
				b := &BrokerComments{URL: "https://broker.invalid", Prefix: "m", Bearer: staticBearer("t")}
				ctx := context.Background()
				start := time.Now()
				var err error
				switch tc.method {
				case http.MethodGet:
					var complete bool
					_, complete, err = b.IssueComments(ctx, "o/r", 7)
					if err == nil && !complete {
						t.Fatal("lookup lost its completeness result")
					}
				case http.MethodPost:
					_, err = b.CreateIssueComment(ctx, "o/r", 7, "new")
				case http.MethodPatch:
					_, err = b.UpdateIssueComment(ctx, "o/r", 9, "edit")
				}
				if tc.limit == 0 {
					if err != nil || time.Since(start) != tc.delay {
						t.Fatalf("lookup after %s = %v, want success after %s", time.Since(start), err, tc.delay)
					}
				} else if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) != tc.limit {
					t.Fatalf("request after %s = %v, want deadline at %s", time.Since(start), err, tc.limit)
				}
			})
		})
	}
}

func TestBrokerCommentsUsesTheBrokerProxy(t *testing.T) {
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != DefaultCommentsPath || r.Header.Get("Authorization") != "Bearer sa-token" {
			t.Errorf("call %s %s with %q", r.Method, r.URL.Path, r.Header.Get("Authorization"))
		}
		body, _ := io.ReadAll(r.Body)
		calls = append(calls, r.Method+" "+r.URL.RawQuery+" "+string(body))
		switch r.Method {
		case http.MethodGet:
			if r.URL.Query().Get("number") == "8" {
				_, _ = w.Write([]byte(`{"comment":null,"complete":false}`))
				return
			}
			_, _ = w.Write([]byte(`{"comment":{"id":1,"author":"bot[bot]","body":"mine"},"complete":true}`))
		case http.MethodPost:
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"id":9,"author":"bot[bot]","body":"new"}`))
		case http.MethodPatch:
			if strings.Contains(string(body), `"id":404`) {
				w.WriteHeader(http.StatusNotFound)
				_, _ = w.Write([]byte(`{"factory_error":"not_found"}`))
				return
			}
			if strings.Contains(string(body), `"id":403`) {
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"factory_error":"not_the_reviewers_comment"}`))
				return
			}
			_, _ = w.Write([]byte(`{"id":9,"author":"bot[bot]","body":"edited"}`))
		}
	}))
	defer srv.Close()
	b := &BrokerComments{URL: srv.URL, Prefix: "<!-- m -->", Bearer: staticBearer("sa-token"), HTTP: srv.Client()}
	ctx := context.Background()

	// Finding the reviewer's comment is one request, however long the PR.
	found, complete, err := b.IssueComments(ctx, "o/r", 7)
	if err != nil || !complete || len(found) != 1 || found[0].ID != 1 || found[0].Author != "bot[bot]" {
		t.Fatalf("find = %+v, %v, %v", found, complete, err)
	}
	none, complete, err := b.IssueComments(ctx, "o/r", 8)
	if err != nil || complete || len(none) != 0 {
		t.Fatalf("find at the bound = %+v, %v, %v", none, complete, err)
	}
	c, err := b.CreateIssueComment(ctx, "o/r", 7, "new")
	if err != nil || c.ID != 9 || c.Author != "bot[bot]" {
		t.Fatalf("create = %+v, %v", c, err)
	}
	if c, err := b.UpdateIssueComment(ctx, "o/r", 9, "edited"); err != nil || c.Body != "edited" {
		t.Fatalf("update = %+v, %v", c, err)
	}
	if _, err := b.UpdateIssueComment(ctx, "o/r", 404, "x"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted comment = %v, want ErrNotFound", err)
	}
	if _, err := b.UpdateIssueComment(ctx, "o/r", 403, "x"); err == nil || errors.Is(err, ErrNotFound) {
		t.Fatalf("refused edit = %v", err)
	}
	want := []string{
		"GET number=7&prefix=%3C%21--+m+--%3E&repository=o%2Fr ",
		"GET number=8&prefix=%3C%21--+m+--%3E&repository=o%2Fr ",
		`POST  {"body":"new","number":7,"repository":"o/r"}`,
		`PATCH  {"body":"edited","id":9,"repository":"o/r"}`,
		`PATCH  {"body":"x","id":404,"repository":"o/r"}`,
		`PATCH  {"body":"x","id":403,"repository":"o/r"}`,
	}
	if len(calls) != len(want) {
		t.Fatalf("calls = %q", calls)
	}
	for i, w := range want {
		if calls[i] != w {
			t.Fatalf("call %d = %q, want %q", i, calls[i], w)
		}
	}
}

// The largest comment the broker can return, escaped, decodes.
func TestBrokerCommentsReadsTheLargestEscapedComment(t *testing.T) {
	answer, _ := json.Marshal(map[string]any{
		"comment":  map[string]any{"id": 1, "author": "bot[bot]", "body": strings.Repeat("&", 65536)},
		"complete": true,
	})
	if len(answer) < 6*65536 || len(answer) > maxBrokerCommentAnswer {
		t.Fatalf("answer is %d bytes", len(answer))
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(answer) }))
	defer srv.Close()
	b := &BrokerComments{URL: srv.URL, Prefix: "m", Bearer: staticBearer("t"), HTTP: srv.Client()}
	found, complete, err := b.IssueComments(context.Background(), "o/r", 1)
	if err != nil || !complete || len(found) != 1 || len(found[0].Body) != 65536 {
		t.Fatalf("largest comment = %d comments, %v, %v", len(found), complete, err)
	}
	// Without a marker there is nothing to find by, so no request is made.
	b.Prefix = ""
	if _, _, err := b.IssueComments(context.Background(), "o/r", 1); err == nil {
		t.Fatal("a find without a marker was sent")
	}
}
