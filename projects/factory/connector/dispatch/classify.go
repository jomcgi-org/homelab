package dispatch

import "github.com/jomcgi/homelab/projects/factory/connector/kagent"

// Error classes of a failed turn.
const (
	ClassEngineering = "engineering" // retried by the connector, bounded per work item
	ClassCredential  = "credential"  // blocks: a person must fix the grant
	ClassQuota       = "quota"       // blocks: retrying burns the same limit
	ClassCanceled    = "canceled"
	// ClassAbandoned is a turn the connector ended after its liveness
	// lease; it takes an engineering retry in a fresh session (HOME-141).
	ClassAbandoned = "abandoned"
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
	if class := kagent.GrantFailure(message); class != "" {
		return class
	}
	return ClassEngineering
}
