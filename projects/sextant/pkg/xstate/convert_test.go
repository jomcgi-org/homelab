package xstate_test

import (
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
	"github.com/jomcgi/homelab/projects/sextant/pkg/xstate"
)

// validSM returns a minimal valid StateMachine for test use.
func validSM() *schema.StateMachine {
	return &schema.StateMachine{
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
			},
		},
	}
}

func TestConvert_ID(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)
	if machine.ID != "TestResource" {
		t.Errorf("Machine.ID = %q, want %q", machine.ID, "TestResource")
	}
}

func TestConvert_InitialState(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)
	if machine.Initial != "Pending" {
		t.Errorf("Machine.Initial = %q, want 'Pending'", machine.Initial)
	}
}

func TestConvert_StatesPresent(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)

	if _, ok := machine.States["Pending"]; !ok {
		t.Error("Expected 'Pending' state in machine")
	}
	if _, ok := machine.States["Ready"]; !ok {
		t.Error("Expected 'Ready' state in machine")
	}
}

func TestConvert_TerminalStateType(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)

	ready := machine.States["Ready"]
	if ready.Type != "final" {
		t.Errorf("Terminal state type = %q, want 'final'", ready.Type)
	}

	pending := machine.States["Pending"]
	if pending.Type != "" {
		t.Errorf("Non-terminal state type = %q, want empty", pending.Type)
	}
}

func TestConvert_AddsUnknownState(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)

	unknown, ok := machine.States["Unknown"]
	if !ok {
		t.Fatal("Expected auto-generated 'Unknown' state")
	}
	if unknown.Meta == nil || !unknown.Meta.Error {
		t.Error("'Unknown' state should have error meta flag set")
	}
}

func TestConvert_DoesNotOverrideExistingUnknown(t *testing.T) {
	sm := validSM()
	sm.States = append(sm.States, schema.State{Name: "Unknown", Error: true, Generated: true})
	machine := xstate.Convert(sm)

	if _, ok := machine.States["Unknown"]; !ok {
		t.Error("Expected 'Unknown' state to exist")
	}
}

func TestConvert_Transitions(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)

	pending := machine.States["Pending"]
	if pending.On == nil {
		t.Fatal("Expected 'Pending' state to have transitions")
	}

	// Action "MarkReady" should become event "MARK_READY"
	tr, ok := pending.On["MARK_READY"]
	if !ok {
		t.Fatalf("Expected MARK_READY transition on Pending state, got %v", pending.On)
	}
	if tr.Target != "Ready" {
		t.Errorf("Transition target = %q, want 'Ready'", tr.Target)
	}
}

func TestConvert_MultipleSourceStates(t *testing.T) {
	sm := validSM()
	sm.States = append(sm.States, schema.State{Name: "Creating"})
	sm.Transitions = append(sm.Transitions, schema.Transition{
		From:   schema.TransitionSource{States: []string{"Pending", "Creating"}},
		To:     "Ready",
		Action: "MarkReady2",
	})
	machine := xstate.Convert(sm)

	// Both Pending and Creating should have the transition
	if tr, ok := machine.States["Pending"].On["MARK_READY2"]; !ok || tr.Target != "Ready" {
		t.Error("Expected MARK_READY2 on Pending state")
	}
	if tr, ok := machine.States["Creating"].On["MARK_READY2"]; !ok || tr.Target != "Ready" {
		t.Error("Expected MARK_READY2 on Creating state")
	}
}

func TestConvert_GuardOnTransition(t *testing.T) {
	sm := validSM()
	sm.Guards = map[string]schema.Guard{
		"isReady": {Description: "Checks if ready"},
	}
	sm.Transitions = []schema.Transition{
		{
			From:   schema.TransitionSource{States: []string{"Pending"}},
			To:     "Ready",
			Action: "MarkReady",
			Guard:  "isReady",
		},
	}
	machine := xstate.Convert(sm)

	tr := machine.States["Pending"].On["MARK_READY"]
	if tr.Cond != "isReady" {
		t.Errorf("Transition guard = %q, want 'isReady'", tr.Cond)
	}
}

func TestConvert_ParamsBecomesActions(t *testing.T) {
	sm := validSM()
	sm.Transitions = []schema.Transition{
		{
			From:   schema.TransitionSource{States: []string{"Pending"}},
			To:     "Ready",
			Action: "MarkReady",
			Params: []schema.TransitionParam{
				{Name: "tunnelID", Type: "string"},
			},
		},
	}
	machine := xstate.Convert(sm)

	tr := machine.States["Pending"].On["MARK_READY"]
	if len(tr.Actions) == 0 {
		t.Error("Expected transition actions from params")
	}
	// "tunnelID" param should produce "assignTunnelID" action
	found := false
	for _, a := range tr.Actions {
		if a == "assignTunnelID" {
			found = true
		}
	}
	if !found {
		t.Errorf("Expected 'assignTunnelID' action, got: %v", tr.Actions)
	}
}

func TestConvert_StateTags(t *testing.T) {
	sm := validSM()
	sm.States = append(sm.States,
		schema.State{Name: "Failed", Error: true},
		schema.State{Name: "Deleting", Deletion: true},
	)
	machine := xstate.Convert(sm)

	pendingTags := machine.States["Pending"].Tags
	if len(pendingTags) == 0 || pendingTags[0] != "initial" {
		t.Errorf("Pending tags = %v, expected first tag to be 'initial'", pendingTags)
	}

	failedTags := machine.States["Failed"].Tags
	found := false
	for _, tag := range failedTags {
		if tag == "error" {
			found = true
		}
	}
	if !found {
		t.Errorf("Failed state tags = %v, expected 'error' tag", failedTags)
	}

	deletingTags := machine.States["Deleting"].Tags
	found = false
	for _, tag := range deletingTags {
		if tag == "deletion" {
			found = true
		}
	}
	if !found {
		t.Errorf("Deleting state tags = %v, expected 'deletion' tag", deletingTags)
	}
}

func TestConvert_StateMetaRequeue(t *testing.T) {
	// Parse the requeue duration from YAML via test helper
	yamlStr := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
states:
  - name: Pending
    initial: true
    requeue: 30s
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	parsedSM, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	machine := xstate.Convert(parsedSM)

	pending := machine.States["Pending"]
	if pending.Meta == nil {
		t.Fatal("Expected Pending state to have meta (requeue)")
	}
	if pending.Meta.Requeue != "30s" {
		t.Errorf("Pending requeue = %q, want '30s'", pending.Meta.Requeue)
	}
}

func TestConvert_Context_FromFields(t *testing.T) {
	yamlStr := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
states:
  - name: Pending
    initial: true
    fields:
      resourceID: string
      count: int
      enabled: bool
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	parsedSM, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	machine := xstate.Convert(parsedSM)

	// Context should have all fields with zero values
	if len(machine.Context) == 0 {
		t.Fatal("Expected machine context to have fields")
	}

	// string type → nil in JSON
	if v, ok := machine.Context["resourceID"]; !ok {
		t.Error("Expected 'resourceID' in context")
	} else if v != nil {
		t.Errorf("Context 'resourceID' (string) = %v, want nil", v)
	}

	// int type → 0
	if v, ok := machine.Context["count"]; !ok {
		t.Error("Expected 'count' in context")
	} else if v != 0 {
		t.Errorf("Context 'count' (int) = %v, want 0", v)
	}

	// bool type → false
	if v, ok := machine.Context["enabled"]; !ok {
		t.Error("Expected 'enabled' in context")
	} else if v != false {
		t.Errorf("Context 'enabled' (bool) = %v, want false", v)
	}
}

// TestToEventName checks the event name conversion (via Convert behaviour)
func TestConvert_EventNameConversion(t *testing.T) {
	tests := []struct {
		action    string
		wantEvent string
	}{
		{"MarkReady", "MARK_READY"},
		{"StartTunnelCreation", "START_TUNNEL_CREATION"},
		{"Delete", "DELETE"},
	}

	for _, tt := range tests {
		sm := validSM()
		sm.Transitions = []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: tt.action,
			},
		}
		machine := xstate.Convert(sm)

		pending := machine.States["Pending"]
		if _, ok := pending.On[tt.wantEvent]; !ok {
			t.Errorf("Action %q should produce event %q, got transitions: %v",
				tt.action, tt.wantEvent, pending.On)
		}
	}
}
