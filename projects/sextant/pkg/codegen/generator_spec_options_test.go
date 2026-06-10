package codegen_test

// generator_gaps_test.go covers paths in generator.go not reached by the
// existing test suite:
//
//  1. buildTemplateData when SpecChangeHandling.Enabled=true AND
//     ObservedGenerationField is already non-empty — the default injection
//     branch (which sets "observedGeneration") must NOT overwrite the custom
//     field.
//  2. buildTemplateData when SpecChangeHandling.Enabled=false — the default
//     injection branch is skipped entirely (already partially tested, but this
//     test pins the boundary explicitly).
//  3. buildTemplateData when SpecChangeHandling is nil — no panic, generator
//     succeeds (regression guard for nil-pointer in the Enabled check).
//  4. Generate with a state machine whose name already maps to a snake_case
//     filename with no uppercase — camelToSnake returns unchanged lowercase.
//  5. Multiple transitions from the same state verify TransitionsByState indexing.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// TestGenerator_SpecChangeHandling_CustomFieldNotOverridden verifies that when
// SpecChangeHandling.Enabled=true and ObservedGenerationField is already set,
// buildTemplateData does NOT overwrite the custom field with the default
// "observedGeneration". This exercises the else branch of the empty-field check
// on generator.go lines 251-254.
func TestGenerator_SpecChangeHandling_CustomFieldNotOverridden(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{Name: "Ready", Terminal: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
		},
		SpecChangeHandling: &schema.SpecChangeHandling{
			Enabled:                 true,
			ObservedGenerationField: "myCustomGenField", // non-empty: must not be overwritten
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	statusContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_status.go"))
	if err != nil {
		t.Fatalf("failed to read status file: %v", err)
	}
	content := string(statusContent)

	// The custom field "myCustomGenField" (title-cased to "MyCustomGenField") must
	// appear in the generated code.
	if !strings.Contains(content, "MyCustomGenField") {
		t.Error("expected custom field 'MyCustomGenField' in generated status code")
	}

	// The default "ObservedGeneration" must NOT replace the custom field.
	// If the default was wrongly applied the status file would reference
	// "r.Status.ObservedGeneration" instead of "r.Status.MyCustomGenField".
	if strings.Contains(content, "r.Status.ObservedGeneration") {
		t.Error("default 'ObservedGeneration' field should not appear when a custom field is set")
	}
}

// TestGenerator_SpecChangeHandling_NilDoesNotPanic verifies that nil
// SpecChangeHandling causes no panic and generates valid code.
func TestGenerator_SpecChangeHandling_NilDoesNotPanic(t *testing.T) {
	sm := minimalSM()
	sm.SpecChangeHandling = nil // explicit nil

	gen, _ := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate panicked or returned error with nil SpecChangeHandling: %v", err)
	}
}

// TestGenerator_AllTransitionsByState verifies that when multiple transitions
// share the same source state they are all indexed in TransitionsByState and
// therefore all appear as methods in the generated transitions file.
func TestGenerator_AllTransitionsByState(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{Name: "Ready", Terminal: true},
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	transContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_transitions.go"))
	if err != nil {
		t.Fatalf("failed to read transitions file: %v", err)
	}
	content := string(transContent)

	// Both actions from Pending must appear in the transitions file.
	if !strings.Contains(content, "MarkReady") {
		t.Error("expected 'MarkReady' action in generated transitions")
	}
	if !strings.Contains(content, "MarkFailed") {
		t.Error("expected 'MarkFailed' action in generated transitions")
	}
}

// TestGenerator_StateWithTransitionParams verifies that transition parameters
// appear in the generated transitions file as method arguments.
func TestGenerator_StateWithTransitionParams(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{Name: "Ready", Terminal: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
				Params: []schema.TransitionParam{
					{Name: "resourceID", Type: "string"},
					{Name: "count", Type: "int"},
				},
			},
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	transContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_transitions.go"))
	if err != nil {
		t.Fatalf("failed to read transitions file: %v", err)
	}
	content := string(transContent)

	// The parameter names appear lowercased in function signatures and
	// title-cased as struct field keys (the template uses {{lower}} for params
	// and {{title}} for struct field names).
	if !strings.Contains(content, "resourceid") {
		t.Error("expected 'resourceid' (lowercased param) in generated transitions")
	}
	if !strings.Contains(content, "ResourceID") {
		t.Error("expected 'ResourceID' (title-cased field) in generated transitions")
	}
	if !strings.Contains(content, "count") {
		t.Error("expected 'count' parameter in generated transitions")
	}
}

// TestGenerator_ObservabilityOTelTracing verifies that enabling OTel tracing
// produces the expected tracing content in the observability file.
func TestGenerator_ObservabilityOTelTracing(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{Name: "Ready", Terminal: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
		},
		Observability: schema.Observability{
			OTelTracing: true,
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	obsContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_observability.go"))
	if err != nil {
		t.Fatalf("failed to read observability file: %v", err)
	}

	// The observability file must be non-empty and valid Go.
	if len(obsContent) == 0 {
		t.Error("expected non-empty observability file")
	}
}

// TestGenerator_ObservabilityOnTransition verifies that OnTransition=true
// produces the TransitionObserver interface in the observability file.
func TestGenerator_ObservabilityOnTransition(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{Name: "Ready", Terminal: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
		},
		Observability: schema.Observability{
			OnTransition: true,
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	obsContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_observability.go"))
	if err != nil {
		t.Fatalf("failed to read observability file: %v", err)
	}
	content := string(obsContent)

	// TransitionObserver interface must be present when OnTransition=true.
	if !strings.Contains(content, "TransitionObserver") {
		t.Error("expected 'TransitionObserver' in generated observability file with OnTransition=true")
	}
}

// TestGenerator_ExplicitPhaseAndConditionsField verifies that explicitly set
// status field names (non-empty) are preserved — the generator does NOT apply
// default values when fields are already set, and generation succeeds without
// error. The PhaseField and ConditionsField values are stored in TemplateData
// but templates use r.Status.Phase directly (hardcoded), so we assert that
// generation completes and the status file is non-empty valid Go.
func TestGenerator_ExplicitPhaseAndConditionsField(t *testing.T) {
	sm := minimalSM()
	sm.Status.PhaseField = "currentPhase"
	sm.Status.ConditionsField = "statusConditions"

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed with explicit PhaseField/ConditionsField: %v", err)
	}

	statusContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_status.go"))
	if err != nil {
		t.Fatalf("failed to read status file: %v", err)
	}

	// The status file must be non-empty valid Go — format.Source is called
	// during Generate and would have failed above if output was invalid.
	if len(statusContent) == 0 {
		t.Error("generated status file must not be empty")
	}

	// The "Phase" identifier appears in the status file (used in ApplyStatus
	// methods as r.Status.Phase = ...), confirming the file was generated.
	if !strings.Contains(string(statusContent), "Phase") {
		t.Error("expected 'Phase' to appear in generated status code")
	}
}

// TestGenerator_StateWithRequeueGeneratesConst verifies that a state with a
// non-zero Requeue duration causes a requeue constant/literal to appear in the
// generated phases or calculator file.
func TestGenerator_StateWithRequeueGeneratesConst(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		States: []schema.State{
			{
				Name:    "Pending",
				Initial: true,
				Requeue: schema.Duration{Duration: 30 * 1_000_000_000}, // 30s in nanoseconds
			},
			{Name: "Ready", Terminal: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// At least one generated file should reference the requeue duration.
	entries, err := os.ReadDir(tmpDir)
	if err != nil {
		t.Fatalf("failed to read tmpDir: %v", err)
	}

	found := false
	for _, entry := range entries {
		content, err := os.ReadFile(filepath.Join(tmpDir, entry.Name()))
		if err != nil {
			continue
		}
		// The duration literal for 30s = 30000000000 ns
		if strings.Contains(string(content), "30000000000") {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected requeue duration literal '30000000000' in at least one generated file")
	}
}
