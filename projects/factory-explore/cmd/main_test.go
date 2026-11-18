package main

import (
	"net/http"
	"net/http/httptest"
	"strconv"
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
				go request(p, "GET", tc.path, strconv.Itoa(i%tc.users))
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
