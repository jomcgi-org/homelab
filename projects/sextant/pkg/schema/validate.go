package schema

import (
	"fmt"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

// ValidationError contains multiple validation errors.
type ValidationError struct {
	Errors []error
}

func (e ValidationError) Error() string {
	if len(e.Errors) == 0 {
		return "no validation errors"
	}

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("%d validation error(s):\n", len(e.Errors)))
	for i, err := range e.Errors {
		sb.WriteString(fmt.Sprintf("  %d. %s\n", i+1, err.Error()))
	}
	return sb.String()
}

// ParseFile reads and parses a state machine YAML file.
func ParseFile(path string) (*StateMachine, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read file: %w", err)
	}

	return Parse(data)
}

// Parse parses state machine YAML data.
func Parse(data []byte) (*StateMachine, error) {
	var sm StateMachine
	if err := yaml.Unmarshal(data, &sm); err != nil {
		return nil, fmt.Errorf("failed to parse YAML: %w", err)
	}

	return &sm, nil
}

// Validate performs comprehensive validation on a state machine definition.
func Validate(sm *StateMachine) error {
	var errs []error

	// Validate API version and kind
	if sm.APIVersion == "" {
		errs = append(errs, fmt.Errorf("apiVersion is required"))
	}
	if sm.Kind != "StateMachine" {
		errs = append(errs, fmt.Errorf("kind must be 'StateMachine', got %q", sm.Kind))
	}

	// Validate metadata
	if err := validateMetadata(&sm.Metadata); err != nil {
		errs = append(errs, err)
	}

	// Validate error handling
	if err := validateErrorHandling(sm.ErrorHandling); err != nil {
		errs = append(errs, err)
	}

	// Validate field groups
	if err := validateFieldGroups(sm.FieldGroups); err != nil {
		errs = append(errs, err)
	}

	// Validate states
	stateErrors := validateStates(sm.States, sm.FieldGroups)
	errs = append(errs, stateErrors...)

	// Validate transitions
	stateNames := collectStateNames(sm.States)
	transitionErrors := validateTransitions(sm.Transitions, stateNames, sm.Guards)
	errs = append(errs, transitionErrors...)

	// Validate guards
	guardErrors := validateGuards(sm.Guards)
	errs = append(errs, guardErrors...)

	// Validate dependencies
	depErrors := validateDependencies(sm.Dependencies)
	errs = append(errs, depErrors...)

	// Validate structural constraints
	structuralErrors := validateStructuralConstraints(sm)
	errs = append(errs, structuralErrors...)

	if len(errs) > 0 {
		return ValidationError{Errors: errs}
	}

	return nil
}

func validateMetadata(m *Metadata) error {
	var errs []error

	if err := CheckMetadataName(m.Name); err != nil {
		errs = append(errs, err)
	}

	if m.Group == "" {
		errs = append(errs, fmt.Errorf("metadata.group is required"))
	}

	if m.Version == "" {
		errs = append(errs, fmt.Errorf("metadata.version is required"))
	}

	if len(errs) > 0 {
		return ValidationError{Errors: errs}
	}
	return nil
}

func validateErrorHandling(eh *ErrorHandling) error {
	if eh == nil {
		return nil
	}

	var errs []error

	if eh.MaxRetries < 0 {
		errs = append(errs, fmt.Errorf("errorHandling.maxRetries must be non-negative"))
	}

	if eh.Backoff.Base.Duration < 0 {
		errs = append(errs, fmt.Errorf("errorHandling.backoff.base must be non-negative"))
	}

	if eh.Backoff.Max.Duration < 0 {
		errs = append(errs, fmt.Errorf("errorHandling.backoff.max must be non-negative"))
	}

	if eh.Backoff.Multiplier < 0 {
		errs = append(errs, fmt.Errorf("errorHandling.backoff.multiplier must be non-negative"))
	}

	if eh.Backoff.Jitter < 0 || eh.Backoff.Jitter > 1.0 {
		errs = append(errs, fmt.Errorf("errorHandling.backoff.jitter must be between 0.0 and 1.0"))
	}

	if len(errs) > 0 {
		return ValidationError{Errors: errs}
	}
	return nil
}

func validateFieldGroups(groups map[string]FieldGroup) error {
	var errs []error

	for name, group := range groups {
		if err := CheckFieldGroupName(name); err != nil {
			errs = append(errs, err)
		}

		for fieldName := range group {
			if err := CheckFieldGroupFieldName(fieldName, name); err != nil {
				errs = append(errs, err)
			}
		}
	}

	if len(errs) > 0 {
		return ValidationError{Errors: errs}
	}
	return nil
}

func validateStates(states []State, fieldGroups map[string]FieldGroup) []error {
	var errs []error
	stateNames := make(map[string]bool)
	initialCount := 0

	for _, state := range states {
		// Check for duplicate state names
		if stateNames[state.Name] {
			errs = append(errs, fmt.Errorf("duplicate state name: %q", state.Name))
		}
		stateNames[state.Name] = true

		// Check reserved state names
		if err := CheckStateName(state.Name); err != nil {
			errs = append(errs, err)
		}

		// Count initial states
		if state.Initial {
			initialCount++
		}

		// Validate field names
		for fieldName := range state.Fields {
			if err := CheckFieldName(fieldName, state.Name); err != nil {
				errs = append(errs, err)
			}
		}

		// Validate field group references
		for _, groupName := range state.FieldGroups {
			if _, ok := fieldGroups[groupName]; !ok {
				errs = append(errs, fmt.Errorf("state %q references undefined field group %q", state.Name, groupName))
			}
		}

		// Validate requires entries: each must be an optional ("?"-suffixed)
		// string field in one of this state's embedded field groups
		for _, required := range state.Requires {
			fieldType, found := "", false
			for _, groupName := range state.FieldGroups {
				if t, ok := fieldGroups[groupName][required]; ok {
					fieldType, found = t, true
					break
				}
			}
			switch {
			case !found:
				errs = append(errs, fmt.Errorf("state %q requires field %q which is not in any of its field groups", state.Name, required))
			case !strings.HasSuffix(fieldType, "?"):
				errs = append(errs, fmt.Errorf("state %q requires field %q which is not optional (only \"?\"-suffixed fields can be required per-state)", state.Name, required))
			case fieldType != "string?":
				errs = append(errs, fmt.Errorf("state %q requires field %q of type %q (only string? fields support per-state requires)", state.Name, required, fieldType))
			}
		}

		// Validate state constraints
		if state.Terminal && state.Initial {
			errs = append(errs, fmt.Errorf("state %q cannot be both initial and terminal", state.Name))
		}
	}

	// Exactly one initial state required
	if initialCount == 0 {
		errs = append(errs, fmt.Errorf("exactly one initial state is required, found none"))
	} else if initialCount > 1 {
		errs = append(errs, fmt.Errorf("exactly one initial state is required, found %d", initialCount))
	}

	return errs
}

func validateTransitions(transitions []Transition, stateNames map[string]bool, guards map[string]Guard) []error {
	var errs []error
	actionNames := make(map[string]bool)

	for i, t := range transitions {
		// Validate source states exist
		for _, from := range t.From.States {
			if !stateNames[from] {
				errs = append(errs, fmt.Errorf("transition %d: source state %q does not exist", i+1, from))
			}
		}

		// Validate destination state exists
		if !stateNames[t.To] {
			errs = append(errs, fmt.Errorf("transition %d: destination state %q does not exist", i+1, t.To))
		}

		// Validate action name
		if t.Action == "" {
			errs = append(errs, fmt.Errorf("transition %d: action name is required", i+1))
		} else {
			if err := CheckActionName(t.Action); err != nil {
				errs = append(errs, err)
			}

			// Check for duplicate action names (within same source state)
			for _, from := range t.From.States {
				key := from + "." + t.Action
				if actionNames[key] {
					errs = append(errs, fmt.Errorf("duplicate action %q from state %q", t.Action, from))
				}
				actionNames[key] = true
			}
		}

		// Validate transition parameters
		for _, param := range t.Params {
			if err := CheckTransitionParamName(param.Name, t.Action); err != nil {
				errs = append(errs, err)
			}
		}

		// Validate guard reference
		if t.Guard != "" {
			if _, ok := guards[t.Guard]; !ok {
				errs = append(errs, fmt.Errorf("transition %q references undefined guard %q", t.Action, t.Guard))
			}
		}

		// Validate trigger
		if t.Trigger != "" && t.Trigger != "deletionTimestamp" {
			errs = append(errs, fmt.Errorf("transition %q: unknown trigger %q (only 'deletionTimestamp' is supported)", t.Action, t.Trigger))
		}
	}

	return errs
}

func validateGuards(guards map[string]Guard) []error {
	var errs []error

	for name, guard := range guards {
		if err := CheckGuardName(name); err != nil {
			errs = append(errs, err)
		}

		// Validate MaxRetries
		if guard.MaxRetries < 0 {
			errs = append(errs, fmt.Errorf("guard %q: maxRetries must be non-negative", name))
		}

		// Validate MinBackoff
		if guard.MinBackoff.Duration < 0 {
			errs = append(errs, fmt.Errorf("guard %q: minBackoff must be non-negative", name))
		}

		// Note: Condition validation is deferred to compile time since it's a Go expression
	}

	return errs
}

func validateDependencies(deps []Dependency) []error {
	var errs []error
	depNames := make(map[string]bool)

	for i, dep := range deps {
		// Check for duplicate names
		if depNames[dep.Name] {
			errs = append(errs, fmt.Errorf("duplicate dependency name: %q", dep.Name))
		}
		depNames[dep.Name] = true

		// Validate required fields
		if dep.Name == "" {
			errs = append(errs, fmt.Errorf("dependency %d: name is required", i+1))
		}
		if dep.Resource == "" {
			errs = append(errs, fmt.Errorf("dependency %q: resource is required", dep.Name))
		}
		if dep.Group == "" {
			errs = append(errs, fmt.Errorf("dependency %q: group is required", dep.Name))
		}
	}

	return errs
}

func validateStructuralConstraints(sm *StateMachine) []error {
	var errs []error

	// Build a map of outgoing transitions per state
	outgoing := make(map[string][]string)
	for _, t := range sm.Transitions {
		for _, from := range t.From.States {
			outgoing[from] = append(outgoing[from], t.To)
		}
	}

	// Check terminal states have no outgoing transitions (except to deletion states)
	for _, state := range sm.States {
		if state.Terminal && !state.Deletion {
			for _, to := range outgoing[state.Name] {
				// Find the target state
				for _, s := range sm.States {
					if s.Name == to && !s.Deletion && !s.Error {
						errs = append(errs, fmt.Errorf("terminal state %q has outgoing transition to non-deletion/non-error state %q", state.Name, to))
					}
				}
			}
		}
	}

	// Check that there's at least one non-error, non-deletion terminal state
	hasTerminal := false
	for _, state := range sm.States {
		if state.Terminal && !state.Error && !state.Deletion {
			hasTerminal = true
			break
		}
	}
	if !hasTerminal {
		// This is a warning, not an error - some state machines may not have terminal states
	}

	// Check deletion states form a valid chain to a terminal state
	deletionStates := make(map[string]bool)
	for _, state := range sm.States {
		if state.Deletion {
			deletionStates[state.Name] = true
		}
	}

	// If there are deletion states, verify at least one is terminal
	if len(deletionStates) > 0 {
		hasTerminalDeletion := false
		for _, state := range sm.States {
			if state.Deletion && state.Terminal {
				hasTerminalDeletion = true
				break
			}
		}
		if !hasTerminalDeletion {
			errs = append(errs, fmt.Errorf("deletion states exist but none are terminal"))
		}
	}

	return errs
}

func collectStateNames(states []State) map[string]bool {
	names := make(map[string]bool)
	for _, s := range states {
		names[s.Name] = true
	}
	return names
}

// ValidateAndParse parses and validates a state machine YAML file.
func ValidateAndParse(path string) (*StateMachine, error) {
	sm, err := ParseFile(path)
	if err != nil {
		return nil, err
	}

	if err := Validate(sm); err != nil {
		return nil, err
	}

	return sm, nil
}
