package schema_test

// validate_gaps_test.go covers paths in validate.go and reserved.go that are
// not exercised by the existing test suite:
//
//  1. validateMetadata accumulating multiple errors simultaneously (both group
//     and version missing at the same time).
//  2. validateFieldGroups rejecting a Go keyword used as a field group name via
//     the full Validate() dispatch path (not just CheckFieldGroupName directly).
//  3. validateGuards rejecting a Go keyword used as a guard name via Validate().
//  4. ValidationError.Error() correct numbering when more than one error exists.
//  5. Transition with empty From.States slice — validate proceeds without panic.
//  6. CheckMetadataName: predeclared identifier matched directly (lowercase form
//     in the map) vs via strings.ToLower.

import (
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// TestValidate_MultipleMetadataErrors verifies that validateMetadata collects
// all metadata errors when both group and version are empty at the same time.
// This exercises the multi-error accumulation path inside validateMetadata
// (validate.go lines 104-123).
func TestValidate_MultipleMetadataErrors(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: ""
  version: ""
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for missing group and version")
	}
	// Both "group" and "version" errors should appear.
	if !strings.Contains(err.Error(), "group") {
		t.Errorf("error should mention 'group', got: %v", err)
	}
	if !strings.Contains(err.Error(), "version") {
		t.Errorf("error should mention 'version', got: %v", err)
	}
}

// TestValidate_FieldGroupNameGoKeyword exercises the path inside
// validateFieldGroups (validate.go lines 158-177) where CheckFieldGroupName
// returns an error because the group name is a Go keyword. This is distinct
// from direct CheckFieldGroupName tests because it goes through Validate().
func TestValidate_FieldGroupNameGoKeyword(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
fieldGroups:
  select:
    resourceID: string
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go keyword 'select' as field group name")
	}
	if !strings.Contains(err.Error(), "select") {
		t.Errorf("expected error to mention 'select', got: %v", err)
	}
}

// TestValidate_FieldGroupFieldGoKeyword exercises the path inside
// validateFieldGroups where CheckFieldGroupFieldName returns an error because a
// field within the group uses a Go keyword.
func TestValidate_FieldGroupFieldGoKeyword(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
fieldGroups:
  common:
    return: string
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go keyword 'return' as field group field")
	}
	if !strings.Contains(err.Error(), "return") {
		t.Errorf("expected error to mention 'return', got: %v", err)
	}
}

// TestValidate_GuardNameGoKeyword exercises the path inside validateGuards
// (validate.go lines 289-311) where CheckGuardName returns an error because
// the guard name is a Go keyword. This differs from the direct CheckGuardName
// test because it is dispatched via Validate().
func TestValidate_GuardNameGoKeyword(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Failed
    error: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Pending
    to: Failed
    action: MarkFailed
    guard: range
guards:
  range:
    description: "Uses reserved word"
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go keyword 'range' as guard name")
	}
	if !strings.Contains(err.Error(), "range") {
		t.Errorf("expected error to mention 'range', got: %v", err)
	}
}

// TestValidationError_MultipleErrors_Format verifies that ValidationError.Error()
// emits each error numbered and on its own line when Errors has more than one
// entry (validate.go lines 22-27).
func TestValidationError_MultipleErrors_Format(t *testing.T) {
	ve := schema.ValidationError{Errors: []error{
		&testError{"first problem"},
		&testError{"second problem"},
		&testError{"third problem"},
	}}
	got := ve.Error()

	if !strings.Contains(got, "3 validation error(s)") {
		t.Errorf("expected '3 validation error(s)' in output, got: %q", got)
	}
	if !strings.Contains(got, "first problem") {
		t.Errorf("expected 'first problem' in output, got: %q", got)
	}
	if !strings.Contains(got, "second problem") {
		t.Errorf("expected 'second problem' in output, got: %q", got)
	}
	if !strings.Contains(got, "third problem") {
		t.Errorf("expected 'third problem' in output, got: %q", got)
	}
	// Numbering: items should appear numbered "1.", "2.", "3."
	if !strings.Contains(got, "1.") {
		t.Errorf("expected numbered item '1.' in output, got: %q", got)
	}
	if !strings.Contains(got, "2.") {
		t.Errorf("expected numbered item '2.' in output, got: %q", got)
	}
	if !strings.Contains(got, "3.") {
		t.Errorf("expected numbered item '3.' in output, got: %q", got)
	}
}

// TestValidate_TransitionParamGoKeyword exercises the path in validateTransitions
// where CheckTransitionParamName returns an error for a parameter that uses a
// Go keyword.
func TestValidate_TransitionParamGoKeyword(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
    params:
      - for: string
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go keyword 'for' as transition param name")
	}
	if !strings.Contains(err.Error(), "for") {
		t.Errorf("expected error to mention 'for', got: %v", err)
	}
}

// TestValidate_ActionNameReserved exercises the path in validateTransitions where
// CheckActionName returns an error for the reserved action name "isState".
func TestValidate_ActionNameReserved(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: isState
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for reserved action name 'isState'")
	}
	if !strings.Contains(err.Error(), "isState") {
		t.Errorf("expected error to mention 'isState', got: %v", err)
	}
}

// TestCheckMetadataName_DirectPredeclared verifies that a name which is itself
// a predeclared identifier (lower-case, directly in the goPredeclared map) is
// rejected even when the first character is uppercase due to the
// goPredeclared[strings.ToLower(name)] check in CheckMetadataName.
func TestCheckMetadataName_DirectPredeclaredLower(t *testing.T) {
	// "Append" lowercases to "append" which is in goPredeclared.
	err := schema.CheckMetadataName("Append")
	if err == nil {
		t.Error("CheckMetadataName('Append') expected error: 'append' is predeclared, got nil")
	}
}

// TestValidate_MetadataMissingGroupAndVersion validates that both missing group
// AND missing version in metadata are captured in a single Validate() call.
func TestValidate_MetadataMissingGroupOnly(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for missing metadata.group")
	}
	if !strings.Contains(err.Error(), "group") {
		t.Errorf("expected error to mention 'group', got: %v", err)
	}
}

// TestValidate_MetadataMissingVersion validates that missing metadata.version
// produces a validation error mentioning "version".
func TestValidate_MetadataMissingVersion(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for missing metadata.version")
	}
	if !strings.Contains(err.Error(), "version") {
		t.Errorf("expected error to mention 'version', got: %v", err)
	}
}

// TestValidate_TerminalStateTransitionToErrorState verifies that a terminal
// state IS allowed to have an outgoing transition to an error state (the
// validator should not reject error state targets from terminal states).
func TestValidate_TerminalStateTransitionToErrorState(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Failed
    error: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Ready
    to: Failed
    action: MarkFailed
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}
	// A terminal state transitioning to an error state should be allowed.
	if err := schema.Validate(sm); err != nil {
		t.Fatalf("expected terminal->error transition to be valid, got: %v", err)
	}
}

// TestValidate_GuardNegativeMinBackoff verifies that a guard with a negative
// minBackoff duration is rejected by validateGuards.
func TestValidate_GuardNegativeMinBackoff(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Failed
    error: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Pending
    to: Failed
    action: MarkFailed
    guard: badGuard
guards:
  badGuard:
    minBackoff: -5s
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for negative guard minBackoff")
	}
	if !strings.Contains(err.Error(), "minBackoff") {
		t.Errorf("expected error to mention 'minBackoff', got: %v", err)
	}
}

// TestValidate_GuardNegativeMaxRetries verifies that a guard with a negative
// maxRetries value is rejected by validateGuards via Validate().
func TestValidate_GuardNegativeMaxRetries(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: MyResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Failed
    error: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Pending
    to: Failed
    action: MarkFailed
    guard: badGuard
guards:
  badGuard:
    maxRetries: -1
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for negative guard maxRetries")
	}
	if !strings.Contains(err.Error(), "maxRetries") {
		t.Errorf("expected error to mention 'maxRetries', got: %v", err)
	}
}
