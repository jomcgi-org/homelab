package main

import (
	"bytes"
	"errors"
	"log/slog"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/server"
)

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
