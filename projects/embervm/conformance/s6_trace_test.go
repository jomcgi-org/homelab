package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func s6TestClient(t *testing.T, body string, status int) (*controlPlaneClient, func()) {
	t.Helper()
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != s6TraceWindowPath {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	return client, server.Close
}

func s6Record(action, vm, session string) string {
	return `{"run_id":"run-1","seq":1,"mono":1,"ts":1,"spec":"adoption","action":"` + action + `","vars":{"vm_id":"` + vm + `","node_id":"n1","session_id":"` + session + `","had_vm":true,"gate":true,"node_confirmed":true,"confirmed_by":"teardown"}}`
}

func TestRunS6ThinWindowIsVacuousNeverPass(t *testing.T) {
	for _, body := range []string{
		`{"enabled":true,"run_ids":[],"record_count":0,"records":[]}`,
		`{"enabled":true,"run_ids":["run-1"],"record_count":1,"records":[` + s6Record("prime", "v1", "") + `]}`,
	} {
		client, close := s6TestClient(t, body, http.StatusOK)
		cfg := config{minTraceEvents: 4}
		result := runS6(context.Background(), cfg, client, time.Now())
		close()
		if result.Verdict != verdictVacuous {
			t.Fatalf("thin window verdict = %q, want vacuous; detail=%s", result.Verdict, result.Detail)
		}
	}
}

func TestRunS6DisabledGateIsVacuous(t *testing.T) {
	client, close := s6TestClient(t, `{"enabled":false,"run_ids":[],"record_count":0,"records":[]}`, http.StatusOK)
	defer close()
	result := runS6(context.Background(), config{minTraceEvents: 4}, client, time.Now())
	if result.Verdict != verdictVacuous {
		t.Fatalf("disabled gate verdict = %q, want vacuous", result.Verdict)
	}
}

func TestRunS6WithoutToolchainIsIncompleteNeverPass(t *testing.T) {
	records := []string{
		s6Record("prime", "v1", ""),
		s6Record("dispatch_warm", "v1", "s1"),
		s6Record("begin_destroy", "v1", "s1"),
		s6Record("confirm_destroy", "v1", "s1"),
		s6Record("checkpoint", "", ""),
	}
	body := `{"enabled":true,"run_ids":["run-1"],"record_count":5,"records":[` + strings.Join(records, ",") + `]}`
	client, close := s6TestClient(t, body, http.StatusOK)
	defer close()
	result := runS6(context.Background(), config{minTraceEvents: 4}, client, time.Now())
	if result.Verdict != verdictIncomplete {
		t.Fatalf("no-toolchain verdict = %q, want incomplete; detail=%s", result.Verdict, result.Detail)
	}
}

func TestClassifyS6TLCOutput(t *testing.T) {
	passOutput := "TLC2 Version 2.19\n133 states generated, 42 distinct states found, 0 destroyed, 0 states left on queue.\nNo error has been found.\n"
	failOutput := "TLC2 Version 2.19\nInvariant NoDestroyBeforeConfirm is violated.\n10 states generated, 8 distinct states found, 0 destroyed, 2 states left on queue.\n"
	truncatedOutput := "TLC2 Version 2.19\n100000 states generated, 90000 distinct states found, 0 destroyed, 15000 states left on queue.\n"
	crashOutput := "Exception in thread \"main\" java.lang.OutOfMemoryError\n"

	tests := []struct {
		name    string
		output  string
		verdict string
		detail  string
	}{
		{name: "exhaustive clean run passes", output: passOutput, verdict: verdictPass, detail: "coverage=7"},
		{name: "named violation fails with invariant", output: failOutput, verdict: verdictFail, detail: "NoDestroyBeforeConfirm"},
		{name: "truncated run is incomplete", output: truncatedOutput, verdict: verdictIncomplete, detail: "withheld"},
		{name: "crash names no invariant so never fail", output: crashOutput, verdict: verdictIncomplete, detail: "withheld"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			verdict, detail := classifyS6TLCOutput(test.output, 7)
			if verdict != test.verdict {
				t.Fatalf("verdict = %q, want %q; detail=%s", verdict, test.verdict, detail)
			}
			if !strings.Contains(detail, test.detail) {
				t.Fatalf("detail %q does not contain %q", detail, test.detail)
			}
		})
	}
}

func TestExtractViolatedInvariant(t *testing.T) {
	if got := extractViolatedInvariant("Invariant NoDoubleAssign is violated.\n"); got != "NoDoubleAssign" {
		t.Fatalf("got %q, want NoDoubleAssign", got)
	}
	if got := extractViolatedInvariant("No error has been found.\n"); got != "" {
		t.Fatalf("clean output named %q, want empty", got)
	}
}

func TestWriteS6TraceModule(t *testing.T) {
	records := []traceRecord{
		{RunID: "run-1", Action: "prime", Vars: map[string]any{"vm_id": "v1", "node_id": "n1"}},
		{RunID: "run-1", Action: "confirm_destroy", Vars: map[string]any{
			"vm_id": `v"1`, "node_id": "n1", "session_id": "s1",
			"had_vm": true, "gate": true, "node_confirmed": true, "confirmed_by": "teardown",
		}},
	}
	var module, cfgText strings.Builder
	writeS6TraceModule(records, 4, &module, &cfgText)
	generated, cfg := module.String(), cfgText.String()
	// The window rides in the module by INSTANCE substitution: TLC
	// configuration files reject tuples and records, so the .cfg carries
	// only the scalar specification and invariant selection.
	for _, want := range []string{
		"---- MODULE window ----",
		"VARIABLES cursor",
		"W ==",
		`AT == INSTANCE adoption_trace WITH Fixture <- "live", LiveTrace <- W, MinTraceEvents <- 4, cursor <- cursor`,
		"Spec == AT!Spec",
		"TraceWellFormed == AT!TraceWellFormed",
		"NoDestroyBeforeConfirm == AT!NoDestroyBeforeConfirm",
		"NoDoubleAssign == AT!NoDoubleAssign",
		`action |-> "prime"`,
		`vm |-> "v1"`,
		`had_vm |-> TRUE`,
		// The quote and backslash in the vm_id must be escaped, never raw.
		`vm |-> "v\"1"`,
	} {
		if !strings.Contains(generated, want) {
			t.Fatalf("generated module missing %q:\n%s", want, generated)
		}
	}
	for _, want := range []string{
		"SPECIFICATION Spec",
		"CHECK_DEADLOCK FALSE",
		"INVARIANT TraceWellFormed",
		"INVARIANT NoDestroyBeforeConfirm",
		"INVARIANT NoDoubleAssign",
	} {
		if !strings.Contains(cfg, want) {
			t.Fatalf("generated window cfg missing %q:\n%s", want, cfg)
		}
	}
}

func TestS6TraceRecordConstantNeutralDefaults(t *testing.T) {
	constant := s6TraceRecordConstant(traceRecord{Action: "checkpoint", Vars: nil})
	for _, want := range []string{`action |-> "checkpoint"`, `vm |-> ""`, `had_vm |-> FALSE`, `confirmed_by |-> ""`} {
		if !strings.Contains(constant, want) {
			t.Fatalf("record constant missing %q: %s", want, constant)
		}
	}
}
