package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

// stubAPI is an instant in-process ate API: the benchmark measures the gate,
// not gRPC.
type stubAPI struct {
	mu sync.Mutex
	n  int
}

func (s *stubAPI) Create(_ context.Context, name string) (actor, error) {
	return actor{Name: name}, nil
}

func (s *stubAPI) Resume(_ context.Context, name string) (actor, error) {
	return actor{Name: name, Running: true}, nil
}

func (s *stubAPI) Get(_ context.Context, name string) (actor, error) {
	return actor{Name: name, Running: true}, nil
}
func (s *stubAPI) Delete(context.Context, string) error          { return nil }
func (s *stubAPI) List(context.Context, string) ([]actor, error) { return nil, nil }

func benchGate(b *testing.B) (*gate, func()) {
	b.Helper()
	cfg, err := parseConfig([]string{"--pod-name=gate-0"})
	if err != nil {
		b.Fatal(err)
	}
	cfg.TargetWaiting, cfg.CreateConcurrency, cfg.MaxInFlight = 16, 8, 16
	m := newMetrics()
	lg := newLimitedLog(slog.New(slog.NewJSONHandler(io.Discard, nil)))
	ctx, cancel := context.WithCancel(context.Background())
	p := newPool(ctx, cfg, &stubAPI{}, m, lg)
	rt := &fakeRouter{}
	g := newGate(cfg, p, rt, m, lg)
	done := make(chan struct{})
	go func() { defer close(done); p.run(ctx) }()
	for !p.ready() {
		time.Sleep(time.Millisecond)
	}
	return g, func() { cancel(); <-done; p.bg.Wait() }
}

func percentile(sorted []time.Duration, q float64) float64 {
	return float64(sorted[int(float64(len(sorted)-1)*q)].Microseconds())
}

// BenchmarkGateOverhead is the cost of the gate itself on a pool hit: admission,
// the claim, the router call (a fake that answers at once) and the reply, with
// a 16 KiB body. Pool misses count in the "misses" metric: they take path A,
// which a stub API makes instant, so keep misses near zero for a clean number.
func BenchmarkGateOverhead(b *testing.B) {
	g, stop := benchGate(b)
	defer stop()
	body := strings.Repeat("a", 16<<10)
	h := g.handler()
	lat := make([]time.Duration, 0, b.N)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		req := httptest.NewRequest(http.MethodPost, "/scan", strings.NewReader(body))
		rec := httptest.NewRecorder()
		began := time.Now()
		h.ServeHTTP(rec, req)
		lat = append(lat, time.Since(began))
		if rec.Code != 200 {
			b.Fatalf("status %d", rec.Code)
		}
	}
	b.StopTimer()
	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	b.ReportMetric(percentile(lat, 0.50), "p50_us")
	b.ReportMetric(percentile(lat, 0.95), "p95_us")
	b.ReportMetric(percentile(lat, 0.99), "p99_us")
}

// BenchmarkGateOverLoopback adds a real HTTP connection with no keep-alive, as
// the scanner makes it, to the same path.
func BenchmarkGateOverLoopback(b *testing.B) {
	g, stop := benchGate(b)
	defer stop()
	srv := httptest.NewServer(g.handler())
	defer srv.Close()
	client := &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
	body := strings.Repeat("a", 16<<10)
	lat := make([]time.Duration, 0, b.N)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		began := time.Now()
		resp, err := client.Post(srv.URL+"/scan", "text/plain", strings.NewReader(body))
		if err != nil {
			b.Fatal(err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		lat = append(lat, time.Since(began))
		if resp.StatusCode != 200 {
			b.Fatalf("status %d", resp.StatusCode)
		}
	}
	b.StopTimer()
	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	b.ReportMetric(percentile(lat, 0.50), "p50_us")
	b.ReportMetric(percentile(lat, 0.95), "p95_us")
	b.ReportMetric(percentile(lat, 0.99), "p99_us")
}
