package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp"
)

// Use the production constructors and startup path with the activation variable
// absent. No test attaches an AuroraStore to the service by hand.
func TestAuroraWiringWithoutActivationEnvironment(t *testing.T) {
	t.Setenv("AURORA_ENABLED", "")
	if err := os.Unsetenv("AURORA_ENABLED"); err != nil {
		t.Fatal(err)
	}
	inputs, err := fixture.AuroraInputs()
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := aurora.Materialize(inputs)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name   string
		body   []byte
		loaded bool
	}{
		{"missing", nil, false},
		{"malformed", []byte(`{"schema":1}`), false},
		{"snapshot", snapshot, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var reads atomic.Int32
			r2srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("ETag", `"fixture"`)
				switch r.URL.Path {
				case "/stars/sites.json":
					io.WriteString(w, fixture.Sites)
				case "/stars/history-map.json":
					io.WriteString(w, fixture.History)
				case "/stars/selected-aurora.json":
					defer reads.Add(1)
					if tc.body == nil {
						w.WriteHeader(http.StatusNotFound)
					} else {
						w.Write(tc.body)
					}
				default:
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer r2srv.Close()
			t.Setenv("R2_ENDPOINT", r2srv.URL)
			t.Setenv("STARS_AURORA_KEY", "selected-aurora.json")
			t.Setenv("S3_ACCESS_KEY_ID", "fixture-key")
			t.Setenv("S3_SECRET_ACCESS_KEY", "fixture-secret")
			cfg, err := loadConfig()
			if err != nil {
				t.Fatal(err)
			}
			// Shorten only the polling interval after config validation so the test
			// proves repeated background refresh as well as the initial fetch.
			cfg.refreshInterval = 10 * time.Millisecond
			log := slog.New(slog.NewTextHandler(io.Discard, nil))
			a, err := newApp(cfg, log, r2srv.Client())
			if err != nil {
				t.Fatal(err)
			}
			if a.aurora == nil {
				t.Fatal("aurora store missing")
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			a.startStores(ctx, log)
			deadline := time.Now().Add(5 * time.Second)
			for time.Now().Before(deadline) && (reads.Load() < 2 || a.store.Current() == nil || tc.loaded && a.aurora.Current() == nil) {
				time.Sleep(time.Millisecond)
			}
			if reads.Load() < 2 || (a.aurora.Current() != nil) != tc.loaded {
				t.Fatal("background refresh", reads.Load(), a.aurora.Current())
			}
			rec := httptest.NewRecorder()
			a.admin.ServeHTTP(rec, httptest.NewRequest("GET", "/readyz", nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("aurora broke readiness: %d %s", rec.Code, rec.Body)
			}
			a.draining.Store(true)
			rec = httptest.NewRecorder()
			a.admin.ServeHTTP(rec, httptest.NewRequest("GET", "/readyz", nil))
			if rec.Code != http.StatusServiceUnavailable {
				t.Fatal("draining stayed ready")
			}
			versions := append([]string{mcp.ModernVersion}, mcp.LegacyVersions...)
			for _, version := range versions {
				discovery := "initialize"
				if version == mcp.ModernVersion {
					discovery = "server/discover"
				}
				r := appMCP(t, a, version, discovery, "", nil)
				if r["instructions"] != mcp.Instructions {
					t.Fatalf("%s instructions: %v", version, r)
				}
				r = appMCP(t, a, version, "tools/list", "", nil)
				var names []string
				for _, tool := range r["tools"].([]any) {
					names = append(names, tool.(map[string]any)["name"].(string))
				}
				if strings.Join(names, ",") != "light,aurora,plan,check" {
					t.Fatal(version, names)
				}
				// Missing, malformed or out-of-date source clocks remain unknown.
				for _, at := range []string{"2026-12-01T23:00:00Z", "2026-12-01T23:01:00Z"} {
					rec = httptest.NewRecorder()
					a.public.ServeHTTP(rec, httptest.NewRequest("GET", "/v1/aurora?lat=58&lon=-4&at="+at, nil))
					if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"verdict":"unknown"`) {
						t.Fatal(rec.Code, rec.Body)
					}
					r = appMCP(t, a, version, "tools/call", "aurora", map[string]any{"lat": 58, "lon": -4, "at": at})
					if r["isError"] != false || r["structuredContent"].(map[string]any)["data"].(map[string]any)["verdict"] != "unknown" {
						t.Fatal(version, r)
					}
				}
			}
		})
	}
}

func appMCP(t *testing.T, a *app, version, method, name string, args map[string]any) map[string]any {
	t.Helper()
	params := map[string]any{}
	if method == "initialize" {
		params["protocolVersion"], params["capabilities"] = version, map[string]any{}
	}
	if name != "" {
		params["name"], params["arguments"] = name, args
	}
	if version == mcp.ModernVersion {
		params["_meta"] = map[string]any{"io.modelcontextprotocol/protocolVersion": version, "io.modelcontextprotocol/clientCapabilities": map[string]any{}}
	}
	body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/mcp", strings.NewReader(string(body)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("MCP-Protocol-Version", version)
	if version == mcp.ModernVersion {
		req.Header.Set("Mcp-Method", method)
		if name != "" {
			req.Header.Set("Mcp-Name", name)
		}
	}
	rec := httptest.NewRecorder()
	a.public.ServeHTTP(rec, req)
	var r struct {
		Result map[string]any
		Error  any
	}
	if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &r) != nil || r.Error != nil || r.Result == nil {
		t.Fatalf("%s %s: %d %s", version, method, rec.Code, rec.Body)
	}
	return r.Result
}

// End to end through the real wiring: a fake R2 serves the fixture
// snapshots, the store loads them, and both public surfaces answer.
func TestAppServesFromR2Snapshots(t *testing.T) {
	r2srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 ") {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		switch r.URL.Path {
		case "/stars/connector-sites.json":
			w.Header().Set("ETag", `"s1"`)
			_, _ = io.WriteString(w, fixture.Sites)
		case "/stars/history-map.json":
			w.Header().Set("ETag", `"h1"`)
			_, _ = io.WriteString(w, fixture.History)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer r2srv.Close()

	t.Setenv("R2_ENDPOINT", r2srv.URL)
	t.Setenv("STARS_SITES_KEY", "connector-sites.json")
	t.Setenv("S3_ACCESS_KEY_ID", "key")
	t.Setenv("S3_SECRET_ACCESS_KEY", "secret")
	t.Setenv("PUBLIC_BASE_URL", "https://example.test/")
	cfg, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	a, err := newApp(cfg, log, r2srv.Client())
	if err != nil {
		t.Fatal(err)
	}

	rec := httptest.NewRecorder()
	a.admin.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("ready before load: %d", rec.Code)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go a.loadUntilReady(ctx, log)
	deadline := time.Now().Add(5 * time.Second)
	for a.store.Current() == nil && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	rec = httptest.NewRecorder()
	a.admin.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("not ready after load: %d %s", rec.Code, rec.Body)
	}

	rec = httptest.NewRecorder()
	a.public.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/plan?lat=57.19&lon=-3.83&activities=stargaze", nil))
	var body struct {
		Meta struct {
			MethodURL   string `json:"method_url"`
			DataVersion string `json:"data_version"`
		} `json:"meta"`
	}
	if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &body) != nil ||
		body.Meta.MethodURL != "https://example.test/v1/method" || body.Meta.DataVersion != `"s1"|"h1"` {
		t.Fatalf("api: %d %s", rec.Code, rec.Body)
	}

	req := httptest.NewRequest(http.MethodPost, "/mcp", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{}}}`))
	rec = httptest.NewRecorder()
	a.public.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"serverInfo"`) {
		t.Fatalf("mcp: %d %s", rec.Code, rec.Body)
	}

	// Paths ServeMux would 301 to a root-relative Location (escaping a
	// prefix-stripping route) are answered in place instead.
	for _, p := range []string{"/v1", "/v1//plan", "/v1/./method"} {
		rec = httptest.NewRecorder()
		a.public.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, p, nil))
		if rec.Code != http.StatusNotFound || rec.Header().Get("Location") != "" {
			t.Fatalf("%s: %d Location %q", p, rec.Code, rec.Header().Get("Location"))
		}
		if !strings.Contains(rec.Body.String(), "https://example.test/v1/openapi.json") {
			t.Fatalf("%s: 404 hint %s", p, rec.Body)
		}
	}

	// Admin paths are not on the public mux.
	rec = httptest.NewRecorder()
	a.public.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("public /metrics: %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	a.admin.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "scotland_connector_calls_total") {
		t.Fatalf("admin /metrics: %d", rec.Code)
	}
}

func TestLoadConfigRequiresEndpoint(t *testing.T) {
	t.Setenv("R2_ENDPOINT", "")
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted an empty R2_ENDPOINT")
	}
	t.Setenv("R2_ENDPOINT", "https://r2.example")
	t.Setenv("REFRESH_INTERVAL", "1s")
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted a 1s refresh")
	}
}

func TestSitesKeyDefaultAndSelection(t *testing.T) {
	t.Setenv("R2_ENDPOINT", "https://r2.example")
	t.Setenv("STARS_SITES_KEY", "")
	cfg, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.sitesKey != "sites.json" {
		t.Fatalf("default: %+v", cfg)
	}
	t.Setenv("STARS_SITES_KEY", "connector-sites.json")
	cfg, err = loadConfig()
	if err != nil || cfg.sitesKey != "connector-sites.json" {
		t.Fatalf("selected: %+v, %v", cfg, err)
	}
}
