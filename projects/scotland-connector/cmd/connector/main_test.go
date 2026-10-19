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
)

// End to end through the real wiring: a fake R2 serves the fixture
// snapshots, the store loads them, and both public surfaces answer.
func TestAppServesFromR2Snapshots(t *testing.T) {
	r2srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 ") {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		switch r.URL.Path {
		case "/stars/sites.json":
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
	a.public.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/dark-sky-windows?lat=57.19&lon=-3.83", nil))
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
