package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/server"
)

func TestStatefulAndGroupProbeOptionsAreIndependent(t *testing.T) {
	cfg := config.Config{
		StatefulProbeInterval:      11 * time.Second,
		StatefulUnhealthyThreshold: 6,
		GroupProbeInterval:         7 * time.Second,
		GroupUnhealthyThreshold:    4,
	}
	var opts server.Options
	setStatefulAndGroupProbeOptions(&opts, cfg)
	if opts.StatefulProbeInterval != 11*time.Second {
		t.Errorf("StatefulProbeInterval = %v, want 11s", opts.StatefulProbeInterval)
	}
	if opts.StatefulUnhealthyThreshold != 6 {
		t.Errorf("StatefulUnhealthyThreshold = %d, want 6", opts.StatefulUnhealthyThreshold)
	}
	if opts.GroupProbeInterval != 7*time.Second {
		t.Errorf("GroupProbeInterval = %v, want 7s", opts.GroupProbeInterval)
	}
	if opts.GroupUnhealthyThreshold != 4 {
		t.Errorf("GroupUnhealthyThreshold = %d, want 4", opts.GroupUnhealthyThreshold)
	}
}

func TestSetupTracingContinuesWhenConfigurationIsInvalid(t *testing.T) {
	t.Setenv("OTEL_SDK_DISABLED", "false")
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example.test:4317")
	t.Setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
	t.Setenv("OTEL_TRACES_SAMPLER_ARG", "not-a-rate")

	var output bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&output, nil))
	shutdown := setupTracing(context.Background(), logger)
	shutdown()

	got := output.String()
	if !strings.Contains(got, "continuing with tracing disabled") ||
		!strings.Contains(got, "invalid OTEL_TRACES_SAMPLER_ARG") {
		t.Fatalf("tracing initialization log = %q, want fail-open error", got)
	}
}

func TestHTTPServerErrorLoggerEmitsStructuredJSON(t *testing.T) {
	var output bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&output, nil))
	server := nodedHTTPServer(logger, http.NotFoundHandler())

	server.ErrorLog.Print("accept failed")

	var record map[string]any
	if err := json.Unmarshal(output.Bytes(), &record); err != nil {
		t.Fatalf("decode HTTP error log %q: %v", output.String(), err)
	}
	if record["level"] != "ERROR" || record["msg"] != "accept failed" {
		t.Fatalf("HTTP error record = %#v, want ERROR JSON message", record)
	}
}

func TestBaseAdoptionNonMarkerIOErrorLogsFailureClass(t *testing.T) {
	var output bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&output, nil))

	logBaseAdoptionRetry(logger, errors.New("scan base bundles: input/output error"))

	got := output.String()
	if !strings.Contains(got, "level=ERROR") || !strings.Contains(got, "base adoption scan failed") {
		t.Fatalf("non-marker IO log = %q, want loud scan failure", got)
	}
	if strings.Contains(got, "waiting for scratch generation") {
		t.Fatalf("non-marker IO error was mislabeled as marker wait: %q", got)
	}
}

func TestBaseAdoptionMarkerErrorKeepsExpectedWaitLog(t *testing.T) {
	var output bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&output, nil))

	logBaseAdoptionRetry(logger, server.ErrScratchGenerationUnavailable)

	got := output.String()
	if !strings.Contains(got, "level=WARN") || !strings.Contains(got, "waiting for scratch generation") {
		t.Fatalf("marker wait log = %q, want quiet expected wait", got)
	}
}
