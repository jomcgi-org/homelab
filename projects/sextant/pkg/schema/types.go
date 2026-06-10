// Package schema defines the YAML DSL schema for operator state machines.
package schema

import (
	"time"
)

// StateMachine defines a complete state machine specification for a Kubernetes operator.
// This is the top-level structure parsed from YAML.
type StateMachine struct {
	// APIVersion is the schema version (e.g., "sextant.jomcgi.dev/v1alpha1")
	APIVersion string `yaml:"apiVersion" json:"apiVersion"`

	// Kind must be "StateMachine"
	Kind string `yaml:"kind" json:"kind"`

	// Metadata contains resource identification
	Metadata Metadata `yaml:"metadata" json:"metadata"`

	// Status configures how state maps to the Kubernetes Status subresource
	Status StatusConfig `yaml:"status" json:"status"`

	// FieldGroups defines reusable field collections to reduce duplication
	FieldGroups map[string]FieldGroup `yaml:"fieldGroups,omitempty" json:"fieldGroups,omitempty"`

	// States defines all possible states in the state machine
	States []State `yaml:"states" json:"states"`

	// Transitions defines valid state transitions
	Transitions []Transition `yaml:"transitions" json:"transitions"`

	// Guards defines named guard conditions referenced by transitions
	Guards map[string]Guard `yaml:"guards,omitempty" json:"guards,omitempty"`

	// Dependencies defines resources this controller depends on
	Dependencies []Dependency `yaml:"dependencies,omitempty" json:"dependencies,omitempty"`

	// Observability configures generated observability hooks
	Observability Observability `yaml:"observability,omitempty" json:"observability,omitempty"`

	// ErrorHandling configures retry behavior
	ErrorHandling *ErrorHandling `yaml:"errorHandling,omitempty" json:"errorHandling,omitempty"`

	// SpecChangeHandling configures spec change detection
	SpecChangeHandling *SpecChangeHandling `yaml:"specChangeHandling,omitempty" json:"specChangeHandling,omitempty"`
}

// SpecChangeHandling configures how the state machine detects spec changes.
type SpecChangeHandling struct {
	// Enabled turns on spec change detection (default: false)
	Enabled bool `yaml:"enabled,omitempty" json:"enabled,omitempty"`

	// ObservedGenerationField is the Status field name storing the last observed generation (default: "observedGeneration")
	ObservedGenerationField string `yaml:"observedGenerationField,omitempty" json:"observedGenerationField,omitempty"`
}

// ErrorHandling configures how the state machine handles errors.
type ErrorHandling struct {
	// Backoff configures exponential backoff
	Backoff BackoffConfig `yaml:"backoff,omitempty" json:"backoff,omitempty"`

	// MaxRetries is the maximum number of retries before moving to Failed state
	MaxRetries int `yaml:"maxRetries,omitempty" json:"maxRetries,omitempty"`
}

// BackoffConfig configures the exponential backoff strategy.
type BackoffConfig struct {
	// Base is the initial backoff duration (default: 1s)
	Base Duration `yaml:"base,omitempty" json:"base,omitempty"`

	// Multiplier is the factor to multiply backoff by each retry (default: 2)
	Multiplier float64 `yaml:"multiplier,omitempty" json:"multiplier,omitempty"`

	// Max is the maximum backoff duration (default: 5m)
	Max Duration `yaml:"max,omitempty" json:"max,omitempty"`

	// Jitter is the randomization factor (0.0 to 1.0) (default: 0.1)
	Jitter float64 `yaml:"jitter,omitempty" json:"jitter,omitempty"`
}

// Metadata identifies the resource this state machine manages.
type Metadata struct {
	// Name is the Go type name (e.g., "CloudflareTunnel")
	Name string `yaml:"name" json:"name"`

	// Group is the Kubernetes API group (e.g., "cloudflare.io")
	Group string `yaml:"group" json:"group"`

	// Version is the API version (e.g., "v1alpha1")
	Version string `yaml:"version" json:"version"`
}

// StatusConfig configures how state machine state maps to Kubernetes Status.
type StatusConfig struct {
	// PhaseField is the Status field storing the current phase (default: "phase")
	PhaseField string `yaml:"phaseField,omitempty" json:"phaseField,omitempty"`

	// ConditionsField is the Status field storing Kubernetes conditions (default: "conditions")
	ConditionsField string `yaml:"conditionsField,omitempty" json:"conditionsField,omitempty"`
}

// FieldGroup defines a reusable collection of fields that can be embedded in
// states. Values are Go types; a "?" suffix (e.g. "string?") marks a field
// optional for group-level validation — see State.Requires.
type FieldGroup map[string]string

// State defines a single state in the state machine.
type State struct {
	// Name is the state identifier (e.g., "Pending", "CreatingTunnel")
	Name string `yaml:"name" json:"name"`

	// Initial marks this as the initial state (exactly one required)
	Initial bool `yaml:"initial,omitempty" json:"initial,omitempty"`

	// Terminal marks this as a terminal state (no outgoing transitions except to error/deleting)
	Terminal bool `yaml:"terminal,omitempty" json:"terminal,omitempty"`

	// Error marks this as an error state
	Error bool `yaml:"error,omitempty" json:"error,omitempty"`

	// Deletion marks this as a deletion state (triggered by deletionTimestamp)
	Deletion bool `yaml:"deletion,omitempty" json:"deletion,omitempty"`

	// Generated marks this state as auto-generated (e.g., Unknown)
	Generated bool `yaml:"generated,omitempty" json:"generated,omitempty"`

	// Requeue specifies the default polling interval for this state
	Requeue Duration `yaml:"requeue,omitempty" json:"requeue,omitempty"`

	// Fields defines state-specific data fields
	Fields map[string]string `yaml:"fields,omitempty" json:"fields,omitempty"`

	// FieldGroups references shared field groups to embed
	FieldGroups []string `yaml:"fieldGroups,omitempty" json:"fieldGroups,omitempty"`

	// Requires lists optional field-group fields (declared with a "?" type
	// suffix, e.g. "string?") that must be set in THIS state. Group-level
	// Validate skips optional fields; each state listing one here gets an
	// explicit check in its own Validate.
	Requires []string `yaml:"requires,omitempty" json:"requires,omitempty"`
}

// Duration wraps time.Duration for YAML parsing (e.g., "5s", "1h")
type Duration struct {
	time.Duration
}

// UnmarshalYAML implements yaml.Unmarshaler for Duration
func (d *Duration) UnmarshalYAML(unmarshal func(interface{}) error) error {
	var s string
	if err := unmarshal(&s); err != nil {
		return err
	}
	if s == "" {
		d.Duration = 0
		return nil
	}
	duration, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	d.Duration = duration
	return nil
}

// MarshalYAML implements yaml.Marshaler for Duration
func (d Duration) MarshalYAML() (interface{}, error) {
	if d.Duration == 0 {
		return "", nil
	}
	return d.Duration.String(), nil
}

// Transition defines a valid state transition.
type Transition struct {
	// From specifies the source state(s) - can be a single state or a list
	From TransitionSource `yaml:"from" json:"from"`

	// To specifies the destination state
	To string `yaml:"to" json:"to"`

	// Action is the method name for this transition (e.g., "StartTunnelCreation")
	Action string `yaml:"action" json:"action"`

	// Params defines parameters required for this transition (idempotency keys, etc.)
	Params []TransitionParam `yaml:"params,omitempty" json:"params,omitempty"`

	// Guard references a named guard condition
	Guard string `yaml:"guard,omitempty" json:"guard,omitempty"`

	// Trigger specifies what triggers this transition automatically
	Trigger string `yaml:"trigger,omitempty" json:"trigger,omitempty"`
}

// TransitionSource can be a single state name or a list of state names.
type TransitionSource struct {
	States []string
}

// UnmarshalYAML implements yaml.Unmarshaler for TransitionSource
func (t *TransitionSource) UnmarshalYAML(unmarshal func(interface{}) error) error {
	// Try as a single string first
	var single string
	if err := unmarshal(&single); err == nil {
		t.States = []string{single}
		return nil
	}

	// Try as a list of strings
	var list []string
	if err := unmarshal(&list); err != nil {
		return err
	}
	t.States = list
	return nil
}

// MarshalYAML implements yaml.Marshaler for TransitionSource
func (t TransitionSource) MarshalYAML() (interface{}, error) {
	if len(t.States) == 1 {
		return t.States[0], nil
	}
	return t.States, nil
}

// TransitionParam defines a parameter for a transition.
type TransitionParam struct {
	// Name is the parameter name
	Name string

	// Type is the Go type (e.g., "string", "int")
	Type string
}

// UnmarshalYAML implements yaml.Unmarshaler for TransitionParam
func (p *TransitionParam) UnmarshalYAML(unmarshal func(interface{}) error) error {
	// Params are specified as a map with a single key-value pair
	var m map[string]string
	if err := unmarshal(&m); err != nil {
		return err
	}
	for name, typ := range m {
		p.Name = name
		p.Type = typ
		break
	}
	return nil
}

// MarshalYAML implements yaml.Marshaler for TransitionParam
func (p TransitionParam) MarshalYAML() (interface{}, error) {
	return map[string]string{p.Name: p.Type}, nil
}

// Guard defines a guard condition for a transition.
type Guard struct {
	// Description explains when this guard passes
	Description string `yaml:"description,omitempty" json:"description,omitempty"`

	// MaxRetries is used for retry guards
	MaxRetries int `yaml:"maxRetries,omitempty" json:"maxRetries,omitempty"`

	// MinBackoff enforces a minimum time before transition is allowed.
	// If specified, the guard only passes when the state has been active for at least this duration.
	MinBackoff Duration `yaml:"minBackoff,omitempty" json:"minBackoff,omitempty"`

	// Condition is a Go expression that must evaluate to true for the guard to pass.
	// Available context: 's' (current state struct), 'r' (resource struct)
	// Example: "r.Spec.Replicas > 0"
	// WARNING: Condition expressions are embedded directly into generated code.
	// Invalid expressions will cause compilation errors in generated code.
	Condition string `yaml:"condition,omitempty" json:"condition,omitempty"`
}

// Dependency defines a resource dependency for the controller.
type Dependency struct {
	// Name identifies this dependency
	Name string `yaml:"name" json:"name"`

	// Resource is the Kubernetes resource type (e.g., "MyResource")
	Resource string `yaml:"resource" json:"resource"`

	// Group is the Kubernetes API group
	Group string `yaml:"group" json:"group"`

	// States defines required states for this dependency
	States DependencyStates `yaml:"states,omitempty" json:"states,omitempty"`
}

// DependencyStates defines state requirements for a dependency.
type DependencyStates struct {
	// Required lists states the dependency must be in
	Required []string `yaml:"required,omitempty" json:"required,omitempty"`
}

// Observability configures generated observability features.
type Observability struct {
	// OnTransition generates a TransitionObserver interface
	OnTransition bool `yaml:"onTransition,omitempty" json:"onTransition,omitempty"`

	// OTelTracing generates OpenTelemetry span helpers
	OTelTracing bool `yaml:"otelTracing,omitempty" json:"otelTracing,omitempty"`

	// EmbedDiagram embeds a Mermaid state diagram in generated code
	EmbedDiagram bool `yaml:"embedDiagram,omitempty" json:"embedDiagram,omitempty"`

	// Metrics generates Prometheus metrics and MetricsObserver
	Metrics bool `yaml:"metrics,omitempty" json:"metrics,omitempty"`
}

// ResolvedState represents a state with all field groups expanded.
type ResolvedState struct {
	State
	// AllFields contains all fields including those from field groups
	AllFields map[string]string
}

// Resolve expands field groups into AllFields.
func (s *State) Resolve(groups map[string]FieldGroup) ResolvedState {
	resolved := ResolvedState{
		State:     *s,
		AllFields: make(map[string]string),
	}

	// First add fields from field groups
	for _, groupName := range s.FieldGroups {
		if group, ok := groups[groupName]; ok {
			for name, typ := range group {
				resolved.AllFields[name] = typ
			}
		}
	}

	// Then add direct fields (which can override group fields)
	for name, typ := range s.Fields {
		resolved.AllFields[name] = typ
	}

	return resolved
}
