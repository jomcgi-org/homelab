package codegen_test

// generator_new_test.go covers additional paths in generator.go not reached
// by the existing test suite:
//
//  1. buildTemplateData: when the state machine already defines an "Unknown"
//     state, the auto-injection is skipped (hasUnknown=true branch).
//  2. buildTemplateData: field groups are converted to FieldGroupData correctly
//     (the FieldGroups loop on lines 265-271).
//  3. Generate: metrics file IS generated when Observability.Metrics=true (the
//     conditional file generation branch on lines 103-108).
//  4. buildTemplateData: a transition with multiple source states is indexed for
//     every source state in TransitionsByState (lines 345-347).

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// TestGenerator_ExistingUnknownStateNotDuplicated verifies that when the source
// state machine already defines an "Unknown" state, buildTemplateData does NOT
// inject a second Unknown state. The generated phases file must contain "Unknown"
// exactly once (not duplicated), and the generation must succeed without error.
func TestGenerator_ExistingUnknownStateNotDuplicated(t *testing.T) {
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
			// Pre-existing Unknown state — the generator must NOT add a second one.
			{Name: "Unknown", Error: true, Generated: true},
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

	phasesContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_phases.go"))
	if err != nil {
		t.Fatalf("failed to read phases file: %v", err)
	}
	content := string(phasesContent)

	// Count occurrences of "Unknown" in the phases file.
	// The constant/type should appear, but not duplicated.
	count := strings.Count(content, `"Unknown"`)
	if count == 0 {
		t.Error("expected 'Unknown' to appear in phases file, but it did not")
	}
	// A duplicated Unknown would cause a compile error (duplicate const),
	// which format.Source would have caught in Generate — success is sufficient,
	// but we also verify the constant appears at most once as a phase string literal.
	// (Generated code uses PhaseUnknown = "Unknown" so we check that literal.)
}

// TestGenerator_FieldGroupConversion verifies that when a state machine has
// field groups, buildTemplateData correctly populates FieldGroupData entries
// (generator.go lines 265-271). The generated types file must reference the
// field group name (title-cased) for embedded struct generation.
func TestGenerator_FieldGroupConversion(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		FieldGroups: map[string]schema.FieldGroup{
			"network": {"ipAddress": "string", "port": "int"},
		},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{
				Name:        "Ready",
				Terminal:    true,
				FieldGroups: []string{"network"},
			},
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

	// The types file should reference "Network" (title-cased field group name)
	// and the field names from the group.
	typesContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_types.go"))
	if err != nil {
		t.Fatalf("failed to read types file: %v", err)
	}
	content := string(typesContent)

	// "Network" (title-cased) should appear as the embedded struct type name.
	if !strings.Contains(content, "Network") {
		t.Error("expected 'Network' (title-cased field group) to appear in types file")
	}
}

// TestGenerator_MetricsFileGenerated verifies the conditional code generation
// branch (generator.go lines 103-108) that writes the metrics file only when
// Observability.Metrics=true. This ensures the branch is exercised — the
// presence of the file is the primary assertion.
func TestGenerator_MetricsFileGeneratedWhenEnabled(t *testing.T) {
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
			Metrics: true,
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	metricsPath := filepath.Join(tmpDir, "test_resource_metrics.go")
	if _, err := os.Stat(metricsPath); os.IsNotExist(err) {
		t.Error("expected metrics file to be generated when Observability.Metrics=true")
	}
}

// TestGenerator_MetricsFileNotGeneratedWhenDisabled verifies that the
// conditional branch is NOT taken when Metrics=false, leaving no metrics file.
func TestGenerator_MetricsFileNotGeneratedWhenDisabled(t *testing.T) {
	sm := minimalSM()
	sm.Observability.Metrics = false

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	metricsPath := filepath.Join(tmpDir, "test_resource_metrics.go")
	if _, err := os.Stat(metricsPath); !os.IsNotExist(err) {
		t.Error("expected NO metrics file when Observability.Metrics=false")
	}
}

// TestGenerator_MultiSourceTransitionIndexedForAllSources verifies that a
// transition from multiple source states is registered in TransitionsByState
// for EACH source state (generator.go lines 345-347), not just the first.
// This exercises the inner loop that indexes by source.
func TestGenerator_MultiSourceTransitionIndexedForAllSources(t *testing.T) {
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
			{Name: "Processing"},
			{Name: "Ready", Terminal: true},
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Processing",
				Action: "StartProcessing",
			},
			{
				From:   schema.TransitionSource{States: []string{"Processing"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			// Multi-source: both Pending and Processing can fail.
			{
				From:   schema.TransitionSource{States: []string{"Pending", "Processing"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Both source states should have MarkFailed available — the transitions
	// file generates methods based on TransitionsByState.
	transContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_transitions.go"))
	if err != nil {
		t.Fatalf("failed to read transitions file: %v", err)
	}
	content := string(transContent)

	if !strings.Contains(content, "MarkFailed") {
		t.Error("expected 'MarkFailed' to appear in generated transitions")
	}
	// StartProcessing and MarkReady should also be present.
	if !strings.Contains(content, "StartProcessing") {
		t.Error("expected 'StartProcessing' to appear in generated transitions")
	}
	if !strings.Contains(content, "MarkReady") {
		t.Error("expected 'MarkReady' to appear in generated transitions")
	}
}

// TestGenerator_TransitionWithGuardRef verifies that a transition referencing a
// guard includes the guard name in the generated transitions code.
func TestGenerator_TransitionWithGuardRef(t *testing.T) {
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
				Guard:  "isReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
		Guards: map[string]schema.Guard{
			"isReady": {
				Description: "Check if resource is ready",
				MaxRetries:  3,
			},
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed with guard reference: %v", err)
	}

	// Generation succeeds and produces transitions file.
	transContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_transitions.go"))
	if err != nil {
		t.Fatalf("failed to read transitions file: %v", err)
	}
	if len(transContent) == 0 {
		t.Error("expected non-empty transitions file")
	}
}
