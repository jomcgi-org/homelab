package schema

import (
	"fmt"
	"strings"
)

// reservedFieldNames contains field names that cannot be used in state definitions
// because they would collide with generated methods.
var reservedFieldNames = map[string]string{
	"Phase":        "Phase() method is generated for all states",
	"Resource":     "Resource() method is generated for all states",
	"Validate":     "Validate() method is generated for state validation",
	"ApplyStatus":  "ApplyStatus() method is generated for SSA",
	"RequeueAfter": "RequeueAfter() method is generated for all states",
}

// reservedStateNames contains state names that have special meaning.
var reservedStateNames = map[string]string{
	"Unknown": "Unknown is auto-generated for unrecognized phases",
}

// reservedActionNames contains action names that cannot be used.
var reservedActionNames = map[string]string{
	"isState": "isState is used for sealed interface implementation",
}

// goKeywords contains Go language keywords that cannot be used as identifiers.
var goKeywords = map[string]bool{
	"break":       true,
	"case":        true,
	"chan":        true,
	"const":       true,
	"continue":    true,
	"default":     true,
	"defer":       true,
	"else":        true,
	"fallthrough": true,
	"for":         true,
	"func":        true,
	"go":          true,
	"goto":        true,
	"if":          true,
	"import":      true,
	"interface":   true,
	"map":         true,
	"package":     true,
	"range":       true,
	"return":      true,
	"select":      true,
	"struct":      true,
	"switch":      true,
	"type":        true,
	"var":         true,
}

// goPredeclared contains predeclared Go identifiers that should be avoided.
var goPredeclared = map[string]bool{
	"bool":       true,
	"byte":       true,
	"complex64":  true,
	"complex128": true,
	"error":      true,
	"float32":    true,
	"float64":    true,
	"int":        true,
	"int8":       true,
	"int16":      true,
	"int32":      true,
	"int64":      true,
	"rune":       true,
	"string":     true,
	"uint":       true,
	"uint8":      true,
	"uint16":     true,
	"uint32":     true,
	"uint64":     true,
	"uintptr":    true,
	"true":       true,
	"false":      true,
	"iota":       true,
	"nil":        true,
	"append":     true,
	"cap":        true,
	"close":      true,
	"complex":    true,
	"copy":       true,
	"delete":     true,
	"imag":       true,
	"len":        true,
	"make":       true,
	"new":        true,
	"panic":      true,
	"print":      true,
	"println":    true,
	"real":       true,
	"recover":    true,
}

// ReservedWordError indicates a reserved word was used.
type ReservedWordError struct {
	Word     string
	Location string
	Reason   string
}

func (e ReservedWordError) Error() string {
	return fmt.Sprintf("reserved word %q used in %s: %s", e.Word, e.Location, e.Reason)
}

// CheckFieldName validates that a field name is not reserved.
func CheckFieldName(name string, stateName string) error {
	if reason, reserved := reservedFieldNames[name]; reserved {
		return ReservedWordError{
			Word:     name,
			Location: fmt.Sprintf("state %q field", stateName),
			Reason:   reason,
		}
	}

	if goKeywords[name] {
		return ReservedWordError{
			Word:     name,
			Location: fmt.Sprintf("state %q field", stateName),
			Reason:   "Go keyword cannot be used as field name",
		}
	}

	return nil
}

// CheckFieldGroupName validates that a field group name is not reserved.
func CheckFieldGroupName(name string) error {
	if goKeywords[name] {
		return ReservedWordError{
			Word:     name,
			Location: "field group name",
			Reason:   "Go keyword cannot be used as field group name",
		}
	}

	return nil
}

// CheckFieldGroupFieldName validates that a field in a field group is not reserved.
func CheckFieldGroupFieldName(fieldName, groupName string) error {
	if reason, reserved := reservedFieldNames[fieldName]; reserved {
		return ReservedWordError{
			Word:     fieldName,
			Location: fmt.Sprintf("field group %q field", groupName),
			Reason:   reason,
		}
	}

	if goKeywords[fieldName] {
		return ReservedWordError{
			Word:     fieldName,
			Location: fmt.Sprintf("field group %q field", groupName),
			Reason:   "Go keyword cannot be used as field name",
		}
	}

	return nil
}

// CheckStateName validates that a state name is not reserved.
func CheckStateName(name string) error {
	if reason, reserved := reservedStateNames[name]; reserved {
		return ReservedWordError{
			Word:     name,
			Location: "state name",
			Reason:   reason,
		}
	}

	if goKeywords[name] {
		return ReservedWordError{
			Word:     name,
			Location: "state name",
			Reason:   "Go keyword cannot be used as state name",
		}
	}

	// Check against predeclared identifiers
	if goPredeclared[name] {
		return ReservedWordError{
			Word:     name,
			Location: "state name",
			Reason:   "Go predeclared identifier should not be used as state name",
		}
	}

	return nil
}

// CheckActionName validates that an action name is not reserved.
func CheckActionName(name string) error {
	if reason, reserved := reservedActionNames[name]; reserved {
		return ReservedWordError{
			Word:     name,
			Location: "action name",
			Reason:   reason,
		}
	}

	if goKeywords[name] {
		return ReservedWordError{
			Word:     name,
			Location: "action name",
			Reason:   "Go keyword cannot be used as action name",
		}
	}

	return nil
}

// CheckTransitionParamName validates that a transition parameter name is not reserved.
func CheckTransitionParamName(name, actionName string) error {
	if reason, reserved := reservedFieldNames[name]; reserved {
		return ReservedWordError{
			Word:     name,
			Location: fmt.Sprintf("transition %q parameter", actionName),
			Reason:   reason,
		}
	}

	if goKeywords[name] {
		return ReservedWordError{
			Word:     name,
			Location: fmt.Sprintf("transition %q parameter", actionName),
			Reason:   "Go keyword cannot be used as parameter name",
		}
	}

	return nil
}

// CheckGuardName validates that a guard name is not reserved.
func CheckGuardName(name string) error {
	if goKeywords[name] {
		return ReservedWordError{
			Word:     name,
			Location: "guard name",
			Reason:   "Go keyword cannot be used as guard name",
		}
	}

	return nil
}

// CheckMetadataName validates that the resource name is valid for Go code generation.
func CheckMetadataName(name string) error {
	if name == "" {
		return fmt.Errorf("metadata.name is required")
	}

	if goKeywords[name] {
		return ReservedWordError{
			Word:     name,
			Location: "metadata.name",
			Reason:   "Go keyword cannot be used as resource name",
		}
	}

	if goPredeclared[name] || goPredeclared[strings.ToLower(name)] {
		return ReservedWordError{
			Word:     name,
			Location: "metadata.name",
			Reason:   "Go predeclared identifier should not be used as resource name",
		}
	}

	// Check first character is uppercase (exported)
	if len(name) > 0 && (name[0] < 'A' || name[0] > 'Z') {
		return fmt.Errorf("metadata.name must start with an uppercase letter for Go export: %q", name)
	}

	return nil
}
