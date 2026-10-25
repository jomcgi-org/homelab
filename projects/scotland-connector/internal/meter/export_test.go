package meter

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	collector "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	"google.golang.org/protobuf/proto"
)

func TestExportDisabledAndValidation(t *testing.T) {
	m := New(prometheus.NewRegistry())
	shutdown, err := m.StartExport(context.Background(), ExportConfig{Endpoint: "not a URL"})
	if err != nil || m.otel != nil || shutdown(context.Background()) != nil {
		t.Fatal("disabled exporter active")
	}
	for _, endpoint := range []string{"", "ftp://example.test", "http://user:password@example.test", "http://example.test?token=secret"} {
		if _, err := m.StartExport(context.Background(), ExportConfig{Enabled: true, Endpoint: endpoint}); err == nil {
			t.Errorf("accepted %q", endpoint)
		}
	}
}

func TestExportOutageNeverBlocksRecordingAndFlushIsBounded(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-release; w.WriteHeader(503) }))
	defer srv.Close()
	defer close(release)
	m := New(prometheus.NewRegistry())
	shutdown, err := m.StartExport(context.Background(), ExportConfig{Enabled: true, Endpoint: srv.URL + "/v1/metrics"})
	if err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	for i := 0; i < 1000; i++ {
		m.Record("plan", "api", ClassDirect, "ok", 1, time.Millisecond)
	}
	if time.Since(started) > time.Second {
		t.Fatal("public recording blocked on exporter")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	started = time.Now()
	if err := shutdown(ctx); err == nil {
		t.Fatal("outage flush succeeded")
	}
	if time.Since(started) > time.Second {
		t.Fatal("shutdown ignored cancellation")
	}
}

func TestRestartCreatesSeparateCumulativeSeries(t *testing.T) {
	var mu sync.Mutex
	var payloads []*collector.ExportMetricsServiceRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		p := new(collector.ExportMetricsServiceRequest)
		if err := proto.Unmarshal(b, p); err != nil {
			t.Error(err)
		}
		mu.Lock()
		payloads = append(payloads, p)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer srv.Close()
	for i := 0; i < 2; i++ {
		m := New(prometheus.NewRegistry())
		shutdown, err := m.StartExport(context.Background(), ExportConfig{Enabled: true, Endpoint: srv.URL + "/v1/metrics", Instance: "same-pod"})
		if err != nil {
			t.Fatal(err)
		}
		m.Record("check", "mcp", ClassOpenAI, "unavailable", 0, time.Millisecond)
		if err := shutdown(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(payloads) != 2 {
		t.Fatalf("exports=%d", len(payloads))
	}
	ids := map[string]bool{}
	for _, p := range payloads {
		for _, rm := range p.ResourceMetrics {
			for _, a := range rm.Resource.Attributes {
				if a.Key == "service.instance.id" {
					ids[a.Value.GetStringValue()] = true
				}
			}
			for _, sm := range rm.ScopeMetrics {
				for _, signal := range sm.Metrics {
					if signal.Name == "scotland_connector_calls_total" {
						for _, dp := range signal.GetSum().DataPoints {
							if dp.GetAsInt() != 1 || dp.StartTimeUnixNano == 0 {
								t.Fatal("restart did not reset cumulative series")
							}
						}
					}
				}
			}
		}
	}
	if len(ids) != 2 {
		t.Fatal("restart identity reused")
	}
}

func TestCumulativeRecoveryDoesNotReplayIncrements(t *testing.T) {
	var deny atomic.Bool
	var mu sync.Mutex
	var values []int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if deny.Load() {
			w.WriteHeader(503)
			return
		}
		b, _ := io.ReadAll(r.Body)
		p := new(collector.ExportMetricsServiceRequest)
		if err := proto.Unmarshal(b, p); err != nil {
			t.Error(err)
		}
		for _, rm := range p.ResourceMetrics {
			for _, sm := range rm.ScopeMetrics {
				for _, signal := range sm.Metrics {
					if signal.Name == "scotland_connector_calls_total" {
						mu.Lock()
						values = append(values, signal.GetSum().DataPoints[0].GetAsInt())
						mu.Unlock()
					}
				}
			}
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	defer srv.Close()
	m := New(prometheus.NewRegistry())
	shutdown, err := m.StartExport(context.Background(), ExportConfig{Enabled: true, Endpoint: srv.URL + "/v1/metrics"})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = shutdown(context.Background()) }()
	m.Record("plan", "api", ClassDirect, "ok", 1, time.Millisecond)
	if err := m.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	deny.Store(true)
	m.Record("plan", "api", ClassDirect, "ok", 1, time.Millisecond)
	if err := m.ForceFlush(context.Background()); err == nil {
		t.Fatal("expected outage")
	}
	deny.Store(false)
	for i := 0; i < 2; i++ {
		if err := m.ForceFlush(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(values) != 3 || values[0] != 1 || values[1] != 2 || values[2] != 2 {
		t.Fatalf("cumulative recovery: %v", values)
	}
}
