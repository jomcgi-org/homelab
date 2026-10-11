package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func armedBrick(id string, jailed, direct, refused uint64) capacityInstance {
	admittable := int64(1216)
	return capacityInstance{
		InstanceID:        id,
		SizeClass:         "2gi",
		MemBudgetMiB:      1792,
		AdmissionReported: true,
		AdmittableMiB:     &admittable,
		AdmissionRefusals: map[string]uint64{"pressure:mem": 1},
		JailerArmed:       true,
		LaunchModes:       map[string]uint64{launchJailed: jailed, launchDirectExec: direct, launchRefused: refused},
	}
}

func report(instances ...capacityInstance) capacityReport {
	return capacityReport{Instances: instances}
}

func TestEvaluateS7(t *testing.T) {
	unarmed := armedBrick("n1/a", 9, 0, 0)
	unarmed.JailerArmed = false
	noModes := armedBrick("n1/a", 0, 0, 0)
	noModes.LaunchModes = nil
	blind := armedBrick("n1/a", 9, 0, 0)
	blind.MemBudgetMiB = 0
	blind.AdmissionReported = false
	wildcard := armedBrick("n1/a", 9, 0, 0)
	wildcard.SizeClass = ""
	wildcard.MemBudgetMiB = 0
	wildcard.AdmissionReported = false

	cases := []struct {
		name    string
		before  capacityReport
		after   capacityReport
		verdict string
		detail  string
	}{
		{
			name:    "jailed moved and nothing else",
			before:  report(armedBrick("n1/a", 1, 0, 0)),
			after:   report(armedBrick("n1/a", 6, 0, 0)),
			verdict: verdictPass,
			detail:  "jailed +5 across the suite",
		},
		{
			name:    "an unarmed brick fails at once",
			before:  report(armedBrick("n1/a", 1, 0, 0)),
			after:   report(unarmed),
			verdict: verdictFail,
			detail:  "jailer_armed=false",
		},
		{
			name:    "a direct exec during the suite fails",
			before:  report(armedBrick("n1/a", 1, 2, 0)),
			after:   report(armedBrick("n1/a", 6, 3, 0)),
			verdict: verdictFail,
			detail:  "direct_exec +1",
		},
		{
			name:    "a refused jailed launch during the suite fails",
			before:  report(armedBrick("n1/a", 1, 0, 0)),
			after:   report(armedBrick("n1/a", 6, 0, 2)),
			verdict: verdictFail,
			detail:  "refused +2",
		},
		{
			name:    "direct exec before the suite is history, not a failure",
			before:  report(armedBrick("n1/a", 1, 4, 1)),
			after:   report(armedBrick("n1/a", 3, 4, 1)),
			verdict: verdictPass,
			detail:  "jailed +2",
		},
		{
			name:   "no jailed launch yet stays pending",
			before: report(armedBrick("n1/a", 4, 0, 0)),
			after:  report(armedBrick("n1/a", 4, 0, 0)),
			detail: "no jailed launch observed",
		},
		{
			name:   "an empty fleet stays pending",
			before: report(armedBrick("n1/a", 4, 0, 0)),
			after:  report(),
			detail: "lists no bricks",
		},
		{
			name:   "a brick without launch modes stays pending",
			before: report(armedBrick("n1/a", 4, 0, 0)),
			after:  report(noModes),
			detail: "no launch_modes",
		},
		{
			name:   "a classed brick blind to its budget stays pending",
			before: report(armedBrick("n1/a", 4, 0, 0)),
			after:  report(blind),
			detail: "mem_budget_mib=0 admission_reported=false",
		},
		{
			name:    "a wildcard brick need not report a budget",
			before:  report(armedBrick("n1/a", 4, 0, 0)),
			after:   report(wildcard),
			verdict: verdictPass,
			detail:  "jailed +5",
		},
		{
			// The brick replaced mid-suite counts its preflight launch as jailed;
			// that is not evidence that S1..S5 booted through the jailer.
			name:   "a brick registered mid-suite credits no jailed launches",
			before: report(armedBrick("n1/a", 4, 0, 0)),
			after:  report(armedBrick("n2/b", 7, 0, 0)),
			detail: "since registering mid-suite",
		},
		{
			name:    "a brick registered mid-suite still fails on a direct exec",
			before:  report(armedBrick("n1/a", 4, 0, 0)),
			after:   report(armedBrick("n1/a", 5, 0, 0), armedBrick("n2/b", 1, 1, 0)),
			verdict: verdictFail,
			detail:  "brick n2/b launched a VMM without the jailer",
		},
		{
			// A daemon restart inside the same pod resets its counters; the new
			// counts are compared against zero rather than underflowing.
			name:    "a counter reset compares from zero",
			before:  report(armedBrick("n1/a", 40, 0, 0)),
			after:   report(armedBrick("n1/a", 3, 0, 0)),
			verdict: verdictPass,
			detail:  "jailed +3",
		},
		{
			name:    "a counter reset that ran a guest unjailed fails",
			before:  report(armedBrick("n1/a", 40, 0, 0)),
			after:   report(armedBrick("n1/a", 3, 1, 0)),
			verdict: verdictFail,
			detail:  "direct_exec +1",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := evaluateS7(tc.before, tc.after)
			if got.verdict != tc.verdict {
				t.Fatalf("verdict = %q, want %q; detail=%s", got.verdict, tc.verdict, got.detail)
			}
			if !strings.Contains(got.detail, tc.detail) {
				t.Fatalf("detail = %q, want it to contain %q", got.detail, tc.detail)
			}
		})
	}
}

func TestEvaluateS7PassDetailNamesAdmissionFacts(t *testing.T) {
	got := evaluateS7(report(armedBrick("n1/a", 1, 0, 0)), report(armedBrick("n1/a", 2, 0, 0)))
	for _, want := range []string{`class="2gi"`, "admittable=1216MiB", "refusals={pressure:mem=1}", "direct_exec+0", "refused+0"} {
		if !strings.Contains(got.detail, want) {
			t.Fatalf("detail = %q, want %q", got.detail, want)
		}
	}
}

// fakeCapacityServer serves /v1/capacity from reports in order, repeating the
// last one, and counts the reads.
func fakeCapacityServer(t *testing.T, reports ...capacityReport) (*controlPlaneClient, *atomic.Int32) {
	t.Helper()
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var reads atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/capacity" {
			http.NotFound(w, r)
			return
		}
		if r.Header.Get("Authorization") != "Bearer test-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		index := int(reads.Add(1)) - 1
		if index >= len(reports) {
			index = len(reports) - 1
		}
		_ = json.NewEncoder(w).Encode(reports[index])
	}))
	t.Cleanup(server.Close)
	return &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}, &reads
}

func TestRunS7PollsUntilTheJailedLaunchIsReported(t *testing.T) {
	baseline := report(armedBrick("n1/a", 2, 0, 0))
	client, reads := fakeCapacityServer(t, baseline, report(armedBrick("n1/a", 4, 0, 0)))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	got := runS7(ctx, client, jailerBaseline{report: baseline})
	if got.Verdict != verdictPass {
		t.Fatalf("S7 = %#v, want pass", got)
	}
	if n := reads.Load(); n != 2 {
		t.Fatalf("capacity reads = %d, want 2 (one pending, one pass)", n)
	}
}

func TestRunS7FailsFastOnAnUnjailedLaunch(t *testing.T) {
	baseline := report(armedBrick("n1/a", 2, 0, 0))
	client, reads := fakeCapacityServer(t, report(armedBrick("n1/a", 2, 1, 0)))
	got := runS7(context.Background(), client, jailerBaseline{report: baseline})
	if got.Verdict != verdictFail || !strings.Contains(got.Detail, "direct_exec +1") {
		t.Fatalf("S7 = %#v, want a direct_exec failure", got)
	}
	if n := reads.Load(); n != 1 {
		t.Fatalf("capacity reads = %d, want 1: a hard failure is not polled", n)
	}
}

func TestRunS7FailsWithTheLastPendingReasonAtItsBudget(t *testing.T) {
	baseline := report(armedBrick("n1/a", 2, 0, 0))
	client, _ := fakeCapacityServer(t, baseline)
	ctx, cancel := context.WithTimeout(context.Background(), 3*pollInterval/2)
	defer cancel()
	got := runS7(ctx, client, jailerBaseline{report: baseline})
	if got.Verdict != verdictFail || !strings.Contains(got.Detail, "no jailed launch observed") {
		t.Fatalf("S7 = %#v, want a failure naming the missing jailed launch", got)
	}
}

func TestRunS7FailsWithoutABaseline(t *testing.T) {
	client, reads := fakeCapacityServer(t, report(armedBrick("n1/a", 2, 0, 0)))
	got := runS7(context.Background(), client, jailerBaseline{err: errors.New("GET /v1/capacity: connection refused")})
	if got.Verdict != verdictFail || !strings.Contains(got.Detail, "baseline read before S1 failed") {
		t.Fatalf("S7 = %#v, want a baseline failure", got)
	}
	if n := reads.Load(); n != 0 {
		t.Fatalf("capacity reads = %d, want none without a baseline", n)
	}
}

func fakeTaskServer(t *testing.T, status int, guest map[string]any) (*controlPlaneClient, *atomic.Int32) {
	t.Helper()
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var posts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/workloads/sandbox-python/tasks" || r.URL.Query().Get("wait") != "true" {
			http.NotFound(w, r)
			return
		}
		if r.Header.Get("Authorization") != "Bearer test-token" || !strings.HasPrefix(r.Header.Get("Idempotency-Key"), "1.2.3-egress-probe-") {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		var request struct {
			Code string `json:"code"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil || request.Code != s8EgressProbeSource {
			http.Error(w, "wrong probe source", http.StatusBadRequest)
			return
		}
		posts.Add(1)
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(guest)
	}))
	t.Cleanup(server.Close)
	return &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}, &posts
}

func TestS8ProbeDialsTheEgressPortForTheCA(t *testing.T) {
	for _, want := range []string{"socket.AF_VSOCK", "s.connect((2, 1025))", `b"ca.egress.internal:80\n"`, "-----BEGIN CERTIFICATE-----", `print("egress-ca:%s" % ("yes"`} {
		if !strings.Contains(s8EgressProbeSource, want) {
			t.Fatalf("probe source lacks %q:\n%s", want, s8EgressProbeSource)
		}
	}
}

func TestRunS8(t *testing.T) {
	cases := []struct {
		name    string
		status  int
		guest   map[string]any
		verdict string
		detail  string
	}{
		{
			name:    "sidecar answered",
			status:  http.StatusOK,
			guest:   map[string]any{"exit_code": 0, "stdout": "egress-bytes:1188\negress-ca:yes\n"},
			verdict: verdictPass,
			detail:  "egress-bytes:1188",
		},
		{
			// A sidecar with no CA loaded, or one that never answered, closes
			// without writing.
			name:    "connected but nothing came back",
			status:  http.StatusOK,
			guest:   map[string]any{"exit_code": 0, "stdout": "egress-bytes:0\negress-ca:no\n"},
			verdict: verdictFail,
			detail:  "egress-ca:no",
		},
		{
			// The jailed Firecracker could not connect to its egress socket.
			name:    "guest connect failed",
			status:  http.StatusOK,
			guest:   map[string]any{"exit_code": 1, "stdout": "", "stderr": "ConnectionResetError: [Errno 104] Connection reset by peer"},
			verdict: verdictFail,
			detail:  "Connection reset by peer",
		},
		{
			name:    "task API failure",
			status:  http.StatusBadGateway,
			guest:   map[string]any{"error": "restore failed"},
			verdict: verdictFail,
			detail:  "status=502",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			client, posts := fakeTaskServer(t, tc.status, tc.guest)
			cfg := config{chartVersion: "1.2.3", taskWorkload: "sandbox-python"}
			got := runS8(context.Background(), cfg, client, time.Unix(1, 0))
			if got.Verdict != tc.verdict || !strings.Contains(got.Detail, tc.detail) {
				t.Fatalf("S8 = %#v, want %s containing %q", got, tc.verdict, tc.detail)
			}
			if n := posts.Load(); n != 1 {
				t.Fatalf("task posts = %d, want 1", n)
			}
		})
	}
}

func TestJailerScenariosRunOnlyWhenEnabledAndS7Last(t *testing.T) {
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var order []string
	requests := func() []string {
		mu.Lock()
		defer mu.Unlock()
		return append([]string(nil), order...)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		order = append(order, r.Method+" "+r.URL.Path)
		jailed := uint64(len(order))
		mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/capacity":
			_ = json.NewEncoder(w).Encode(report(armedBrick("n1/a", jailed, 0, 0)))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/workloads/sandbox-python/tasks":
			_ = json.NewEncoder(w).Encode(map[string]any{"exit_code": 0, "stdout": "egress-ca:yes\n"})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	budgets := map[string]time.Duration{"S7": 5 * time.Second, "S8": 5 * time.Second}

	off := config{chartVersion: "1.2.3", taskWorkload: "sandbox-python", budgets: budgets}
	if got := runJailerScenarios(context.Background(), off, client, time.Unix(1, 0), readJailerBaseline(context.Background(), off, client)); len(got) != 0 || len(requests()) != 0 {
		t.Fatalf("disabled tail ran %#v with requests %q, want nothing", got, requests())
	}

	on := off
	on.requireJailer = true
	on.egressProbe = true
	baseline := readJailerBaseline(context.Background(), on, client)
	got := runJailerScenarios(context.Background(), on, client, time.Unix(1, 0), baseline)
	ids := make([]string, 0, len(got))
	for _, scenario := range got {
		ids = append(ids, fmt.Sprintf("%s=%s", scenario.ID, scenario.Verdict))
	}
	if strings.Join(ids, ",") != "S8=pass,S7=pass" {
		t.Fatalf("tail = %v, want S8 then S7, both passing; requests %q", ids, requests())
	}
	want := []string{"GET /v1/capacity", "POST /v1/workloads/sandbox-python/tasks", "GET /v1/capacity"}
	if got := requests(); strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("requests = %q, want %q: the baseline first, S7's read last", got, want)
	}
}

func TestLoadConfigReadsJailerScenarioGates(t *testing.T) {
	t.Setenv("EMBERVM_URL", "http://embervm.test")
	t.Setenv("CHART_VERSION", "1.2.3")
	cfg, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.requireJailer || cfg.egressProbe {
		t.Fatalf("gates default on: requireJailer=%t egressProbe=%t", cfg.requireJailer, cfg.egressProbe)
	}
	if cfg.budgets["S7"] != 15*time.Second || cfg.budgets["S8"] != 20*time.Second {
		t.Fatalf("budgets = %v, want S7 15s and S8 20s by default", cfg.budgets)
	}

	t.Setenv("S7_REQUIRE_JAILER", "true")
	t.Setenv("S8_EGRESS_PROBE", "true")
	t.Setenv("S7_BUDGET", "45s")
	if cfg, err = loadConfig(); err != nil {
		t.Fatal(err)
	}
	if !cfg.requireJailer || !cfg.egressProbe || cfg.budgets["S7"] != 45*time.Second {
		t.Fatalf("cfg = requireJailer %t egressProbe %t S7 %s, want both gates on and S7 45s", cfg.requireJailer, cfg.egressProbe, cfg.budgets["S7"])
	}
}
