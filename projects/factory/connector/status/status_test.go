package status

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/config"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

type fakeStore struct { items []store.StatusItem }
func (s fakeStore) StatusItems(_ context.Context, names []string) ([]store.StatusItem, error) {
	if !reflect.DeepEqual(names, []string{"homelab"}) { return nil, errors.New("wrong binding") }
	return s.items, nil
}

type fakePlan struct{}
func (fakePlan) NeedsYou(_ context.Context, project, username string) ([]itsaplan.IssueSummary, error) {
	if project != "HOME" || username != "joe" { return nil, errors.New("wrong task filter") }
	return []itsaplan.IssueSummary{{ID: 20, Identifier: "HOME-20", Title: "Decision", CreatedAt: "2026-11-14T10:00:00Z"}}, nil
}
func (fakePlan) GetIssueSummary(_ context.Context, id int64) (*itsaplan.IssueSummary, error) {
	return &itsaplan.IssueSummary{Title: "Change"}, nil
}

type fakeGitHub struct{}
func (fakeGitHub) Status(_ context.Context, repo string, number int, reviewer github.CheckRunFilter) (*github.PRStatus, error) {
	if repo != "jomcgi-org/homelab" || number != 8000 || reviewer.AppID != 123 || reviewer.Name != "factory review" { return nil, errors.New("wrong PR read") }
	return &github.PRStatus{Number: number, URL: "https://github.com/jomcgi-org/homelab/pull/8000", CI: "success", Review: "in_progress", Queue: "not_queued"}, nil
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
	if r.Code != 200 || r.Header().Get("Content-Type") != "application/json" { t.Fatalf("response = %d %s", r.Code, r.Body) }
	var body map[string]any
	if err := json.Unmarshal(r.Body.Bytes(), &body); err != nil { t.Fatal(err) }
	assertFields(t, body, "needsYou", "inFlight", "shipped", "generatedAt")
	need := body["needsYou"].([]any)[0].(map[string]any)
	assertFields(t, need, "key", "title", "priority", "url", "createdAt")
	if need["url"] != "https://factory-plan.jomcgi.dev/issue/20" { t.Fatal(need) }
	work := body["inFlight"].([]any)[0].(map[string]any)
	assertFields(t, work, "key", "title", "state", "reviewRounds", "pr")
	assertFields(t, work["pr"].(map[string]any), "number", "url", "ci", "review", "queue")
	if work["reviewRounds"] != float64(2) || body["inFlight"].([]any)[1].(map[string]any)["pr"] != nil { t.Fatal(body) }
	shipped := body["shipped"].([]any)[0].(map[string]any)
	assertFields(t, shipped, "key", "title", "pr", "mergedAt")
	assertFields(t, shipped["pr"].(map[string]any), "number", "url")
	if shipped["mergedAt"] != "2026-11-14T10:00:00Z" { t.Fatal(shipped) }
}

func assertFields(t *testing.T, fields map[string]any, want ...string) {
	t.Helper()
	var got []string
	for k := range fields { got = append(got, k) }
	sort.Strings(got)
	sort.Strings(want)
	if !reflect.DeepEqual(got, want) { t.Fatalf("fields = %v, want %v", got, want) }
}

func TestCacheRefreshesOnTicksAndRequestsNeverReadUpstream(t *testing.T) {
	if RefreshInterval != 30*time.Second { t.Fatal("cache interval changed") }
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
	if !ok || time.Until(deadline) > ReadTimeout { t.Fatal("unbounded refresh") }
	check := func(want int) {
		t.Helper()
		r := httptest.NewRecorder()
		h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
		if r.Code != want { t.Fatalf("status = %d, want %d", r.Code, want) }
	}
	check(503)
	results <- nil
	// A tick is accepted only after the first refresh publishes its result.
	ticks <- time.Now()
	<-calls
	for range 20 { check(200) }
	select { case <-calls: t.Fatal("request caused a read"); default: }
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
			h := &Handler{now: func() time.Time { return time.Unix(0, nanos.Load()) },
				Load: func(context.Context) (*Snapshot, error) { return &Snapshot{}, nil }}
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
			if elapsed == ReadTimeout { want = http.StatusServiceUnavailable }
			close(finish)
			<-done
			if r.Code != want { t.Fatalf("during refresh = %d, want %d", r.Code, want) }
			r = httptest.NewRecorder()
			h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
			if r.Code != http.StatusOK { t.Fatalf("after refresh = %d", r.Code) }
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
	if r.Code != http.StatusServiceUnavailable { t.Fatal("published a canceled refresh") }
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
	transport := &WriterTransport{Check: func(context.Context) error {
		checks++
		return writerErr
	}}
	client := &http.Client{Transport: transport}
	// The scaffold, listing, title, broker and GraphQL requests all pass the
	// guard, including repeated requests by the GitHub client's 401 retry.
	for _, path := range []string{"/projects/HOME", "/projects/HOME/issues", "/issues/24", "/github/connector-token", "/graphql", "/graphql"} {
		method := http.MethodGet
		if path == "/graphql" { method = http.MethodPost }
		req := httptest.NewRequest(method, srv.URL+path, nil)
		req.RequestURI = ""
		res, err := client.Do(req)
		if err != nil { t.Fatal(err) }
		res.Body.Close()
	}
	if checks != 6 || reads.Load() != 6 { t.Fatalf("checks=%d reads=%d", checks, reads.Load()) }
	writerErr = store.ErrNotWriter
	if _, err := client.Get(srv.URL); !errors.Is(err, store.ErrNotWriter) { t.Fatalf("lost writer: %v", err) }
	if checks != 7 || reads.Load() != 6 { t.Fatal("lost writer reached upstream") }
	transport.Check = nil
	if _, err := client.Get(srv.URL); !errors.Is(err, store.ErrNotWriter) { t.Fatalf("missing writer: %v", err) }
	if reads.Load() != 6 { t.Fatal("missing writer reached upstream") }
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
	client := &http.Client{Transport: &WriterTransport{Check: func(context.Context) error { return nil }}}
	finished := make(chan error, 1)
	go func() {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL, nil)
		res, err := client.Do(req)
		if res != nil { res.Body.Close() }
		finished <- err
	}()
	<-started
	cancel()
	if err := <-finished; !errors.Is(err, context.Canceled) { t.Fatalf("in-progress read: %v", err) }
}

func TestHandlerRejectsOtherRoutesAndMethods(t *testing.T) {
	h := &Handler{}
	for _, test := range []struct { method, path string; code int }{
		{"POST", "/status", http.StatusMethodNotAllowed},
		{"GET", "/metrics", http.StatusNotFound},
		{"GET", "/status", http.StatusServiceUnavailable},
	} {
		r := httptest.NewRecorder()
		h.ServeHTTP(r, httptest.NewRequest(test.method, test.path, nil))
		if r.Code != test.code || strings.Contains(r.Body.String(), "token") { t.Fatalf("response = %d %s", r.Code, r.Body) }
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
	if r.Code != 200 { t.Fatal(r.Code) }
	now = now.Add(time.Nanosecond)
	r = httptest.NewRecorder()
	h.ServeHTTP(r, httptest.NewRequest("GET", "/status", nil))
	if r.Code != 503 { t.Fatal("expired cache was served") }
}
