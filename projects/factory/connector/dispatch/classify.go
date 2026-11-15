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
