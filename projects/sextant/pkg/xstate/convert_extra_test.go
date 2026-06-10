package xstate_test

import (
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
	"github.com/jomcgi/homelab/projects/sextant/pkg/xstate"
)

// TestConvert_ContextDeduplication verifies that when the same field name
// appears in multiple states it is only stored once in the machine context.
func TestConvert_ContextDeduplication(t *testing.T) {
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
  - name: Creating
    fields:
      resourceID: string
      count: int
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
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	machine := xstate.Convert(sm)

	// resourceID appears in two states but should only be one entry in context.
	if len(machine.Context) != 2 {
		t.Errorf("expected 2 context entries (resourceID, count), got %d: %v",
			len(machine.Context), machine.Context)
	}

	if _, ok := machine.Context["resourceID"]; !ok {
		t.Error("expected 'resourceID' in context")
	}
	if _, ok := machine.Context["count"]; !ok {
		t.Error("expected 'count' in context")
	}
}

// TestConvert_EmptyContext verifies that a machine with no state fields produces
// an empty (but non-nil) context map.
func TestConvert_EmptyContext(t *testing.T) {
	sm := validSM() // no fields on any state
	machine := xstate.Convert(sm)
	if machine.Context == nil {
		t.Error("expected non-nil context even when no fields are defined")
	}
	if len(machine.Context) != 0 {
		t.Errorf("expected empty context, got %v", machine.Context)
	}
}

// TestConvert_DeletionStateMeta verifies that a deletion state has
// Meta.Deletion = true and the "deletion" tag.
func TestConvert_DeletionStateMeta(t *testing.T) {
	sm := validSM()
	sm.States = append(sm.States, schema.State{Name: "Deleting", Deletion: true})
	machine := xstate.Convert(sm)

	deleting, ok := machine.States["Deleting"]
	if !ok {
		t.Fatal("expected 'Deleting' state in machine")
	}

	if deleting.Meta == nil || !deleting.Meta.Deletion {
		t.Error("expected Meta.Deletion=true on deletion state")
	}

	found := false
	for _, tag := range deleting.Tags {
		if tag == "deletion" {
			found = true
		}
	}
	if !found {
		t.Errorf("Deleting state tags = %v, expected 'deletion' tag", deleting.Tags)
	}
}

// TestConvert_ContextDefaultValues_NumericTypes checks that unsigned int and
// float field types produce a zero numeric default value in the context.
func TestConvert_ContextDefaultValues_NumericTypes(t *testing.T) {
	tests := []struct {
		fieldType   string
		wantDefault interface{}
	}{
		{"uint", 0},
		{"uint8", 0},
		{"uint16", 0},
		{"uint32", 0},
		{"uint64", 0},
		{"int8", 0},
		{"int16", 0},
		{"float32", 0.0},
		{"float64", 0.0},
	}

	for _, tt := range tests {
		t.Run(tt.fieldType, func(t *testing.T) {
			sm := &schema.StateMachine{
				APIVersion: "sextant.jomcgi.dev/v1alpha1",
				Kind:       "StateMachine",
				Metadata: schema.Metadata{
					Name:    "TestResource",
					Group:   "test.io",
					Version: "v1alpha1",
				},
				States: []schema.State{
					{
						Name:    "Pending",
						Initial: true,
						Fields:  map[string]string{"theField": tt.fieldType},
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

			machine := xstate.Convert(sm)
			val, ok := machine.Context["theField"]
			if !ok {
				t.Fatalf("expected 'theField' in context for type %q", tt.fieldType)
			}
			if val != tt.wantDefault {
				t.Errorf("context['theField'] for type %q = %v (%T), want %v (%T)",
					tt.fieldType, val, val, tt.wantDefault, tt.wantDefault)
			}
		})
	}
}

// TestConvert_ContextDefaultValue_UnknownType checks that an unrecognised type
// produces nil in the context (treated as an opaque object type).
func TestConvert_ContextDefaultValue_UnknownType(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		States: []schema.State{
			{
				Name:    "Pending",
				Initial: true,
				Fields:  map[string]string{"meta": "SomeCustomType"},
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

	machine := xstate.Convert(sm)
	val, ok := machine.Context["meta"]
	if !ok {
		t.Fatal("expected 'meta' field in context")
	}
	if val != nil {
		t.Errorf("context['meta'] for unknown type = %v, want nil", val)
	}
}

// TestConvert_GeneratedTagOnUnknownState checks that the auto-generated Unknown
// state carries the "generated" tag.
func TestConvert_GeneratedTagOnUnknownState(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)

	unknown, ok := machine.States["Unknown"]
	if !ok {
		t.Fatal("expected auto-generated 'Unknown' state")
	}

	found := false
	for _, tag := range unknown.Tags {
		if tag == "generated" {
			found = true
		}
	}
	if !found {
		t.Errorf("Unknown state tags = %v, expected 'generated' tag", unknown.Tags)
	}
}

// TestConvert_FieldGroupsResolvedIntoContext verifies that fields from field
// groups are included in the machine context.
func TestConvert_FieldGroupsResolvedIntoContext(t *testing.T) {
	yamlStr := `
apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
fieldGroups:
  common:
    networkID: string
    region: string
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
    fieldGroups: [common]
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`
	sm, err := schema.Parse([]byte(yamlStr))
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	machine := xstate.Convert(sm)

	for _, field := range []string{"networkID", "region"} {
		if _, ok := machine.Context[field]; !ok {
			t.Errorf("expected field %q from field group to appear in context", field)
		}
	}
}

// TestConvert_NoTransitionOnTerminalState checks that a terminal state
// (type="final") has no On transitions registered by the converter.
func TestConvert_NoTransitionOnTerminalState(t *testing.T) {
	sm := validSM()
	machine := xstate.Convert(sm)

	ready := machine.States["Ready"]
	if len(ready.On) != 0 {
		t.Errorf("expected terminal state 'Ready' to have no transitions, got: %v", ready.On)
	}
}

// TestConvert_StateWithRequeueAndFields checks that a state with both a requeue
// duration and fields produces the correct meta.
func TestConvert_StateWithRequeueAndFields(t *testing.T) {
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
    requeue: 10s
    fields:
      attempt: int
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

	machine := xstate.Convert(sm)
	pending := machine.States["Pending"]

	if pending.Meta == nil {
		t.Fatal("expected 'Pending' to have meta")
	}
	if pending.Meta.Requeue != "10s" {
		t.Errorf("Pending requeue = %q, want '10s'", pending.Meta.Requeue)
	}
	if _, ok := pending.Meta.Fields["attempt"]; !ok {
		t.Error("expected 'attempt' field in state meta")
	}
}
