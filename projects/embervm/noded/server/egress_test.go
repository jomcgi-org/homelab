package server

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

func TestEgressWorkloadScope(t *testing.T) {
	if !egressWorkloadAllowed([]string{"claude-runtime"}, "claude-runtime") {
		t.Error("allowed workload was rejected")
	}
	if egressWorkloadAllowed([]string{"claude-runtime"}, "task") {
		t.Error("disallowed workload was accepted")
	}
	if !egressWorkloadAllowed(nil, "task") {
		t.Error("empty workload scope should preserve EgressEnabled behavior")
	}
}

func TestStartEgressHonorsWorkloadScope(t *testing.T) {
	uds := filepath.Join(t.TempDir(), "guest.sock")
	s := &Server{
		cfg:    config.Config{EgressEnabled: true, EgressWorkloads: []string{"claude-runtime"}},
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	cancel := s.startEgress(uds, "vm-allowed", "claude-runtime")
	defer cancel()
	deadline := time.Now().Add(time.Second)
	for {
		if _, err := os.Stat(uds + "_1025"); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("forwarder did not start for the allowed workload")
		}
		time.Sleep(time.Millisecond)
	}

	disallowed := filepath.Join(t.TempDir(), "guest.sock")
	noOp := s.startEgress(disallowed, "vm-denied", "task")
	noOp()
	if _, err := os.Stat(disallowed + "_1025"); !os.IsNotExist(err) {
		t.Fatalf("forwarder started for a disallowed workload, stat err = %v", err)
	}
}
