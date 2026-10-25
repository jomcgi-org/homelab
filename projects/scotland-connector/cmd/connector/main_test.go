package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	collector "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	metrics "go.opentelemetry.io/proto/otlp/metrics/v1"
	"google.golang.org/protobuf/proto"
)

// End to end through the real wiring: a fake R2 serves the fixture
// snapshots, the store loads them, and both public surfaces answer.
func TestAppServesFromR2Snapshots(t *testing.T) {
	var captured *collector.ExportMetricsServiceRequest
	var captureMu sync.Mutex
	otlp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/metrics" {
			t.Errorf("export endpoint not consumed: %s", r.URL.Path)
		}
		b, _ := io.ReadAll(r.Body)
		payload := new(collector.ExportMetricsServiceRequest)
		if err := proto.Unmarshal(b, payload); err != nil {
			t.Error(err)
		}
		captureMu.Lock()
		captured = payload
		captureMu.Unlock()
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer otlp.Close()
	t.Setenv("METER_EXPORT_ENABLED", "true")
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", otlp.URL+"/v1/metrics")
	t.Setenv("POD_UID", "fixture-pod")
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
	cfg.now = func() time.Time { return fixture.Now }
	a, err := newApp(cfg, log, r2srv.Client())
	if err != nil {
		t.Fatal(err)
	}
	shutdown, err := a.meter.StartExport(context.Background(), cfg.export)
	if err != nil {
		t.Fatal(err)
	}

	rec := httptest.NewRecorder()
	a.admin.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("ready before load: %d", rec.Code)
	}
	unavailable := httptest.NewRecorder()
	a.public.ServeHTTP(unavailable, httptest.NewRequest(http.MethodGet, "/v1/plan?lat=57.19&lon=-3.83&activities=stargaze", nil))
	if unavailable.Code != http.StatusServiceUnavailable {
		t.Fatal("expected unloaded invocation")
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
	// Concurrent real dispatch, no helper-only accounting. Registered invalid
	// invocations consume units; unknown names and malformed protocol do not.
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for _, call := range []struct{ method, path, body string }{
				{"GET", "/v1/plan?lat=not-a-number", ""},
				{"POST", "/v1/check", `{"items":[]}`},
				{"POST", "/mcp", `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"plan","arguments":{"lat":57.19,"lon":-3.83,"activities":["stargaze"]}}}`},
				{"POST", "/mcp", `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"check","arguments":{}}}`},
				{"POST", "/mcp", `{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"unbounded-user-name","arguments":{}}}`},
				{"POST", "/mcp", `broken`},
			} {
				req := httptest.NewRequest(call.method, call.path, strings.NewReader(call.body))
				a.public.ServeHTTP(httptest.NewRecorder(), req)
			}
		}()
	}
	wg.Wait()
	if err := shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	captureMu.Lock()
	payload := captured
	captureMu.Unlock()
	if payload == nil || len(payload.ResourceMetrics) != 1 {
		t.Fatal("no captured OTLP")
	}
	rm := payload.ResourceMetrics[0]
	resource := map[string]string{}
	for _, a := range rm.Resource.Attributes {
		resource[a.Key] = a.Value.GetStringValue()
	}
	if resource["service.name"] != "scotland-connector" || !strings.HasPrefix(resource["service.instance.id"], "fixture-pod/") {
		t.Fatalf("resource: %v", resource)
	}
	var totalCalls, totalUnits int64
	seen := map[string]bool{}
	for _, scope := range rm.ScopeMetrics {
		for _, signal := range scope.Metrics {
			seen[signal.Name] = true
			if sum := signal.GetSum(); sum != nil {
				if sum.AggregationTemporality != metrics.AggregationTemporality_AGGREGATION_TEMPORALITY_CUMULATIVE || !sum.IsMonotonic {
					t.Error("counter temporality")
				}
				for _, point := range sum.DataPoints {
					attrs := map[string]string{}
					for _, a := range point.Attributes {
						attrs[a.Key] = a.Value.GetStringValue()
					}
					if point.StartTimeUnixNano == 0 || attrs["surface"] == "" || attrs["tool"] == "" || attrs["client_class"] == "" {
						t.Errorf("missing attributes: %v", attrs)
					}
					if attrs["tool"] == "unbounded-user-name" {
						t.Error("unbounded tool label")
					}
					if signal.Name == "scotland_connector_calls_total" {
						totalCalls += point.GetAsInt()
						if attrs["outcome"] == "" {
							t.Error("missing outcome")
						}
					}
					if signal.Name == "scotland_connector_invocation_units_total" {
						totalUnits += point.GetAsInt()
						if attrs["tool"] == "unknown" || attrs["unit"] == "" {
							t.Error("unknown priced tool or missing unit")
						}
					}
				}
			}
			if h := signal.GetHistogram(); h != nil {
				for _, p := range h.DataPoints {
					if len(p.Attributes) != 2 {
						t.Error("latency attributes changed")
					}
				}
			}
		}
	}
	if totalCalls != 102 || totalUnits != 82 {
		t.Fatalf("double or missing recording: calls=%d units=%d", totalCalls, totalUnits)
	}
	for _, name := range []string{"scotland_connector_calls_total", "scotland_connector_results_total", "scotland_connector_call_duration_seconds", "scotland_connector_invocation_units_total"} {
		if !seen[name] {
			t.Errorf("missing %s", name)
		}
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

func TestMeterConfigConsumersAndRegisteredTools(t *testing.T) {
	t.Setenv("R2_ENDPOINT", "https://r2.example")
	t.Setenv("CACHE_ANALYTICS_TOKEN", "ignored-while-disabled")
	t.Setenv("CACHE_ANALYTICS_ZONE", "invalid-while-disabled")
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.analytics.Token != "" || c.export.Enabled || c.analytics.Enabled {
		t.Fatal("disabled configuration consumed")
	}
	for _, tool := range mcp.Tools {
		price, units := c.policy.Quote(tool.Name)
		if units != 1 || price.PriceMicros != "0" {
			t.Fatalf("unconfigured registered tool %s", tool.Name)
		}
	}
	for key, value := range map[string]string{"METER_EXPORT_ENABLED": "invalid", "CACHE_ANALYTICS_ENABLED": "invalid", "TOOL_UNIT_PRICES": "[]"} {
		t.Run(key, func(t *testing.T) {
			t.Setenv(key, value)
			if _, err := loadConfig(); err == nil {
				t.Fatalf("accepted invalid %s", key)
			}
		})
	}
	t.Setenv("METER_EXPORT_ENABLED", "true")
	if _, err := loadConfig(); err == nil {
		t.Fatal("export missing endpoint accepted")
	}
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://collector.example:4318/v1/metrics")
	c, err = loadConfig()
	if err != nil || c.export.Endpoint != "http://collector.example:4318/v1/metrics" {
		t.Fatal("endpoint consumer")
	}
	t.Setenv("METER_EXPORT_ENABLED", "false")
	t.Setenv("CACHE_ANALYTICS_ENABLED", "true")
	if _, err := loadConfig(); err == nil {
		t.Fatal("analytics missing prerequisites accepted")
	}
	t.Setenv("CACHE_ANALYTICS_ZONE", strings.Repeat("a", 32))
	t.Setenv("CACHE_ANALYTICS_HOSTNAME", "example.test")
	t.Setenv("CACHE_ANALYTICS_PLAN_PATH", "/scotland/v1/plan")
	t.Setenv("PUBLIC_BASE_URL", "https://example.test/other")
	if _, err := loadConfig(); err == nil {
		t.Fatal("whole-zone/wrong population accepted")
	}
	t.Setenv("PUBLIC_BASE_URL", "https://example.test/scotland")
	if _, err := loadConfig(); err != nil {
		t.Fatal(err)
	}
	if _, err := meter.ParsePolicy(meter.DefaultPrices); err != nil {
		t.Fatal(err)
	}
}
