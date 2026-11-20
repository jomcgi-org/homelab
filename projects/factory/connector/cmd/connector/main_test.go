package main

import (
	"context"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/metrics"
	"github.com/prometheus/client_golang/prometheus"
)

func TestFailedDispatchScrapePreservesOutage(t *testing.T) {
	reg := prometheus.NewRegistry()
	metrics.New(reg).ItsaplanUnavailable.WithLabelValues("admissions").Set(1)
	handler := scrapeHandler(reg, func(ctx context.Context, snap *metrics.DispatchSnapshot) error {
		snap.BusyRuns.WithLabelValues("coder").Set(3)
		return errors.New("query timed out")
	})
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest("GET", "/metrics", nil))
	body := response.Body.String()
	if response.Code != 200 || !strings.Contains(body, `factory_connector_itsaplan_unavailable{phase="admissions"} 1`) || !strings.Contains(body, "factory_connector_dispatch_collection_success 0") {
		t.Fatalf("missing scrape health or outage metric: %d %s", response.Code, body)
	}
	for _, name := range []string{"busy_runs", "dispatched_runs", "runnable_runs"} {
		if strings.Contains(body, "factory_connector_"+name) {
			t.Errorf("failed scrape exposed %s", name)
		}
	}
}
