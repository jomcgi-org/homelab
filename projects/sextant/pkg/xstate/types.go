// Package xstate provides XState JSON types and conversion from the YAML DSL.
//
// XState is a JavaScript state machine library with formal SCXML semantics.
// We use its JSON format as an interchange format because:
//   - It has well-defined execution semantics
//   - Excellent tooling (stately.ai visualizer)
//   - Schema validation is straightforward
//   - Industry adoption and documentation
package xstate

import "encoding/json"

// Machine represents an XState state machine definition.
type Machine struct {
	// ID is the unique identifier for this machine
	ID string `json:"id"`

	// Initial is the initial state ID
	Initial string `json:"initial"`

	// Context contains the machine's extended state (data)
	Context map[string]interface{} `json:"context,omitempty"`

	// States maps state IDs to state definitions
	States map[string]State `json:"states"`
}

// State represents an XState state definition.
type State struct {
	// Type can be "final", "parallel", "history", or empty for normal states
	Type string `json:"type,omitempty"`

	// Meta contains custom metadata for the state
	Meta *StateMeta `json:"meta,omitempty"`

	// On maps event names to transitions
	On map[string]Transition `json:"on,omitempty"`

	// Entry actions to run when entering this state
	Entry []string `json:"entry,omitempty"`

	// Exit actions to run when exiting this state
	Exit []string `json:"exit,omitempty"`

	// Tags for categorizing states
	Tags []string `json:"tags,omitempty"`
}

// StateMeta contains custom metadata for a state.
type StateMeta struct {
	// Requeue is the default requeue interval for this state
	Requeue string `json:"requeue,omitempty"`

	// Description provides documentation for this state
	Description string `json:"description,omitempty"`

	// Error indicates this is an error state
	Error bool `json:"error,omitempty"`

	// Deletion indicates this is a deletion state
	Deletion bool `json:"deletion,omitempty"`

	// Fields lists the fields associated with this state
	Fields map[string]string `json:"fields,omitempty"`
}

// Transition represents a state transition in XState.
type Transition struct {
	// Target is the destination state
	Target string `json:"target"`

	// Actions to execute during the transition
	Actions []string `json:"actions,omitempty"`

	// Cond is the guard condition name
	Cond string `json:"cond,omitempty"`

	// Internal indicates an internal transition (doesn't exit/enter state)
	Internal bool `json:"internal,omitempty"`
}

// MarshalJSON implements custom JSON marshaling for Transition.
// If the transition is simple (just a target), it marshals as a string.
func (t Transition) MarshalJSON() ([]byte, error) {
	// If it's a simple transition with just a target, marshal as string
	if t.Cond == "" && len(t.Actions) == 0 && !t.Internal {
		return json.Marshal(t.Target)
	}

	// Otherwise, marshal as an object
	type transitionAlias Transition
	return json.Marshal(transitionAlias(t))
}

// UnmarshalJSON implements custom JSON unmarshaling for Transition.
// It can unmarshal from either a string or an object.
func (t *Transition) UnmarshalJSON(data []byte) error {
	// Try as a string first
	var target string
	if err := json.Unmarshal(data, &target); err == nil {
		t.Target = target
		return nil
	}

	// Try as an object
	type transitionAlias Transition
	var alias transitionAlias
	if err := json.Unmarshal(data, &alias); err != nil {
		return err
	}
	*t = Transition(alias)
	return nil
}
