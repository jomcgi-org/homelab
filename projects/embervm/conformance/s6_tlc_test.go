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

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

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

// TestS6TLCVerdictsUnderChartJavaToolOptions runs the pass and a fail window
// with the JAVA_TOOL_OPTIONS the chart renders when conformance.s6.enabled is
// on (conformance.s6.tlc.javaToolOptions in chart/values.yaml). The JVM echoes
// the variable on stderr, which the runner classifies with stdout, so this
// proves the banner moves no verdict and the worker count stays pinned.
func TestS6TLCVerdictsUnderChartJavaToolOptions(t *testing.T) {
	cfg := mustS6TLCTestConfig(t)
	t.Setenv("JAVA_TOOL_OPTIONS", "-XX:ActiveProcessorCount=2 -XX:MaxRAMPercentage=50")

	verdict, detail, output := runS6TLCWithOutput(context.Background(), cfg, s6TLCPassWindow())
	if !strings.Contains(output, "Picked up JAVA_TOOL_OPTIONS") {
		t.Fatalf("JVM did not pick up JAVA_TOOL_OPTIONS; output:\n%s", output)
	}
	if !strings.Contains(output, "with 2 workers") {
		t.Fatalf("-workers auto did not follow ActiveProcessorCount=2; output:\n%s", output)
	}
	if verdict != verdictPass || !tlcQueueDrained(output) {
		t.Fatalf("pass window verdict = %q, want an exhaustive pass; detail=%s", verdict, detail)
	}

	verdict, detail, _ = runS6TLCWithOutput(context.Background(), cfg, s6TLCDoubleDispatchWindow())
	if verdict != verdictFail || !strings.Contains(detail, "NoDoubleAssign") {
		t.Fatalf("double-dispatch verdict = %q, want fail naming NoDoubleAssign; detail=%s", verdict, detail)
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

// These cases run the generated window through real TLC, including mutations
// that satisfy the legacy ordering invariants but violate the shared contract.
func TestS6DestroyContract(t *testing.T) {
	cfg := mustS6TLCTestConfig(t)
	tests := []struct {
		name    string
		mutate  func([]traceRecord) []traceRecord
		verdict string
		detail  string
	}{
		{"duplicate completion", func(r []traceRecord) []traceRecord { return append(r, r[8]) }, verdictFail, "DestroyTransitionsConform"},
		{"begin after completion", func(r []traceRecord) []traceRecord { return append(r, r[7]) }, verdictFail, "DestroyTransitionsConform"},
		{"redrive intent", func(r []traceRecord) []traceRecord { return append(append(r[:8:8], r[7]), r[8:]...) }, verdictPass, "observed completions=1"},
		{"wrong vm", func(r []traceRecord) []traceRecord { r[8].Vars["vm_id"] = "v2"; return r }, verdictIncomplete, "matching intent"},
		{"wrong session", func(r []traceRecord) []traceRecord { r[8].Vars["session_id"] = "s2"; return r }, verdictIncomplete, "matching intent"},
		{"missing intent", func(r []traceRecord) []traceRecord { return append(r[:7:7], r[8:]...) }, verdictIncomplete, "matching intent"},
		{"missing completion", func(r []traceRecord) []traceRecord { return append(r[:8:8], r[9:]...) }, verdictIncomplete, "open destruction"},
		{"unconfirmed", func(r []traceRecord) []traceRecord { r[8].Vars["node_confirmed"] = false; return r }, verdictFail, "NoDestroyBeforeConfirm"},
		{"unsupported node gone", func(r []traceRecord) []traceRecord { r[8].Vars["confirmed_by"] = "node_gone"; return r }, verdictIncomplete, "unsupported destruction evidence"},
		{"missing typed evidence", func(r []traceRecord) []traceRecord { delete(r[8].Vars, "node_confirmed"); return r }, verdictIncomplete, "typed confirmation"},
		{"missing identity", func(r []traceRecord) []traceRecord { delete(r[7].Vars, "session_id"); return r }, verdictIncomplete, "missing destroy identity"},
		{"cross-run", func(r []traceRecord) []traceRecord { r[8].RunID = "run-2"; return r }, verdictIncomplete, "incarnation"},
		{"drop on another spec", func(r []traceRecord) []traceRecord {
			r[5].Spec = "other"
			r[5].Vars["spec_trace_dropped_before"] = 1
			return r
		}, verdictIncomplete, "dropped events"},
		{"no destruction", func(r []traceRecord) []traceRecord { return r[:6] }, verdictVacuous, "no completed"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			records := test.mutate(s6TLCPassWindow())
			for i := range records {
				records[i].Seq = int64(i + 1)
			}
			client, close := s6TLCClient(t, s6TLCWindowBody(t, records))
			defer close()
			result := runS6(context.Background(), cfg, client, time.Now())
			if result.Verdict != test.verdict || !strings.Contains(result.Detail, test.detail) {
				t.Fatalf("got %s: %s; want %s containing %q", result.Verdict, result.Detail, test.verdict, test.detail)
			}
		})
	}
}
