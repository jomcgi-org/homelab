package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
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
				"headRefOid":"sha-1","baseRefName":"main","mergeQueueEntry":{"state":"QUEUED"}}}}}`))
		}
	}))
	defer gh.Close()
	c := &Client{BaseURL: gh.URL, Tokens: fixedTokens{}}
	ctx := context.Background()
	q, err := c.PullRequestQueue(ctx, "example-org/homelab", 7)
	if err != nil {
		t.Fatal(err)
	}
	if *q != (PullRequestQueue{NodeID: "PR_7", State: "open", HeadSHA: "sha-1", BaseRef: "main", MergeQueueState: "QUEUED"}) {
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
			_, _ = w.Write([]byte(`[{"user": {"login": "jomcgi"}, "state": "APPROVED", "commit_id": "sha-1", "body": "ok"}]`))
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
	if err != nil || len(rv) != 1 || rv[0] != (PullRequestReview{Author: "jomcgi", State: "APPROVED", CommitSHA: "sha-1", Body: "ok"}) {
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
		return fmt.Sprintf(`{"number": %d, "state": "open", "updated_at": "2026-11-14T12:00:00Z", "user": {"login": "u"},
			"head": {"ref": "b", "sha": "s"}, "base": {"ref": "main"}}`, i)
	})
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	prs, complete, err := c.ListPullRequests(context.Background(), "example-org/homelab", "main")
	if err != nil || !complete || len(prs) != 250 || prs[249].Number != 250 || prs[0].UpdatedAt.IsZero() {
		t.Fatalf("%d PRs, complete %v, err %v", len(prs), complete, err)
	}
	if len(*seen) != 3 || !strings.Contains((*seen)[2], "page=3") || !strings.Contains((*seen)[0], "sort=created") {
		t.Fatalf("queries = %v", *seen)
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

func TestTreeHeadsAndTruncatedCompare(t *testing.T) {
	var gql map[string]any
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/repos/example-org/homelab/git/trees/base-1" && r.URL.Query().Get("recursive") == "1":
			_, _ = w.Write([]byte(`{"tree": [{"path": "REVIEW.md", "type": "blob"}, {"path": "docs", "type": "tree"},
				{"path": "docs/REVIEW.md", "type": "blob"}], "truncated": false}`))
		case r.URL.Path == "/graphql":
			var req struct {
				Variables map[string]any `json:"variables"`
			}
			_ = json.NewDecoder(r.Body).Decode(&req)
			gql = req.Variables
			_, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":{"timelineItems":{"pageInfo":{"hasPreviousPage":true},"nodes":[
				{"__typename":"PullRequestCommit","commit":{"oid":"a","checkSuites":{"nodes":[
					{"app":{"databaseId":5758326},"checkRuns":{"nodes":[{"name":"factory review","conclusion":"FAILURE"}]}}]}}},
				{"__typename":"PullRequestCommit","commit":{"oid":"b","checkSuites":{"nodes":[
					{"app":{"databaseId":5758326},"checkRuns":{"nodes":[{"name":"factory review","conclusion":"CANCELLED"}]}},
					{"app":{"databaseId":1},"checkRuns":{"nodes":[{"name":"factory review","conclusion":"SUCCESS"}]}}]}}},
				{"__typename":"HeadRefForcePushedEvent","beforeCommit":{"oid":"b","checkSuites":{"nodes":[]}},"afterCommit":{"oid":"c","checkSuites":{"nodes":[]}}}
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
	want := []Head{{SHA: "a", Checked: true}, {SHA: "b"}, {SHA: "b"}, {SHA: "c"}}
	if fmt.Sprint(h.Heads) != fmt.Sprint(want) || !h.Truncated || gql["appId"] != float64(5758326) || gql["check"] != "factory review" {
		t.Fatalf("heads = %+v, truncated %v, vars %v", h.Heads, h.Truncated, gql)
	}
	cmp, err := c.CompareCommits(ctx, "example-org/homelab", "a", "c")
	if err != nil || !cmp.Truncated || len(cmp.Files) != MaxCompareFiles {
		t.Fatalf("compare = %d files, truncated %v, err %v", len(cmp.Files), cmp.Truncated, err)
	}
}
