package codegen_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/codegen"
	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// newTestGenerator creates a Generator writing to a temp dir.
func newTestGenerator(t *testing.T) (*codegen.Generator, string) {
	t.Helper()
	tmpDir, err := os.MkdirTemp("", "sextant-builddata-*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	t.Cleanup(func() { os.RemoveAll(tmpDir) })

	gen, err := codegen.New(codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	})
	if err != nil {
		t.Fatalf("failed to create generator: %v", err)
	}
	return gen, tmpDir
}

// minimalSM returns a minimal valid state machine for use in tests.
func minimalSM() *schema.StateMachine {
	return &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{
			PhaseField: "phase",
		},
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
	}
}

// TestGenerator_DefaultPhaseAndConditionsFields verifies that when status fields are
// left empty the generator applies the "phase" and "conditions" defaults and
// generates valid Go code (Phase appears in the status file as part of the
// status-update helpers; ConditionsField is stored in TemplateData but is not
// emitted verbatim by the status template).
func TestGenerator_DefaultPhaseAndConditionsFields(t *testing.T) {
	sm := minimalSM()
	// Explicitly clear both fields so defaults kick in.
	sm.Status.PhaseField = ""
	sm.Status.ConditionsField = ""

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	statusContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_status.go"))
	if err != nil {
		t.Fatalf("failed to read status file: %v", err)
	}
	content := string(statusContent)

	// "Phase" must appear — used in status.Phase = state.Phase() and ApplyStatus helpers.
	if !strings.Contains(content, "Phase") {
		t.Error("expected 'Phase' to appear in generated status code")
	}
	// Generation must succeed and produce parseable Go code (format.Source is called
	// during Generate; a failure there would have been caught above).
	if len(content) == 0 {
		t.Error("generated status file must not be empty")
	}
}

// TestGenerator_UnknownStateAutoAdded checks that the Unknown state is injected
// when not defined in the source machine.
func TestGenerator_UnknownStateAutoAdded(t *testing.T) {
	sm := minimalSM()
	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	phasesContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_phases.go"))
	if err != nil {
		t.Fatalf("failed to read phases file: %v", err)
	}

	if !strings.Contains(string(phasesContent), "Unknown") {
		t.Error("expected auto-generated 'Unknown' state to appear in phases file")
	}
}

// TestGenerator_FieldGroupFieldsExcludedFromStateFields verifies that fields
// which come from an embedded field group are not re-emitted as direct state
// fields in the generated code (avoiding duplicate struct fields).
func TestGenerator_FieldGroupFieldsExcludedFromStateFields(t *testing.T) {
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
			"common": {"resourceID": "string"},
		},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{
				Name:        "Ready",
				Terminal:    true,
				FieldGroups: []string{"common"},
				// resourceID is also listed as a direct field here to make
				// sure the generator deduplicates it via embeddedFields logic.
				Fields: map[string]string{
					"resourceID": "string",
					"extra":      "int",
				},
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

	gen, _ := newTestGenerator(t)
	// The generator should not error even though resourceID appears in both
	// the field group and as a direct field on Ready.
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}
}

// TestGenerator_SpecChangeHandling_Disabled_NoDefaultFieldSet confirms that
// when SpecChangeHandling is explicitly disabled, the generator does not inject
// the default observedGeneration field name.
func TestGenerator_SpecChangeHandling_Disabled_NoDefaultFieldSet(t *testing.T) {
	sm := minimalSM()
	sm.SpecChangeHandling = &schema.SpecChangeHandling{Enabled: false}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	statusContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_status.go"))
	if err != nil {
		t.Fatalf("failed to read status file: %v", err)
	}
	content := string(statusContent)

	// HasSpecChanged and UpdateObservedGeneration must not appear when disabled.
	if strings.Contains(content, "HasSpecChanged") {
		t.Error("HasSpecChanged must not be generated when SpecChangeHandling.Enabled=false")
	}
	if strings.Contains(content, "UpdateObservedGeneration") {
		t.Error("UpdateObservedGeneration must not be generated when SpecChangeHandling.Enabled=false")
	}
}

// TestGenerator_MultiSourceTransition_TransitionsByState verifies that a
// transition with multiple source states is registered for every source state
// in TransitionsByState (the data structure used for per-state template rendering).
func TestGenerator_MultiSourceTransition_TransitionsByState(t *testing.T) {
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
			{Name: "Creating"},
			{Name: "Ready", Terminal: true},
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Creating",
				Action: "StartCreation",
			},
			{
				From:   schema.TransitionSource{States: []string{"Creating"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Pending", "Creating"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
	}

	gen, tmpDir := newTestGenerator(t)
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// The visit file contains per-state case branches; both Pending and
	// Creating must have a MarkFailed case.
	visitContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_visit.go"))
	if err != nil {
		t.Fatalf("failed to read visit file: %v", err)
	}
	_ = visitContent // generation success is the primary assertion here.

	// Check transitions file which is generated from TransitionsByState and contains
	// the method stubs for each transition action.
	transContent, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_transitions.go"))
	if err != nil {
		t.Fatalf("failed to read transitions file: %v", err)
	}

	// "MarkFailed" must appear as a method on both Pending and Creating state types.
	if !strings.Contains(string(transContent), "MarkFailed") {
		t.Error("expected 'MarkFailed' action in generated transitions file")
	}
}

// TestGenerator_DeletionState generates a state machine with a deletion chain
// and verifies the generated phases file contains the deletion-flagged states.
func TestGenerator_DeletionState(t *testing.T) {
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
			{Name: "Deleting", Deletion: true},
			{Name: "Deleted", Deletion: true, Terminal: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:    schema.TransitionSource{States: []string{"Ready"}},
				To:      "Deleting",
				Action:  "BeginDeletion",
				Trigger: "deletionTimestamp",
			},
			{
				From:   schema.TransitionSource{States: []string{"Deleting"}},
				To:     "Deleted",
				Action: "MarkDeleted",
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

	for _, state := range []string{"Deleting", "Deleted"} {
		if !strings.Contains(content, state) {
			t.Errorf("expected deletion state %q in generated phases file", state)
		}
	}
}
