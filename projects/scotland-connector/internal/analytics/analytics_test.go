package analytics

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

func response(groups string) string {
	return `{"errors":null,"data":{"viewer":{"zones":[{"httpRequestsAdaptiveGroups":[` + groups + `]}]}}}`
}
func group(status string, count string) string {
	return fmt.Sprintf(`{"count":%s,"dimensions":{"cacheStatus":%q}}`, count, status)
}
func config() Config {
	return Config{Enabled: true, Zone: strings.Repeat("a", 32), Hostname: "example.test", PlanPath: "/scotland/v1/plan", Token: "fixture-only"}
}

func TestHTTPSFiltersAndUnknownAfterFailure(t *testing.T) {
	var denied atomic.Bool
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.Header.Get("Authorization") != "Bearer fixture-only" {
			t.Error("missing authorized POST")
		}
		var req struct {
			Query     string
			Variables struct {
				ZoneTag string
				Filter  map[string]string
			}
		}
		if json.NewDecoder(r.Body).Decode(&req) != nil {
			t.Error("request JSON")
		}
		f := req.Variables.Filter
		if len(f) != 6 || f["clientRequestHTTPHost"] != "example.test" || f["clientRequestPath"] != "/scotland/v1/plan" || f["clientRequestHTTPMethod"] != "GET" || f["requestSource"] != "eyeball" || !strings.Contains(req.Query, "limit: 16") || !strings.Contains(req.Query, "cacheStatus") {
			t.Errorf("population: %+v %s", f, req.Query)
		}
		start, _ := time.Parse(time.RFC3339, f["datetime_geq"])
		end, _ := time.Parse(time.RFC3339, f["datetime_lt"])
		if end.Sub(start) != Window {
			t.Error("wrong window")
		}
		if denied.Load() {
			w.WriteHeader(403)
			_, _ = w.Write([]byte("sensitive body"))
			return
		}
		_, _ = w.Write([]byte(response(group("hit", "8") + "," + group("miss", "2") + "," + group("dynamic", "90"))))
	}))
	defer srv.Close()
	r, err := New(config(), srv.Client().Transport)
	if err != nil {
		t.Fatal(err)
	}
	r.endpoint = srv.URL
	now := time.Now()
	if err := r.Poll(context.Background(), now); err != nil {
		t.Fatal(err)
	}
	o, ok := r.Current(now)
	if !ok || o.Hits != 8 || o.Eligible != 10 || o.Ratio != 0.8 || o.End.Sub(o.Start) != Window {
		t.Fatalf("%+v %v", o, ok)
	}
	if _, ok := r.Current(now.Add(MaxAge + time.Second)); ok {
		t.Fatal("stale is known")
	}
	denied.Store(true)
	if err := r.Poll(context.Background(), now); err == nil || strings.Contains(err.Error(), "sensitive") || strings.Contains(err.Error(), "fixture-only") {
		t.Fatalf("failure: %v", err)
	}
	if _, ok := r.Current(now); ok {
		t.Fatal("retained successful ratio after denial")
	}
	reg := prometheus.NewRegistry()
	reg.MustRegister(r)
	families, err := reg.Gather()
	if err != nil || len(families) != 0 {
		t.Fatalf("unknown emitted metrics: %v %v", families, err)
	}
}

func TestMalformedAndPartialObservations(t *testing.T) {
	for _, b := range []string{
		`broken`, `{}`, `{"data":null}`, `{"data":{"viewer":{"zones":[]}}}`,
		response(""), response(group("hit", "0")), response(group("dynamic", "10")),
		response(group("hit", "null")), response(group("hit", "-1")), response(group("HIT", "5")),
		response(group("hit", "1") + "," + group("hit", "2")),
		strings.Replace(response(group("hit", "10")), `"errors":null`, `"errors":[{"message":"partial"}]`, 1),
		response(strings.TrimSuffix(strings.Repeat(group("hit", "1")+",", GroupLimit), ",")),
	} {
		if _, err := decode([]byte(b)); err == nil {
			t.Errorf("accepted %s", b)
		}
	}
	o, err := decode([]byte(response(group("hit", "2") + "," + group("stale", "1") + "," + group("updating", "1") + "," + group("revalidated", "2") + "," + group("expired", "2") + "," + group("miss", "2") + "," + group("bypass", "99"))))
	if err != nil || o.Ratio != 0.4 {
		t.Fatalf("cache status accounting %+v %v", o, err)
	}
}

func TestDisabledNoTrafficAndValidation(t *testing.T) {
	var calls atomic.Int64
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	defer srv.Close()
	r, err := New(Config{}, srv.Client().Transport)
	if err != nil {
		t.Fatal(err)
	}
	r.endpoint = srv.URL
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r.Run(ctx)
	if err := r.Poll(ctx, time.Now()); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 0 {
		t.Fatal("disabled traffic")
	}
	for _, change := range []func(*Config){func(c *Config) { c.Zone = "" }, func(c *Config) { c.Hostname = "example.test/other" }, func(c *Config) { c.PlanPath = "/%/v1/plan" }, func(c *Config) { c.Token = "" }, func(c *Config) { c.PlanPath = "/v1/other" }} {
		c := config()
		change(&c)
		if _, err := New(c, nil); err == nil {
			t.Fatalf("accepted %+v", c)
		}
	}
}

func TestRedirectOversizeAndCancellation(t *testing.T) {
	var forwarded atomic.Bool
	target := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { forwarded.Store(true) }))
	defer target.Close()
	for _, mode := range []string{"redirect", "oversize", "truncated", "cancel"} {
		t.Run(mode, func(t *testing.T) {
			release := make(chan struct{})
			srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch mode {
				case "redirect":
					http.Redirect(w, r, target.URL, 302)
				case "oversize":
					_, _ = w.Write([]byte(strings.Repeat(" ", MaxResponse+1)))
				case "truncated":
					w.Header().Set("Content-Length", "1000")
					_, _ = w.Write([]byte("{}"))
				case "cancel":
					<-release
				}
			}))
			defer srv.Close()
			defer close(release)
			r, _ := New(config(), srv.Client().Transport)
			r.endpoint = srv.URL
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			started := time.Now()
			if err := r.Poll(ctx, time.Now()); err == nil {
				t.Fatal("accepted failed read")
			}
			if time.Since(started) > time.Second {
				t.Fatal("cancellation not bounded")
			}
			if _, ok := r.Current(time.Now()); ok {
				t.Fatal("failed read known")
			}
		})
	}
	if forwarded.Load() {
		t.Fatal("credential-forwarding redirect")
	}
}
