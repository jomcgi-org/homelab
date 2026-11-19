package main

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/time/rate"
)

func newTestProxy() *proxy {
	return &proxy{users: map[string]*window{}, now: time.Now, subject: func(s string) (string, error) { return s, nil }, upstream: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })}
}

func request(p *proxy, method, path, sub string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, nil)
	r.Header.Set("Authorization", "Bearer "+sub)
	w := httptest.NewRecorder()
	p.ServeHTTP(w, r)
	return w
}

func TestWindow(t *testing.T) {
	start := time.Now().UTC().Truncate(time.Second)
	now := start
	p := newTestProxy()
	p.now = func() time.Time { return now }
	for _, tc := range []struct {
		elapsed time.Duration
		code    int
		end     time.Duration
	}{{0, 200, 10}, {9 * time.Minute, 200, 10}, {10 * time.Minute, 403, 10}, {time.Hour - time.Second, 403, 10}, {time.Hour, 200, 70}} {
		now = start.Add(tc.elapsed)
		w := request(p, "GET", "/api", "a")
		if w.Code != tc.code || w.Header().Get("X-Explore-Window-End") != start.Add(tc.end*time.Minute).Format(time.RFC3339) {
			t.Fatalf("%v: %v", tc, w.Result())
		}
		if tc.code == 403 && w.Header().Get("X-Explore-Next-Window") != start.Add(time.Hour).Format(time.RFC3339) {
			t.Fatal(w.Header())
		}
	}
}

func TestWatchEnds(t *testing.T) {
	p := newTestProxy()
	p.users["a"] = &window{start: time.Now().Add(-10*time.Minute + 200*time.Millisecond), rate: rate.NewLimiter(10, 40)}
	p.upstream = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
			t.Error("watch remained open")
		}
	})
	w := request(p, "GET", "/api/v1/pods?watch=true", "a")
	if w.Code != 200 || p.watches != 0 {
		t.Fatal(w.Code, p.watches)
	}
}

func TestMethods(t *testing.T) {
	p := newTestProxy()
	for _, path := range []string{"/apis/authentication.k8s.io/v1/selfsubjectreviews", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"} {
		if request(p, "POST", path, "a").Code != 200 {
			t.Fatal(path)
		}
	}
	for _, method := range []string{"POST", "DELETE", "PUT", "PATCH", "CONNECT", "HEAD"} {
		if request(p, method, "/api/v1/pods", "a").Code != 403 {
			t.Fatal(method)
		}
	}
	r := httptest.NewRequest("GET", "/api", nil)
	r.Header.Set("Authorization", "Bearer a")
	r.Header.Set("Upgrade", "websocket")
	w := httptest.NewRecorder()
	p.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal(w.Code)
	}
}

func TestLimits(t *testing.T) {
	for _, tc := range []struct {
		name, path string
		max, users int
	}{{"individual watches", "/api?watch=1", 8, 1}, {"total watches", "/api?watch=true", 200, 30}, {"reads", "/api", 32, 1}} {
		t.Run(tc.name, func(t *testing.T) {
			p := newTestProxy()
			entered := make(chan struct{}, 1)
			release := make(chan struct{})
			defer close(release)
			p.upstream = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { entered <- struct{}{}; <-release })
			for i := 0; i < tc.max; i++ {
				path := tc.path
				if tc.name == "reads" {
					path += "?id=" + strconv.Itoa(i)
				}
				go request(p, "GET", path, strconv.Itoa(i%tc.users))
				select {
				case <-entered:
				case <-time.After(time.Second):
					t.Fatal("early rejection", i)
				}
			}
			done := make(chan *httptest.ResponseRecorder, 1)
			go func() { done <- request(p, "GET", tc.path, "0") }()
			var w *httptest.ResponseRecorder
			select {
			case w = <-done:
			case <-entered:
				t.Fatal("limit exceeded")
				return
			}
			if w.Code != 429 || w.Header().Get("Retry-After") == "" {
				t.Fatal(w.Code)
			}
		})
	}
}

func TestRate(t *testing.T) {
	p := newTestProxy()
	now := time.Now()
	p.now = func() time.Time { return now }
	for i := 0; i < 40; i++ {
		if request(p, "GET", "/api", "a").Code != 200 {
			t.Fatal(i)
		}
	}
	if request(p, "GET", "/api", "a").Code != 429 || request(p, "GET", "/api", "b").Code != 200 {
		t.Fatal("burst")
	}
	now = now.Add(100 * time.Millisecond)
	if request(p, "GET", "/api", "a").Code != 200 {
		t.Fatal("refill")
	}
}

// Exercise the HTTP stream on both sides of the production reverse proxy.
func TestExplorerHourlySession(t *testing.T) {
	var calls atomic.Int32
	closed := make(chan struct{})
	api := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer explorer" {
			t.Error("missing caller credential")
		}
		if r.URL.Query().Get("watch") != "true" {
			return
		}
		defer close(closed)
		io.WriteString(w, "event\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer api.Close()
	target, err := url.Parse(api.URL)
	if err != nil {
		t.Fatal(err)
	}
	p := newTestProxy()
	p.upstream = apiProxy(target, api.Client().Transport)
	// Place the session origin in the past, then let the watch run on wall time.
	var offset atomic.Int64
	offset.Store(int64(-10*time.Minute + time.Second))
	p.now = func() time.Time { return time.Now().Add(time.Duration(offset.Load())) }
	edge := httptest.NewServer(p)
	defer edge.Close()
	client := edge.Client()
	client.Timeout = 4 * time.Second
	get := func(query string, status int) *http.Response {
		t.Helper()
		req, err := http.NewRequest("GET", edge.URL+"/api/v1/pods"+query, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer explorer")
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != status {
			resp.Body.Close()
			t.Fatalf("status %d, want %d", resp.StatusCode, status)
		}
		return resp
	}
	first := get("", 200)
	first.Body.Close()
	end, err := time.Parse(time.RFC3339, first.Header.Get("X-Explore-Window-End"))
	if err != nil {
		t.Fatal(err)
	}
	offset.Store(0)
	watch := get("?watch=true", 200)
	data, err := io.ReadAll(watch.Body)
	watch.Body.Close()
	if string(data) != "event\n" || (err != nil && !errors.Is(err, io.ErrUnexpectedEOF)) {
		t.Fatalf("watch returned %q, %v", data, err)
	}
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("upstream watch remained open")
	}
	denied := get("", 403)
	denied.Body.Close()
	if denied.Header.Get("X-Explore-Next-Window") != end.Add(50*time.Minute).Format(time.RFC3339) || calls.Load() != 2 {
		t.Fatal("cooldown did not isolate the API server")
	}
	offset.Store(int64(time.Hour))
	next := get("", 200)
	next.Body.Close()
	if calls.Load() != 3 || next.Header.Get("X-Explore-Window-End") == first.Header.Get("X-Explore-Window-End") {
		t.Fatal("new session failed to open")
	}
}

func TestSharedReads(t *testing.T) {
	p := newTestProxy()
	now := time.Now()
	p.now = func() time.Time { return now }
	var calls atomic.Int32
	start := make(chan struct{})
	p.upstream = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); <-start; io.WriteString(w, "pods") })
	done := make(chan *httptest.ResponseRecorder, 50)
	for i := range 50 {
		go func() { done <- request(p, "GET", "/api", strconv.Itoa(i)) }()
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		p.cache.Lock()
		joined := p.cache.coalesced
		p.cache.Unlock()
		if joined == 49 {
			break
		}
		if time.Now().After(deadline) {
			close(start)
			t.Fatalf("only %d followers joined", joined)
		}
		time.Sleep(time.Millisecond)
	}
	close(start)
	for range 50 {
		w := <-done
		if w.Code != 200 || w.Body.String() != "pods" {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	if calls.Load() != 1 {
		t.Fatal(calls.Load())
	}
	p.users["expired"] = &window{start: now.Add(-11 * time.Minute)}
	if w := request(p, "GET", "/api", "expired"); w.Code != 403 {
		t.Fatal(w.Code)
	}
	now = now.Add(3 * time.Second)
	request(p, "GET", "/api", "0")
	if calls.Load() != 2 {
		t.Fatal(calls.Load())
	}
}

func TestUncachedRequests(t *testing.T) {
	for _, tc := range []struct {
		method, path string
		status       int
	}{
		{"GET", "/api?watch=true", 200}, {"POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", 200}, {"GET", "/api", 503},
	} {
		p := newTestProxy()
		calls := 0
		p.upstream = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(tc.status) })
		for range 2 {
			if w := request(p, tc.method, tc.path, "user"); w.Code != tc.status {
				t.Fatal(w.Code)
			}
		}
		if calls != 2 {
			t.Fatal(tc, calls)
		}
	}
}
