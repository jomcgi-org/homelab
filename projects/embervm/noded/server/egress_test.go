package server

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

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

type buildEgressResult struct {
	resp *nodev1.BuildBaseResponse
	err  error
}

func newBuildEgressServer(t *testing.T, enabled bool, workloads []string, tr *fakeTransport, logger *slog.Logger) (*Server, *fakeDriver) {
	t.Helper()
	build := &fakeDriver{vsockDir: t.TempDir()}
	s := New(Options{
		Config: config.Config{
			Arch:              "amd64",
			Node:              "node-4",
			SnapshotRoot:      t.TempDir(),
			BootReadyTimeout:  5 * time.Second,
			Images:            map[string]config.Image{"img:1": {RootfsPath: "/rootfs.ext4"}},
			EgressEnabled:     enabled,
			EgressWorkloads:   workloads,
			EgressSidecarAddr: "127.0.0.1:1",
		},
		Driver:         &fakeDriver{},
		Transport:      tr,
		NewBuildDriver: func(BuildDriverSpec) BuildDriver { return build },
		Logger:         logger,
	})
	return s, build
}

func startBuildEgressTest(s *Server, workload string) <-chan buildEgressResult {
	result := make(chan buildEgressResult, 1)
	go func() {
		resp, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
			Trace:            &nodev1.Trace{Workload: workload},
			ImageRef:         "img:1",
			WorkloadRevision: "r1",
			ReadyPath:        "/shim/ready",
		})
		result <- buildEgressResult{resp: resp, err: err}
	}()
	return result
}

func waitForBuildReady(t *testing.T, started <-chan string) string {
	t.Helper()
	select {
	case uds := <-started:
		return uds
	case <-time.After(2 * time.Second):
		t.Fatal("base build did not reach WaitReady")
		return ""
	}
}

func waitForEgressSocket(t *testing.T, path string, present bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		_, err := os.Stat(path)
		if present && err == nil {
			return
		}
		if !present && os.IsNotExist(err) {
			return
		}
		if err != nil && !os.IsNotExist(err) {
			t.Fatalf("stat egress socket %q: %v", path, err)
		}
		if time.Now().After(deadline) {
			t.Fatalf("egress socket %q present = %v, want %v", path, err == nil, present)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func assertNoEgressSocket(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(100 * time.Millisecond)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			t.Fatalf("unexpected egress socket %q", path)
		} else if !os.IsNotExist(err) {
			t.Fatalf("stat egress socket %q: %v", path, err)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func receiveBuildEgressResult(t *testing.T, result <-chan buildEgressResult) buildEgressResult {
	t.Helper()
	select {
	case got := <-result:
		return got
	case <-time.After(2 * time.Second):
		t.Fatal("base build did not return")
		return buildEgressResult{}
	}
}

func TestBuildBaseEgressUsesExactWorkloadAndCancelsOnSuccess(t *testing.T) {
	started := make(chan string, 1)
	continueReady := make(chan struct{})
	tr := &fakeTransport{waitReadyStarted: started, waitReadyContinue: continueReady}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	workload := "claude.runtime"
	s, build := newBuildEgressServer(t, true, []string{workload}, tr, logger)
	result := startBuildEgressTest(s, workload)

	uds := waitForBuildReady(t, started)
	egressSocket := uds + "_1025"
	// The dot in the request workload is changed to an underscore in baseKey.
	// Reaching this listener proves runBuild received the exact request workload
	// instead of recovering a lossy value from the base key.
	waitForEgressSocket(t, egressSocket, true)
	close(continueReady)

	got := receiveBuildEgressResult(t, result)
	if got.err != nil || got.resp.GetSnapshotRef() == "" {
		t.Fatalf("BuildBase = %+v, %v, want successful snapshot", got.resp, got.err)
	}
	waitForEgressSocket(t, egressSocket, false)
	claims, releases, removeBundles, _ := build.counts()
	if claims != 1 || releases != 1 || removeBundles != 1 || build.snapshots != 1 {
		t.Fatalf("build lifecycle claims=%d releases=%d removeBundles=%d snapshots=%d, want 1/1/1/1",
			claims, releases, removeBundles, build.snapshots)
	}
}

func TestBuildBaseEgressCancelsOnWaitReadyFailure(t *testing.T) {
	started := make(chan string, 1)
	continueReady := make(chan struct{})
	tr := &fakeTransport{
		waitReadyStarted:  started,
		waitReadyContinue: continueReady,
		waitReadyErr:      errors.New("guest not ready"),
	}
	s, build := newBuildEgressServer(t, true, []string{"claude-runtime"}, tr, slog.New(slog.NewTextHandler(io.Discard, nil)))
	result := startBuildEgressTest(s, "claude-runtime")

	uds := waitForBuildReady(t, started)
	egressSocket := uds + "_1025"
	waitForEgressSocket(t, egressSocket, true)
	close(continueReady)

	got := receiveBuildEgressResult(t, result)
	if got.err == nil || !strings.Contains(got.err.Error(), "guest readiness: guest not ready") {
		t.Fatalf("BuildBase error = %v, want guest readiness failure", got.err)
	}
	waitForEgressSocket(t, egressSocket, false)
	claims, releases, removeBundles, _ := build.counts()
	if claims != 1 || releases != 1 || removeBundles != 1 || build.snapshots != 0 {
		t.Fatalf("failed build lifecycle claims=%d releases=%d removeBundles=%d snapshots=%d, want 1/1/1/0",
			claims, releases, removeBundles, build.snapshots)
	}
}

func TestBuildBaseEgressSkipsWorkloadOutsideScope(t *testing.T) {
	started := make(chan string, 1)
	continueReady := make(chan struct{})
	tr := &fakeTransport{waitReadyStarted: started, waitReadyContinue: continueReady}
	var logs bytes.Buffer
	s, build := newBuildEgressServer(t, true, []string{"claude-runtime"}, tr, slog.New(slog.NewTextHandler(&logs, nil)))
	result := startBuildEgressTest(s, "outside.runtime")

	uds := waitForBuildReady(t, started)
	assertNoEgressSocket(t, uds+"_1025")
	logText := logs.String()
	if !strings.Contains(logText, "egress lane not opened; workload is outside the configured scope") ||
		!strings.Contains(logText, "workload=outside.runtime") {
		t.Fatalf("egress skip log = %q, want skip message with exact workload", logText)
	}
	close(continueReady)
	if got := receiveBuildEgressResult(t, result); got.err != nil {
		t.Fatalf("BuildBase outside egress scope: %v", got.err)
	}
	_, releases, removeBundles, _ := build.counts()
	if releases != 1 || removeBundles != 1 {
		t.Fatalf("build teardown releases=%d removeBundles=%d, want 1/1", releases, removeBundles)
	}
}

func TestBuildBaseEgressDisabledDoesNotChangeTeardown(t *testing.T) {
	started := make(chan string, 1)
	continueReady := make(chan struct{})
	tr := &fakeTransport{waitReadyStarted: started, waitReadyContinue: continueReady}
	s, build := newBuildEgressServer(t, false, []string{"claude-runtime"}, tr, slog.New(slog.NewTextHandler(io.Discard, nil)))
	result := startBuildEgressTest(s, "claude-runtime")

	uds := waitForBuildReady(t, started)
	assertNoEgressSocket(t, uds+"_1025")
	close(continueReady)
	if got := receiveBuildEgressResult(t, result); got.err != nil {
		t.Fatalf("BuildBase with egress disabled: %v", got.err)
	}
	_, releases, removeBundles, _ := build.counts()
	if releases != 1 || removeBundles != 1 {
		t.Fatalf("build teardown releases=%d removeBundles=%d, want 1/1", releases, removeBundles)
	}
}
