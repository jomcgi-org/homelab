package status

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/config"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

type fakeStore struct {
	items []store.StatusItem
	names []string
}

func (s fakeStore) StatusItems(_ context.Context, names []string) ([]store.StatusItem, error) {
	want := s.names
	if want == nil {
		want = []string{"homelab"}
	}
	if !reflect.DeepEqual(names, want) {
		return nil, errors.New("wrong binding")
	}
	return s.items, nil
}

type countingStatusGitHub struct{ calls int }

func (g *countingStatusGitHub) StatusesForLanding(context.Context, string, []int, github.CheckRunFilter) (map[int]*github.PRStatus, error) {
	g.calls++
	return map[int]*github.PRStatus{}, nil
}

func TestSourceBoundsGitHubQueriesAcrossAllBindings(t *testing.T) {
	for _, count := range []int{4, 5} {
		st := fakeStore{}
		gh := &countingStatusGitHub{}
		source := &Source{Plan: fakePlan{}, GitHub: gh}
		for i := range count {
			name := fmt.Sprintf("binding-%d", i)
			st.names = append(st.names, name)
			st.items = append(st.items, store.StatusItem{BindingName: name, IssueID: int64(i + 1), PRNumber: i + 1})
			source.Bindings = append(source.Bindings, config.Binding{Name: name, ItsaplanProject: "HOME", Owner: "org", Repo: name})
		}
		source.Store = st
		_, err := source.Load(context.Background())
		if count == 4 {
			if err != nil || gh.calls != 4 {
				t.Fatalf("calls=%d err=%v", gh.calls, err)
			}
		} else if err == nil || gh.calls != 0 {
			t.Fatalf("over-budget refresh sent %d requests: %v", gh.calls, err)
		}
	}
}

type fakePlan struct{}

func (fakePlan) NeedsYou(_ context.Context, project, username string) ([]itsaplan.IssueSummary, error) {
	if project != "HOME" || username != "joe" {
		return nil, errors.New("wrong task filter")
	}
	return []itsaplan.IssueSummary{{ID: 20, Identifier: "HOME-20", Title: "Decision", CreatedAt: "2026-11-14T10:00:00Z"}}, nil
}

func (fakePlan) GetIssueSummary(_ context.Context, id int64) (*itsaplan.IssueSummary, error) {
	return &itsaplan.IssueSummary{Title: "Change"}, nil
}

type fakeGitHub struct{}

func (fakeGitHub) StatusesForLanding(_ context.Context, repo string, numbers []int, reviewer github.CheckRunFilter) (map[int]*github.PRStatus, error) {
	if repo != "jomcgi-org/homelab" || !reflect.DeepEqual(numbers, []int{8000}) || reviewer.AppID != 123 || reviewer.Name != "factory review" {
		return nil, errors.New("wrong PR read")
	}
	return map[int]*github.PRStatus{8000: {Number: 8000, URL: "https://github.com/jomcgi-org/homelab/pull/8000", CI: "success", Review: "in_progress", Queue: "not_queued"}}, nil
}

func TestHandlerFieldsContainOnlySummaries(t *testing.T) {
	merged := time.Date(2026, 11, 14, 10, 0, 0, 0, time.UTC)
	source := &Source{
		Store: fakeStore{items: []store.StatusItem{
			{BindingName: "homelab", IssueID: 24, Key: "HOME-24", State: "review", ReviewRounds: 2, PRNumber: 8000},
			{BindingName: "homelab", IssueID: 25, Key: "HOME-25", State: "active"},
			{BindingName: "homelab", IssueID: 19, Key: "HOME-19", PRNumber: 7999, PRURL: "https://github.com/jomcgi-org/homelab/pull/7999", MergedAt: &merged},
		}}, Plan: fakePlan{}, GitHub: fakeGitHub{},
		Bindings: []config.Binding{{Name: "homelab", ItsaplanProject: "HOME", Owner: "jomcgi-org", Repo: "homelab", ReviewerCheck: &config.Check{Name: "factory review", AppID: 123}}},
		IssueURL: "https://factory-plan.jomcgi.dev/issue/{issueId}",
	}
	h := &Handler{Load: source.Load}
	h.refresh(context.Background())
	r := httptest.NewRecorder()
	h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
	if r.Code != 200 || r.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("response = %d %s", r.Code, r.Body)
	}
	var body map[string]any
	if err := json.Unmarshal(r.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	assertFields(t, body, "needsYou", "inFlight", "shipped", "generatedAt")
	need := body["needsYou"].([]any)[0].(map[string]any)
	assertFields(t, need, "key", "title", "priority", "url", "createdAt")
	if need["url"] != "https://factory-plan.jomcgi.dev/issue/20" {
		t.Fatal(need)
	}
	work := body["inFlight"].([]any)[0].(map[string]any)
	assertFields(t, work, "key", "title", "state", "reviewRounds", "pr")
	assertFields(t, work["pr"].(map[string]any), "number", "url", "ci", "review", "queue")
	if work["reviewRounds"] != float64(2) || body["inFlight"].([]any)[1].(map[string]any)["pr"] != nil {
		t.Fatal(body)
	}
	shipped := body["shipped"].([]any)[0].(map[string]any)
	assertFields(t, shipped, "key", "title", "pr", "mergedAt")
	assertFields(t, shipped["pr"].(map[string]any), "number", "url")
	if shipped["mergedAt"] != "2026-11-14T10:00:00Z" {
		t.Fatal(shipped)
	}
}

func assertFields(t *testing.T, fields map[string]any, want ...string) {
	t.Helper()
	var got []string
	for k := range fields {
		got = append(got, k)
	}
	sort.Strings(got)
	sort.Strings(want)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("fields = %v, want %v", got, want)
	}
}

func TestCacheRefreshesOnTicksAndRequestsNeverReadUpstream(t *testing.T) {
	if RefreshInterval != 30*time.Second {
		t.Fatal("cache interval changed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	calls := make(chan context.Context, 4)
	results := make(chan error)
	h := &Handler{Load: func(ctx context.Context) (*Snapshot, error) {
		calls <- ctx
		err := <-results
		return &Snapshot{NeedsYou: []NeedsYou{}, InFlight: []InFlight{}, Shipped: []Shipped{}}, err
	}}
	ticks := make(chan time.Time)
	done := make(chan struct{})
	go func() { h.run(ctx, ticks); close(done) }()
	readCtx := <-calls
	deadline, ok := readCtx.Deadline()
	if !ok || time.Until(deadline) > ReadTimeout {
		t.Fatal("unbounded refresh")
	}
	check := func(want int) {
		t.Helper()
		r := httptest.NewRecorder()
		h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
		if r.Code != want {
			t.Fatalf("status = %d, want %d", r.Code, want)
		}
	}
	check(503)
	results <- nil
	// A tick is accepted only after the first refresh publishes its result.
	ticks <- time.Now()
	<-calls
	for range 20 {
		check(200)
	}
	select {
	case <-calls:
		t.Fatal("request caused a read")
	default:
	}
	results <- errors.New("private token and upstream response")
	ticks <- time.Now()
	<-calls
	check(503)
	results <- nil
	cancel()
	<-done
	check(503)
}

func TestCacheRemainsAvailableDuringSlowerRefresh(t *testing.T) {
	for _, elapsed := range []time.Duration{2 * time.Second, ReadTimeout} {
		t.Run(elapsed.String(), func(t *testing.T) {
			var nanos atomic.Int64
			h := &Handler{
				now:  func() time.Time { return time.Unix(0, nanos.Load()) },
				Load: func(context.Context) (*Snapshot, error) { return &Snapshot{}, nil },
			}
			h.refresh(context.Background())
			started, finish, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
			h.Load = func(context.Context) (*Snapshot, error) {
				close(started)
				<-finish
				return &Snapshot{}, nil
			}
			nanos.Store(int64(RefreshInterval))
			go func() { h.refresh(context.Background()); close(done) }()
			<-started
			nanos.Store(int64(RefreshInterval + elapsed))
			r := httptest.NewRecorder()
			h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
			// A refresh may extend availability only through its read deadline.
			want := http.StatusOK
			if elapsed == ReadTimeout {
				want = http.StatusServiceUnavailable
			}
			close(finish)
			<-done
			if r.Code != want {
				t.Fatalf("during refresh = %d, want %d", r.Code, want)
			}
			r = httptest.NewRecorder()
			h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
			if r.Code != http.StatusOK {
				t.Fatalf("after refresh = %d", r.Code)
			}
		})
	}
}

func TestCanceledStatusLoopDoesNotReadOrPublish(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	h := &Handler{Load: func(context.Context) (*Snapshot, error) {
		t.Fatal("canceled status loop read upstream")
		return nil, nil
	}}
	h.run(ctx, make(chan time.Time))

	ctx, cancel = context.WithCancel(context.Background())
	h.Load = func(context.Context) (*Snapshot, error) {
		cancel()
		return &Snapshot{}, nil
	}
	h.refresh(ctx)
	r := httptest.NewRecorder()
	h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
	if r.Code != http.StatusServiceUnavailable {
		t.Fatal("published a canceled refresh")
	}
}

func TestWriterTransportFencesEveryRequest(t *testing.T) {
	var reads atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reads.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()
	var checks int
	var writerErr error
	transport := &WriterTransport{Begin: func(context.Context, string) error {
		checks++
		return writerErr
	}}
	client := &http.Client{Transport: transport}
	// The scaffold, listing, title, broker and GraphQL requests all pass the
	// guard, including repeated requests by the GitHub client's 401 retry.
	for _, path := range []string{"/projects/HOME", "/projects/HOME/issues", "/issues/24", "/github/connector-token", "/graphql", "/graphql"} {
		method := http.MethodGet
		if path == "/graphql" {
			method = http.MethodPost
		}
		req := httptest.NewRequest(method, srv.URL+path, nil)
		req.RequestURI = ""
		res, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
	}
	if checks != 6 || reads.Load() != 6 {
		t.Fatalf("checks=%d reads=%d", checks, reads.Load())
	}
	journalErr := errors.New("journal unavailable")
	writerErr = journalErr
	if _, err := client.Get(srv.URL); !errors.Is(err, journalErr) {
		t.Fatalf("failed journal: %v", err)
	}
	if reads.Load() != 6 {
		t.Fatal("unjournaled request reached upstream")
	}
	writerErr = store.ErrNotWriter
	if _, err := client.Get(srv.URL); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("lost writer: %v", err)
	}
	if checks != 8 || reads.Load() != 6 {
		t.Fatal("lost writer reached upstream")
	}
	transport.Begin = nil
	if _, err := client.Get(srv.URL); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("missing writer: %v", err)
	}
	if reads.Load() != 6 {
		t.Fatal("missing writer reached upstream")
	}
}

type statusTestBearer struct{}

func (statusTestBearer) Token() (string, error) { return "test-service-account", nil }

func TestStatusClientsJournalReadsAndAuthRetriesBeforeSending(t *testing.T) {
	var mu sync.Mutex
	attempts := map[string]int{}
	paths := map[string]int{}
	ids := map[string]string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		id := r.Header.Get("X-Request-ID")
		paths[r.URL.Path]++
		if !strings.HasPrefix(id, "status-read/") || attempts[id] != paths[r.URL.Path] {
			t.Errorf("request sent without committed attempt: path=%s id=%s", r.URL.Path, id)
		}
		if previous := ids[r.URL.Path]; previous != "" && previous != id {
			t.Error("retry changed message ID")
		}
		ids[r.URL.Path] = id
		switch r.URL.Path {
		case "/issues/24":
			_, _ = w.Write([]byte(`{"id":24,"title":"Landing"}`))
		case "/github/connector-token":
			_ = json.NewEncoder(w).Encode(map[string]any{"repository": "org/repo", "token": fmt.Sprintf("test-token-%d", paths[r.URL.Path]), "expires_at": time.Now().Add(time.Hour)})
		case "/graphql":
			if paths[r.URL.Path] == 1 {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			_, _ = w.Write([]byte(`{"data":{"repository":{"p1":{"number":1,"url":"https://github.com/org/repo/pull/1"}}}}`))
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
		}
	}))
	defer srv.Close()
	httpClient := &http.Client{Transport: &WriterTransport{Begin: func(_ context.Context, id string) error {
		mu.Lock()
		defer mu.Unlock()
		attempts[id]++
		return nil
	}}}
	plan := &itsaplan.Client{BaseURL: srv.URL, HTTP: httpClient, Coordinator: "coord", Keys: map[string]string{"coord": "test-key"}}
	for range 2 {
		if _, err := plan.GetIssueSummary(context.Background(), 24); err != nil {
			t.Fatal(err)
		}
	}
	gh := &github.Client{
		BaseURL: srv.URL, HTTP: httpClient,
		Tokens: &github.BrokerTokens{URL: srv.URL, HTTP: httpClient, Bearer: statusTestBearer{}},
	}
	if _, err := gh.StatusesForLanding(context.Background(), "org/repo", []int{1}, github.CheckRunFilter{}); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(attempts) != 3 || len(paths) != 3 {
		t.Fatalf("attempt identities=%d, paths=%v", len(attempts), paths)
	}
	for _, count := range attempts {
		if count != 2 {
			t.Fatalf("retry count=%d", count)
		}
	}
}

func TestReadMessageIDIncludesResourceAndQueryButNotCredentials(t *testing.T) {
	request := func(url, body, token string) *http.Request {
		r, err := http.NewRequest(http.MethodPost, url, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		r.Header.Set("Authorization", "Bearer "+token)
		return r
	}
	first, err := readMessageID(request("https://github.test/graphql", `{"number":1}`, "old"))
	if err != nil {
		t.Fatal(err)
	}
	retry, err := readMessageID(request("https://github.test/graphql", `{"number":1}`, "new"))
	if err != nil || first != retry {
		t.Fatalf("retry=%s err=%v", retry, err)
	}
	for _, r := range []*http.Request{
		request("https://github.test/graphql", `{"number":2}`, "new"),
		request("https://other.test/graphql", `{"number":1}`, "new"),
	} {
		id, err := readMessageID(r)
		if err != nil || id == first {
			t.Fatalf("different read reused ID: %s %v", id, err)
		}
	}
}

func TestWriterTransportCancelsAnInProgressRead(t *testing.T) {
	started := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		close(started)
		<-r.Context().Done()
	}))
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	client := &http.Client{Transport: &WriterTransport{Begin: func(context.Context, string) error { return nil }}}
	finished := make(chan error, 1)
	go func() {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL, nil)
		res, err := client.Do(req)
		if res != nil {
			res.Body.Close()
		}
		finished <- err
	}()
	<-started
	cancel()
	if err := <-finished; !errors.Is(err, context.Canceled) {
		t.Fatalf("in-progress read: %v", err)
	}
}

func TestHandlerRejectsOtherRoutesAndMethods(t *testing.T) {
	h := &Handler{}
	for _, test := range []struct {
		method, path string
		code         int
	}{
		{"POST", "/status", http.StatusMethodNotAllowed},
		{"GET", "/metrics", http.StatusNotFound},
		{"GET", "/status", http.StatusServiceUnavailable},
	} {
		r := httptest.NewRecorder()
		h.ServeHTTP(r, httptest.NewRequest(test.method, test.path, nil))
		if r.Code != test.code || strings.Contains(r.Body.String(), "token") {
			t.Fatalf("response = %d %s", r.Code, r.Body)
		}
	}
}

func TestCacheExpiresAfterThirtySeconds(t *testing.T) {
	now := time.Now()
	h := &Handler{now: func() time.Time { return now }, Load: func(context.Context) (*Snapshot, error) {
		return &Snapshot{}, nil
	}}
	h.refresh(context.Background())
	now = now.Add(30*time.Second - time.Nanosecond)
	r := httptest.NewRecorder()
	h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
	if r.Code != 200 {
		t.Fatal(r.Code)
	}
	now = now.Add(time.Nanosecond)
	r = httptest.NewRecorder()
	h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
	if r.Code != 503 {
		t.Fatal("expired cache was served")
	}
}
