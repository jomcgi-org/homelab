package xstate_test

import (
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
	"github.com/jomcgi/homelab/projects/sextant/pkg/xstate"
)

// TestConvert_TransitionFromNonExistentState verifies that a transition
// referencing a from-state not in machine.States is silently skipped
// (no panic, no spurious state entry added).
func TestConvert_TransitionFromNonExistentState(t *testing.T) {
	sm := validSM()
	// Append a transition whose source state does not exist in the machine.
	sm.Transitions = append(sm.Transitions, schema.Transition{
		From:   schema.TransitionSource{States: []string{"NonExistentState"}},
		To:     "Ready",
		Action: "SomeAction",
	})

	// Should not panic.
	machine := xstate.Convert(sm)

	// The non-existent state must not be added to the machine.
	if _, ok := machine.States["NonExistentState"]; ok {
		t.Error("non-existent from-state should not appear in machine after transition reference")
	}

	// The event must not appear on any of the real states that were defined.
	for stateName, state := range machine.States {
		if _, ok := state.On["SOME_ACTION"]; ok {
			t.Errorf("SOME_ACTION event from non-existent state should not appear on state %q", stateName)
		}
	}
}
