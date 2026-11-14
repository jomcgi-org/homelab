package dispatch

import (
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Error classes of a failed turn.
const (
	ClassEngineering = "engineering" // retried by the connector, bounded per work item
	ClassCredential  = "credential"  // blocks: a person must fix the grant
	ClassQuota       = "quota"       // blocks: retrying burns the same limit
	ClassCanceled    = "canceled"
)

// credentialMarkers name a dead or missing credential. codex_login_required
// is the broker's answer for a dead Codex grant.
var credentialMarkers = []string{
	"codex_login_required",
	"login required",
	"invalid_api_key",
	"invalid api key",
	"unauthorized",
	"401",
	"authentication failed",
	"bad credentials",
	"token expired",
	"refresh_token",
}

var quotaMarkers = []string{
	"usage_limit_reached",
	"usage limit",
	"insufficient_quota",
	"quota",
	"rate limit",
	"rate_limit",
	"429",
	"too many requests",
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
	m := strings.ToLower(message)
	for _, k := range credentialMarkers {
		if strings.Contains(m, k) {
			return ClassCredential
		}
	}
	for _, k := range quotaMarkers {
		if strings.Contains(m, k) {
			return ClassQuota
		}
	}
	return ClassEngineering
}
