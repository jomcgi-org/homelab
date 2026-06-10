package xstate_test

import (
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
	"github.com/jomcgi/homelab/projects/sextant/pkg/xstate"
)

// TestConvert_ToEventName_SingleLetterAction verifies that a single-letter action
// name is converted to an event name without any underscores or modifications.
func TestConvert_ToEventName_SingleLetterAction(t *testing.T) {
	sm := validSM()
	sm.Transitions[0].Action = "A"

	machine := xstate.Convert(sm)

	pendingState := machine.States["Pending"]
	if _, ok := pendingState.On["A"]; !ok {
		t.Errorf("expected event 'A' on Pending state, got events: %v", pendingState.On)
	}
}

// TestConvert_ToEventName_LowercaseFirstLetter verifies that an action starting
// with a lowercase letter is converted to an uppercase event name correctly.
// e.g., "markReady" → "MARK_READY"
func TestConvert_ToEventName_LowercaseFirstLetter(t *testing.T) {
	sm := validSM()
	sm.Transitions[0].Action = "markReady"

	machine := xstate.Convert(sm)

	pendingState := machine.States["Pending"]
	if _, ok := pendingState.On["MARK_READY"]; !ok {
		t.Errorf("expected event 'MARK_READY' on Pending state, got events: %v", pendingState.On)
	}
}

// TestConvert_ToEventName_AllCapsAction verifies that an all-caps action name
// produces underscores between each letter.
// e.g., "ABC" → "A_B_C"
func TestConvert_ToEventName_AllCapsAction(t *testing.T) {
	sm := validSM()
	sm.Transitions[0].Action = "ABC"

	machine := xstate.Convert(sm)

	pendingState := machine.States["Pending"]
	if _, ok := pendingState.On["A_B_C"]; !ok {
		t.Errorf("expected event 'A_B_C' on Pending state, got events: %v", pendingState.On)
	}
}

// TestConvert_ToEventName_ActionWithNumber verifies that digits in action names
// do not trigger an underscore insertion (digits are not uppercase letters).
// e.g., "Create2Resource" → "CREATE2_RESOURCE"
func TestConvert_ToEventName_ActionWithNumber(t *testing.T) {
	sm := validSM()
	sm.Transitions[0].Action = "Create2Resource"

	machine := xstate.Convert(sm)

	pendingState := machine.States["Pending"]
	if _, ok := pendingState.On["CREATE2_RESOURCE"]; !ok {
		t.Errorf("expected event 'CREATE2_RESOURCE' on Pending state, got events: %v", pendingState.On)
	}
}

// TestConvert_ToEventName_AlreadyAllCapsWord verifies a single camel-cased
// word with a common ALL-CAPS prefix like "HTTPGet" → "H_T_T_P_GET".
func TestConvert_ToEventName_AllCapsPrefix(t *testing.T) {
	sm := validSM()
	sm.Transitions[0].Action = "HTTPGet"

	machine := xstate.Convert(sm)

	pendingState := machine.States["Pending"]
	// H_T_T_P_GET: each letter in HTTP triggers underscore except the first
	if _, ok := pendingState.On["H_T_T_P_GET"]; !ok {
		t.Errorf("expected event 'H_T_T_P_GET' on Pending state, got events: %v", pendingState.On)
	}
}

// TestConvert_TransitionWithNoParams verifies that when a transition has no
// params, no actions are set on the event in the state machine.
func TestConvert_TransitionWithNoParams(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
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
				// No Params
			},
		},
	}

	machine := xstate.Convert(sm)

	pendingState := machine.States["Pending"]
	event, ok := pendingState.On["MARK_READY"]
	if !ok {
		t.Fatal("expected MARK_READY event on Pending state")
	}

	// No params means no actions should be set on the transition
	if len(event.Actions) != 0 {
		t.Errorf("expected no actions for transition without params, got: %v", event.Actions)
	}
}

// TestConvert_TransitionWithNoGuard verifies that when a transition has no
// guard, the event's Guard field is empty.
func TestConvert_TransitionWithNoGuard(t *testing.T) {
	sm := validSM()

	machine := xstate.Convert(sm)

	pendingState := machine.States["Pending"]
	event, ok := pendingState.On["MARK_READY"]
	if !ok {
		t.Fatal("expected MARK_READY event on Pending state")
	}

	if event.Cond != "" {
		t.Errorf("expected empty cond for transition without guard, got: %q", event.Cond)
	}
}
