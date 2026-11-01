package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp"
)

func TestAppDocumentationMatchesHTTP(t *testing.T) {
	r2srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("documentation fetched upstream data")
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer r2srv.Close()
	t.Setenv("R2_ENDPOINT", r2srv.URL)
	t.Setenv("S3_ACCESS_KEY_ID", "fixture-key")
	t.Setenv("S3_SECRET_ACCESS_KEY", "fixture-secret")
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	for _, baseURL := range []string{"", "https://jomcgi.dev/scotland", "https://jomcgi.dev/scotland/"} {
		t.Run(baseURL, func(t *testing.T) {
			t.Setenv("PUBLIC_BASE_URL", baseURL)
			cfg, err := loadConfig()
			if err != nil {
				t.Fatal(err)
			}
			a, err := newApp(cfg, log, r2srv.Client())
			if err != nil {
				t.Fatal(err)
			}
			for _, doc := range []struct{ uri, path, mime string }{
				{"scotland://docs/openapi", "/v1/openapi.json", "application/json"},
				{"scotland://docs/method", "/v1/method", "text/markdown"},
			} {
				rest := httptest.NewRecorder()
				a.public.ServeHTTP(rest, httptest.NewRequest(http.MethodGet, doc.path, nil))
				if rest.Code != http.StatusOK {
					t.Fatalf("HTTP documentation status: %d", rest.Code)
				}
				if doc.mime == "application/json" {
					var spec struct {
						Servers []struct{ URL string } `json:"servers"`
					}
					want := strings.TrimRight(baseURL, "/")
					if want == "" {
						want = "/"
					}
					if err := json.Unmarshal(rest.Body.Bytes(), &spec); err != nil || len(spec.Servers) != 1 || spec.Servers[0].URL != want {
						t.Fatalf("HTTP spec server: %+v, error: %v", spec.Servers, err)
					}
				}
				for _, version := range []string{mcp.ModernVersion, mcp.LegacyVersions[0]} {
					t.Run(doc.uri+"/"+version, func(t *testing.T) {
						params := map[string]any{"uri": doc.uri}
						if version == mcp.ModernVersion {
							params["_meta"] = map[string]any{
								"io.modelcontextprotocol/protocolVersion":    version,
								"io.modelcontextprotocol/clientCapabilities": map[string]any{},
							}
						}
						body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": params})
						if err != nil {
							t.Fatal(err)
						}
						req := httptest.NewRequest(http.MethodPost, "/mcp", strings.NewReader(string(body)))
						req.Header.Set("Content-Type", "application/json")
						req.Header.Set("Accept", "application/json")
						req.Header.Set("MCP-Protocol-Version", version)
						if version == mcp.ModernVersion {
							req.Header.Set("Mcp-Method", "resources/read")
						}
						rec := httptest.NewRecorder()
						a.public.ServeHTTP(rec, req)
						var rpc struct {
							Error  json.RawMessage `json:"error"`
							Result struct {
								Contents []struct{ URI, MIMEType, Text string } `json:"contents"`
							} `json:"result"`
						}
						if err := json.Unmarshal(rec.Body.Bytes(), &rpc); err != nil || rec.Code != http.StatusOK || len(rpc.Error) != 0 || len(rpc.Result.Contents) != 1 {
							t.Fatalf("MCP documentation: %d %s, error: %v", rec.Code, rec.Body, err)
						}
						content := rpc.Result.Contents[0]
						if content.URI != doc.uri || content.MIMEType != doc.mime || content.Text != rest.Body.String() {
							t.Fatalf("HTTP and MCP documentation differ: %s (HTTP %d bytes, MCP %d bytes)", doc.uri, rest.Body.Len(), len(content.Text))
						}
					})
				}
			}
		})
	}
}

func TestAuroraActivationIsIndependentOfReadiness(t *testing.T) {
	r2srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("ETag", `"fixture"`)
		switch r.URL.Path {
		case "/stars/sites.json":
			io.WriteString(w, fixture.Sites)
		case "/stars/history-map.json":
			io.WriteString(w, fixture.History)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer r2srv.Close()
	t.Setenv("R2_ENDPOINT", r2srv.URL)
	t.Setenv("S3_ACCESS_KEY_ID", "fixture-key")
	t.Setenv("S3_SECRET_ACCESS_KEY", "fixture-secret")
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	for _, enabled := range []string{"false", "true"} {
		t.Setenv("AURORA_ENABLED", enabled)
		cfg, err := loadConfig()
		if err != nil {
			t.Fatal(err)
		}
		a, err := newApp(cfg, log, r2srv.Client())
		if err != nil {
			t.Fatal(err)
		}
		if (a.aurora != nil) != (enabled == "true") {
			t.Fatal("activation flag")
		}
		if err = a.store.Refresh(context.Background()); err != nil {
			t.Fatal(err)
		}
		if a.aurora != nil && a.aurora.Refresh(context.Background()) == nil {
			t.Fatal("missing aurora snapshot accepted")
		}
		r := httptest.NewRecorder()
		a.admin.ServeHTTP(r, httptest.NewRequest("GET", "/readyz", nil))
		if r.Code != 200 {
			t.Fatal("aurora broke readiness", r.Body)
		}
		r = httptest.NewRecorder()
		a.public.ServeHTTP(r, httptest.NewRequest("GET", "/v1/aurora?lat=58&lon=-4&at=2026-12-01T23:00:00Z", nil))
		if r.Code != 200 || !strings.Contains(r.Body.String(), `"verdict":"unknown"`) {
			t.Fatal(r.Code, r.Body)
		}
	}
	t.Setenv("AURORA_ENABLED", "maybe")
	if _, err := loadConfig(); err == nil {
		t.Fatal("invalid activation value")
	}
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
