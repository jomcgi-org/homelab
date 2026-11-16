package dispatch

import (
	"regexp"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Error classes of a failed turn.
const (
	ClassEngineering = "engineering" // retried by the connector, bounded per work item
	ClassCredential  = "credential"  // blocks: a person must fix the grant
	ClassQuota       = "quota"       // blocks: retrying burns the same limit
	ClassCanceled    = "canceled"
)

// kagent v1.0.1 reports a failed turn as status text only; the Codex error
// category stays in its traces. These patterns match whole identifiers the
// failure text carries: the broker's codex_login_required, Codex error
// categories, OpenAI error codes, and HTTP statuses written as a status.
//
// The broker's quota refusal (a 429 with no Retry-After) reaches StatusText
// as "Codex execution failed: exceeded retry limit, last status: 429 Too
// Many Requests", which quotaRE matches (HOME-127):
//   - Codex 0.161.0 does not retry a Responses 429,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/model-provider-info/src/lib.rs#L447-L452,
//     maps one without a usage-limit body to RetryLimit,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/codex-api/src/api_bridge.rs#L181-L240,
//     ends the turn without server advice to wait,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/protocol/src/error.rs#L416-L418,
//     and writes its text,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/protocol/src/error.rs#L668-L679.
//   - kagent v1.0.1 pins that Codex and prefixes the turn's error,
//     https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/harness/codex/internal/driver/events.go#L140-L150,
//     into the failed task's status message,
//     https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/harness/runtime/a2a/executor.go#L411-L416.
var (
	credentialRE = regexp.MustCompile(`(?i)\b(codex_login_required|invalid_api_key|unauthorized|` +
		`(http|status|status code)[ :=]*401|token_expired|refresh_token_(expired|invalid|reused))\b`)
	quotaRE = regexp.MustCompile(`(?i)\b(usage_?limit_?(reached|exceeded)|insufficient_quota|rate_limit_exceeded|` +
		`(http|status|status code)[ :=]*429)\b`)
)

// PublicFailure names a failed turn's class in words fit for an issue
// comment. The status text it stands in for is model or provider output,
// which can carry provider error bodies and request IDs; the attempt, the
// work item's block and the itsaplan result keep it whole.
func PublicFailure(class string) string {
	switch class {
	case ClassCredential:
		return "the agent's provider credentials were refused"
	case ClassQuota:
		return "the agent's provider usage limit was reached"
	}
	return "the agent's turn failed"
}

// Classify assigns a failed turn's error class from kagent's task state and
// status text. Unknown failures are engineering failures.
func Classify(state, message string) string {
	switch state {
	case kagent.StateAuthRequired:
		return ClassCredential
	case kagent.StateCanceled:
		return ClassCanceled
	}
	switch {
	case credentialRE.MatchString(message):
		return ClassCredential
	case quotaRE.MatchString(message):
		return ClassQuota
	}
	return ClassEngineering
}
