package main

// End-to-end proof for the S6 TLC leg (issue #6415).
//
// s6_trace_test.go checks string shape and classifies canned TLC output, and
// the specs/BUILD genrules check the committed in-spec fixtures. Neither runs
// the composition /verdict depends on: the Go-generated window module
// (writeS6TraceModule, a `window` module that INSTANCEs adoption_trace WITH
// Fixture <- "live", LiveTrace <- W) through real TLC. This test closes that
// gap against the pinned toolchain (bazel/tla/repositories.bzl): an httptest
// server serves the traceWindowView JSON the control plane's trace-window
// route emits, runS6 fetches it, and the assertions below read real TLC
// output.
//
// The JSON fixtures mirror the committed TLA windows record for record, using
// the envelope the route emits (run_id, seq, mono, ts, spec, action, vars;
// router.ex trace_window_view over the SpecTrace writer) and the vars keys
// s6TraceRecordConstant reads (vm_id, node_id, session_id, had_vm, gate,
// node_confirmed, confirmed_by), with native JSON booleans as the writer's
// jsonable/1 preserves them. Verified against the emit sites: confirm_destroy
// carries all seven keys, while dispatch, prime, succeed, adopt, checkpoint
// and restart_cp carry their subset and the exporter fills neutral defaults,
// so the fixtures needed no runner change.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// s6TLCRunfile resolves a $(rlocationpath ...) env entry to an existing file,
// joining the runfiles-relative path onto every known runfiles root. Empty
// when unset or missing: callers skip, which keeps plain `go test` (no Bazel
// data staged) green while CI runs the real toolchain.
func s6TLCRunfile(envKey string) string {
	rel := os.Getenv(envKey)
	if rel == "" {
		return ""
	}
	if filepath.IsAbs(rel) {
		if s6TLCFileExists(rel) {
			return rel
		}
		return ""
	}
	roots := []string{""}
	if dir := os.Getenv("RUNFILES_DIR"); dir != "" {
		roots = append(roots, dir)
	}
	if dir := os.Getenv("TEST_SRCDIR"); dir != "" {
		roots = append(roots, dir)
	}
	if ex, err := os.Executable(); err == nil {
		dir := filepath.Dir(ex)
		for i := 0; i < 6; i++ {
			if strings.HasSuffix(dir, ".runfiles") {
				roots = append(roots, dir)
				break
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	for _, root := range roots {
		candidate := rel
		if root != "" {
			candidate = filepath.Join(root, rel)
		}
		if s6TLCFileExists(candidate) {
			return candidate
		}
	}
	return ""
}

func s6TLCFileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

// mustS6TLCTestConfig points cfg at the pinned toolchain staged as test data.
// It skips when the data is absent, so the TLC leg only ever runs where the
// real toolchain is present (CI's Linux executor via Bazel).
func mustS6TLCTestConfig(t *testing.T) config {
	t.Helper()
	java := s6TLCRunfile("S6_TLC_TEST_JAVA")
	jar := s6TLCRunfile("S6_TLC_TEST_JAR")
	spec := s6TLCRunfile("S6_TLC_TEST_SPEC")
	if java == "" || jar == "" || spec == "" {
		t.Skip("S6 TLC toolchain not staged; run with Bazel: //projects/embervm/conformance:s6_tlc_test")
	}
	return config{
		minTraceEvents: 4,
		tlcJava:        java,
		tlcJar:         jar,
		tlcSpecDir:     filepath.Dir(spec),
	}
}

func s6TLCRecord(seq int64, action string, vars map[string]any) traceRecord {
	return traceRecord{
		RunID:  "run-1",
		Seq:    seq,
		Mono:   seq,
		TS:     1750000000000 + seq,
		Spec:   "adoption",
		Action: action,
		Vars:   vars,
	}
}

// s6TLCPassWindow mirrors PassWindow in adoption_trace.tla record for record:
// prime, adopt, dispatch, CP restart, re-adopt, checkpoint, succeed (destroy
// intent), begin_destroy, node-confirmed confirm_destroy, checkpoint. TLC
// must accept it exhaustively.
func s6TLCPassWindow() []traceRecord {
	return []traceRecord{
		s6TLCRecord(1, "prime", map[string]any{"vm_id": "v1", "node_id": "n1", "workload": "pi-runtime", "lane": "dev"}),
		s6TLCRecord(2, "adopt_inventory", map[string]any{"node_id": "n1", "vm_id": "v1", "vm_ids": []any{"v1"}}),
		s6TLCRecord(3, "dispatch_warm", map[string]any{"task_id": "t1", "workload": "pi-runtime", "vm_id": "v1", "node_id": "n1", "session_id": "s1", "provenance": "adopted"}),
		s6TLCRecord(4, "restart_cp", map[string]any{"incarnation_id": "run-1"}),
		s6TLCRecord(5, "adopt_inventory", map[string]any{"node_id": "n1", "vm_id": "v1", "vm_ids": []any{"v1"}}),
		s6TLCRecord(6, "checkpoint", map[string]any{"node_id": "n1", "node_reported": true}),
		s6TLCRecord(7, "succeed", map[string]any{"task_id": "t1", "workload": "pi-runtime", "vm_id": "v1", "session_id": "s1"}),
		s6TLCRecord(8, "begin_destroy", map[string]any{"session_id": "s1", "vm_id": "v1", "node_id": "n1", "gate": true, "resumed": false}),
		s6TLCRecord(9, "confirm_destroy", map[string]any{"session_id": "s1", "vm_id": "v1", "node_id": "n1", "gate": true, "had_vm": true, "node_confirmed": true, "confirmed_by": "teardown"}),
		s6TLCRecord(10, "checkpoint", map[string]any{"node_id": "n1", "node_reported": true}),
	}
}

// s6TLCDestroyBeforeConfirmWindow mirrors DestroyBeforeConfirmWindow: the
// gated confirm for live VM s1/v1 carries node_confirmed FALSE with no
// teardown or absence proof, ordered before any intent record. TLC must
// reject it naming NoDestroyBeforeConfirm.
func s6TLCDestroyBeforeConfirmWindow() []traceRecord {
	return []traceRecord{
		s6TLCRecord(1, "prime", map[string]any{"vm_id": "v1", "node_id": "n1", "workload": "pi-runtime", "lane": "dev"}),
		s6TLCRecord(2, "dispatch_warm", map[string]any{"task_id": "t1", "workload": "pi-runtime", "vm_id": "v1", "node_id": "n1", "session_id": "s1", "provenance": "adopted"}),
		s6TLCRecord(3, "confirm_destroy", map[string]any{"session_id": "s1", "vm_id": "v1", "node_id": "n1", "gate": true, "had_vm": true, "node_confirmed": false, "confirmed_by": ""}),
		s6TLCRecord(4, "succeed", map[string]any{"task_id": "t1", "workload": "pi-runtime", "vm_id": "v1", "session_id": "s1"}),
		s6TLCRecord(5, "begin_destroy", map[string]any{"session_id": "s1", "vm_id": "v1", "node_id": "n1", "gate": true, "resumed": false}),
	}
}

// s6TLCDoubleDispatchWindow mirrors DoubleDispatchWindow: v1 dispatched twice
// (warm for s1, miss for s2) with no intervening consumption. TLC must reject
// it naming NoDoubleAssign.
func s6TLCDoubleDispatchWindow() []traceRecord {
	return []traceRecord{
		s6TLCRecord(1, "prime", map[string]any{"vm_id": "v1", "node_id": "n1", "workload": "pi-runtime", "lane": "dev"}),
		s6TLCRecord(2, "dispatch_warm", map[string]any{"task_id": "t1", "workload": "pi-runtime", "vm_id": "v1", "node_id": "n1", "session_id": "s1", "provenance": "adopted"}),
		s6TLCRecord(3, "checkpoint", map[string]any{"node_id": "n1", "node_reported": true}),
		s6TLCRecord(4, "dispatch_miss", map[string]any{"task_id": "t2", "workload": "pi-runtime", "vm_id": "v1", "node_id": "n1", "session_id": "s2", "provenance": "miss"}),
		s6TLCRecord(5, "checkpoint", map[string]any{"node_id": "n1", "node_reported": true}),
	}
}

// s6TLCWindowBody serializes records as the trace-window route emits them.
func s6TLCWindowBody(t *testing.T, records []traceRecord) string {
	t.Helper()
	if records == nil {
		records = []traceRecord{}
	}
	raw, err := json.Marshal(traceWindowView{Enabled: true, RunIDs: []string{"run-1"}, RecordCount: len(records), Records: records})
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// s6TLCClient serves one trace-window body over httptest. It mirrors the
// s6TestClient helper in s6_trace_test.go without sharing it, so this target
// stays self-contained in one file.
func s6TLCClient(t *testing.T, body string) (*controlPlaneClient, func()) {
	t.Helper()
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != s6TraceWindowPath {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(body))
	}))
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	return client, server.Close
}

func TestS6TLCPassWindowEndToEnd(t *testing.T) {
	cfg := mustS6TLCTestConfig(t)
	records := s6TLCPassWindow()
	client, close := s6TLCClient(t, s6TLCWindowBody(t, records))
	defer close()

	result := runS6(context.Background(), cfg, client, time.Now())
	if result.Verdict != verdictPass {
		t.Fatalf("pass window verdict = %q, want pass; detail=%s", result.Verdict, result.Detail)
	}
	if !strings.Contains(result.Detail, "coverage=10") {
		t.Fatalf("pass window detail %q does not carry coverage=10", result.Detail)
	}

	// The verdict above already proves classification, but a pass is only
	// meaningful when the run was exhaustive: assert the queue-drained line
	// on the captured TLC output itself.
	_, _, output := runS6TLCWithOutput(context.Background(), cfg, records)
	if !tlcQueueDrained(output) {
		t.Fatalf("pass window TLC run was not exhaustive; output:\n%s", output)
	}
}

func TestS6TLCDestroyBeforeConfirmFailsEndToEnd(t *testing.T) {
	cfg := mustS6TLCTestConfig(t)
	client, close := s6TLCClient(t, s6TLCWindowBody(t, s6TLCDestroyBeforeConfirmWindow()))
	defer close()

	result := runS6(context.Background(), cfg, client, time.Now())
	if result.Verdict != verdictFail {
		t.Fatalf("destroy-before-confirm verdict = %q, want fail; detail=%s", result.Verdict, result.Detail)
	}
	if !strings.Contains(result.Detail, "NoDestroyBeforeConfirm") {
		t.Fatalf("destroy-before-confirm detail %q does not name NoDestroyBeforeConfirm", result.Detail)
	}
}

func TestS6TLCDoubleDispatchFailsEndToEnd(t *testing.T) {
	cfg := mustS6TLCTestConfig(t)
	client, close := s6TLCClient(t, s6TLCWindowBody(t, s6TLCDoubleDispatchWindow()))
	defer close()

	result := runS6(context.Background(), cfg, client, time.Now())
	if result.Verdict != verdictFail {
		t.Fatalf("double-dispatch verdict = %q, want fail; detail=%s", result.Verdict, result.Detail)
	}
	if !strings.Contains(result.Detail, "NoDoubleAssign") {
		t.Fatalf("double-dispatch detail %q does not name NoDoubleAssign", result.Detail)
	}
}

func TestS6TLCThinWindowNeverReachesTLC(t *testing.T) {
	// No toolchain resolution here: this test runs everywhere, including
	// plain `go test` with no Bazel data. The toolchain paths below must
	// never be executed, because vacuity is checked before TLC. If that
	// ordering ever regresses, the bogus java fails and the verdict reads
	// incomplete, never vacuous, so the assertions below catch it.
	cfg := config{
		minTraceEvents: 4,
		tlcJava:        "/nonexistent/java-must-never-run",
		tlcJar:         "/nonexistent/tla2tools-must-never-run.jar",
		tlcSpecDir:     "/nonexistent/spec-dir-must-never-run",
	}
	thin := []traceRecord{
		s6TLCRecord(1, "prime", map[string]any{"vm_id": "v1", "node_id": "n1"}),
		s6TLCRecord(2, "dispatch_warm", map[string]any{"vm_id": "v1", "node_id": "n1", "session_id": "s1"}),
	}
	for _, records := range [][]traceRecord{nil, thin} {
		client, close := s6TLCClient(t, s6TLCWindowBody(t, records))
		result := runS6(context.Background(), cfg, client, time.Now())
		close()
		if result.Verdict != verdictVacuous {
			t.Fatalf("%d-record window verdict = %q, want vacuous; detail=%s", len(records), result.Verdict, result.Detail)
		}
		if !strings.Contains(result.Detail, "thin window") {
			t.Fatalf("%d-record window detail %q does not explain thin-window vacuity", len(records), result.Detail)
		}
	}
}
