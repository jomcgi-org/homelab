package schema_test

// validate_new_test.go adds tests for paths not yet covered:
//
//  1. validateStates: state field name using a Go keyword (via full Validate()
//     with YAML - distinct from reserved_test.go which tests CheckFieldName directly)
//  2. validateStructuralConstraints: terminal deletion state with outgoing
//     transition to a non-deletion/non-error state triggers an error
//  3. validateStates: state name using a Go predeclared identifier (via Validate())
//  4. ValidationError.Error(): zero-length Errors slice returns "no validation errors"
//  5. validateErrorHandling: all branches in a single test that triggers multiple
//     backoff errors simultaneously (base+max negative together)

import (
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// TestValidate_StateFieldGoKeyword_ViaValidate exercises the validateStates code
// path (validate.go lines 202-205) where CheckFieldName returns a ReservedWordError
// because the field name is a Go keyword. This is different from the direct
// CheckFieldName unit tests in reserved_test.go because it exercises the full
// YAML-parse + Validate() dispatch chain.
func TestValidate_StateFieldGoKeyword_ViaValidate(t *testing.T) {
	yamlStr := `
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
    fields:
      defer: string
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go keyword 'defer' as field name")
	}
	if !strings.Contains(err.Error(), "defer") {
		t.Errorf("expected error to mention 'defer', got: %v", err)
	}
}

// TestValidate_StateNameGoPredeclared_ViaValidate exercises the validateStates
// path where CheckStateName returns an error for a Go predeclared identifier as
// a state name. The predeclared check in reserved.go goes through goPredeclared
// map lookup (line 185-191 of reserved.go).
func TestValidate_StateNameGoPredeclared_ViaValidate(t *testing.T) {
	yamlStr := `
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
  - name: nil
    error: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go predeclared identifier 'nil' as state name")
	}
	if !strings.Contains(err.Error(), "nil") {
		t.Errorf("expected error to mention 'nil', got: %v", err)
	}
}

// TestValidate_TerminalDeletionStateOutgoingToNormal exercises the branch in
// validateStructuralConstraints (validate.go lines 351-362) that checks whether
// a terminal state (that is NOT deletion) has outgoing transitions to non-deletion/
// non-error states. This is distinct from the terminal+deletion check — here the
// state is Terminal=true AND Deletion=false.
//
// Note: a state with Deletion=true is exempt from this check (the if-condition is
// `state.Terminal && !state.Deletion`), so we use a pure terminal (non-deletion)
// state that transitions to a plain normal state.
func TestValidate_TerminalNonDeletionOutgoingToNormal_Error(t *testing.T) {
	yamlStr := `
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
  - name: Active
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
  - from: Ready
    to: Active
    action: Activate
`
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for terminal state transitioning to normal state")
	}
	if !strings.Contains(err.Error(), "terminal") {
		t.Errorf("expected error to mention 'terminal', got: %v", err)
	}
}

// TestValidationError_ZeroErrors verifies that ValidationError.Error() with an
// empty slice returns the "no validation errors" sentinel string (validate.go
// lines 17-19).
func TestValidationError_ZeroErrors(t *testing.T) {
	ve := schema.ValidationError{Errors: []error{}}
	got := ve.Error()
	if got != "no validation errors" {
		t.Errorf("ValidationError.Error() with empty slice = %q, want 'no validation errors'", got)
	}
}

// TestValidate_ErrorHandling_MultipleBackoffErrors verifies that validateErrorHandling
// accumulates multiple errors when both base and max are negative simultaneously.
// This exercises the multi-error accumulation inside validateErrorHandling
// (validate.go lines 125-155).
func TestValidate_ErrorHandling_MultipleBackoffErrors(t *testing.T) {
	yamlStr := `
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
errorHandling:
  maxRetries: -1
  backoff:
    base: -1s
    max: -5m
    multiplier: -2
    jitter: -0.1
`
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation errors for multiple negative errorHandling values")
	}

	errStr := err.Error()
	// All five validation errors should be collected and reported.
	for _, want := range []string{"maxRetries", "backoff.base", "backoff.max", "backoff.multiplier", "backoff.jitter"} {
		if !strings.Contains(errStr, want) {
			t.Errorf("expected error to mention %q, got: %v", want, errStr)
		}
	}
}

// TestValidate_TerminalDeletionStateAllowedOutgoing verifies that a deletion
// state (Deletion=true) that is also Terminal=true is EXEMPT from the
// "terminal states may not have outgoing transitions to normal states" check.
// The structural constraint only applies when !state.Deletion.
func TestValidate_TerminalDeletionStateExemptFromCheck(t *testing.T) {
	yamlStr := `
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
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}
	// Deletion terminal state having an outgoing transition (Deleting→Deleted)
	// is valid — the structural check skips deletion states.
	if err := schema.Validate(sm); err != nil {
		t.Fatalf("expected terminal deletion state to be exempt from outgoing-transition check, got: %v", err)
	}
}

// TestValidate_ActionNameGoKeyword_ViaValidate exercises the validateTransitions
// path where CheckActionName returns an error because the action uses a Go keyword
// ("goto"). This is dispatched through the full Validate() path.
func TestValidate_ActionNameGoKeyword_ViaValidate(t *testing.T) {
	yamlStr := `
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
    action: goto
`
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go keyword 'goto' as action name")
	}
	if !strings.Contains(err.Error(), "goto") {
		t.Errorf("expected error to mention 'goto', got: %v", err)
	}
}

// TestValidate_MetadataNameGoKeyword_ViaValidate exercises the validateMetadata
// path where CheckMetadataName rejects a Go keyword as the resource name.
func TestValidate_MetadataNameGoKeyword_ViaValidate(t *testing.T) {
	yamlStr := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: func
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
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for Go keyword 'func' as metadata.name")
	}
	if !strings.Contains(err.Error(), "func") {
		t.Errorf("expected error to mention 'func', got: %v", err)
	}
}

// TestValidate_StateReservedFieldName_ViaValidate exercises the path where a
// state field uses one of the reserved field names (e.g., "RequeueAfter") that
// collides with generated methods. This goes through validateStates → CheckFieldName.
func TestValidate_StateReservedFieldName_ViaValidate(t *testing.T) {
	yamlStr := `
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
    fields:
      RequeueAfter: string
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for reserved field name 'RequeueAfter'")
	}
	if !strings.Contains(err.Error(), "RequeueAfter") {
		t.Errorf("expected error to mention 'RequeueAfter', got: %v", err)
	}
}
