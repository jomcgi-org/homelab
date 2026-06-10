package schema_test

import (
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// TestValidationError_NoErrors verifies that ValidationError.Error() returns
// a stable string even when the slice is empty (the zero case).
func TestValidationError_NoErrors(t *testing.T) {
	ve := schema.ValidationError{Errors: nil}
	got := ve.Error()
	if got == "" {
		t.Error("ValidationError.Error() returned empty string for nil Errors")
	}
}

// TestValidationError_SingleError verifies formatting with exactly one error.
func TestValidationError_SingleError(t *testing.T) {
	ve := schema.ValidationError{Errors: []error{
		&testError{"something went wrong"},
	}}
	got := ve.Error()
	if !strings.Contains(got, "something went wrong") {
		t.Errorf("ValidationError.Error() = %q, want it to contain %q", got, "something went wrong")
	}
}

// testError is a simple error implementation used in table tests.
type testError struct{ msg string }

func (e *testError) Error() string { return e.msg }

// TestValidate_TerminalStateWithOutgoingTransition checks that a terminal state
// transitioning to a non-deletion/non-error state is rejected.
func TestValidate_TerminalStateWithOutgoingTransition(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Active
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Ready
    to: Active
    action: Activate
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for terminal state with outgoing transition to non-deletion/non-error state")
	}
	if !strings.Contains(err.Error(), "terminal") {
		t.Errorf("error should mention 'terminal', got: %v", err)
	}
}

// TestValidate_DeletionStatesWithoutTerminal checks that deletion states require
// at least one to be terminal.
func TestValidate_DeletionStatesWithoutTerminal(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Deleting
    deletion: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Ready
    to: Deleting
    action: BeginDeletion
    trigger: deletionTimestamp
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error when deletion states exist but none are terminal")
	}
	if !strings.Contains(err.Error(), "deletion") {
		t.Errorf("error should mention 'deletion', got: %v", err)
	}
}

// TestValidate_DeletionStatesWithTerminal checks that a valid deletion chain
// (at least one terminal deletion state) passes validation.
func TestValidate_DeletionStatesWithTerminal(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Deleting
    deletion: true
  - name: Deleted
    deletion: true
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Ready
    to: Deleting
    action: BeginDeletion
    trigger: deletionTimestamp
  - from: Deleting
    to: Deleted
    action: MarkDeleted
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}
	if err := schema.Validate(sm); err != nil {
		t.Fatalf("expected valid deletion chain to pass, got: %v", err)
	}
}

// TestValidate_MissingTransitionAction checks that a transition without an
// action name produces a validation error.
func TestValidate_MissingTransitionAction(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
    action: ""
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for missing transition action")
	}
	if !strings.Contains(err.Error(), "action") {
		t.Errorf("error should mention 'action', got: %v", err)
	}
}

// TestValidate_InvalidTrigger checks that an unknown trigger value is rejected.
func TestValidate_InvalidTrigger(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
    trigger: unknownTrigger
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for unknown trigger")
	}
	if !strings.Contains(err.Error(), "trigger") {
		t.Errorf("error should mention 'trigger', got: %v", err)
	}
}

// TestValidate_ValidDeletionTimestampTrigger checks that "deletionTimestamp" is
// accepted as a transition trigger.
func TestValidate_ValidDeletionTimestampTrigger(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Deleting
    deletion: true
  - name: Deleted
    deletion: true
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Ready
    to: Deleting
    action: BeginDeletion
    trigger: deletionTimestamp
  - from: Deleting
    to: Deleted
    action: MarkDeleted
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}
	if err := schema.Validate(sm); err != nil {
		t.Fatalf("expected 'deletionTimestamp' trigger to be valid, got: %v", err)
	}
}

// TestValidate_DependencyMissingResource checks that a dependency without a
// resource field fails validation.
func TestValidate_DependencyMissingResource(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
dependencies:
  - name: myDep
    resource: ""
    group: core
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for dependency with missing resource")
	}
	if !strings.Contains(err.Error(), "resource") {
		t.Errorf("error should mention 'resource', got: %v", err)
	}
}

// TestValidate_DependencyMissingGroup checks that a dependency without a group
// field fails validation.
func TestValidate_DependencyMissingGroup(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
dependencies:
  - name: myDep
    resource: SomeResource
    group: ""
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for dependency with missing group")
	}
	if !strings.Contains(err.Error(), "group") {
		t.Errorf("error should mention 'group', got: %v", err)
	}
}

// TestValidate_DuplicateDependencyName checks that two dependencies with the
// same name are rejected.
func TestValidate_DuplicateDependencyName(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
dependencies:
  - name: myDep
    resource: SomeResource
    group: core.io
  - name: myDep
    resource: OtherResource
    group: core.io
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for duplicate dependency name")
	}
	if !strings.Contains(err.Error(), "duplicate") {
		t.Errorf("error should mention 'duplicate', got: %v", err)
	}
}

// TestValidate_DependencyMissingName checks that a dependency with an empty
// name field produces a validation error ("name is required").
func TestValidate_DependencyMissingName(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
dependencies:
  - name: ""
    resource: SomeResource
    group: core.io
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for dependency with empty name")
	}
	if !strings.Contains(err.Error(), "name") {
		t.Errorf("error should mention 'name', got: %v", err)
	}
}

// TestValidate_ValidDependency checks that a well-formed dependency passes.
func TestValidate_ValidDependency(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
dependencies:
  - name: myDep
    resource: SomeResource
    group: core.io
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}
	if err := schema.Validate(sm); err != nil {
		t.Fatalf("expected valid dependency to pass validation, got: %v", err)
	}
}

// TestValidate_MissingAPIVersion checks that omitting apiVersion is an error.
func TestValidate_MissingAPIVersion(t *testing.T) {
	yaml := `
kind: StateMachine
metadata:
  name: TestResource
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
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for missing apiVersion")
	}
	if !strings.Contains(err.Error(), "apiVersion") {
		t.Errorf("error should mention 'apiVersion', got: %v", err)
	}
}

// TestValidate_WrongKind checks that a wrong kind value produces an error.
func TestValidate_WrongKind(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: NotAStateMachine
metadata:
  name: TestResource
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
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for wrong kind")
	}
	if !strings.Contains(err.Error(), "kind") {
		t.Errorf("error should mention 'kind', got: %v", err)
	}
}

// TestValidate_InitialAndTerminalState checks that a state cannot be both initial
// and terminal.
func TestValidate_InitialAndTerminalState(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
    terminal: true
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
		t.Fatal("expected validation error for state that is both initial and terminal")
	}
	if !strings.Contains(err.Error(), "initial") && !strings.Contains(err.Error(), "terminal") {
		t.Errorf("error should mention 'initial' or 'terminal', got: %v", err)
	}
}

// TestValidate_UndefinedSourceState checks that a transition from a non-existent
// state is rejected.
func TestValidate_UndefinedSourceState(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
  - from: NonExistent
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for undefined source state")
	}
	if !strings.Contains(err.Error(), "NonExistent") {
		t.Errorf("error should mention 'NonExistent', got: %v", err)
	}
}

// TestValidate_DuplicateActionWithinSameSourceState verifies that the same
// action name cannot appear twice from the same source state.
func TestValidate_DuplicateActionWithinSameSourceState(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
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
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for duplicate action from the same source state")
	}
	if !strings.Contains(err.Error(), "duplicate") {
		t.Errorf("error should mention 'duplicate', got: %v", err)
	}
}
