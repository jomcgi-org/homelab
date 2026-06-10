package xstate

import (
	"strings"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// Convert transforms a schema.StateMachine into an XState Machine.
func Convert(sm *schema.StateMachine) *Machine {
	machine := &Machine{
		ID:      sm.Metadata.Name,
		Context: buildContext(sm),
		States:  make(map[string]State),
	}

	// Find initial state
	for _, s := range sm.States {
		if s.Initial {
			machine.Initial = s.Name
			break
		}
	}

	// Convert states
	for _, s := range sm.States {
		machine.States[s.Name] = convertState(s, sm)
	}

	// Add Unknown state if not already defined (auto-generated)
	if _, exists := machine.States["Unknown"]; !exists {
		machine.States["Unknown"] = State{
			Meta: &StateMeta{
				Requeue: "1m",
				Error:   true,
			},
			Tags: []string{"error", "generated"},
			On:   make(map[string]Transition),
		}
	}

	// Add transitions
	for _, t := range sm.Transitions {
		addTransition(machine, t)
	}

	return machine
}

// buildContext creates the XState context from state fields.
func buildContext(sm *schema.StateMachine) map[string]interface{} {
	context := make(map[string]interface{})

	// Collect all unique fields from all states
	seen := make(map[string]bool)

	for _, s := range sm.States {
		resolved := s.Resolve(sm.FieldGroups)
		for fieldName, fieldType := range resolved.AllFields {
			if seen[fieldName] {
				continue
			}
			seen[fieldName] = true
			context[fieldName] = defaultValueForType(fieldType)
		}
	}

	return context
}

// defaultValueForType returns the appropriate default value for a Go type.
func defaultValueForType(goType string) interface{} {
	switch goType {
	case "int", "int8", "int16", "int32", "int64",
		"uint", "uint8", "uint16", "uint32", "uint64":
		return 0
	case "float32", "float64":
		return 0.0
	case "bool":
		return false
	case "string":
		return nil // null in JSON
	default:
		return nil
	}
}

// convertState transforms a schema.State into an XState State.
func convertState(s schema.State, sm *schema.StateMachine) State {
	state := State{
		On:   make(map[string]Transition),
		Tags: buildTags(s),
	}

	// Set type for terminal states
	if s.Terminal {
		state.Type = "final"
	}

	// Build metadata
	meta := &StateMeta{}
	hasMeta := false

	if s.Requeue.Duration > 0 {
		meta.Requeue = s.Requeue.Duration.String()
		hasMeta = true
	}

	if s.Error {
		meta.Error = true
		hasMeta = true
	}

	if s.Deletion {
		meta.Deletion = true
		hasMeta = true
	}

	// Include resolved fields
	resolved := s.Resolve(sm.FieldGroups)
	if len(resolved.AllFields) > 0 {
		meta.Fields = resolved.AllFields
		hasMeta = true
	}

	if hasMeta {
		state.Meta = meta
	}

	return state
}

// buildTags creates XState tags for a state.
func buildTags(s schema.State) []string {
	var tags []string

	if s.Initial {
		tags = append(tags, "initial")
	}
	if s.Terminal {
		tags = append(tags, "terminal")
	}
	if s.Error {
		tags = append(tags, "error")
	}
	if s.Deletion {
		tags = append(tags, "deletion")
	}
	if s.Generated {
		tags = append(tags, "generated")
	}

	return tags
}

// addTransition adds a transition to the XState machine.
func addTransition(machine *Machine, t schema.Transition) {
	eventName := toEventName(t.Action)

	transition := Transition{
		Target: t.To,
	}

	// Add guard if specified
	if t.Guard != "" {
		transition.Cond = t.Guard
	}

	// Add actions if there are params (these become context assignments)
	if len(t.Params) > 0 {
		var actions []string
		for _, p := range t.Params {
			actions = append(actions, "assign"+capitalize(p.Name))
		}
		transition.Actions = actions
	}

	// Add to each source state
	for _, from := range t.From.States {
		if state, ok := machine.States[from]; ok {
			if state.On == nil {
				state.On = make(map[string]Transition)
			}
			state.On[eventName] = transition
			machine.States[from] = state
		}
	}
}

// toEventName converts an action name to an XState event name.
// Example: "StartTunnelCreation" -> "START_TUNNEL_CREATION"
func toEventName(action string) string {
	var result strings.Builder
	for i, r := range action {
		if i > 0 && isUpperCase(r) {
			result.WriteRune('_')
		}
		result.WriteRune(toUpper(r))
	}
	return result.String()
}

func isUpperCase(r rune) bool {
	return r >= 'A' && r <= 'Z'
}

func toUpper(r rune) rune {
	if r >= 'a' && r <= 'z' {
		return r - 32
	}
	return r
}

func capitalize(s string) string {
	if s == "" {
		return ""
	}
	return strings.ToUpper(s[:1]) + s[1:]
}
