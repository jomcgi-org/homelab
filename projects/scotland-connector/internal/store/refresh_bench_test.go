package store

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"runtime"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
)

// diskSnapshot returns a fresh body on every refresh, like a changed R2 object.
// It does not keep another copy of the input alive in the benchmark harness.
type diskSnapshot struct{ path string }

func (d diskSnapshot) Get(_ context.Context, _, key, _ string, limit int64) (r2.Object, error) {
	if key == "history-map.json" {
		return r2.Object{Body: []byte(history), ETag: "history"}, nil
	}
	f, err := os.Open(d.path)
	if err != nil {
		return r2.Object{}, err
	}
	defer f.Close()
	// Match r2.Client.Get's bounded ReadAll, including buffer growth. A known-
	// size ReadFile would understate the allocation peak of the production path.
	body, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return r2.Object{}, err
	}
	if int64(len(body)) > limit {
		return r2.Object{}, fmt.Errorf("benchmark snapshot exceeds %d bytes", limit)
	}
	return r2.Object{Body: body, ETag: "changed"}, nil
}

// BenchmarkSnapshotRefresh measures a replacement with the old dataset/body
// already resident. Supply a producer fixture through STARS_BENCH_SNAPSHOT;
// run each object in a fresh process with -benchtime=1x and GOMEMLIMIT=220MiB.
// This is a synthetic lower bound: no HTTP/TLS, server, walks or real history.
func BenchmarkSnapshotRefresh(b *testing.B) {
	path := os.Getenv("STARS_BENCH_SNAPSHOT")
	if path == "" {
		b.Skip("set STARS_BENCH_SNAPSHOT to an offline producer fixture")
	}
	s := New(Config{Bucket: "stars", SitesKey: "sites", HistoryKey: "history-map.json"},
		diskSnapshot{path}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := s.Refresh(context.Background()); err != nil {
		b.Fatal(err)
	}
	previous := s.Current()
	runtime.GC()
	type peaks struct{ heap, managed uint64 }
	done := make(chan struct{})
	started := make(chan struct{})
	result := make(chan peaks)
	go func() {
		var peak peaks
		sample := func() {
			var m runtime.MemStats
			runtime.ReadMemStats(&m)
			peak.heap = max(peak.heap, m.HeapInuse)
			peak.managed = max(peak.managed, m.Sys-m.HeapReleased)
		}
		sample()
		close(started)
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				sample()
			case <-done:
				sample()
				result <- peak
				return
			}
		}
	}()
	<-started
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if err := s.Refresh(context.Background()); err != nil {
			b.Fatal(err)
		}
	}
	b.StopTimer()
	close(done)
	peak := <-result
	runtime.KeepAlive(previous)
	runtime.KeepAlive(s)
	b.ReportMetric(float64(peak.heap), "peak-heap-inuse-B")
	b.ReportMetric(float64(peak.managed), "peak-go-managed-B")
}
