package schema_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

func TestParse(t *testing.T) {
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
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if sm.Metadata.Name != "TestResource" {
		t.Errorf("Expected name 'TestResource', got %q", sm.Metadata.Name)
	}

	if len(sm.States) != 2 {
		t.Errorf("Expected 2 states, got %d", len(sm.States))
	}

	if len(sm.Transitions) != 1 {
		t.Errorf("Expected 1 transition, got %d", len(sm.Transitions))
	}
}

func TestValidate_ValidMachine(t *testing.T) {
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
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("Validate failed: %v", err)
	}
}

func TestValidate_MissingInitialState(t *testing.T) {
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
		t.Fatal("Expected validation error for missing initial state")
	}

	if !strings.Contains(err.Error(), "initial state") {
		t.Errorf("Expected error about initial state, got: %v", err)
	}
}

func TestValidate_MultipleInitialStates(t *testing.T) {
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
  - name: Other
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
		t.Fatal("Expected validation error for multiple initial states")
	}

	if !strings.Contains(err.Error(), "initial state") {
		t.Errorf("Expected error about initial state, got: %v", err)
	}
}

func TestValidate_ReservedFieldName(t *testing.T) {
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
    fields:
      Phase: string
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
		t.Fatal("Expected validation error for reserved field name 'Phase'")
	}

	if !strings.Contains(err.Error(), "Phase") {
		t.Errorf("Expected error about 'Phase', got: %v", err)
	}
}

func TestValidate_ReservedStateName(t *testing.T) {
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
  - name: Unknown
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
		t.Fatal("Expected validation error for reserved state name 'Unknown'")
	}

	if !strings.Contains(err.Error(), "Unknown") {
		t.Errorf("Expected error about 'Unknown', got: %v", err)
	}
}

func TestValidate_UndefinedFieldGroup(t *testing.T) {
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
    fieldGroups: [nonexistent]
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
		t.Fatal("Expected validation error for undefined field group")
	}

	if !strings.Contains(err.Error(), "nonexistent") {
		t.Errorf("Expected error about 'nonexistent', got: %v", err)
	}
}

func TestValidate_UndefinedTransitionState(t *testing.T) {
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
    to: NonExistent
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("Expected validation error for undefined destination state")
	}

	if !strings.Contains(err.Error(), "NonExistent") {
		t.Errorf("Expected error about 'NonExistent', got: %v", err)
	}
}

func TestValidate_UndefinedGuard(t *testing.T) {
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
    guard: nonexistent
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	err = schema.Validate(sm)
	if err == nil {
		t.Fatal("Expected validation error for undefined guard")
	}

	if !strings.Contains(err.Error(), "nonexistent") {
		t.Errorf("Expected error about 'nonexistent', got: %v", err)
	}
}

func TestValidate_DuplicateStateName(t *testing.T) {
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
  - name: Pending
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
		t.Fatal("Expected validation error for duplicate state name")
	}

	if !strings.Contains(err.Error(), "duplicate") {
		t.Errorf("Expected error about 'duplicate', got: %v", err)
	}
}

func TestValidate_TransitionFromMultipleStates(t *testing.T) {
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
  - name: Creating
  - name: Ready
    terminal: true
  - name: Failed
    error: true
transitions:
  - from: Pending
    to: Creating
    action: StartCreation
  - from: Creating
    to: Ready
    action: MarkReady
  - from: [Pending, Creating]
    to: Failed
    action: MarkFailed
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("Validate failed: %v", err)
	}

	// Verify the multi-source transition was parsed correctly
	found := false
	for _, tr := range sm.Transitions {
		if tr.Action == "MarkFailed" {
			if len(tr.From.States) != 2 {
				t.Errorf("Expected 2 source states for MarkFailed, got %d", len(tr.From.States))
			}
			found = true
		}
	}
	if !found {
		t.Error("MarkFailed transition not found")
	}
}

func TestValidate_FieldGroups(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
fieldGroups:
  commonData:
    resourceID: string
    resourceName: string
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
    fieldGroups: [commonData]
    fields:
      extraField: int
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("Validate failed: %v", err)
	}

	// Verify field group resolution
	var readyState schema.State
	for _, s := range sm.States {
		if s.Name == "Ready" {
			readyState = s
			break
		}
	}

	resolved := readyState.Resolve(sm.FieldGroups)
	if len(resolved.AllFields) != 3 {
		t.Errorf("Expected 3 total fields, got %d: %v", len(resolved.AllFields), resolved.AllFields)
	}

	if _, ok := resolved.AllFields["resourceID"]; !ok {
		t.Error("Expected 'resourceID' from field group")
	}

	if _, ok := resolved.AllFields["extraField"]; !ok {
		t.Error("Expected 'extraField' from direct fields")
	}
}

func TestValidate_GoKeywordAsStateName(t *testing.T) {
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
  - name: func
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
		t.Fatal("Expected validation error for Go keyword as state name")
	}

	if !strings.Contains(err.Error(), "func") {
		t.Errorf("Expected error about 'func', got: %v", err)
	}
}

func TestValidate_LowercaseMetadataName(t *testing.T) {
	yaml := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: testResource
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
		t.Fatal("Expected validation error for lowercase metadata name")
	}

	if !strings.Contains(err.Error(), "uppercase") {
		t.Errorf("Expected error about uppercase, got: %v", err)
	}
}

func TestValidate_RequeueDuration(t *testing.T) {
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
    requeue: 5s
  - name: Creating
    requeue: 1m30s
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Creating
    action: StartCreation
  - from: Creating
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("Validate failed: %v", err)
	}

	// Verify durations were parsed
	for _, s := range sm.States {
		switch s.Name {
		case "Pending":
			if s.Requeue.Seconds() != 5 {
				t.Errorf("Expected Pending requeue 5s, got %v", s.Requeue)
			}
		case "Creating":
			if s.Requeue.Seconds() != 90 {
				t.Errorf("Expected Creating requeue 90s, got %v", s.Requeue)
			}
		}
	}
}

func TestValidate_ErrorHandling(t *testing.T) {
	tests := []struct {
		name          string
		errorHandling string
		wantErr       string
	}{
		{
			name: "ValidConfig",
			errorHandling: `errorHandling:
  maxRetries: 5
  backoff:
    base: 1s
    multiplier: 2
    max: 1m
    jitter: 0.1`,
			wantErr: "",
		},
		{
			name: "ValidConfigZeroJitter",
			errorHandling: `errorHandling:
  backoff:
    jitter: 0.0`,
			wantErr: "",
		},
		{
			name: "ValidConfigMaxJitter",
			errorHandling: `errorHandling:
  backoff:
    jitter: 1.0`,
			wantErr: "",
		},
		{
			name: "ValidConfigPartial_OnlyMaxRetries",
			errorHandling: `errorHandling:
  maxRetries: 3`,
			wantErr: "",
		},
		{
			name: "ValidConfigPartial_OnlyBackoff",
			errorHandling: `errorHandling:
  backoff:
    base: 2s
    max: 10m`,
			wantErr: "",
		},
		{
			name: "ValidConfigZeroMaxRetries",
			errorHandling: `errorHandling:
  maxRetries: 0`,
			wantErr: "",
		},
		{
			name: "InvalidMaxRetries",
			errorHandling: `errorHandling:
  maxRetries: -1`,
			wantErr: "maxRetries must be non-negative",
		},
		{
			name: "InvalidBase",
			errorHandling: `errorHandling:
  backoff:
    base: -1s`,
			wantErr: "backoff.base must be non-negative",
		},
		{
			name: "InvalidMax",
			errorHandling: `errorHandling:
  backoff:
    max: -5m`,
			wantErr: "backoff.max must be non-negative",
		},
		{
			name: "InvalidMultiplier",
			errorHandling: `errorHandling:
  backoff:
    multiplier: -1`,
			wantErr: "backoff.multiplier must be non-negative",
		},
		{
			name: "InvalidJitterHigh",
			errorHandling: `errorHandling:
  backoff:
    jitter: 1.5`,
			wantErr: "jitter must be between 0.0 and 1.0",
		},
		{
			name: "InvalidJitterLow",
			errorHandling: `errorHandling:
  backoff:
    jitter: -0.1`,
			wantErr: "jitter must be between 0.0 and 1.0",
		},
	}

	template := `
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
%s
`

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			yamlStr := fmt.Sprintf(template, tt.errorHandling)
			sm, err := schema.Parse([]byte(yamlStr))
			if err != nil {
				t.Fatalf("Parse failed: %v", err)
			}

			err = schema.Validate(sm)
			if tt.wantErr == "" {
				if err != nil {
					t.Errorf("Validate() unexpected error: %v", err)
				}
			} else {
				if err == nil {
					t.Error("Validate() expected error, got nil")
				} else if !strings.Contains(err.Error(), tt.wantErr) {
					t.Errorf("Validate() error = %v, want substring %q", err, tt.wantErr)
				}
			}
		})
	}
}

func TestValidate_NoErrorHandling(t *testing.T) {
	// Verify that state machines without errorHandling section still work
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
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("Validate failed: %v", err)
	}

	// ErrorHandling should be nil when not specified
	if sm.ErrorHandling != nil {
		t.Error("Expected ErrorHandling to be nil when not specified in YAML")
	}
}

func TestValidate_ErrorHandlingParsedValues(t *testing.T) {
	// Verify that parsed error handling values are correct
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
errorHandling:
  maxRetries: 7
  backoff:
    base: 2s
    multiplier: 1.5
    max: 3m
    jitter: 0.25
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("Validate failed: %v", err)
	}

	if sm.ErrorHandling == nil {
		t.Fatal("Expected ErrorHandling to be non-nil")
	}

	if sm.ErrorHandling.MaxRetries != 7 {
		t.Errorf("Expected MaxRetries=7, got %d", sm.ErrorHandling.MaxRetries)
	}

	if sm.ErrorHandling.Backoff.Base.Seconds() != 2 {
		t.Errorf("Expected Base=2s, got %v", sm.ErrorHandling.Backoff.Base)
	}

	if sm.ErrorHandling.Backoff.Multiplier != 1.5 {
		t.Errorf("Expected Multiplier=1.5, got %v", sm.ErrorHandling.Backoff.Multiplier)
	}

	if sm.ErrorHandling.Backoff.Max.Minutes() != 3 {
		t.Errorf("Expected Max=3m, got %v", sm.ErrorHandling.Backoff.Max)
	}

	if sm.ErrorHandling.Backoff.Jitter != 0.25 {
		t.Errorf("Expected Jitter=0.25, got %v", sm.ErrorHandling.Backoff.Jitter)
	}
}

func TestValidate_SpecChangeHandling(t *testing.T) {
	tests := []struct {
		name               string
		specChangeHandling string
		wantEnabled        bool
		wantField          string
	}{
		{
			name:               "Enabled with default field",
			specChangeHandling: "specChangeHandling:\n  enabled: true",
			wantEnabled:        true,
			wantField:          "", // Will be empty in schema, generator applies default
		},
		{
			name:               "Enabled with custom field",
			specChangeHandling: "specChangeHandling:\n  enabled: true\n  observedGenerationField: lastSeenGen",
			wantEnabled:        true,
			wantField:          "lastSeenGen",
		},
		{
			name:               "Disabled explicitly",
			specChangeHandling: "specChangeHandling:\n  enabled: false",
			wantEnabled:        false,
			wantField:          "",
		},
	}

	template := `
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
%s
`

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			yamlStr := fmt.Sprintf(template, tt.specChangeHandling)
			sm, err := schema.Parse([]byte(yamlStr))
			if err != nil {
				t.Fatalf("Parse failed: %v", err)
			}

			if err := schema.Validate(sm); err != nil {
				t.Fatalf("Validate failed: %v", err)
			}

			if sm.SpecChangeHandling == nil {
				t.Fatal("SpecChangeHandling should not be nil")
			}

			if sm.SpecChangeHandling.Enabled != tt.wantEnabled {
				t.Errorf("Expected Enabled=%v, got %v", tt.wantEnabled, sm.SpecChangeHandling.Enabled)
			}

			if sm.SpecChangeHandling.ObservedGenerationField != tt.wantField {
				t.Errorf("Expected ObservedGenerationField=%q, got %q", tt.wantField, sm.SpecChangeHandling.ObservedGenerationField)
			}
		})
	}
}

func TestValidate_NoSpecChangeHandling(t *testing.T) {
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
`
	sm, err := schema.Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("Validate failed: %v", err)
	}

	// SpecChangeHandling should be nil when not specified
	if sm.SpecChangeHandling != nil {
		t.Error("Expected SpecChangeHandling to be nil when not specified in YAML")
	}
}

func TestValidate_EnhancedGuards(t *testing.T) {
	tests := []struct {
		name    string
		guards  string
		wantErr string
	}{
		{
			name: "ValidGuardWithMaxRetries",
			guards: `guards:
  retryable:
    description: "Can retry if under limit"
    maxRetries: 5`,
			wantErr: "",
		},
		{
			name: "ValidGuardWithMinBackoff",
			guards: `guards:
  cooldown:
    description: "Wait before retry"
    minBackoff: 30s`,
			wantErr: "",
		},
		{
			name: "ValidGuardWithCondition",
			guards: `guards:
  hasReplicas:
    description: "Has replicas configured"
    condition: "r.Spec.Replicas > 0"`,
			wantErr: "",
		},
		{
			name: "ValidGuardCombined",
			guards: `guards:
  complexGuard:
    description: "Multiple conditions"
    maxRetries: 3
    minBackoff: 10s
    condition: "s.RetryCount < 5"`,
			wantErr: "",
		},
		{
			name: "InvalidNegativeMaxRetries",
			guards: `guards:
  invalid:
    maxRetries: -1`,
			wantErr: "maxRetries must be non-negative",
		},
		{
			name: "InvalidNegativeMinBackoff",
			guards: `guards:
  invalid:
    minBackoff: -5s`,
			wantErr: "minBackoff must be non-negative",
		},
	}

	template := `
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
    action: MarkFailed
%s
`

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			yamlStr := fmt.Sprintf(template, tt.guards)
			sm, err := schema.Parse([]byte(yamlStr))
			if err != nil {
				t.Fatalf("Parse failed: %v", err)
			}

			err = schema.Validate(sm)
			if tt.wantErr == "" {
				if err != nil {
					t.Errorf("Validate() unexpected error: %v", err)
				}
			} else {
				if err == nil {
					t.Error("Validate() expected error, got nil")
				} else if !strings.Contains(err.Error(), tt.wantErr) {
					t.Errorf("Validate() error = %v, want substring %q", err, tt.wantErr)
				}
			}
		})
	}
}
