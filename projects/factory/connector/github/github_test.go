package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

type staticBearer string

func (s staticBearer) Token() (string, error) { return string(s), nil }

// fakeBroker mints a new token per request, valid for an hour from now(),
// at path (the connector's endpoint when empty).
type fakeBroker struct {
	mu      sync.Mutex
	now     func() time.Time
	path    string
	mints   int
	bearers []string
	repos   []string
}

func (b *fakeBroker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b.mu.Lock()
	defer b.mu.Unlock()
	want := b.path
	if want == "" {
		want = "/github/connector-token"
	}
	if r.URL.Path != want {
		http.NotFound(w, r)
		return
	}
	b.mints++
	b.bearers = append(b.bearers, r.Header.Get("Authorization"))
	repo := r.URL.Query().Get("repository")
	b.repos = append(b.repos, repo)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"token": fmt.Sprintf("ghs_%d", b.mints), "expires_at": b.now().Add(time.Hour).UTC().Format(time.RFC3339),
		"repository": repo, "permissions": map[string]string{"pull_requests": "write"},
	})
}

func (b *fakeBroker) Mints() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.mints
}

func TestBrokerTokenIsCachedUntilFifteenMinutesBeforeExpiry(t *testing.T) {
	now := time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }
	broker := &fakeBroker{now: clock}
	srv := httptest.NewServer(broker)
	defer srv.Close()
	toks := &BrokerTokens{URL: srv.URL, Bearer: staticBearer("projected-sa-token"), Now: clock}
	ctx := context.Background()

	tok, err := toks.Token(ctx, "example-org/homelab")
	if err != nil || tok != "ghs_1" {
		t.Fatalf("token = %q, %v", tok, err)
	}
	if broker.bearers[0] != "Bearer projected-sa-token" || broker.repos[0] != "example-org/homelab" {
		t.Fatalf("broker saw %q for %q", broker.bearers[0], broker.repos[0])
	}
	now = now.Add(44 * time.Minute)
	if tok, _ := toks.Token(ctx, "example-org/homelab"); tok != "ghs_1" || broker.Mints() != 1 {
		t.Fatalf("token = %q after 44m with %d mints, want the cached one", tok, broker.Mints())
	}
	now = now.Add(time.Minute) // 15 minutes before expiry
	if tok, _ := toks.Token(ctx, "example-org/homelab"); tok != "ghs_2" || broker.Mints() != 2 {
		t.Fatalf("token = %q after 45m with %d mints, want a fresh one", tok, broker.Mints())
	}
	if tok, _ := toks.Token(ctx, "example-org/other"); tok != "ghs_3" {
		t.Fatalf("another repository shares a token: %q", tok)
	}
}

func TestBrokerTokenPathIsConfigurable(t *testing.T) {
	now := time.Now()
	ctx := context.Background()
	// The reviewer's endpoint answers with the connector's JSON shape.
	reviewer := &fakeBroker{now: func() time.Time { return now }, path: "/github/reviewer-token"}
	srv := httptest.NewServer(reviewer)
	defer srv.Close()
	toks := &BrokerTokens{URL: srv.URL + "/", Path: "/github/reviewer-token", Bearer: staticBearer("t")}
	if tok, err := toks.Token(ctx, "example-org/homelab"); err != nil || tok != "ghs_1" {
		t.Fatalf("reviewer token = %q, %v", tok, err)
	}
	// Without a path the connector's endpoint is asked, which this broker
	// does not serve.
	if _, err := (&BrokerTokens{URL: srv.URL, Bearer: staticBearer("t")}).Token(ctx, "example-org/homelab"); err == nil ||
		!strings.Contains(err.Error(), "/github/connector-token") {
		t.Fatalf("default path err = %v, want a 404 from /github/connector-token", err)
	}
	if reviewer.Mints() != 1 || reviewer.repos[0] != "example-org/homelab" {
		t.Fatalf("reviewer endpoint minted %d for %v", reviewer.Mints(), reviewer.repos)
	}
}

func TestBrokerTokenForAnotherRepositoryIsRefused(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"token": "ghs_x", "expires_at": time.Now().Add(time.Hour).Format(time.RFC3339), "repository": "example-org/other",
		})
	}))
	defer srv.Close()
	toks := &BrokerTokens{URL: srv.URL, Bearer: staticBearer("t")}
	if _, err := toks.Token(context.Background(), "example-org/homelab"); err == nil {
		t.Fatal("accepted a token for another repository")
	}
}

// TestUnauthorizedRefreshesTokenOnce covers a token GitHub revoked before the
// cache would have replaced it.
func TestUnauthorizedRefreshesTokenOnce(t *testing.T) {
	now := time.Now()
	broker := &fakeBroker{now: func() time.Time { return now }}
	bsrv := httptest.NewServer(broker)
	defer bsrv.Close()
	var mu sync.Mutex
	var seen []string
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		seen = append(seen, r.Header.Get("Authorization"))
		if r.Header.Get("Authorization") == "Bearer ghs_1" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(`[]`))
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: &BrokerTokens{URL: bsrv.URL, Bearer: staticBearer("t"), Now: func() time.Time { return now }}}
	pr, err := c.FindPullRequest(context.Background(), "example-org/homelab", "factory/HL-1")
	if err != nil || pr != nil {
		t.Fatalf("find = %v, %v", pr, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 2 || seen[1] != "Bearer ghs_2" || broker.Mints() != 2 {
		t.Fatalf("authorizations = %v with %d mints, want a retry with a fresh token", seen, broker.Mints())
	}
}

type fixedTokens struct{}

func TestHeadRefChangedAtUsesLatestMatchingActivity(t *testing.T) {
	when := time.Date(2026, 11, 17, 20, 33, 0, 0, time.UTC)
	valid := RefActivity{ID: 3, Ref: "refs/heads/factory/test", Type: "push", Before: "old", After: "head", Timestamp: when}
	for _, tc := range []struct {
		name   string
		change func(*RefActivity)
		ok     bool
	}{
		{name: "push", ok: true},
		{name: "force push", change: func(e *RefActivity) { e.Type = "force_push" }, ok: true},
		{name: "creation", change: func(e *RefActivity) { e.Type = "branch_creation" }, ok: true},
		{name: "different head", change: func(e *RefActivity) { e.After = "newer" }},
		{name: "different ref", change: func(e *RefActivity) { e.Ref = "refs/heads/other" }},
		{name: "missing timestamp", change: func(e *RefActivity) { e.Timestamp = time.Time{} }},
		{name: "unchanged head", change: func(e *RefActivity) { e.Before = "head" }},
		{name: "deletion", change: func(e *RefActivity) { e.Type = "branch_deletion" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			event := valid
			if tc.change != nil {
				tc.change(&event)
			}
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				q := r.URL.Query()
				if r.URL.Path != "/repos/example-org/homelab/activity" || q.Get("ref") != valid.Ref || q.Get("direction") != "desc" || q.Get("per_page") != "1" || q.Get("activity_type") != "" {
					t.Errorf("activity request = %s", r.URL)
				}
				_ = json.NewEncoder(w).Encode([]RefActivity{event})
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			got, err := c.HeadRefChangedAt(context.Background(), "example-org/homelab", "factory/test", "head")
			if (err == nil) != tc.ok || (tc.ok && !got.Equal(when)) {
				t.Fatalf("head changed at = %v, error = %v", got, err)
			}
		})
	}
}

func TestPullRequestReadySinceRequiresCompleteEligibilityHistory(t *testing.T) {
	opened := time.Date(2026, 11, 18, 8, 0, 0, 0, time.UTC)
	ready := opened.Add(time.Hour)
	for _, tc := range []struct {
		name, event, label            string
		truncated, draft, changedHead bool
		want                          time.Time
	}{
		{name: "creation", want: opened},
		{name: "ready from draft", event: "ReadyForReviewEvent", want: ready},
		{name: "skip removed", event: "UnlabeledEvent", label: "NO-REVIEW", want: ready},
		{name: "ordinary label removed", event: "UnlabeledEvent", label: "documentation", want: opened},
		{name: "reopened", event: "ReopenedEvent", want: ready},
		{name: "base changed", event: "BaseRefChangedEvent", want: ready},
		{name: "truncated history", truncated: true},
		{name: "draft again", draft: true},
		{name: "head changed during read", changedHead: true},
		{name: "missing label", event: "UnlabeledEvent"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var req struct {
					Query     string
					Variables map[string]any
				}
				_ = json.NewDecoder(r.Body).Decode(&req)
				if r.URL.Path != "/graphql" || !strings.Contains(req.Query, "READY_FOR_REVIEW_EVENT") || req.Variables["number"] != float64(7) {
					t.Errorf("eligibility request = %s, %+v", r.URL, req)
				}
				events := []map[string]any{}
				if tc.event != "" {
					events = append(events, map[string]any{"__typename": tc.event, "createdAt": ready, "label": map[string]string{"name": tc.label}})
				}
				head := "head"
				if tc.changedHead {
					head = "newer"
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"repository": map[string]any{"pullRequest": map[string]any{
					"createdAt": opened, "headRefOid": head, "baseRefName": "main", "state": "OPEN", "isDraft": tc.draft,
					"labels":        map[string]any{"nodes": []any{}, "pageInfo": map[string]bool{"hasNextPage": false}},
					"timelineItems": map[string]any{"nodes": events, "pageInfo": map[string]bool{"hasPreviousPage": tc.truncated}},
				}}}})
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			got, err := c.PullRequestReadySince(context.Background(), "example-org/homelab", PullRequest{Number: 7, HeadSHA: "head", BaseRef: "main"}, []string{"no-review"})
			if tc.want.IsZero() {
				if err == nil {
					t.Fatalf("incomplete history dated the wait: %v", got)
				}
			} else if err != nil || !got.Equal(tc.want) {
				t.Fatalf("ready since = %v, error = %v, want %v", got, err, tc.want)
			}
		})
	}
}

func TestHeadRefActivityRequiresCreationWithinOneCompletePage(t *testing.T) {
	activity := func(id int, kind string) RefActivity {
		e := RefActivity{ID: int64(id), Ref: "refs/heads/session/joe", Type: kind, Before: "before", After: "after"}
		e.Actor.Login = "jomcgi"
		return e
	}
	full := make([]RefActivity, 100)
	for i := range full {
		full[i] = activity(100-i, "push")
	}
	full[99].Type = "branch_creation"
	for _, tc := range []struct {
		name   string
		events []RefActivity
		ok     bool
	}{
		{"complete", []RefActivity{activity(3, "push"), activity(2, "branch_creation"), activity(1, "branch_deletion")}, true},
		{"exact limit", full, true},
		{"over limit", append(append([]RefActivity(nil), full...), activity(101, "push")), false},
		{"truncated", full[:99], false},
		{"missing page", nil, false},
		{"empty page", []RefActivity{}, false},
		{"duplicate event", []RefActivity{activity(1, "push"), activity(1, "branch_creation")}, false},
		{"no identity", []RefActivity{activity(0, "branch_creation")}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var mu sync.Mutex
			calls := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				mu.Lock()
				calls++
				mu.Unlock()
				q := r.URL.Query()
				if r.URL.Path != "/repos/example-org/homelab/activity" || q.Get("ref") != "refs/heads/session/joe" || q.Get("direction") != "desc" || q.Get("per_page") != "100" || q.Get("actor") != "" || q.Get("activity_type") != "" {
					t.Errorf("activity request = %s", r.URL)
				}
				if r.Header.Get("If-None-Match") != "" {
					t.Error("activity reused ETag evidence")
				}
				w.Header().Set("ETag", "activity")
				_ = json.NewEncoder(w).Encode(tc.events)
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}, ETags: &ETagCache{}}
			for range 2 {
				got, err := c.HeadRefActivity(context.Background(), "example-org/homelab", "session/joe")
				if (err == nil) != tc.ok {
					t.Fatalf("events = %+v, error = %v", got, err)
				}
				if tc.ok && got[len(got)-1].Type != "branch_creation" {
					t.Fatalf("read older incarnation: %+v", got)
				}
			}
			mu.Lock()
			defer mu.Unlock()
			if calls != 2 {
				t.Fatalf("calls = %d", calls)
			}
		})
	}
}

func TestPullRequestCommitsFailsClosed(t *testing.T) {
	for _, tc := range []struct {
		name                            string
		count                           int
		badEmail, mismatch, missingPage bool
		ok                              bool
	}{
		{name: "complete", count: 101, ok: true},
		{name: "empty", count: 0},
		{name: "GitHub cap", count: 250},
		{name: "missing author", count: 1, badEmail: true},
		{name: "head changed", count: 1, mismatch: true},
		{name: "missing page", count: 101, missingPage: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/repos/example-org/homelab/pulls/7/commits" || r.URL.Query().Get("per_page") != "100" {
					t.Errorf("request = %s", r.URL)
				}
				page, _ := strconv.Atoi(r.URL.Query().Get("page"))
				if tc.missingPage && page > 1 {
					_, _ = w.Write([]byte("null"))
					return
				}
				out := []PullRequestCommit{}
				for i := (page - 1) * 100; i < min(page*100, tc.count); i++ {
					var cm PullRequestCommit
					cm.SHA, cm.Commit.Author.Email = strconv.Itoa(i), "joe@example.com"
					if tc.badEmail {
						cm.Commit.Author.Email = ""
					}
					out = append(out, cm)
				}
				_ = json.NewEncoder(w).Encode(out)
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			head := strconv.Itoa(tc.count - 1)
			if tc.mismatch {
				head = "changed"
			}
			commits, err := c.PullRequestCommits(context.Background(), "example-org/homelab", 7, head)
			if (err == nil) != tc.ok {
				t.Fatalf("commits = %d, error = %v", len(commits), err)
			}
			if tc.ok && (len(commits) != tc.count || commits[0].Commit.Author.Email != "joe@example.com") {
				t.Fatalf("commits = %+v", commits)
			}
		})
	}
}

func TestPullRequestPreservesHeadRepository(t *testing.T) {
	for _, tc := range []struct{ repo, want string }{
		{`{"full_name":"example-org/homelab"}`, "example-org/homelab"},
		{`{"full_name":"someone/fork"}`, "someone/fork"},
		{"null", ""},
	} {
		var p restPull
		if err := json.Unmarshal([]byte(`{"head":{"ref":"session/joe","sha":"head","repo":`+tc.repo+`}}`), &p); err != nil {
			t.Fatal(err)
		}
		if got := p.pr(); got.HeadRepo != tc.want || got.HeadRef != "session/joe" || got.HeadSHA != "head" {
			t.Fatalf("PR = %+v", got)
		}
	}
}

func TestGetPullRequestReadsRenovateIdentityAndClosure(t *testing.T) {
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/repos/jomcgi-org/homelab/pulls/42" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"number":42,"state":"closed","merged_at":"2026-11-15T12:00:00Z", "user":{"login":"jomcgi-factory-renovate[bot]"},"labels":[{"name":"major-upgrade"}],"head":{"ref":"renovate/pkg-2.x","sha":"head"},"base":{"ref":"main"},"body":"version table"}`))
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	p, err := c.GetPullRequest(context.Background(), "jomcgi-org/homelab", 42)
	if err != nil || !p.Merged || p.Author != "jomcgi-factory-renovate[bot]" || p.HeadRef != "renovate/pkg-2.x" || p.Body != "version table" || len(p.Labels) != 1 {
		t.Fatalf("PR = %+v, %v", p, err)
	}
}

func (fixedTokens) Token(context.Context, string) (string, error) { return "ghs_fixed", nil }
func (fixedTokens) Invalidate(string)                             {}

func TestFindPrefersOpenPullRequestAndMapsExisting(t *testing.T) {
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/repos/example-org/homelab/pulls":
			if r.URL.Query().Get("head") != "example-org:factory/HL-1" || r.URL.Query().Get("state") != "all" {
				t.Errorf("query = %s", r.URL.RawQuery)
			}
			_, _ = w.Write([]byte(`[
				{"number": 3, "node_id": "PR_3", "state": "closed", "merged_at": null, "head": {"ref": "factory/HL-1", "sha": "a"}},
				{"number": 5, "node_id": "PR_5", "state": "open", "head": {"ref": "factory/HL-1", "sha": "b"}, "base": {"ref": "main"}},
				{"number": 7, "node_id": "PR_7", "state": "closed", "merged_at": "2026-11-01T00:00:00Z", "head": {"ref": "factory/HL-1", "sha": "c"}}
			]`))
		case r.Method == http.MethodPost && r.URL.Path == "/repos/example-org/homelab/pulls":
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"message":"Validation Failed","errors":[{"message":"A pull request already exists for example-org:factory/HL-1."}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	ctx := context.Background()
	pr, err := c.FindPullRequest(ctx, "example-org/homelab", "factory/HL-1")
	if err != nil || pr == nil || pr.Number != 5 || pr.NodeID != "PR_5" || pr.HeadSHA != "b" {
		t.Fatalf("find = %+v, %v; want the open PR 5", pr, err)
	}
	if _, err := c.CreatePullRequest(ctx, "example-org/homelab", NewPullRequest{Head: "factory/HL-1", Base: "main"}); !errors.Is(err, ErrPRExists) {
		t.Fatalf("create = %v, want ErrPRExists", err)
	}
	if _, err := c.Compare(ctx, "example-org/homelab", "main", "factory/HL-9"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("compare of a missing branch = %v, want ErrNotFound", err)
	}
}

func TestStatusesKeepTheLatestPerContextWithItsCreator(t *testing.T) {
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/repos/example-org/homelab/commits/sha-1/statuses" {
			http.NotFound(w, r)
			return
		}
		// GitHub's list endpoint, newest first; each entry names its creator.
		_, _ = w.Write([]byte(`[
			{"node_id": "SC_3", "context": "pr-checks", "state": "success", "target_url": "https://ci.example/3",
			 "creator": {"login": "buildbuddy-io[bot]", "type": "Bot"}},
			{"node_id": "SC_2", "context": "lint", "state": "pending", "creator": {"login": "someone"}},
			{"node_id": "SC_1", "context": "pr-checks", "state": "failure", "creator": {"login": "buildbuddy-io[bot]"}}
		]`))
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	sts, err := c.Statuses(context.Background(), "example-org/homelab", "sha-1")
	if err != nil {
		t.Fatal(err)
	}
	want := []Status{
		{NodeID: "SC_3", Context: "pr-checks", State: "success", Creator: "buildbuddy-io[bot]", URL: "https://ci.example/3", SHA: "sha-1"},
		{NodeID: "SC_2", Context: "lint", State: "pending", Creator: "someone", SHA: "sha-1"},
	}
	if fmt.Sprint(sts) != fmt.Sprint(want) {
		t.Fatalf("statuses = %+v, want %+v", sts, want)
	}
}

func TestPullRequestDetailQueueEvidence(t *testing.T) {
	for _, tc := range []struct {
		name       string
		queue      string
		mergeable  string
		wantState  string
		wantCommit string
	}{
		{"testing", `{"state":"AWAITING_CHECKS","headCommit":{"oid":"group-sha"}}`, "MERGEABLE", "AWAITING_CHECKS", "group-sha"},
		{"building", `{"state":"QUEUED","headCommit":null}`, "UNKNOWN", "QUEUED", ""},
		{"conflict", `null`, "CONFLICTING", "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var req struct{ Query string }
				if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
					t.Error(err)
				}
				if !strings.Contains(req.Query, "mergeQueueEntry { state headCommit { oid } }") || !strings.Contains(req.Query, "mergeable") {
					t.Errorf("query omits ejection evidence: %s", req.Query)
				}
				fmt.Fprintf(w, `{"data":{"repository":{"pullRequest":{"id":"PR_5","number":5,"state":"OPEN","headRefOid":"pr-sha","mergeable":%q,"mergeQueueEntry":%s}}}}`, tc.mergeable, tc.queue)
			}))
			defer gh.Close()
			c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
			d, err := c.PullRequestDetail(context.Background(), "example-org/homelab", 5)
			if err != nil {
				t.Fatal(err)
			}
			if d.HeadSHA != "pr-sha" || d.MergeGroupSHA != tc.wantCommit || d.MergeQueueState != tc.wantState || d.Mergeable != tc.mergeable {
				t.Fatalf("detail = %+v", d)
			}
		})
	}
}

func TestPullRequestDetailAndEnqueue(t *testing.T) {
	var mu sync.Mutex
	var enqueued map[string]any
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.URL.Path != "/graphql" {
			http.NotFound(w, r)
			return
		}
		var req struct {
			Query     string         `json:"query"`
			Variables map[string]any `json:"variables"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		if strings.Contains(req.Query, "dequeuePullRequest") {
			enqueued = map[string]any{"dequeued": req.Variables["id"]}
			_, _ = w.Write([]byte(`{"data":{"dequeuePullRequest":{"mergeQueueEntry":{"state":"QUEUED"}}}}`))
			return
		}
		if strings.Contains(req.Query, "enqueuePullRequest") {
			enqueued = req.Variables
			_, _ = w.Write([]byte(`{"data":{"enqueuePullRequest":{"mergeQueueEntry":{"state":"QUEUED"}}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":{
			"id":"PR_5","number":5,"url":"https://github.com/example-org/homelab/pull/5","state":"MERGED","merged":true,
			"mergedAt":"2026-11-14T12:00:00Z","headRefOid":"b","headRefName":"factory/HL-1","baseRefName":"main","baseRefOid":"base-sha",
			"author":{"login":"jomcgi-factory"},"mergeCommit":{"oid":"m"},"mergeQueueEntry":null,
			"commits":{"nodes":[{"commit":{"oid":"b","message":"fix: ship\n\nActivation: none"}}]},
			"labels":{"nodes":[{"name":"human"},{"name":"factory"}]},
			"reviews":{"nodes":[
				{"id":"R_2","state":"APPROVED","submittedAt":"2026-11-14T11:00:00Z","author":{"login":"reviewer"},"commit":{"oid":"b"}},
				{"id":"R_1","state":"CHANGES_REQUESTED","body":"fix","submittedAt":"2026-11-14T10:00:00Z","author":{"login":"reviewer"},"commit":{"oid":"a"}}
			]},
			"reviewThreads":{"nodes":[{"id":"T_1","isResolved":false,"isOutdated":true,"path":"main.go","line":null,
				"comments":{"nodes":[{"id":"C_1","body":"rename","url":"u","author":{"login":"reviewer"},"commit":{"oid":"a"}}]}}]}
		}}}}`))
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	ctx := context.Background()
	d, err := c.PullRequestDetail(ctx, "example-org/homelab", 5)
	if err != nil {
		t.Fatal(err)
	}
	if d.BaseSHA != "base-sha" || !d.Merged || d.State != "closed" || d.MergeCommitSHA != "m" || d.MergeQueueState != "" || d.HeadSHA != "b" || d.Author != "jomcgi-factory" {
		t.Fatalf("detail = %+v", d.PullRequest)
	}
	if d.HeadMessage != "fix: ship\n\nActivation: none" {
		t.Fatalf("head message = %q", d.HeadMessage)
	}
	if strings.Join(d.Labels, ",") != "human,factory" {
		t.Fatalf("labels = %v", d.Labels)
	}
	if len(d.Reviews) != 2 || d.Reviews[0].NodeID != "R_1" || d.Reviews[0].CommitSHA != "a" {
		t.Fatalf("reviews = %+v, want oldest first", d.Reviews)
	}
	if len(d.Threads) != 1 || d.Threads[0].Comments[0].Body != "rename" {
		t.Fatalf("threads = %+v", d.Threads)
	}
	state, err := c.EnqueuePullRequest(ctx, "example-org/homelab", "PR_5", "b")
	if err != nil || state != "QUEUED" {
		t.Fatalf("enqueue = %q, %v", state, err)
	}
	mu.Lock()
	if enqueued["id"] != "PR_5" || enqueued["head"] != "b" {
		t.Fatalf("enqueue variables = %v, want the node ID and expected head", enqueued)
	}
	mu.Unlock()
	if err := c.DequeuePullRequest(ctx, "example-org/homelab", "PR_5"); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if enqueued["dequeued"] != "PR_5" {
		t.Fatalf("dequeue variables = %v", enqueued)
	}
}

// slowBroker holds each mint until released, counting the mints.
type slowBroker struct {
	fakeBroker
	release chan struct{}
}

func (b *slowBroker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	<-b.release
	b.fakeBroker.ServeHTTP(w, r)
}

func TestConcurrentCallersShareOneFetchPerRepository(t *testing.T) {
	now := time.Now()
	broker := &slowBroker{fakeBroker: fakeBroker{now: func() time.Time { return now }}, release: make(chan struct{})}
	srv := httptest.NewServer(broker)
	defer srv.Close()
	toks := &BrokerTokens{URL: srv.URL, Bearer: staticBearer("t")}
	var wg sync.WaitGroup
	got := make([]string, 8)
	for i := range got {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			got[i], _ = toks.Token(context.Background(), "example-org/homelab")
		}(i)
	}
	// A second repository gets its own fetch.
	other := make(chan string)
	go func() {
		tok, _ := toks.Token(context.Background(), "example-org/other")
		other <- tok
	}()
	time.Sleep(50 * time.Millisecond)
	close(broker.release)
	wg.Wait()
	<-other
	if n := broker.Mints(); n != 2 {
		t.Fatalf("mints = %d, want one per repository", n)
	}
	for _, tok := range got {
		if tok != got[0] || tok == "" {
			t.Fatalf("tokens = %v, want one shared token", got)
		}
	}
}

// conditionalServer serves body with an ETag naming its version and answers
// a matching If-None-Match with 304, counting each kind.
type conditionalServer struct {
	mu          sync.Mutex
	body        string
	version     int
	full, unmod int
	paths       []string
}

func (s *conditionalServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.paths = append(s.paths, r.URL.String())
	etag := fmt.Sprintf(`W/"v%d"`, s.version)
	if r.Header.Get("If-None-Match") == etag {
		s.unmod++
		w.WriteHeader(http.StatusNotModified)
		return
	}
	s.full++
	w.Header().Set("ETag", etag)
	_, _ = w.Write([]byte(s.body))
}

func TestConditionalGetReusesTheCachedBodyOn304(t *testing.T) {
	srv := &conditionalServer{body: `[{"number": 7, "node_id": "PR_7", "state": "open", "title": "t", "body": "b",
		"user": {"login": "renovate[bot]"}, "labels": [{"name": "deps"}],
		"head": {"ref": "renovate/x", "sha": "sha-1"}, "base": {"ref": "main"}}]`}
	gh := httptest.NewServer(srv)
	defer gh.Close()
	var hits, misses int
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}, ETags: &ETagCache{Observe: func(nm bool) {
		if nm {
			hits++
		} else {
			misses++
		}
	}}}
	ctx := context.Background()
	for i := range 3 {
		prs, complete, err := c.ListPullRequests(ctx, "example-org/homelab", "main")
		if err != nil || !complete {
			t.Fatal(complete, err)
		}
		want := PullRequest{
			Number: 7, NodeID: "PR_7", State: "open", Title: "t", Body: "b", Author: "renovate[bot]",
			Labels: []string{"deps"}, HeadSHA: "sha-1", HeadRef: "renovate/x", BaseRef: "main",
		}
		if len(prs) != 1 || fmt.Sprint(prs[0]) != fmt.Sprint(want) {
			t.Fatalf("read %d: prs = %+v", i, prs)
		}
	}
	if srv.full != 1 || srv.unmod != 2 || hits != 2 || misses != 0 {
		t.Fatalf("full = %d, 304s = %d, observed %d/%d; want one full read then 304s", srv.full, srv.unmod, hits, misses)
	}
	if !strings.Contains(srv.paths[0], "state=open") || !strings.Contains(srv.paths[0], "base=main") {
		t.Fatalf("query = %s", srv.paths[0])
	}
	srv.mu.Lock()
	srv.body, srv.version = `[]`, srv.version+1
	srv.mu.Unlock()
	if prs, _, err := c.ListPullRequests(ctx, "example-org/homelab", "main"); err != nil || len(prs) != 0 || srv.full != 2 || misses != 1 {
		t.Fatalf("changed body: prs = %v, err = %v, full = %d, misses = %d", prs, err, srv.full, misses)
	}
}

func TestETagCacheEvictsTheLeastRecentlyUsed(t *testing.T) {
	c := &ETagCache{Max: 2}
	c.put("a", "1", nil)
	c.put("b", "2", nil)
	c.get("a")
	c.put("c", "3", nil)
	if _, _, ok := c.get("b"); ok || c.Len() != 2 {
		t.Fatalf("b kept or len %d; want b evicted", c.Len())
	}
	if _, _, ok := c.get("a"); !ok {
		t.Fatal("recently read a was evicted")
	}
}

func TestAllCheckRunsRejectsTruncatedHistory(t *testing.T) {
	for _, total := range []int{1, 2} {
		t.Run(fmt.Sprint(total), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Query().Get("filter") != "all" || r.URL.Query().Get("app_id") != "5758326" {
					t.Error("recovery did not request all runs from the reviewer App")
				}
				fmt.Fprintf(w, `{"total_count":%d,"check_runs":[{"id":42,"name":"factory review","head_sha":"sha-1","app":{"id":5758326}}]}`, total)
			}))
			defer server.Close()
			c := &Client{BaseURL: server.URL, Tokens: fixedTokens{}}
			runs, err := c.ListCheckRuns(context.Background(), "example-org/homelab", "sha-1", CheckRunFilter{Name: "factory review", AppID: 5758326, All: true})
			if total == 1 && (err != nil || len(runs) != 1) {
				t.Fatalf("complete history: %v", err)
			}
			if total == 2 && (err == nil || len(runs) != 0) {
				t.Fatal("truncated history was accepted")
			}
		})
	}
}

func TestCheckRunsFilterCreateAndUpdate(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	var bodies []map[string]any
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		seen = append(seen, r.Method+" "+r.URL.String())
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		bodies = append(bodies, body)
		switch {
		case r.Method == http.MethodGet:
			_, _ = w.Write([]byte(`{"total_count": 1, "check_runs": [{"id": 42, "name": "factory review", "status": "in_progress",
				"head_sha": "sha-1", "external_id": "factory-review/example-org/homelab/7/sha-1",
				"started_at": "2026-11-14T12:00:00Z", "app": {"id": 5758326},
				"output": {"title": "Review in progress", "summary": "s", "text": "x"}}]}`))
		default:
			_, _ = w.Write([]byte(`{"id": 42, "name": "factory review", "status": "completed", "conclusion": "success", "head_sha": "sha-1", "app": {"id": 5758326}}`))
		}
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	ctx := context.Background()
	runs, err := c.ListCheckRuns(ctx, "example-org/homelab", "sha-1", CheckRunFilter{Name: "factory review", AppID: 5758326})
	if err != nil {
		t.Fatal(err)
	}
	if len(runs) != 1 || runs[0].ID != 42 || runs[0].ExternalID != "factory-review/example-org/homelab/7/sha-1" ||
		runs[0].StartedAt.IsZero() || runs[0].Text != "x" || runs[0].AppID != 5758326 {
		t.Fatalf("runs = %+v", runs)
	}
	started := time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
	if _, err := c.CreateCheckRun(ctx, "example-org/homelab", CheckRunWrite{
		Name: "factory review", HeadSHA: "sha-1", Status: "in_progress", StartedAt: &started,
		Output: &CheckRunOutput{Title: "t", Summary: "s"},
	}); err != nil {
		t.Fatal(err)
	}
	run, err := c.UpdateCheckRun(ctx, "example-org/homelab", 42, CheckRunWrite{
		Status: "completed", Conclusion: "success",
		Output: &CheckRunOutput{Title: "t", Summary: "s", Annotations: []Annotation{{Path: "a.go", StartLine: 3, EndLine: 3, Level: "warning", Message: "m"}}},
	})
	if err != nil || run.Conclusion != "success" {
		t.Fatalf("update = %+v, %v", run, err)
	}
	mu.Lock()
	defer mu.Unlock()
	want := []string{
		"GET /repos/example-org/homelab/commits/sha-1/check-runs?app_id=5758326&check_name=factory+review&filter=latest&per_page=100",
		"POST /repos/example-org/homelab/check-runs",
		"PATCH /repos/example-org/homelab/check-runs/42",
	}
	if fmt.Sprint(seen) != fmt.Sprint(want) {
		t.Fatalf("requests = %v, want %v", seen, want)
	}
	if bodies[1]["head_sha"] != "sha-1" || bodies[1]["started_at"] != "2026-11-14T12:00:00Z" || bodies[1]["conclusion"] != nil {
		t.Fatalf("create body = %v", bodies[1])
	}
	if _, ok := bodies[2]["name"]; ok || bodies[2]["conclusion"] != "success" {
		t.Fatalf("update body = %v, want no name and a conclusion", bodies[2])
	}
	ann := bodies[2]["output"].(map[string]any)["annotations"].([]any)[0].(map[string]any)
	if ann["annotation_level"] != "warning" || ann["start_line"] != float64(3) {
		t.Fatalf("annotation = %v", ann)
	}
}

func TestPullRequestQueueAndMergeQueueEntries(t *testing.T) {
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Query     string         `json:"query"`
			Variables map[string]any `json:"variables"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		switch {
		case strings.Contains(req.Query, "mergeQueue(branch"):
			if req.Variables["branch"] != "main" {
				t.Errorf("branch = %v", req.Variables["branch"])
			}
			_, _ = w.Write([]byte(`{"data":{"repository":{"mergeQueue":{"entries":{"nodes":[
				{"state":"AWAITING_CHECKS","headCommit":{"oid":"mg-1"},"pullRequest":{"number":7,"headRefOid":"sha-1"}},
				{"state":"QUEUED","headCommit":null,"pullRequest":{"number":8,"headRefOid":"sha-2"}}
			]}}}}}`))
		default:
			_, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":{"id":"PR_7","state":"OPEN","isDraft":false,
				"headRefOid":"sha-1","baseRefName":"main","mergeQueueEntry":{"state":"QUEUED"},"labels":{"nodes":[{"name":"human"}],"pageInfo":{"hasNextPage":false}}}}}}`))
		}
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	ctx := context.Background()
	q, err := c.PullRequestQueue(ctx, "example-org/homelab", 7)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(*q, PullRequestQueue{NodeID: "PR_7", State: "open", HeadSHA: "sha-1", BaseRef: "main", MergeQueueState: "QUEUED", Labels: []string{"human"}}) {
		t.Fatalf("queue = %+v", q)
	}
	entries, err := c.MergeQueueEntries(ctx, "example-org/homelab", "main")
	if err != nil {
		t.Fatal(err)
	}
	want := []MergeQueueEntry{
		{State: "AWAITING_CHECKS", HeadCommit: "mg-1", Number: 7, PRHeadSHA: "sha-1"},
		{State: "QUEUED", Number: 8, PRHeadSHA: "sha-2"},
	}
	if fmt.Sprint(entries) != fmt.Sprint(want) {
		t.Fatalf("entries = %+v", entries)
	}
}

func TestReviewsCommentsAndCompare(t *testing.T) {
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/repos/example-org/homelab/pulls/7/reviews":
			_, _ = w.Write([]byte(`[{"id": 80, "node_id": "PRR_80", "user": {"login": "jomcgi"}, "state": "APPROVED", "commit_id": "sha-1", "body": "ok", "submitted_at": "2026-11-14T12:00:00Z"}]`))
		case "/repos/example-org/homelab/issues/7/comments":
			_, _ = w.Write([]byte(`[{"user": {"login": "someone"}, "body": "hi"}]`))
		case "/repos/example-org/homelab/compare/sha-0...sha-1":
			_, _ = w.Write([]byte(`{"status": "ahead", "files": [{"filename": "a.go", "status": "modified", "additions": 2, "deletions": 1}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	ctx := context.Background()
	rv, err := c.PullRequestReviews(ctx, "example-org/homelab", 7)
	if err != nil || len(rv) != 1 || rv[0] != (PullRequestReview{ID: 80, NodeID: "PRR_80", Author: "jomcgi", State: "APPROVED", CommitSHA: "sha-1", Body: "ok", SubmittedAt: time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)}) {
		t.Fatalf("reviews = %+v, %v", rv, err)
	}
	cm, err := c.PullRequestComments(ctx, "example-org/homelab", 7)
	if err != nil || len(cm) != 1 || cm[0].Author != "someone" {
		t.Fatalf("comments = %+v, %v", cm, err)
	}
	cmp, err := c.CompareCommits(ctx, "example-org/homelab", "sha-0", "sha-1")
	if err != nil || cmp.Status != "ahead" || len(cmp.Files) != 1 || cmp.Files[0].Additions != 2 {
		t.Fatalf("compare = %+v, %v", cmp, err)
	}
	if _, err := c.CompareCommits(ctx, "example-org/homelab", "gone", "sha-1"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("compare with a missing commit = %v", err)
	}
}

func TestIssueCommentsKeepsItsOriginalLimitAndCompleteness(t *testing.T) {
	for _, n := range []int{999, 1000, 1001, 2500} {
		t.Run(strconv.Itoa(n), func(t *testing.T) {
			srv, seen := pagedServer(t, "/repos/example-org/homelab/issues/7/comments", n, func(i int) string {
				return fmt.Sprintf(`{"id":%d,"user":{"login":"u"},"body":"b"}`, i)
			})
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			comments, complete, err := c.IssueComments(context.Background(), "example-org/homelab", 7)
			if err != nil || complete != (n < 1000) || len(comments) != min(n, 1000) || len(*seen) != 10 {
				t.Fatalf("comments = %d, complete %v, pages %d, err %v", len(comments), complete, len(*seen), err)
			}
		})
	}
}

func TestIssueCommentsAreListedCreatedAndUpdated(t *testing.T) {
	srv, seen := pagedServer(t, "/repos/example-org/homelab/issues/7/comments", 2*MaxListed+5, func(i int) string {
		return fmt.Sprintf(`{"id": %d, "user": {"login": "u%d"}, "body": "c%d"}`, i, i, i)
	})
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	ctx := context.Background()
	cms, complete, err := c.IssueComments(ctx, "example-org/homelab", 7)
	if err != nil || !complete || len(cms) != 2*MaxListed+5 || cms[204] != (Comment{ID: 205, Author: "u205", Body: "c205"}) {
		t.Fatalf("comments = %d, complete %v, %v", len(cms), complete, err)
	}
	if len(*seen) != 3 {
		t.Fatalf("pages read = %v", *seen)
	}

	full, _ := pagedServer(t, "/repos/example-org/homelab/issues/7/comments", MaxCommentPages*MaxListed+1, func(i int) string {
		return fmt.Sprintf(`{"id": %d, "user": {"login": "u"}, "body": "b"}`, i)
	})
	c = &Client{BaseURL: full.URL, Tokens: fixedTokens{}}
	if cms, complete, err := c.IssueComments(ctx, "example-org/homelab", 7); err != nil || complete || len(cms) != MaxCommentPages*MaxListed {
		t.Fatalf("a listing past the page bound = %d, complete %v, %v", len(cms), complete, err)
	}

	var writes []string
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		writes = append(writes, r.Method+" "+r.URL.Path+" "+body["body"])
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/repos/example-org/homelab/issues/7/comments":
			fmt.Fprintf(w, `{"id": 9, "user": {"login": "reviewer[bot]"}, "body": %q}`, body["body"])
		case r.Method == http.MethodPatch && r.URL.Path == "/repos/example-org/homelab/issues/comments/9":
			fmt.Fprintf(w, `{"id": 9, "user": {"login": "reviewer[bot]"}, "body": %q}`, body["body"])
		default:
			http.NotFound(w, r)
		}
	}))
	defer gh.Close()
	c = &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	created, err := c.CreateIssueComment(ctx, "example-org/homelab", 7, "first")
	if err != nil || *created != (Comment{ID: 9, Author: "reviewer[bot]", Body: "first"}) {
		t.Fatalf("created = %+v, %v", created, err)
	}
	updated, err := c.UpdateIssueComment(ctx, "example-org/homelab", 9, "second")
	if err != nil || updated.Body != "second" {
		t.Fatalf("updated = %+v, %v", updated, err)
	}
	if _, err := c.UpdateIssueComment(ctx, "example-org/homelab", 10, "gone"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("updating a deleted comment = %v", err)
	}
	want := []string{
		"POST /repos/example-org/homelab/issues/7/comments first",
		"PATCH /repos/example-org/homelab/issues/comments/9 second",
		"PATCH /repos/example-org/homelab/issues/comments/10 gone",
	}
	if !slices.Equal(writes, want) {
		t.Fatalf("writes = %q", writes)
	}
}

func TestPullRequestQueueRequiresCompleteLabels(t *testing.T) {
	for _, labels := range []string{`null`, `{}`, `{"nodes":[],"pageInfo":{"hasNextPage":true}}`} {
		t.Run(labels, func(t *testing.T) {
			gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprintf(w, `{"data":{"repository":{"pullRequest":{"id":"PR_7","state":"OPEN","headRefOid":"sha-1","baseRefName":"main","labels":%s}}}}`, labels)
			}))
			defer gh.Close()
			c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
			if _, err := c.PullRequestQueue(context.Background(), "example-org/homelab", 7); err == nil {
				t.Fatal("accepted an incomplete label snapshot")
			}
		})
	}
}

// pagedServer serves n numbered items from path, per_page at a time.
func pagedServer(t *testing.T, path string, n int, item func(i int) string) (*httptest.Server, *[]string) {
	var mu sync.Mutex
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r.URL.RawQuery)
		mu.Unlock()
		if r.URL.Path != path {
			http.NotFound(w, r)
			return
		}
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		per, _ := strconv.Atoi(r.URL.Query().Get("per_page"))
		var items []string
		for i := (page-1)*per + 1; i <= min(page*per, n); i++ {
			items = append(items, item(i))
		}
		_, _ = w.Write([]byte("[" + strings.Join(items, ",") + "]"))
	}))
	t.Cleanup(srv.Close)
	return srv, &seen
}

func TestListPullRequestsReadsEveryPage(t *testing.T) {
	srv, seen := pagedServer(t, "/repos/example-org/homelab/pulls", 250, func(i int) string {
		return fmt.Sprintf(`{"number": %d, "state": "open", "created_at": "2026-11-14T11:00:00Z", "updated_at": "2026-11-14T12:00:00Z", "user": {"login": "u"},
			"head": {"ref": "b", "sha": "s"}, "base": {"ref": "main"}}`, i)
	})
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	prs, complete, err := c.ListPullRequests(context.Background(), "example-org/homelab", "main")
	if err != nil || !complete || len(prs) != 250 || prs[249].Number != 250 || prs[0].UpdatedAt.IsZero() || prs[0].CreatedAt.IsZero() {
		t.Fatalf("%d PRs, complete %v, err %v", len(prs), complete, err)
	}
	if len(*seen) != 3 || !strings.Contains((*seen)[2], "page=3") || !strings.Contains((*seen)[0], "sort=created") {
		t.Fatalf("queries = %v", *seen)
	}
}

func clientCheck(t *testing.T, ok bool, format string, args ...any) {
	t.Helper()
	if !ok {
		t.Fatalf(format, args...)
	}
}

// pagedReplyClient can put a complete valid page before the response under test.
func pagedReplyClient(t *testing.T, later bool, code int, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer ghs_fixed" {
			t.Error("history read omitted repository authentication")
		}
		if later && r.URL.Query().Get("page") == "1" {
			items := make([]map[string]any, MaxListed)
			for i := range items {
				items[i] = map[string]any{"id": i + 1, "event": "labeled", "created_at": "2026-11-15T12:00:00Z", "label": map[string]string{"name": "human"}}
			}
			_ = json.NewEncoder(w).Encode(items)
			return
		}
		w.WriteHeader(code)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
}

func TestPullRequestCreationBoundary(t *testing.T) {
	for _, tc := range []struct {
		body string
		code int
		ok   bool
	}{
		{`[{"state":"closed","draft":true,"created_at":"2026-11-15T12:00:00Z"}]`, 200, true},
		{`[]`, 200, true},
		{`null`, 200, false},
		{`[{}]`, 200, false},
		{`{}`, 200, false},
		{`[{"created_at":"invalid"}]`, 200, false},
		{`[{"created_at":"2026-11-15T12:00:00Z"},{"created_at":"2026-11-15T12:00:00Z"}]`, 200, false},
		{`unavailable`, 502, false},
	} {
		t.Run(tc.body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/repos/example-org/homelab/pulls" || r.URL.RawQuery != "direction=desc&per_page=1&sort=created&state=all" || r.Header.Get("Authorization") != "Bearer ghs_fixed" {
					t.Errorf("unexpected creation boundary request: %s %s", r.Method, r.URL)
				}
				w.WriteHeader(tc.code)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer srv.Close()
			got, err := (&Client{BaseURL: srv.URL, Tokens: fixedTokens{}}).PullRequestCreationBoundary(context.Background(), "example-org/homelab")
			want := time.Time{}
			if tc.ok && tc.body != `[]` {
				want = time.Date(2026, 11, 15, 12, 0, 0, 0, time.UTC)
			}
			clientCheck(t, (err == nil) == tc.ok && got.Equal(want), "creation boundary = %v, %v; want %v, success=%v", got, err, want, tc.ok)
		})
	}
}

func TestPullRequestReviewsReadsEveryPage(t *testing.T) {
	srv, _ := pagedServer(t, "/repos/example-org/homelab/pulls/7/reviews", 230, func(i int) string {
		state := "COMMENTED"
		if i == 230 {
			state = "APPROVED"
		}
		return fmt.Sprintf(`{"user": {"login": "jomcgi"}, "state": %q, "commit_id": "sha-%d"}`, state, i)
	})
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	rv, err := c.PullRequestReviews(context.Background(), "example-org/homelab", 7)
	if err != nil || len(rv) != 230 || rv[229].State != "APPROVED" || rv[229].CommitSHA != "sha-230" {
		t.Fatalf("%d reviews, err %v", len(rv), err)
	}
	// Past the bound the listing fails instead of leaving the newest out.
	srv, _ = pagedServer(t, "/repos/example-org/homelab/pulls/7/reviews", MaxReviewPages*MaxListed+1, func(int) string {
		return `{"user": {"login": "x"}, "state": "COMMENTED"}`
	})
	c = &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	if _, err := c.PullRequestReviews(context.Background(), "example-org/homelab", 7); !errors.Is(err, ErrTooMany) {
		t.Fatalf("err = %v, want ErrTooMany", err)
	}
}

func TestPullRequestReviewsRejectsAMissingPage(t *testing.T) {
	for _, firstPage := range []bool{true, false} {
		t.Run(fmt.Sprint("first page ", firstPage), func(t *testing.T) {
			c := pagedReplyClient(t, !firstPage, 200, "null")
			reviews, err := c.PullRequestReviews(context.Background(), "example-org/homelab", 7)
			clientCheck(t, err != nil && reviews == nil, "missing page returned a usable snapshot: %v, %v", reviews, err)
		})
	}
}

func acknowledgementNode() map[string]any {
	return map[string]any{
		"__typename": "PullRequestReview", "id": "PRR_80", "author": map[string]string{"login": "jomcgi"},
		"editor": nil, "lastEditedAt": nil, "body": "factory hold acknowledgement", "state": "APPROVED",
		"commit":      map[string]string{"oid": "sha-1"},
		"pullRequest": map[string]any{"number": 7, "repository": map[string]string{"nameWithOwner": "Example-Org/Homelab"}},
	}
}

func TestPullRequestReviewAcknowledgementReadsOneReview(t *testing.T) {
	for editor, wantEditor := range map[string]string{"": "", "jomcgi": "jomcgi", "someone-else": "someone-else", "unknown": ""} {
		t.Run(editor, func(t *testing.T) {
			node := acknowledgementNode()
			if editor != "" {
				node["lastEditedAt"] = "2026-11-15T12:00:01Z"
				if editor != "unknown" {
					node["editor"] = map[string]string{"login": editor}
				}
			}
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var req struct {
					Query     string         `json:"query"`
					Variables map[string]any `json:"variables"`
				}
				if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
					t.Error(err)
					http.Error(w, "bad request", http.StatusBadRequest)
					return
				}
				if r.URL.Path != "/graphql" || r.Method != http.MethodPost || r.Header.Get("Authorization") != "Bearer ghs_fixed" || req.Variables["id"] != "PRR_80" {
					t.Errorf("wrong acknowledgement request: %s %s, variables %+v", r.Method, r.URL.Path, req.Variables)
				}
				for _, field := range []string{"node(id:", "... on PullRequestReview", "author", "editor", "lastEditedAt", "body", "state", "commit", "pullRequest", "nameWithOwner"} {
					if !strings.Contains(req.Query, field) {
						t.Errorf("acknowledgement query omitted %s", field)
					}
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"node": node}})
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			got, err := c.PullRequestReviewAcknowledgement(context.Background(), "example-org/homelab", 7, "PRR_80")
			clientCheck(t, err == nil && got != nil && got.Author == "jomcgi" && got.Body == "factory hold acknowledgement" && got.State == "APPROVED" && got.CommitSHA == "sha-1", "acknowledgement = %+v, %v", got, err)
			clientCheck(t, got.Editor == wantEditor && (got.LastEditedAt == nil) == (editor == ""), "edit provenance changed: %+v", got)
			clientCheck(t, got.LastEditedAt == nil || got.LastEditedAt.Equal(time.Date(2026, 11, 15, 12, 0, 1, 0, time.UTC)), "edit timestamp = %v", got.LastEditedAt)
		})
	}
}

func TestPullRequestReviewAcknowledgementRejectsIncompleteOrForeignNode(t *testing.T) {
	for name, change := range map[string]func(map[string]any){
		"wrong type": func(n map[string]any) { n["__typename"] = "IssueComment" },
		"wrong node": func(n map[string]any) { n["id"] = "PRR_81" },
		"wrong PR":   func(n map[string]any) { n["pullRequest"].(map[string]any)["number"] = 8 },
		"wrong repo": func(n map[string]any) {
			n["pullRequest"].(map[string]any)["repository"] = map[string]string{"nameWithOwner": "other/repo"}
		},
		"missing PR":                   func(n map[string]any) { n["pullRequest"] = nil },
		"missing repo":                 func(n map[string]any) { n["pullRequest"].(map[string]any)["repository"] = nil },
		"missing editor field":         func(n map[string]any) { delete(n, "editor") },
		"missing edit timestamp field": func(n map[string]any) { delete(n, "lastEditedAt") },
		"malformed edit timestamp":     func(n map[string]any) { n["lastEditedAt"] = "not a timestamp" },
		"missing body":                 func(n map[string]any) { delete(n, "body") },
		"missing state":                func(n map[string]any) { delete(n, "state") },
	} {
		t.Run(name, func(t *testing.T) {
			node := acknowledgementNode()
			change(node)
			body, _ := json.Marshal(map[string]any{"data": map[string]any{"node": node}})
			c := pagedReplyClient(t, false, 200, string(body))
			got, err := c.PullRequestReviewAcknowledgement(context.Background(), "example-org/homelab", 7, "PRR_80")
			clientCheck(t, err != nil && got == nil, "unusable node returned acknowledgement: %+v, %v", got, err)
		})
	}
	for _, reply := range []string{`{"data":{"node":null}}`, `{"data":{}}`, `{"data":null}`, `{"data":{"node":{}},"errors":[{"type":"FORBIDDEN","message":"no"}]}`} {
		t.Run(reply, func(t *testing.T) {
			c := pagedReplyClient(t, false, 200, reply)
			got, err := c.PullRequestReviewAcknowledgement(context.Background(), "example-org/homelab", 7, "PRR_80")
			clientCheck(t, err != nil && got == nil, "missing/error node returned acknowledgement: %+v, %v", got, err)
		})
	}
}

func TestPullRequestLabelEventsReadEveryPage(t *testing.T) {
	srv, seen := pagedServer(t, "/repos/example-org/homelab/issues/7/events", 250, func(i int) string {
		kind := "unlabeled"
		if i == 1 || i == 250 {
			kind = "labeled"
		}
		return fmt.Sprintf(`{"id":%d,"event":%q,"created_at":"2026-11-15T12:00:00Z","label":{"name":"needs-human"}}`, i, kind)
	})
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	events, err := c.PullRequestLabelEvents(context.Background(), "example-org/homelab", 7)
	want := []LabelEvent{
		{ID: 1, Label: "needs-human", CreatedAt: time.Date(2026, 11, 15, 12, 0, 0, 0, time.UTC)},
		{ID: 250, Label: "needs-human", CreatedAt: time.Date(2026, 11, 15, 12, 0, 0, 0, time.UTC)},
	}
	clientCheck(t, err == nil && reflect.DeepEqual(events, want), "label events = %+v, %v", events, err)
	clientCheck(t, len(*seen) == 3 && strings.Contains((*seen)[2], "page=3") && strings.Contains((*seen)[0], "per_page=100"), "queries = %v", *seen)
}

func TestPullRequestLabelEventsRejectIncompleteHistory(t *testing.T) {
	for _, body := range []string{
		`null`, `[null]`, `[{}]`,
		`[{"event":"labeled","created_at":"2026-11-15T12:00:00Z","label":{"name":"human"}}]`,
		`[{"id":1,"event":"labeled","label":{"name":"human"}}]`,
		`[{"id":1,"event":"labeled","created_at":"2026-11-15T12:00:00Z","label":{}}]`,
		`[{"id":1,"event":"labeled","created_at":"2026-11-15T12:00:00Z","label":null}]`,
		`[{"id":1,"event":"labeled","created_at":"invalid","label":{"name":"human"}}]`,
	} {
		for _, laterPage := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s later=%v", body, laterPage), func(t *testing.T) {
				c := pagedReplyClient(t, laterPage, 200, body)
				events, err := c.PullRequestLabelEvents(context.Background(), "example-org/homelab", 7)
				clientCheck(t, err != nil && events == nil, "incomplete history returned usable events: %+v, %v", events, err)
			})
		}
	}
}

func TestPullRequestLabelEventsBoundAllEvents(t *testing.T) {
	srv, seen := pagedServer(t, "/repos/example-org/homelab/issues/7/events", MaxIssueEventPages*MaxListed+1, func(i int) string {
		return fmt.Sprintf(`{"id":%d,"event":"closed"}`, i)
	})
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	events, err := c.PullRequestLabelEvents(context.Background(), "example-org/homelab", 7)
	clientCheck(t, errors.Is(err, ErrTooMany) && events == nil, "oversized history returned usable events: %+v, %v", events, err)
	clientCheck(t, len(*seen) == MaxIssueEventPages, "read %d pages, want %d", len(*seen), MaxIssueEventPages)
}

func TestPullRequestLabelEventsEmptyAndFailedPages(t *testing.T) {
	for failure, code := range map[bool]int{false: http.StatusOK, true: http.StatusBadGateway} {
		t.Run(fmt.Sprint("failure=", failure), func(t *testing.T) {
			c := pagedReplyClient(t, failure, code, `[]`)
			events, err := c.PullRequestLabelEvents(context.Background(), "example-org/homelab", 7)
			clientCheck(t, !failure || (err != nil && events == nil), "failed later page returned usable history: %+v, %v", events, err)
			clientCheck(t, failure || (err == nil && len(events) == 0), "empty history = %+v, %v", events, err)
		})
	}
}

func TestTreeHeadsAndTruncatedCompare(t *testing.T) {
	var gql map[string]any
	var query string
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/repos/example-org/homelab/git/trees/base-1" && r.URL.Query().Get("recursive") == "1":
			_, _ = w.Write([]byte(`{"tree": [{"path": "REVIEW.md", "type": "blob"}, {"path": "docs", "type": "tree"},
				{"path": "docs/REVIEW.md", "type": "blob"}], "truncated": false}`))
		case r.URL.Path == "/graphql":
			var req struct {
				Query     string         `json:"query"`
				Variables map[string]any `json:"variables"`
			}
			_ = json.NewDecoder(r.Body).Decode(&req)
			gql, query = req.Variables, req.Query
			_, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":{"timelineItems":{"pageInfo":{"hasPreviousPage":true},"nodes":[
				{"__typename":"PullRequestCommit","commit":{"oid":"a","checkSuites":{"nodes":[
					{"app":{"databaseId":5758326},"checkRuns":{"nodes":[{"name":"factory review","conclusion":"FAILURE"}]}}]}}},
				{"__typename":"PullRequestCommit","commit":{"oid":"b","checkSuites":{"nodes":[
					{"app":{"databaseId":5758326},"checkRuns":{"nodes":[{"name":"factory review","conclusion":"CANCELLED"}]}},
					{"app":{"databaseId":1},"checkRuns":{"nodes":[{"name":"factory review","conclusion":"SUCCESS"}]}}]}}},
				{"__typename":"HeadRefForcePushedEvent","beforeCommit":{"oid":"b","checkSuites":{"nodes":[]}},"afterCommit":{"oid":"c","checkSuites":{"nodes":[]}}},
				{"__typename":"PullRequestCommit","commit":{"oid":"d","checkSuites":{"nodes":[
					{"app":{"databaseId":5758326},"checkRuns":{"pageInfo":{"hasPreviousPage":true},"nodes":[
						{"name":"factory review","conclusion":"CANCELLED"},{"name":"factory review","conclusion":"CANCELLED"},
						{"name":"factory review","conclusion":"CANCELLED"},{"name":"factory review","conclusion":"CANCELLED"},
						{"name":"factory review","conclusion":"CANCELLED"}]}},
					{"app":{"databaseId":1},"checkRuns":{"pageInfo":{"hasPreviousPage":true},"nodes":[]}}]}}},
				{"__typename":"PullRequestCommit","commit":{"oid":"e","checkSuites":{"pageInfo":{"hasNextPage":true},"nodes":[
					{"app":{"databaseId":5758326},"checkRuns":{"nodes":[{"name":"factory review","conclusion":"CANCELLED"}]}}]}}},
				{"__typename":"PullRequestCommit","commit":{"oid":"f","checkSuites":{"nodes":[
					{"app":{"databaseId":1},"checkRuns":{"pageInfo":{"hasPreviousPage":true},"nodes":[]}}]}}}
			]}}}}}`))
		case strings.HasPrefix(r.URL.Path, "/repos/example-org/homelab/compare/"):
			var files []string
			for i := range MaxCompareFiles {
				files = append(files, fmt.Sprintf(`{"filename": "f%d.go", "status": "modified"}`, i))
			}
			_, _ = w.Write([]byte(`{"status": "ahead", "files": [` + strings.Join(files, ",") + `]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}, ETags: &ETagCache{}}
	ctx := context.Background()
	tree, err := c.Tree(ctx, "example-org/homelab", "base-1")
	if err != nil || !tree.Has("docs/REVIEW.md") || tree.Has("docs") || tree.Has("x/REVIEW.md") || c.ETags.Len() != 0 {
		t.Fatalf("tree = %+v, err %v, cached %d", tree, err, c.ETags.Len())
	}
	h, err := c.PullRequestHeads(ctx, "example-org/homelab", 7, CheckRunFilter{Name: "factory review", AppID: 5758326})
	if err != nil {
		t.Fatal(err)
	}
	// Five cancelled runs on d fill the read, and e has more suites than
	// it lists: either may hide a completed review, so both are read. f's
	// overflow is another App's.
	want := []Head{{SHA: "a", Checked: true}, {SHA: "b"}, {SHA: "b"}, {SHA: "c"}, {SHA: "d", Checked: true}, {SHA: "e", Checked: true}, {SHA: "f"}}
	if fmt.Sprint(h.Heads) != fmt.Sprint(want) || !h.Truncated || gql["appId"] != float64(5758326) || gql["check"] != "factory review" {
		t.Fatalf("heads = %+v, truncated %v, vars %v", h.Heads, h.Truncated, gql)
	}
	if !strings.Contains(query, "checkRuns(last: 5,") {
		t.Fatalf("the query does not read the newest check runs:\n%s", query)
	}
	cmp, err := c.CompareCommits(ctx, "example-org/homelab", "a", "c")
	if err != nil || !cmp.Truncated || len(cmp.Files) != MaxCompareFiles {
		t.Fatalf("compare = %d files, truncated %v, err %v", len(cmp.Files), cmp.Truncated, err)
	}
}

func TestCitedPullRequest(t *testing.T) {
	merged := `{"data":{"repository":{"pullRequest":{"number":75,"state":"MERGED",
		"merged":true,"mergedAt":"2026-11-15T15:35:36Z","baseRefName":"main","headRefOid":"sha-head",
		"mergeCommit":{"oid":"sha-merge"},"author":{"login":"renovate-app"},"mergedBy":{"login":"renovate-app"},
		"autoMergeRequest":null,"queued":{"filteredCount":1,"nodes":[{"actor":{"login":"renovate-app"}}]},
		"autoMerge":{"filteredCount":1,"nodes":[{"actor":{"login":"renovate-app"}}]},
		"commits":{"nodes":[{"commit":{"oid":"sha-head","author":{"user":{"login":"renovate-app[bot]"}},
		"signature":{"state":"VALID","wasSignedByGitHub":true}}}]}}}}}`
	for _, tc := range []struct {
		name, body string
		want       *CitedPullRequest
		err        error
	}{
		{"merged", merged, &CitedPullRequest{
			Number: 75, State: "MERGED", Merged: true, MergedAt: time.Date(2026, 11, 15, 15, 35, 36, 0, time.UTC),
			MergeCommitSHA: "sha-merge", BaseRef: "main", HeadSHA: "sha-head", Author: "renovate-app", MergedBy: "renovate-app",
			Queued: true, QueuedBy: "renovate-app", AutoMergeEnabledBy: "renovate-app",
			HeadCommitAuthor: "renovate-app[bot]", HeadSignature: "VALID", HeadSignedByGitHub: true,
		}, nil},
		{"missing", `{"data":{"repository":{"pullRequest":null}},"errors":[{"type":"NOT_FOUND","message":"no"}]}`, nil, ErrNotFound},
		// A last commit other than the head reports no signature.
		{"stale-commit", strings.Replace(merged, `"commit":{"oid":"sha-head"`, `"commit":{"oid":"sha-other"`, 1), &CitedPullRequest{
			Number: 75, State: "MERGED", Merged: true, MergedAt: time.Date(2026, 11, 15, 15, 35, 36, 0, time.UTC),
			MergeCommitSHA: "sha-merge", BaseRef: "main", HeadSHA: "sha-head", Author: "renovate-app", MergedBy: "renovate-app",
			Queued: true, QueuedBy: "renovate-app", AutoMergeEnabledBy: "renovate-app",
		}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var req struct {
					Query     string         `json:"query"`
					Variables map[string]any `json:"variables"`
				}
				_ = json.NewDecoder(r.Body).Decode(&req)
				if !strings.Contains(req.Query, "ADDED_TO_MERGE_QUEUE_EVENT") || req.Variables["number"] != float64(75) {
					t.Errorf("query = %q vars = %v", req.Query, req.Variables)
				}
				_, _ = w.Write([]byte(tc.body))
			}))
			defer gh.Close()
			c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
			got, err := c.CitedPullRequest(context.Background(), "example-org/homelab", 75)
			if !errors.Is(err, tc.err) || (tc.err == nil && err != nil) {
				t.Fatalf("err = %v, want %v", err, tc.err)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("got %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestActivationTrailerRequiresOneFinalTrailer(t *testing.T) {
	for _, tc := range []struct {
		message, want string
	}{
		{"fix: ship\n\nActivation: none", "none"},
		{"fix: ship\r\n\r\nActivation: enable\r\n after the migration", "enable after the migration"},
		{"fix: ship", ""},
		{"fix: ship\n\nActivation: ", ""},
		{"fix: ship\n\nActivation: none\nActivation: enable", ""},
		{"fix: ship\n\nActivation: none\n  Activation: enable", ""},
		{"fix: ship\n\nActivation: none\n\nMore prose.", ""},
		{"fix: ship\n\nActivation: none\nSigned-off-by: author", ""},
		{"fix: ship\n\nCo-Authored-By: A <a@example.com>\nSigned-off-by: B\nActivation: none", "none"},
		{"fix: ship\n\nCo-Authored-By: A\nActivation: enable\n after the migration", "enable after the migration"},
		{"fix: ship\n\nActivation: none\nCo-Authored-By: A\nActivation: none", ""},
		{"fix: ship\n\nSee the notes.\nActivation: none", ""},
		{"fix: ship\n\n continued\nActivation: none", ""},
	} {
		if got, ok := ActivationTrailer(tc.message); got != tc.want || ok != (tc.want != "") {
			t.Errorf("trailer(%q) = %q, %v; want %q", tc.message, got, ok, tc.want)
		}
	}
}

func TestRevertedMergeWindow(t *testing.T) {
	start := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	end := start.Add(7 * 24 * time.Hour)
	for _, tc := range []struct {
		at    time.Time
		match bool
	}{
		{start.Add(-time.Second), false},
		{end.Add(-time.Hour), true},
		{end, true},
		{end.Add(time.Second), false},
	} {
		t.Run(tc.at.String(), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/repos/org/repo/commits" || r.URL.Query().Get("sha") != "main" {
					t.Errorf("unexpected request %s", r.URL)
				}
				fmt.Fprintf(w, `[{"commit":{"message":"Undo change\n\nThis reverts commit abc.","committer":{"date":%q}}}]`, tc.at.Format(time.RFC3339))
			}))
			defer srv.Close()
			client := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			got, err := client.RevertedMerge(context.Background(), "org/repo", "main", "abc", start, end)
			if err != nil || (got != nil) != tc.match || (got != nil && !got.Equal(tc.at)) {
				t.Fatalf("revert = %v, err = %v", got, err)
			}
		})
	}
}
