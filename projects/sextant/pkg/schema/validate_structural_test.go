package schema_test

import (
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// TestValidate_TerminalStateTransitionToRegularState verifies that
// validateStructuralConstraints rejects a terminal state that has an outgoing
// transition targeting a non-error, non-deletion state.
func TestValidate_TerminalStateTransitionToRegularState(t *testing.T) {
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
			{Name: "Processing"}, // regular (non-error, non-deletion) state
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				// Terminal -> regular: violates structural constraint
				From:   schema.TransitionSource{States: []string{"Ready"}},
				To:     "Processing",
				Action: "Reprocess",
			},
		},
	}

	err := schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for terminal state transitioning to regular state, got nil")
	}
	if !strings.Contains(err.Error(), "terminal state") {
		t.Errorf("expected error to mention 'terminal state', got: %v", err)
	}
	if !strings.Contains(err.Error(), "Ready") {
		t.Errorf("expected error to mention 'Ready', got: %v", err)
	}
	if !strings.Contains(err.Error(), "Processing") {
		t.Errorf("expected error to mention 'Processing', got: %v", err)
	}
}

// TestValidate_DeletionStatesWithoutTerminalDeletion verifies that
// validateStructuralConstraints returns an error when deletion states exist
// but none are terminal.
func TestValidate_DeletionStatesWithoutTerminalDeletion(t *testing.T) {
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
			// Deletion state, but NOT terminal
			{Name: "Deleting", Deletion: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Ready"}},
				To:     "Deleting",
				Action: "BeginDeletion",
			},
		},
	}

	err := schema.Validate(sm)
	if err == nil {
		t.Fatal("expected validation error for deletion states without terminal deletion state, got nil")
	}
	if !strings.Contains(err.Error(), "deletion") {
		t.Errorf("expected error to mention 'deletion', got: %v", err)
	}
}

// TestValidate_TerminalStateTransitionToDeletionAllowed verifies that a terminal
// state IS allowed to transition to a deletion state (not a structural violation).
func TestValidate_TerminalStateTransitionToDeletionAllowed(t *testing.T) {
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
				// Terminal → Deletion: allowed
				From:   schema.TransitionSource{States: []string{"Ready"}},
				To:     "Deleting",
				Action: "BeginDeletion",
			},
			{
				From:   schema.TransitionSource{States: []string{"Deleting"}},
				To:     "Deleted",
				Action: "MarkDeleted",
			},
		},
	}

	if err := schema.Validate(sm); err != nil {
		t.Fatalf("expected no error for terminal→deletion transition, got: %v", err)
	}
}
