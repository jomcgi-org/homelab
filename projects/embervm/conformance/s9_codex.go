package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	codexProbeEffort     = "xhigh"
	codexGuestDigestPath = "/embervm-image-digest"
	// codexProbeFailRetryAfter bounds how long a retained failure blocks a
	// fresh probe. A provider-side fix, such as restoring the dev broker grant,
	// otherwise stays invisible until the chart version or runner changes.
	codexProbeFailRetryAfter = time.Hour
)

// Bound provider traffic and unresolved session side effects within this runner
// process and chart version. Charge before the first invoke, or when creation
// or cleanup leaves an unknown outcome. Only a proven no-allocation denial or
// confirmed pre-invoke cleanup leaves the budget untouched. Success is retained;
// two charged failures retain a fail-closed verdict for
// codexProbeFailRetryAfter, so provider spend stays at two charged attempts
// per hour. A process restart clears this cache. Ordinary lifecycle scenarios
// keep running either way.
type codexProbeGate struct {
	chartVersion string
	attempts     int
	last         *scenarioVerdict
	lastAt       time.Time
	now          func() time.Time
}

func (g *codexProbeGate) clock() time.Time {
	if g.now != nil {
		return g.now()
	}
	return time.Now()
}

func (g *codexProbeGate) run(ctx context.Context, cfg config, client *controlPlaneClient) scenarioVerdict {
	if g.chartVersion != cfg.chartVersion {
		g.chartVersion, g.attempts, g.last = cfg.chartVersion, 0, nil
	}
	if g.last != nil && g.last.Verdict != verdictPass && g.attempts >= 2 && g.clock().Sub(g.lastAt) >= codexProbeFailRetryAfter {
		g.attempts, g.last = 0, nil
	}
	if g.last != nil && (g.last.Verdict == verdictPass || g.attempts >= 2) {
		result := *g.last
		result.Detail += "; retained for this runner's chart version; no new provider call"
		return result
	}
	result := runBudgeted(ctx, "S9", cfg.budgets["S9"], func(scenarioCtx context.Context) scenarioVerdict {
		// runBudgeted and runS9 are synchronous: invocation or unresolved
		// side effects are charged before this gate returns, never by a worker.
		return runS9(scenarioCtx, cfg, client, func() { g.attempts++ })
	})
	g.last, g.lastAt = &result, g.clock()
	return result
}

type codexProbeTurn struct {
	SessionID      string `json:"session_id"`
	Model          string `json:"model"`
	ProviderModel  string `json:"provider_model"`
	Effort         string `json:"effort"`
	TerminalReason string `json:"terminal_reason"`
	IsError        bool   `json:"is_error"`
	Error          string `json:"error"`
	Usage          struct {
		InputTokens     int64 `json:"input_tokens"`
		OutputTokens    int64 `json:"output_tokens"`
		CacheReadTokens int64 `json:"cache_read_tokens"`
	} `json:"usage"`
	Activities []struct {
		Type    string `json:"type"`
		Command string `json:"command"`
	} `json:"activities"`
	Artifact struct {
		Path       string `json:"path"`
		Outcome    string `json:"outcome"`
		ContentB64 string `json:"content_b64"`
	} `json:"artifact"`
}

// Use the same guest, native session capability and brokered egress as ordinary
// agent sessions. The runner never obtains a provider credential. The shim's
// independent artifact capture must prove shell execution; model prose alone
// is not evidence that the command ran.
func runS9(ctx context.Context, cfg config, client *controlPlaneClient, onCharge func()) (result scenarioVerdict) {
	charged := false
	chargeAttempt := func() {
		if !charged {
			charged = true
			if onCharge != nil {
				onCharge()
			}
		}
	}
	var nonceBytes [16]byte
	if _, err := rand.Read(nonceBytes[:]); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "create Codex shell challenge: " + err.Error()}
	}
	nonce := "ember-codex-" + hex.EncodeToString(nonceBytes[:])
	noncePath := nonce + ".txt"
	artifactPath := nonce + ".first.proof"
	probeCfg := cfg
	probeCfg.sessionWorkload = cfg.codexWorkload
	session, initialState, err := createSession(ctx, probeCfg, client)
	if err != nil {
		var denial *sessionCreateDeniedError
		if !errors.As(err, &denial) {
			// A lost or malformed create response can hide an allocated session
			// whose identity is unavailable for cleanup. Keep it bounded.
			chargeAttempt()
		}
		return scenarioVerdict{Verdict: verdictFail, Detail: "create Codex session: " + err.Error()}
	}
	cleanupNeeded := true
	defer func() {
		if cleanupNeeded {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			err := destroySession(cleanupCtx, client, session)
			if err == nil {
				err = waitForSessionGone(cleanupCtx, client, session)
			}
			if err != nil {
				// A pre-invoke failure is free only after confirmed cleanup.
				// An already charged invoke and its cleanup share one attempt.
				chargeAttempt()
				result.Verdict = verdictFail
				result.Detail += "; session cleanup failed: " + err.Error()
			}
		}
	}()
	if !liveSessionStates[initialState] {
		if _, err := waitForSessionState(ctx, client, session, liveSessionStates); err != nil {
			return scenarioVerdict{Verdict: verdictFail, Detail: "Codex session never reached live: " + err.Error()}
		}
	}

	command := fmt.Sprintf("printf '%%s\\n' '%s' > '%s' && ", nonce, noncePath) + codexProofCommand(noncePath, artifactPath)
	first, err := invokeCodexProbe(ctx, cfg, client, session, "", artifactPath, command, chargeAttempt)
	if err == nil {
		err = validateCodexProbeTurn(first, cfg, "", artifactPath, nonce, "printf")
	}
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "first Codex shell turn: " + err.Error()}
	}

	// Persistence-enabled dev sessions park and tear down the VM. A second
	// request to the still-running app-server would not test native restore.
	if _, err := waitForSessionState(ctx, client, session, map[string]bool{"parked": true}); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "Codex session did not park before resume: " + err.Error()}
	}
	// Capture another proof after rejoin; reusing the first proof would miss
	// a stale base selected when the parked session is restored.
	artifactPath = nonce + ".resume.proof"
	command = codexProofCommand(noncePath, artifactPath)
	second, err := invokeCodexProbe(ctx, cfg, client, session, first.SessionID, artifactPath, command, nil)
	if err == nil {
		err = validateCodexProbeTurn(second, cfg, first.SessionID, artifactPath, nonce, "cat")
	}
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "resumed Codex shell turn: " + err.Error()}
	}
	if err := destroySession(ctx, client, session); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "destroy Codex session: " + err.Error()}
	}
	if err := waitForSessionGone(ctx, client, session); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "Codex session not destroyed: " + err.Error()}
	}
	cleanupNeeded = false
	return scenarioVerdict{
		Verdict: verdictPass,
		Detail: fmt.Sprintf("%s at %s: authenticated shell write, native park, same-thread resume and shell read; independently captured guest digest %s, Codex %s and positive usage on both turns; session destroyed",
			cfg.codexProviderModel, codexProbeEffort, cfg.codexGuestDigest, cfg.codexCLIVersion),
	}
}

// The b3 baker stamps the manifest it actually exported into this read-only
// rootfs file. The session API's base_digest names the desired catalog image,
// not necessarily the selected guest. Neither expected value is in the prompt.
func codexProofCommand(noncePath, artifactPath string) string {
	return fmt.Sprintf("{ cat '%s' && cat %s && /usr/local/bin/codex --version; } > '%s' && cat '%s'", noncePath, codexGuestDigestPath, artifactPath, artifactPath)
}

func validCodexGuestDigest(digest string) bool {
	encoded, ok := strings.CutPrefix(digest, "sha256:")
	decoded, err := hex.DecodeString(encoded)
	return ok && err == nil && len(decoded) == 32 && encoded == strings.ToLower(encoded)
}

func invokeCodexProbe(ctx context.Context, cfg config, client *controlPlaneClient, session sessionIdentity, cliSessionID, artifactPath, command string, beforeInvoke func()) (codexProbeTurn, error) {
	message := "Use only the shell tool to execute this exact harmless command in the current directory:\n" + command +
		"\nReturn only its complete output. Do not use other tools or change any other file."
	request := map[string]any{
		"message":       message,
		"model":         cfg.codexModel,
		"effort":        codexProbeEffort,
		"artifact_path": artifactPath,
	}
	// An empty session_id is rejected by the shim; the first turn omits it.
	if cliSessionID != "" {
		request["session_id"] = cliSessionID
	}
	body, err := json.Marshal(request)
	if err != nil {
		return codexProbeTurn{}, fmt.Errorf("encode Codex turn: %w", err)
	}
	path := "/v1/sessions/" + url.PathEscape(session.ID) + "/invoke"
	// The shared client caps ordinary requests at two minutes, but a native
	// Claude turn may take 180s. Keep its transport settings and use S9's
	// scenario context to bound both invokes together; do not mutate the client
	// used by other scenarios or by the independently bounded cleanup.
	invokeClient := *client
	httpClient := *client.http
	httpClient.Timeout = 0
	invokeClient.http = &httpClient
	if err := ctx.Err(); err != nil {
		return codexProbeTurn{}, fmt.Errorf("invoke: %w", err)
	}
	if beforeInvoke != nil {
		// Once this boundary is crossed, even an unknown request outcome must
		// consume the attempt. The resumed turn belongs to the same attempt.
		beforeInvoke()
	}
	response, err := invokeClient.request(ctx, http.MethodPost, path, body, session.Token, map[string]string{"X-Ember-Guest-Path": "/shim/turn"})
	if err != nil {
		return codexProbeTurn{}, fmt.Errorf("invoke: %w", err)
	}
	if response.status != http.StatusOK {
		// Provider or egress failures must fail S9, unlike lifecycle-only S2.
		// Do not copy arbitrary provider response bodies to verdicts or logs.
		if diagnostic := safeInvokeDiagnostic(response.diagnostic); diagnostic != "" {
			return codexProbeTurn{}, fmt.Errorf("invoke returned status=%d diagnostic=%s", response.status, diagnostic)
		}
		return codexProbeTurn{}, fmt.Errorf("invoke returned status=%d", response.status)
	}
	var turn codexProbeTurn
	if err := json.Unmarshal(response.body, &turn); err != nil {
		return codexProbeTurn{}, fmt.Errorf("invalid guest turn response: %w", err)
	}
	return turn, nil
}

func validateCodexProbeTurn(turn codexProbeTurn, cfg config, cliSessionID, artifactPath, nonce, commandVerb string) error {
	if turn.IsError || turn.Error != "" || turn.TerminalReason != "completed" {
		return fmt.Errorf("guest did not complete successfully")
	}
	if turn.Model != cfg.codexModel || turn.ProviderModel != cfg.codexProviderModel || turn.Effort != codexProbeEffort {
		return fmt.Errorf("guest model or effort does not match the configured Codex probe")
	}
	if turn.SessionID == "" || (cliSessionID != "" && turn.SessionID != cliSessionID) {
		return fmt.Errorf("missing or changed Codex conversation identity")
	}
	if turn.Usage.InputTokens <= 0 || turn.Usage.OutputTokens <= 0 || turn.Usage.CacheReadTokens < 0 || turn.Usage.CacheReadTokens > turn.Usage.InputTokens {
		return fmt.Errorf("missing or invalid per-turn token usage")
	}
	shellSeen := false
	for _, activity := range turn.Activities {
		if activity.Type == "bash" && strings.Contains(activity.Command, commandVerb) && strings.Contains(activity.Command, artifactPath) && strings.Contains(activity.Command, codexGuestDigestPath) && strings.Contains(activity.Command, "/usr/local/bin/codex --version") {
			shellSeen = true
		}
	}
	if !shellSeen {
		return fmt.Errorf("required shell command activity was not observed")
	}
	if turn.Artifact.Outcome != "ok" || turn.Artifact.Path != artifactPath {
		return fmt.Errorf("shell artifact was not captured independently")
	}
	artifact, err := base64.StdEncoding.DecodeString(turn.Artifact.ContentB64)
	if err != nil {
		return fmt.Errorf("captured shell artifact does not match this run's challenge")
	}
	lines := strings.Split(string(artifact), "\n")
	if len(lines) != 4 || lines[0] != nonce || lines[3] != "" {
		return fmt.Errorf("captured shell artifact does not match this run's challenge")
	}
	if !validCodexGuestDigest(lines[1]) || lines[1] != cfg.codexGuestDigest {
		return fmt.Errorf("captured guest image digest does not match the candidate image")
	}
	if lines[2] != "codex-cli "+cfg.codexCLIVersion {
		return fmt.Errorf("captured Codex CLI version does not match the candidate package")
	}
	return nil
}
