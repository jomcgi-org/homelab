package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// S7 (jailer and admission facts, #5255) and S8 (jailed egress lane) are
// staged default-off like S6. S7 runs only with S7_REQUIRE_JAILER=true and S8
// only with S8_EGRESS_PROBE=true; with both off the suite is unchanged.
//
// S7 reads GET /v1/capacity before S1 and again after every other scenario,
// and passes only when every brick reports jailer_armed, no brick's
// launch_modes.direct_exec or launch_modes.refused moved across the suite, and
// launch_modes.jailed moved on a brick whose counters ran unbroken from suite
// start (the guests S1 to S5 booted went through the jailer). A brick that
// registers or restarts mid-suite counts its startup preflight as a jailed
// launch and has no counts from before, so it credits nothing. S7 fails at once
// when every brick from suite start is listed with its counters reset. When no
// listed brick kept its counters but one from suite start is missing from the
// read, S7 keeps polling inside its budget: the registry drops a brick whose
// stream is unknown or draining and restores it with its counters. A classed
// brick must also report a nonzero budget and its own admission predicate,
// which is what a jailer cgroup delegation that hid the brick's memory.max
// would break.
//
// S8 runs one task on the task workload that dials the host's egress vsock
// port itself and asks the egress sidecar for its CA certificate. A PEM back
// proves the jailed Firecracker could connect to its egress socket inside the
// jail and that the sidecar answered.
const (
	s7ScenarioID = "S7"
	s8ScenarioID = "S8"
)

// Launch-mode keys of NodeStatus.launch_modes, as /v1/capacity reports them.
const (
	launchJailed     = "jailed"
	launchDirectExec = "direct_exec"
	launchRefused    = "refused"
)

type capacityReport struct {
	Instances []capacityInstance `json:"instances"`
}

// capacityInstance is the subset of a /v1/capacity instance row S7 reads
// (control/lib/embervm/capacity_report.ex instance_row).
type capacityInstance struct {
	InstanceID        string            `json:"instance_id"`
	SizeClass         string            `json:"size_class"`
	MemBudgetMiB      int64             `json:"mem_budget_mib"`
	AdmissionReported bool              `json:"admission_reported"`
	AdmittableMiB     *int64            `json:"admittable_mib"`
	AdmissionRefusals map[string]uint64 `json:"admission_refusals"`
	JailerArmed       bool              `json:"jailer_armed"`
	LaunchModes       map[string]uint64 `json:"launch_modes"`
}

// jailerBaseline is the /v1/capacity read taken before S1. err keeps a failed
// read so S7 reports it instead of comparing against an empty fleet.
type jailerBaseline struct {
	report capacityReport
	err    error
}

func getCapacity(ctx context.Context, client *controlPlaneClient) (capacityReport, error) {
	response, err := client.request(ctx, http.MethodGet, "/v1/capacity", nil, "", nil)
	if err != nil {
		return capacityReport{}, fmt.Errorf("GET /v1/capacity: %w", err)
	}
	if response.status != http.StatusOK {
		return capacityReport{}, fmt.Errorf("%s", httpErrorDetail(http.MethodGet, "/v1/capacity", response))
	}
	var report capacityReport
	if err := json.Unmarshal(response.body, &report); err != nil {
		return capacityReport{}, fmt.Errorf("GET /v1/capacity invalid response: %w", err)
	}
	return report, nil
}

// readJailerBaseline reads the pre-suite launch counters, or nothing when S7
// is off.
func readJailerBaseline(ctx context.Context, cfg config, client *controlPlaneClient) jailerBaseline {
	if !cfg.requireJailer {
		return jailerBaseline{}
	}
	report, err := getCapacity(ctx, client)
	return jailerBaseline{report: report, err: err}
}

// runJailerScenarios runs the gated tail of the suite: S8 first, so S7's final
// read also covers the launch S8's task causes, then S7.
func runJailerScenarios(ctx context.Context, cfg config, client *controlPlaneClient, suiteStarted time.Time, baseline jailerBaseline) []scenarioVerdict {
	var scenarios []scenarioVerdict
	if cfg.egressProbe {
		s8 := runBudgeted(ctx, s8ScenarioID, cfg.budgets["S8"], func(scenarioCtx context.Context) scenarioVerdict {
			return runS8(scenarioCtx, cfg, client, suiteStarted)
		})
		logScenario(s8)
		scenarios = append(scenarios, s8)
	}
	if cfg.requireJailer {
		s7 := runBudgeted(ctx, s7ScenarioID, cfg.budgets["S7"], func(scenarioCtx context.Context) scenarioVerdict {
			return runS7(scenarioCtx, client, baseline)
		})
		logScenario(s7)
		scenarios = append(scenarios, s7)
	}
	return scenarios
}

// runS7 polls /v1/capacity until the facts pass or a hard failure shows. The
// control plane learns launch counters from each brick's next NodeStatus, so a
// launch at the end of S5 can take a report interval to appear; only the
// "not yet" outcomes are polled, never a failure.
func runS7(ctx context.Context, client *controlPlaneClient, baseline jailerBaseline) scenarioVerdict {
	if baseline.err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "baseline read before S1 failed: " + baseline.err.Error()}
	}
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	last := "no capacity report read"
	for {
		report, err := getCapacity(ctx, client)
		if err != nil {
			last = err.Error()
		} else {
			evaluation := evaluateS7(baseline.report, report)
			if evaluation.verdict != "" {
				return scenarioVerdict{Verdict: evaluation.verdict, Detail: evaluation.detail}
			}
			last = evaluation.detail
		}
		select {
		case <-ctx.Done():
			return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("%s: %v", last, ctx.Err())}
		case <-ticker.C:
		}
	}
}

// s7Evaluation is one comparison. An empty verdict means not settled yet.
type s7Evaluation struct {
	verdict string
	detail  string
}

func evaluateS7(before, after capacityReport) s7Evaluation {
	if len(after.Instances) == 0 {
		return s7Evaluation{detail: "GET /v1/capacity lists no bricks"}
	}
	baseline := make(map[string]map[string]uint64, len(before.Instances))
	for _, instance := range before.Instances {
		baseline[instance.InstanceID] = instance.LaunchModes
	}
	instances := append([]capacityInstance(nil), after.Instances...)
	sort.Slice(instances, func(i, j int) bool { return instances[i].InstanceID < instances[j].InstanceID })

	var failures, pending, summaries []string
	var jailedDelta uint64
	// unbroken counts the bricks whose counters ran from suite start to now
	// without a reset: the only bricks whose jailed launches are the suite's.
	unbroken := 0
	listed := make(map[string]bool, len(instances))
	for _, instance := range instances {
		id := instance.InstanceID
		listed[id] = true
		if !instance.JailerArmed {
			failures = append(failures, fmt.Sprintf("brick %s reports jailer_armed=false", id))
			continue
		}
		if instance.LaunchModes == nil {
			pending = append(pending, fmt.Sprintf("brick %s reports no launch_modes", id))
			continue
		}
		base, present := baseline[id]
		// A counter below its baseline means the daemon restarted inside the same
		// pod and counts from zero again. Its new counts still fail S7 on a
		// direct exec or a refusal, but the launches before the restart are gone,
		// and its first jailed launch is the restart preflight, which boots no
		// guest, so it credits nothing to S1 to S5.
		restarted := present && counterReset(base, instance.LaunchModes)
		if restarted {
			base = nil
		}
		survived := present && !restarted
		direct := counterDelta(base, instance.LaunchModes, launchDirectExec)
		refused := counterDelta(base, instance.LaunchModes, launchRefused)
		if direct > 0 {
			failures = append(failures, fmt.Sprintf("brick %s launched a VMM without the jailer during the suite (direct_exec +%d)", id, direct))
		}
		if refused > 0 {
			failures = append(failures, fmt.Sprintf("brick %s failed a jailed launch closed during the suite (refused +%d)", id, refused))
		}
		jailed := counterDelta(base, instance.LaunchModes, launchJailed)
		// A brick that registered or restarted mid-suite counts its startup
		// preflight as a jailed launch, so only bricks whose counters ran
		// unbroken from suite start credit S1 to S5.
		if survived {
			jailedDelta += jailed
			unbroken++
		}
		if instance.SizeClass != "" && (instance.MemBudgetMiB <= 0 || !instance.AdmissionReported) {
			pending = append(pending, fmt.Sprintf("brick %s (class %s) reports mem_budget_mib=%d admission_reported=%t", id, instance.SizeClass, instance.MemBudgetMiB, instance.AdmissionReported))
		}
		summaries = append(summaries, instanceSummary(instance, jailed, direct, refused, present, restarted))
	}
	if len(failures) > 0 {
		return s7Evaluation{verdict: verdictFail, detail: strings.Join(failures, "; ")}
	}
	if len(pending) > 0 {
		return s7Evaluation{detail: strings.Join(pending, "; ")}
	}
	if unbroken == 0 {
		// A baseline brick absent from this read may only be out of the
		// registry for now: NodeRegistry drops a brick's row while its stream
		// is unknown or draining and restores it, same instance id and counters
		// intact, on its next healthy status. Wait for it, and let the S7
		// budget bound a fleet that really was replaced.
		var missing []string
		for id := range baseline {
			if !listed[id] {
				missing = append(missing, id)
			}
		}
		if len(missing) > 0 {
			sort.Strings(missing)
			return s7Evaluation{detail: fmt.Sprintf("baseline bricks missing from GET /v1/capacity: %s (a brick drops out while its stream is unknown or draining and returns with its counters); no listed brick kept its launch counters across the suite; %s", strings.Join(missing, ", "), strings.Join(summaries, "; "))}
		}
		// Every baseline brick is listed and reset its counters. No later read
		// can bring back a reset brick's lost counts, so the suite's launches
		// are unobservable: fail rather than wait out the budget or pass on
		// preflight launches alone.
		return s7Evaluation{verdict: verdictFail, detail: "no brick kept its launch counters across the suite (every brick registered or restarted mid-suite), so the suite's launches are unobservable; " + strings.Join(summaries, "; ")}
	}
	if jailedDelta == 0 {
		return s7Evaluation{detail: "no jailed launch observed across the suite on a brick whose counters ran from its start; " + strings.Join(summaries, "; ")}
	}
	return s7Evaluation{verdict: verdictPass, detail: fmt.Sprintf("every brick armed; jailed +%d across the suite; %s", jailedDelta, strings.Join(summaries, "; "))}
}

func counterReset(base, current map[string]uint64) bool {
	for key, value := range base {
		if current[key] < value {
			return true
		}
	}
	return false
}

func counterDelta(base, current map[string]uint64, key string) uint64 {
	return current[key] - base[key]
}

func instanceSummary(instance capacityInstance, jailed, direct, refused uint64, present, restarted bool) string {
	admittable := "unreported"
	if instance.AdmittableMiB != nil {
		admittable = fmt.Sprintf("%dMiB", *instance.AdmittableMiB)
	}
	since := "since suite start"
	switch {
	case !present:
		since = "since registering mid-suite"
	case restarted:
		since = "since restarting mid-suite"
	}
	return fmt.Sprintf("%s class=%q jailed+%d direct_exec+%d refused+%d %s admittable=%s refusals=%s",
		instance.InstanceID, instance.SizeClass, jailed, direct, refused, since, admittable, formatCounts(instance.AdmissionRefusals))
}

func formatCounts(counts map[string]uint64) string {
	if len(counts) == 0 {
		return "{}"
	}
	keys := make([]string, 0, len(counts))
	for key := range counts {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, fmt.Sprintf("%s=%d", key, counts[key]))
	}
	return "{" + strings.Join(parts, ",") + "}"
}

// s8EgressProbeSource dials the host's egress vsock port (CID 2, port 1025) and
// speaks the sidecar's one-line preamble for its reserved CA host, the same
// exchange the runtime shim's fetch_egress_ca makes before a turn. The sidecar
// writes the CA's public PEM and closes; with no CA loaded it closes without
// writing, so only a sidecar answer prints egress-ca:yes.
const s8EgressProbeSource = `import socket
s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
s.settimeout(10)
s.connect((2, 1025))
s.sendall(b"ca.egress.internal:80\n")
data = b""
while len(data) < 65536:
    chunk = s.recv(4096)
    if not chunk:
        break
    data += chunk
s.close()
print("egress-bytes:%d" % len(data))
print("egress-ca:%s" % ("yes" if b"-----BEGIN CERTIFICATE-----" in data else "no"))
`

const s8EgressAnswered = "egress-ca:yes"

func runS8(ctx context.Context, cfg config, client *controlPlaneClient, suiteStarted time.Time) scenarioVerdict {
	path := "/v1/workloads/" + url.PathEscape(cfg.taskWorkload) + "/tasks?wait=true"
	body, err := json.Marshal(map[string]any{"code": s8EgressProbeSource})
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "encode egress probe: " + err.Error()}
	}
	response, err := client.request(ctx, http.MethodPost, path, body, "", map[string]string{
		"Idempotency-Key": cfg.chartVersion + "-egress-probe-" + suiteStarted.UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("POST %s: %v", path, err)}
	}
	if response.status < 200 || response.status >= 300 {
		return scenarioVerdict{Verdict: verdictFail, Detail: httpErrorDetail(http.MethodPost, path, response)}
	}
	var guest struct {
		ExitCode int    `json:"exit_code"`
		Stdout   string `json:"stdout"`
		Stderr   string `json:"stderr"`
		Error    string `json:"error"`
	}
	if err := json.Unmarshal(response.body, &guest); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("POST %s invalid guest response: %v", path, err)}
	}
	if guest.ExitCode != 0 || !strings.Contains(guest.Stdout, s8EgressAnswered) {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf(
			"guest egress probe did not get the sidecar's CA: exit_code=%d stdout=%q stderr=%q error=%q",
			guest.ExitCode,
			truncate(guest.Stdout, maxErrorBody),
			truncate(guest.Stderr, maxErrorBody),
			truncate(guest.Error, maxErrorBody),
		)}
	}
	return scenarioVerdict{Verdict: verdictPass, Detail: fmt.Sprintf("task guest dialed vsock 1025 and the egress sidecar answered with its CA; %s", strings.TrimSpace(truncate(guest.Stdout, maxErrorBody)))}
}
