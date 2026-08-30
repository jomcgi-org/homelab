package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"
)

const (
	maxErrorBody = 200
	pollInterval = time.Second
)

// These are the runnable and banked states in
// projects/embervm/control/lib/embervm/session_state.ex and session_manager.ex.
var (
	liveSessionStates   = map[string]bool{"live": true, "running": true}
	bankedSessionStates = map[string]bool{"banked": true, "idle_banked": true, "parked": true}
	terminalStates      = map[string]bool{"destroyed": true, "expired": true, "evicted": true, "failed": true}
)

type config struct {
	baseURL              string
	tokenFile            string
	chartVersion         string
	listenAddr           string
	runInterval          time.Duration
	readyWait            time.Duration
	taskWorkload         string
	sessionWorkload      string
	idleBankSeconds      int
	sweepGraceSeconds    int
	minPassingInvariants int
	budgets              map[string]time.Duration
}

type controlPlaneClient struct {
	baseURL   string
	tokenFile string
	http      *http.Client
}

type apiResponse struct {
	status int
	body   []byte
}

type nodesView struct {
	Nodes []nodeView `json:"nodes"`
}

type nodeView struct {
	Dispatchable bool       `json:"dispatchable"`
	Draining     bool       `json:"draining"`
	Facts        *nodeFacts `json:"facts"`
}

type nodeFacts struct {
	LiveVMs        *int                    `json:"live_vms"`
	MemHeadroomMiB *int                    `json:"mem_headroom_mib"`
	Workloads      map[string]workloadView `json:"workloads"`
}

type workloadView struct {
	BaseState   string `json:"base_state"`
	SnapshotRef string `json:"snapshot_ref"`
}

type sessionIdentity struct {
	ID    string
	Token string
}

type s2Result struct {
	verdict   scenarioVerdict
	liveDelay time.Duration
}

func (c *controlPlaneClient) request(ctx context.Context, method, path string, body []byte, tokenOverride string, headers map[string]string) (apiResponse, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return apiResponse{}, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	token := tokenOverride
	if token == "" {
		raw, readErr := os.ReadFile(c.tokenFile)
		if readErr != nil {
			return apiResponse{}, fmt.Errorf("read bearer token: %w", readErr)
		}
		token = strings.TrimSpace(string(raw))
	}
	if token == "" {
		return apiResponse{}, fmt.Errorf("bearer token is empty")
	}
	req.Header.Set("Authorization", "Bearer "+token)
	for key, value := range headers {
		req.Header.Set(key, value)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return apiResponse{}, err
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return apiResponse{}, err
	}
	return apiResponse{status: resp.StatusCode, body: responseBody}, nil
}

func httpErrorDetail(method, path string, response apiResponse) string {
	prefix := response.body
	if len(prefix) > maxErrorBody {
		prefix = prefix[:maxErrorBody]
	}
	return fmt.Sprintf("%s %s status=%d body=%q", method, path, response.status, string(prefix))
}

func waitForReady(ctx context.Context, client *controlPlaneClient, workloads ...string) error {
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	unready := append([]string(nil), workloads...)
	for {
		healthRequest, requestErr := http.NewRequestWithContext(ctx, http.MethodGet, client.baseURL+"/healthz", nil)
		var health *http.Response
		var healthErr error
		if requestErr == nil {
			health, healthErr = client.http.Do(healthRequest)
		} else {
			healthErr = requestErr
		}
		healthOK := healthErr == nil && health.StatusCode == http.StatusOK
		if healthErr == nil {
			health.Body.Close()
		}
		if healthOK {
			view, err := getNodes(ctx, client)
			if err == nil {
				unready = unreadyWorkloads(view, workloads)
				if len(unready) == 0 {
					return nil
				}
			}
		}

		select {
		case <-ctx.Done():
			return fmt.Errorf("workloads not ready: %s: %w", strings.Join(unready, ", "), ctx.Err())
		case <-ticker.C:
		}
	}
}

func unreadyWorkloads(view nodesView, workloads []string) []string {
	unready := make([]string, 0, len(workloads))
	for _, workloadName := range workloads {
		ready := false
		for _, node := range view.Nodes {
			if node.Facts == nil {
				continue
			}
			workload, ok := node.Facts.Workloads[workloadName]
			if node.Dispatchable && !node.Draining && ok && workload.BaseState == "BASE_BUILD_STATE_READY" && workload.SnapshotRef != "" {
				ready = true
				break
			}
		}
		if !ready {
			unready = append(unready, workloadName)
		}
	}
	return unready
}

func runScenarios(ctx context.Context, cfg config, client *controlPlaneClient, started time.Time) []scenarioVerdict {
	s1 := runBudgeted(ctx, "S1", cfg.budgets["S1"], func(scenarioCtx context.Context) scenarioVerdict {
		return runS1(scenarioCtx, cfg, client, started)
	})
	logScenario(s1)

	s2Holder := s2Result{}
	s2 := runBudgeted(ctx, "S2", cfg.budgets["S2"], func(scenarioCtx context.Context) scenarioVerdict {
		s2Holder = runS2(scenarioCtx, cfg, client)
		return s2Holder.verdict
	})
	logScenario(s2)

	var s3 scenarioVerdict
	if s2.Verdict != verdictPass {
		s3 = scenarioVerdict{ID: "S3", Verdict: verdictVacuous, Detail: "S2 did not establish a baseline"}
	} else {
		s3 = runBudgeted(ctx, "S3", cfg.budgets["S3"], func(scenarioCtx context.Context) scenarioVerdict {
			return runS3(scenarioCtx, cfg, client, s2Holder.liveDelay)
		})
	}
	logScenario(s3)

	s4 := runBudgeted(ctx, "S4", cfg.budgets["S4"], func(scenarioCtx context.Context) scenarioVerdict {
		return runS4(scenarioCtx, cfg, client, started)
	})
	logScenario(s4)
	return []scenarioVerdict{s1, s2, s3, s4}
}

func runBudgeted(parent context.Context, id string, budget time.Duration, run func(context.Context) scenarioVerdict) scenarioVerdict {
	started := time.Now()
	ctx, cancel := context.WithTimeout(parent, budget)
	defer cancel()
	result := run(ctx)
	result.ID = id
	result.MS = time.Since(started).Milliseconds()
	if ctx.Err() != nil && result.Verdict == "" {
		result.Verdict = verdictFail
		result.Detail = fmt.Sprintf("budget %s exceeded", budget)
	}
	if id == "S1" {
		result.Detail = fmt.Sprintf("%s; duration=%s", result.Detail, time.Since(started).Round(time.Millisecond))
	}
	return result
}

func logScenario(result scenarioVerdict) {
	slog.Info("conformance scenario completed", "id", result.ID, "verdict", result.Verdict, "detail", result.Detail, "ms", result.MS)
}

func runS1(ctx context.Context, cfg config, client *controlPlaneClient, suiteStarted time.Time) scenarioVerdict {
	path := "/v1/workloads/" + url.PathEscape(cfg.taskWorkload) + "/tasks?wait=true"
	body := []byte("{\"code\":\"print(\\\"conformance ok\\\")\"}")
	response, err := client.request(ctx, http.MethodPost, path, body, "", map[string]string{
		"Idempotency-Key": cfg.chartVersion + "-" + suiteStarted.UTC().Format(time.RFC3339),
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
	}
	if err := json.Unmarshal(response.body, &guest); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("POST %s invalid guest response: %v", path, err)}
	}
	if guest.ExitCode != 0 || !strings.Contains(guest.Stdout, "conformance ok") {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("guest exit_code=%d stdout=%q", guest.ExitCode, truncate(guest.Stdout, maxErrorBody))}
	}
	reapDelay, finalLiveVMs, err := waitForWorkloadVMsZero(ctx, client, cfg.taskWorkload)
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("task guest was not reaped; final live VM count=%d: %v", finalLiveVMs, err)}
	}
	return scenarioVerdict{Verdict: verdictPass, Detail: fmt.Sprintf("guest exited 0 and printed conformance ok; VM reap observed in %s", reapDelay.Round(time.Millisecond))}
}

func createSession(ctx context.Context, cfg config, client *controlPlaneClient) (sessionIdentity, string, error) {
	path := "/v1/workloads/" + url.PathEscape(cfg.sessionWorkload) + "/sessions"
	response, err := client.request(ctx, http.MethodPost, path, []byte(`{}`), "", nil)
	if err != nil {
		return sessionIdentity{}, "", fmt.Errorf("POST %s: %w", path, err)
	}
	if response.status != http.StatusCreated {
		return sessionIdentity{}, "", fmt.Errorf("%s", httpErrorDetail(http.MethodPost, path, response))
	}
	var created struct {
		SessionID    string `json:"session_id"`
		SessionToken string `json:"session_token"`
		State        string `json:"state"`
	}
	if err := json.Unmarshal(response.body, &created); err != nil {
		return sessionIdentity{}, "", fmt.Errorf("POST %s invalid response: %w", path, err)
	}
	if created.SessionID == "" || created.SessionToken == "" {
		return sessionIdentity{}, "", fmt.Errorf("POST %s response missing session_id or session_token", path)
	}
	return sessionIdentity{ID: created.SessionID, Token: created.SessionToken}, created.State, nil
}

func getSessionState(ctx context.Context, client *controlPlaneClient, session sessionIdentity) (string, int, error) {
	path := "/v1/sessions/" + url.PathEscape(session.ID)
	response, err := client.request(ctx, http.MethodGet, path, nil, session.Token, nil)
	if err != nil {
		return "", 0, err
	}
	if response.status != http.StatusOK {
		return "", response.status, fmt.Errorf("%s", httpErrorDetail(http.MethodGet, path, response))
	}
	var view struct {
		State string `json:"state"`
	}
	if err := json.Unmarshal(response.body, &view); err != nil {
		return "", response.status, err
	}
	return view.State, response.status, nil
}

func waitForSessionState(ctx context.Context, client *controlPlaneClient, session sessionIdentity, desired map[string]bool) (string, error) {
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		state, _, err := getSessionState(ctx, client, session)
		if err != nil {
			return "", err
		}
		if desired[state] {
			return state, nil
		}
		if terminalStates[state] {
			return "", fmt.Errorf("session entered terminal state %q", state)
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-ticker.C:
		}
	}
}

func runS2(ctx context.Context, cfg config, client *controlPlaneClient) s2Result {
	createStarted := time.Now()
	session, initialState, err := createSession(ctx, cfg, client)
	if err != nil {
		return s2Result{verdict: scenarioVerdict{Verdict: verdictFail, Detail: err.Error()}}
	}
	cleanupNeeded := true
	defer func() {
		if cleanupNeeded {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = destroySession(cleanupCtx, client, session)
		}
	}()

	if !liveSessionStates[initialState] {
		if _, err := waitForSessionState(ctx, client, session, liveSessionStates); err != nil {
			return s2Result{verdict: scenarioVerdict{Verdict: verdictFail, Detail: "session never reached live: " + err.Error()}}
		}
	}
	liveDelay := time.Since(createStarted)

	idleWait := time.Duration(cfg.idleBankSeconds+cfg.sweepGraceSeconds) * time.Second
	if err := waitContext(ctx, idleWait); err != nil {
		return s2Result{verdict: scenarioVerdict{Verdict: verdictFail, Detail: "idle bank wait interrupted: " + err.Error()}, liveDelay: liveDelay}
	}
	if _, err := waitForSessionState(ctx, client, session, bankedSessionStates); err != nil {
		return s2Result{verdict: scenarioVerdict{Verdict: verdictVacuous, Detail: "bank never observed: " + err.Error()}, liveDelay: liveDelay}
	}

	invokePath := "/v1/sessions/" + url.PathEscape(session.ID) + "/invoke"
	invokeBody := []byte("{\"message\":\"Reply with ok.\",\"session_id\":null,\"thinking\":\"off\"}")
	response, err := client.request(ctx, http.MethodPost, invokePath, invokeBody, session.Token, map[string]string{"X-Ember-Guest-Path": "/shim/turn"})
	if err != nil {
		return s2Result{verdict: scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("POST %s: %v", invokePath, err)}, liveDelay: liveDelay}
	}
	classification := classifyInvokeResponse(response.status, response.body)
	if response.status < 200 || response.status >= 300 {
		classification.detail += "; " + httpErrorDetail(http.MethodPost, invokePath, response)
	}
	if !classification.pass {
		return s2Result{verdict: scenarioVerdict{Verdict: verdictFail, Detail: classification.detail}, liveDelay: liveDelay}
	}

	if err := destroySession(ctx, client, session); err != nil {
		return s2Result{verdict: scenarioVerdict{Verdict: verdictFail, Detail: err.Error()}, liveDelay: liveDelay}
	}
	cleanupNeeded = false
	if err := waitForSessionGone(ctx, client, session); err != nil {
		return s2Result{verdict: scenarioVerdict{Verdict: verdictFail, Detail: err.Error()}, liveDelay: liveDelay}
	}
	return s2Result{verdict: scenarioVerdict{Verdict: verdictPass, Detail: fmt.Sprintf("live in %s; bank observed; %s; destroyed", liveDelay.Round(time.Millisecond), classification.detail)}, liveDelay: liveDelay}
}

type invokeClassification struct {
	pass   bool
	detail string
}

func classifyInvokeResponse(status int, body []byte) invokeClassification {
	if status >= 200 && status < 300 {
		return invokeClassification{pass: true, detail: fmt.Sprintf("relight completed; guest answered with status=%d", status)}
	}
	text := strings.ToLower(string(body))
	sessionFailures := []string{"session invoke failed", "session not ready", "session gone", "relight", "wake-rate", "invalid session token"}
	for _, marker := range sessionFailures {
		if strings.Contains(text, marker) {
			return invokeClassification{detail: fmt.Sprintf("session/relight failure: status=%d body=%q", status, truncate(string(body), maxErrorBody))}
		}
	}
	modelFailures := []string{"egress", "model unreachable", "network unreachable", "enetunreach", "econnrefused", "failed to connect", "connection refused", "fetch failed", "api error", "model provider", "pi turn produced no output"}
	for _, marker := range modelFailures {
		if strings.Contains(text, marker) {
			return invokeClassification{pass: true, detail: fmt.Sprintf("relight completed; model unreachable as expected (%s)", marker)}
		}
	}
	return invokeClassification{detail: fmt.Sprintf("invoke outcome was not an expected model-unreachable response: status=%d body=%q", status, truncate(string(body), maxErrorBody))}
}

func destroySession(ctx context.Context, client *controlPlaneClient, session sessionIdentity) error {
	path := "/v1/sessions/" + url.PathEscape(session.ID)
	response, err := client.request(ctx, http.MethodDelete, path, nil, "", nil)
	if err != nil {
		return fmt.Errorf("DELETE %s: %w", path, err)
	}
	if response.status != http.StatusOK && response.status != http.StatusAccepted {
		return fmt.Errorf("%s", httpErrorDetail(http.MethodDelete, path, response))
	}
	return nil
}

func waitForSessionGone(ctx context.Context, client *controlPlaneClient, session sessionIdentity) error {
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	path := "/v1/sessions/" + url.PathEscape(session.ID)
	for {
		response, err := client.request(ctx, http.MethodGet, path, nil, "", nil)
		if err != nil {
			return fmt.Errorf("GET %s: %w", path, err)
		}
		if response.status == http.StatusNotFound {
			return nil
		}
		if response.status != http.StatusOK {
			return fmt.Errorf("%s", httpErrorDetail(http.MethodGet, path, response))
		}
		var view struct {
			State string `json:"state"`
		}
		if json.Unmarshal(response.body, &view) == nil && terminalStates[view.State] {
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("session did not become terminal: %w", ctx.Err())
		case <-ticker.C:
		}
	}
}

func getNodes(ctx context.Context, client *controlPlaneClient) (nodesView, error) {
	response, err := client.request(ctx, http.MethodGet, "/v1/nodes", nil, "", nil)
	if err != nil {
		return nodesView{}, fmt.Errorf("GET /v1/nodes: %w", err)
	}
	if response.status != http.StatusOK {
		return nodesView{}, fmt.Errorf("%s", httpErrorDetail(http.MethodGet, "/v1/nodes", response))
	}
	var view nodesView
	if err := json.Unmarshal(response.body, &view); err != nil {
		return nodesView{}, fmt.Errorf("GET /v1/nodes invalid response: %w", err)
	}
	return view, nil
}

func workloadLiveVMCount(view nodesView, workload string) (int, bool) {
	// The router exposes live_vms at node scope, not inside each workload fact.
	// Limit the aggregate to nodes that advertise the requested workload.
	total := 0
	observed := false
	for _, node := range view.Nodes {
		if node.Facts == nil || node.Facts.LiveVMs == nil {
			continue
		}
		if _, ok := node.Facts.Workloads[workload]; !ok {
			continue
		}
		observed = true
		total += *node.Facts.LiveVMs
	}
	return total, observed
}

func workloadHeadroomMiB(view nodesView, workload string) (int, bool) {
	total := 0
	observed := false
	for _, node := range view.Nodes {
		if node.Facts == nil || node.Facts.MemHeadroomMiB == nil {
			continue
		}
		if _, ok := node.Facts.Workloads[workload]; !ok {
			continue
		}
		observed = true
		total += *node.Facts.MemHeadroomMiB
	}
	return total, observed
}

func waitForWorkloadVMsZero(ctx context.Context, client *controlPlaneClient, workload string) (time.Duration, int, error) {
	started := time.Now()
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	lastLiveVMs := -1
	for {
		if view, err := getNodes(ctx, client); err == nil {
			if liveVMs, observed := workloadLiveVMCount(view, workload); observed {
				lastLiveVMs = liveVMs
				if liveVMs == 0 {
					return time.Since(started), liveVMs, nil
				}
			}
		}
		select {
		case <-ctx.Done():
			return time.Since(started), lastLiveVMs, ctx.Err()
		case <-ticker.C:
		}
	}
}

func waitForWorkloadCapacityRecovery(ctx context.Context, client *controlPlaneClient, workload string) (time.Duration, int, error) {
	started := time.Now()
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	initialLiveVMs := -1
	initialHeadroomMiB := -1
	lastLiveVMs := -1
	for {
		if view, err := getNodes(ctx, client); err == nil {
			if liveVMs, observed := workloadLiveVMCount(view, workload); observed {
				lastLiveVMs = liveVMs
				if initialLiveVMs < 0 {
					initialLiveVMs = liveVMs
				}
				if liveVMs == 0 || liveVMs < initialLiveVMs {
					return time.Since(started), liveVMs, nil
				}
			}
			if headroomMiB, observed := workloadHeadroomMiB(view, workload); observed {
				if initialHeadroomMiB < 0 {
					initialHeadroomMiB = headroomMiB
				} else if headroomMiB > initialHeadroomMiB {
					return time.Since(started), lastLiveVMs, nil
				}
			}
		}
		select {
		case <-ctx.Done():
			return time.Since(started), lastLiveVMs, ctx.Err()
		case <-ticker.C:
		}
	}
}

func runS3(ctx context.Context, cfg config, client *controlPlaneClient, baseline time.Duration) scenarioVerdict {
	recoveryDelay, finalLiveVMs, err := waitForWorkloadCapacityRecovery(ctx, client, cfg.sessionWorkload)
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("session teardown capacity did not recover; last observed live VM count=%d: %v", finalLiveVMs, err)}
	}
	started := time.Now()
	session, initialState, err := createSession(ctx, cfg, client)
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: err.Error()}
	}
	defer func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = destroySession(cleanupCtx, client, session)
	}()
	if !liveSessionStates[initialState] {
		if _, err := waitForSessionState(ctx, client, session, liveSessionStates); err != nil {
			return scenarioVerdict{Verdict: verdictFail, Detail: "second session never reached live: " + err.Error()}
		}
	}
	latency := time.Since(started)
	detail := fmt.Sprintf("teardown capacity recovered in %s with live VM count=%d; second session live in %s (baseline %s)", recoveryDelay.Round(time.Millisecond), finalLiveVMs, latency.Round(time.Millisecond), baseline.Round(time.Millisecond))
	if s3LatencyRegressed(latency, baseline) {
		return scenarioVerdict{Verdict: verdictFail, Detail: detail}
	}
	return scenarioVerdict{Verdict: verdictPass, Detail: detail}
}

// A second session under this bound is healthy no matter what the baseline
// was. The ratio check alone failed a 138ms session against a 59ms baseline
// while an earlier run passed 103ms against 175ms: with sub-200ms restores an
// unusually FAST baseline is the failure trigger, not the second session. The
// degradation S3 exists to catch (teardown leaving the restore path wedged)
// measures in seconds.
//
// The value must stay at pollInterval, not above it: latency is bimodal, sub
// 200ms when create returns live synchronously and pollInterval-plus when it
// needed a waitForSessionState tick, so a floor of exactly one tick keeps the
// entire slow bucket failing the ratio. Raising it "to be safer" would
// silently forgive that whole bucket.
const s3AbsoluteLatencyFloor = pollInterval

func s3LatencyRegressed(latency, baseline time.Duration) bool {
	return latency > baseline*2 && latency > s3AbsoluteLatencyFloor
}

type invariantVerdict struct {
	Invariant string  `json:"invariant"`
	Verdict   string  `json:"verdict"`
	Coverage  float64 `json:"coverage"`
}

func runS4(ctx context.Context, cfg config, client *controlPlaneClient, suiteStarted time.Time) scenarioVerdict {
	query := url.Values{"since_ts_ms": []string{fmt.Sprintf("%d", suiteStarted.UnixMilli())}}
	path := "/v1/conformance?" + query.Encode()
	response, err := client.request(ctx, http.MethodGet, path, nil, "", nil)
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("GET %s: %v", path, err)}
	}
	if response.status != http.StatusOK {
		return scenarioVerdict{Verdict: verdictFail, Detail: httpErrorDetail(http.MethodGet, path, response)}
	}
	var view struct {
		Enabled  bool            `json:"enabled"`
		Verdicts json.RawMessage `json:"verdicts"`
	}
	if err := json.Unmarshal(response.body, &view); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "invalid conformance response: " + err.Error()}
	}
	if !view.Enabled {
		return scenarioVerdict{Verdict: verdictVacuous, Detail: "conformance endpoint enabled=false"}
	}
	invariants, err := decodeInvariants(view.Verdicts)
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "invalid invariant verdicts: " + err.Error()}
	}
	sort.Slice(invariants, func(i, j int) bool { return invariants[i].Invariant < invariants[j].Invariant })
	details := make([]string, 0, len(invariants))
	passing := 0
	allVacuous := len(invariants) > 0
	hasFailure := false
	for _, invariant := range invariants {
		details = append(details, fmt.Sprintf("%s=%s(coverage=%g)", invariant.Invariant, invariant.Verdict, invariant.Coverage))
		if invariant.Verdict != verdictVacuous {
			allVacuous = false
		}
		if invariant.Verdict == verdictFail {
			hasFailure = true
		}
		if invariant.Verdict == verdictPass && invariant.Coverage > 0 {
			passing++
		}
	}
	detail := strings.Join(details, ", ")
	if len(invariants) == 0 || allVacuous {
		return scenarioVerdict{Verdict: verdictVacuous, Detail: "all invariants vacuous: " + detail}
	}
	if hasFailure {
		return scenarioVerdict{Verdict: verdictFail, Detail: detail}
	}
	if passing < cfg.minPassingInvariants {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("%d passing invariants with coverage, need %d: %s", passing, cfg.minPassingInvariants, detail)}
	}
	return scenarioVerdict{Verdict: verdictPass, Detail: detail}
}

func decodeInvariants(raw json.RawMessage) ([]invariantVerdict, error) {
	var list []invariantVerdict
	if err := json.Unmarshal(raw, &list); err == nil {
		return list, nil
	}
	var keyed map[string]struct {
		Verdict  string  `json:"verdict"`
		Coverage float64 `json:"coverage"`
	}
	if err := json.Unmarshal(raw, &keyed); err != nil {
		return nil, err
	}
	for name, item := range keyed {
		list = append(list, invariantVerdict{Invariant: name, Verdict: item.Verdict, Coverage: item.Coverage})
	}
	return list, nil
}

func waitContext(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func truncate(value string, length int) string {
	if len(value) <= length {
		return value
	}
	return value[:length]
}
