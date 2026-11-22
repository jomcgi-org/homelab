package notify

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func smoke() Text { return Text{Key: SmokeKey, Title: "Factory notify sink is live"} }

func client(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	srv := httptest.NewTLSServer(handler)
	t.Cleanup(srv.Close)
	path := filepath.Join(t.TempDir(), "password")
	if err := os.WriteFile(path, []byte("test-app-password"), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := New(Config{
		URL: srv.URL + "/notify", AppPasswordFile: path,
		BeforeRequest: func(context.Context, string, string) error { return nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	c.http.Transport = srv.Client().Transport
	c.backoff = time.Millisecond
	return c
}

func protocol(t *testing.T, tool func(http.ResponseWriter, map[string]any)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, password, ok := r.BasicAuth()
		if !ok || user != "factory-connector" || password != "test-app-password" {
			t.Error("wrong caller")
		}
		var req Request
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error(err)
		}
		if req.Channel != "homelab" {
			t.Error("wrong channel")
		}
		tool(w, map[string]any{"dedupe_key": req.Text.Key, "message": req.Text.Title, "level": req.Level})
	}
}
func accepted(w http.ResponseWriter) { w.WriteHeader(204) }

func TestFixedFields(t *testing.T) {
	var value Text
	if err := json.Unmarshal([]byte(`{"Key":"caller-secret","Title":"Scheduled agent digest","Status":"failed","Count":3,"Link":"https://github.com/jomcgi-org/homelab/issues/7935","body":"private issue body","logs":"private logs","secret":"private token"}`), &value); err != nil {
		t.Fatal(err)
	}
	text, err := value.Render()
	if err != nil {
		t.Fatal(err)
	}
	if text != "Scheduled agent digest\nStatus: failed\nCount: 3\nhttps://github.com/jomcgi-org/homelab/issues/7935" {
		t.Fatalf("unexpected projection: %q", text)
	}
	if len(text) > MaxText {
		t.Fatal("unbounded text")
	}
	for _, mutate := range []func(*Text){
		func(v *Text) { v.Title = "private issue body" },
		func(v *Text) { v.Title = strings.Repeat("x", 1001) },
		func(v *Text) { v.Status = "token=secret" },
		func(v *Text) { v.Count = 10000 },
		func(v *Text) { v.Count = -1 },
		func(v *Text) { v.Key = strings.Repeat("x", 257) },
		func(v *Text) { v.Key = "" },
		func(v *Text) { v.Link = "https://github.com/jomcgi-org/homelab/issues/7935?token=secret" },
		func(v *Text) { v.Link = "https://github.com/jomcgi-org/homelab/issues/7935#secret" },
		func(v *Text) { v.Link = "https://evil.example/secret" },
		func(v *Text) { v.Link = "https://github.com@evil.example/secret" },
		func(v *Text) { v.Link = "https://factory-plan.jomcgi.dev/issue/secret" },
	} {
		v := value
		mutate(&v)
		if _, err := v.Render(); err == nil {
			t.Fatal("unsafe field accepted")
		}
	}
}

func TestOffAndPartialConfiguration(t *testing.T) {
	c, err := New(Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Notify(context.Background(), "invalid", Text{}); err != nil {
		t.Fatal(err)
	}
	for _, cfg := range []Config{
		{URL: "http://example.com"},
		{AppPasswordFile: "password"},
		{URL: "https://user:password@example.com", AppPasswordFile: "password"},
	} {
		if _, err := New(cfg); err == nil {
			t.Fatal("partial or insecure configuration accepted")
		}
	}
}

func TestEveryRequestRequiresWriterAdmission(t *testing.T) {
	sent := false
	c := client(t, func(w http.ResponseWriter, r *http.Request) { sent = true })
	c.config.BeforeRequest = func(_ context.Context, key, phase string) error {
		if !strings.HasPrefix(key, "factory/") || phase != "tools/call" {
			t.Error("incorrect audit key or phase")
		}
		return errors.New("lost writer")
	}
	if err := c.Notify(context.Background(), "info", smoke()); err == nil || sent {
		t.Fatal("lost writer sent")
	}
}

func TestDedupeConcurrentAndNextDay(t *testing.T) {
	calls := 0
	keys := []string{}
	c := client(t, protocol(t, func(w http.ResponseWriter, args map[string]any) {
		calls++
		keys = append(keys, args["dedupe_key"].(string))
		if _, ok := args["channel"]; ok {
			t.Error("channel override sent")
		}
		if args["message"] != smoke().Title || args["level"] != "info" {
			t.Error("incorrect smoke")
		}
		accepted(w)
	}))
	now := time.Date(2026, 11, 15, 12, 0, 0, 0, time.UTC)
	c.now = func() time.Time { return now }
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := c.Notify(context.Background(), "info", smoke()); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if calls != 1 {
		t.Fatalf("duplicate requests: %d", calls)
	}
	now = now.Add(24 * time.Hour)
	if err := c.Notify(context.Background(), "info", smoke()); err != nil {
		t.Fatal(err)
	}
	if calls != 2 || keys[0] == keys[1] || strings.Contains(keys[0], SmokeKey) {
		t.Fatal("daily hashed dedupe failed")
	}
}

func TestRetryUsesSameKeyAndBackoff(t *testing.T) {
	var keys []string
	var times []time.Time
	c := client(t, protocol(t, func(w http.ResponseWriter, args map[string]any) {
		keys = append(keys, args["dedupe_key"].(string))
		times = append(times, time.Now())
		if len(keys) < 3 {
			http.Error(w, "private upstream log", 503)
			return
		}
		accepted(w)
	}))
	c.backoff = 10 * time.Millisecond
	if err := c.Notify(context.Background(), "info", smoke()); err != nil {
		t.Fatal(err)
	}
	if len(keys) != 3 || keys[0] != keys[1] || keys[1] != keys[2] {
		t.Fatal("retry key changed")
	}
	if times[1].Sub(times[0]) < c.backoff || times[2].Sub(times[1]) < 2*c.backoff {
		t.Fatal("missing backoff")
	}
}

func TestTimeoutCoversWholeOperation(t *testing.T) {
	c := client(t, func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(200 * time.Millisecond):
		}
	})
	c.timeout = 20 * time.Millisecond
	start := time.Now()
	if err := c.Notify(context.Background(), "info", smoke()); err != context.DeadlineExceeded {
		t.Fatalf("got %v", err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("timeout not bounded")
	}
}

func TestRetryBudgetAndTransientTimeout(t *testing.T) {
	for _, timeout := range []bool{false, true} {
		t.Run(fmt.Sprint(timeout), func(t *testing.T) {
			var mu sync.Mutex
			calls := 0
			c := client(t, protocol(t, func(w http.ResponseWriter, _ map[string]any) {
				mu.Lock()
				calls++
				n := calls
				mu.Unlock()
				if timeout {
					if n == 1 {
						time.Sleep(80 * time.Millisecond)
					}
					accepted(w)
				} else {
					w.WriteHeader(429)
				}
			}))
			if timeout {
				c.http.Timeout = 40 * time.Millisecond
			}
			err := c.Notify(context.Background(), "info", smoke())
			mu.Lock()
			defer mu.Unlock()
			if timeout && (err != nil || calls != 2) {
				t.Fatalf("timeout retry: calls=%d error=%v", calls, err)
			}
			if !timeout && (err == nil || calls != 3) {
				t.Fatalf("retry budget: calls=%d error=%v", calls, err)
			}
		})
	}
}

func TestFailureIsNotCachedOrLeaked(t *testing.T) {
	calls := 0
	c := client(t, protocol(t, func(w http.ResponseWriter, args map[string]any) {
		calls++
		http.Error(w, "private body with test-app-password test-bearer", 403)
	}))
	for i := 0; i < 2; i++ {
		err := c.Notify(context.Background(), "info", smoke())
		if err == nil || err.Error() != "notify: HTTP 403" {
			t.Fatalf("unsafe error: %v", err)
		}
	}
	if calls != 2 {
		t.Fatal("permanent failure retried or cached")
	}
}
