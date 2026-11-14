package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type staticBearer string

func (s staticBearer) Token() (string, error) { return string(s), nil }

// fakeBroker mints a new token per request, valid for an hour from now().
type fakeBroker struct {
	mu      sync.Mutex
	now     func() time.Time
	mints   int
	bearers []string
	repos   []string
}

func (b *fakeBroker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if r.URL.Path != "/github/connector-token" {
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
		if strings.Contains(req.Query, "enqueuePullRequest") {
			enqueued = req.Variables
			_, _ = w.Write([]byte(`{"data":{"enqueuePullRequest":{"mergeQueueEntry":{"state":"QUEUED"}}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":{
			"id":"PR_5","number":5,"url":"https://github.com/example-org/homelab/pull/5","state":"MERGED","merged":true,
			"mergedAt":"2026-11-14T12:00:00Z","headRefOid":"b","headRefName":"factory/HL-1","baseRefName":"main",
			"mergeCommit":{"oid":"m"},"mergeQueueEntry":null,
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
	if !d.Merged || d.State != "closed" || d.MergeCommitSHA != "m" || d.MergeQueueState != "" || d.HeadSHA != "b" {
		t.Fatalf("detail = %+v", d.PullRequest)
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
	defer mu.Unlock()
	if enqueued["id"] != "PR_5" || enqueued["head"] != "b" {
		t.Fatalf("enqueue variables = %v, want the node ID and expected head", enqueued)
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
