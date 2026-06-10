package xstate_test

import (
	"encoding/json"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/xstate"
)

// --- Transition.MarshalJSON tests ---

func TestTransition_MarshalJSON_SimpleTarget(t *testing.T) {
	tr := xstate.Transition{Target: "Ready"}
	data, err := json.Marshal(tr)
	if err != nil {
		t.Fatalf("MarshalJSON failed: %v", err)
	}
	// Simple transition should marshal as a string, not an object
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		t.Fatalf("expected string marshaling for simple transition, got: %s", data)
	}
	if s != "Ready" {
		t.Errorf("MarshalJSON() = %q, want %q", s, "Ready")
	}
}

func TestTransition_MarshalJSON_WithActions(t *testing.T) {
	tr := xstate.Transition{
		Target:  "Ready",
		Actions: []string{"doSomething"},
	}
	data, err := json.Marshal(tr)
	if err != nil {
		t.Fatalf("MarshalJSON failed: %v", err)
	}
	// Should marshal as an object since it has actions
	var obj map[string]interface{}
	if err := json.Unmarshal(data, &obj); err != nil {
		t.Fatalf("expected object marshaling for transition with actions, got: %s, err: %v", data, err)
	}
	if obj["target"] != "Ready" {
		t.Errorf("target = %v, want Ready", obj["target"])
	}
	actions, ok := obj["actions"].([]interface{})
	if !ok || len(actions) != 1 || actions[0] != "doSomething" {
		t.Errorf("actions = %v, want [doSomething]", obj["actions"])
	}
}

func TestTransition_MarshalJSON_WithCond(t *testing.T) {
	tr := xstate.Transition{
		Target: "Ready",
		Cond:   "hasPermission",
	}
	data, err := json.Marshal(tr)
	if err != nil {
		t.Fatalf("MarshalJSON failed: %v", err)
	}
	// Should marshal as an object since it has a condition
	var obj map[string]interface{}
	if err := json.Unmarshal(data, &obj); err != nil {
		t.Fatalf("expected object marshaling for transition with cond: %s", data)
	}
	if obj["cond"] != "hasPermission" {
		t.Errorf("cond = %v, want hasPermission", obj["cond"])
	}
}

func TestTransition_MarshalJSON_WithInternal(t *testing.T) {
	tr := xstate.Transition{
		Target:   "Ready",
		Internal: true,
	}
	data, err := json.Marshal(tr)
	if err != nil {
		t.Fatalf("MarshalJSON failed: %v", err)
	}
	// Should marshal as an object since internal=true
	var obj map[string]interface{}
	if err := json.Unmarshal(data, &obj); err != nil {
		t.Fatalf("expected object marshaling for internal transition: %s", data)
	}
	if obj["internal"] != true {
		t.Errorf("internal = %v, want true", obj["internal"])
	}
}

func TestTransition_MarshalJSON_EmptyTarget(t *testing.T) {
	tr := xstate.Transition{Target: ""}
	data, err := json.Marshal(tr)
	if err != nil {
		t.Fatalf("MarshalJSON failed: %v", err)
	}
	// Empty target with no other fields marshals as empty string
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		t.Fatalf("expected string marshaling for empty target: %s", data)
	}
	if s != "" {
		t.Errorf("MarshalJSON() = %q, want empty string", s)
	}
}

// --- Transition.UnmarshalJSON tests ---

func TestTransition_UnmarshalJSON_FromString(t *testing.T) {
	data := `"Ready"`
	var tr xstate.Transition
	if err := json.Unmarshal([]byte(data), &tr); err != nil {
		t.Fatalf("UnmarshalJSON failed: %v", err)
	}
	if tr.Target != "Ready" {
		t.Errorf("Target = %q, want Ready", tr.Target)
	}
	if tr.Cond != "" {
		t.Errorf("Cond = %q, want empty", tr.Cond)
	}
	if len(tr.Actions) != 0 {
		t.Errorf("Actions = %v, want empty", tr.Actions)
	}
	if tr.Internal {
		t.Error("Internal should be false for simple string unmarshal")
	}
}

func TestTransition_UnmarshalJSON_FromObject(t *testing.T) {
	data := `{"target":"Ready","actions":["doSomething"],"cond":"hasPermission","internal":true}`
	var tr xstate.Transition
	if err := json.Unmarshal([]byte(data), &tr); err != nil {
		t.Fatalf("UnmarshalJSON failed: %v", err)
	}
	if tr.Target != "Ready" {
		t.Errorf("Target = %q, want Ready", tr.Target)
	}
	if tr.Cond != "hasPermission" {
		t.Errorf("Cond = %q, want hasPermission", tr.Cond)
	}
	if len(tr.Actions) != 1 || tr.Actions[0] != "doSomething" {
		t.Errorf("Actions = %v, want [doSomething]", tr.Actions)
	}
	if !tr.Internal {
		t.Error("Internal should be true")
	}
}

func TestTransition_UnmarshalJSON_FromObjectWithoutOptionalFields(t *testing.T) {
	data := `{"target":"Pending"}`
	var tr xstate.Transition
	if err := json.Unmarshal([]byte(data), &tr); err != nil {
		t.Fatalf("UnmarshalJSON failed: %v", err)
	}
	if tr.Target != "Pending" {
		t.Errorf("Target = %q, want Pending", tr.Target)
	}
	if tr.Cond != "" || len(tr.Actions) != 0 || tr.Internal {
		t.Error("optional fields should be zero values")
	}
}

func TestTransition_UnmarshalJSON_InvalidJSON(t *testing.T) {
	data := `{invalid json`
	var tr xstate.Transition
	err := json.Unmarshal([]byte(data), &tr)
	if err == nil {
		t.Error("expected error for invalid JSON, got nil")
	}
}

// --- Round-trip tests ---

func TestTransition_RoundTrip_Simple(t *testing.T) {
	original := xstate.Transition{Target: "Ready"}
	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("MarshalJSON failed: %v", err)
	}
	var got xstate.Transition
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("UnmarshalJSON failed: %v", err)
	}
	if got.Target != original.Target {
		t.Errorf("round-trip Target = %q, want %q", got.Target, original.Target)
	}
}

func TestTransition_RoundTrip_Complex(t *testing.T) {
	original := xstate.Transition{
		Target:   "Ready",
		Actions:  []string{"action1", "action2"},
		Cond:     "myGuard",
		Internal: true,
	}
	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("MarshalJSON failed: %v", err)
	}
	var got xstate.Transition
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("UnmarshalJSON failed: %v", err)
	}
	if got.Target != original.Target {
		t.Errorf("Target = %q, want %q", got.Target, original.Target)
	}
	if got.Cond != original.Cond {
		t.Errorf("Cond = %q, want %q", got.Cond, original.Cond)
	}
	if len(got.Actions) != len(original.Actions) {
		t.Errorf("len(Actions) = %d, want %d", len(got.Actions), len(original.Actions))
	}
	if !got.Internal {
		t.Error("Internal should be true after round-trip")
	}
}

// --- Machine and State JSON marshaling tests ---

func TestMachine_MarshalJSON(t *testing.T) {
	machine := xstate.Machine{
		ID:      "TestMachine",
		Initial: "Pending",
		States: map[string]xstate.State{
			"Pending": {Type: ""},
			"Ready":   {Type: "final"},
		},
	}
	data, err := json.Marshal(machine)
	if err != nil {
		t.Fatalf("json.Marshal(Machine) failed: %v", err)
	}
	var got xstate.Machine
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("json.Unmarshal(Machine) failed: %v", err)
	}
	if got.ID != machine.ID {
		t.Errorf("ID = %q, want %q", got.ID, machine.ID)
	}
	if got.Initial != machine.Initial {
		t.Errorf("Initial = %q, want %q", got.Initial, machine.Initial)
	}
	if len(got.States) != 2 {
		t.Errorf("len(States) = %d, want 2", len(got.States))
	}
}

func TestMachine_ContextOmittedWhenEmpty(t *testing.T) {
	machine := xstate.Machine{
		ID:      "TestMachine",
		Initial: "Pending",
		States:  map[string]xstate.State{},
	}
	data, err := json.Marshal(machine)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	// context field should be omitted when nil/empty
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("Unmarshal failed: %v", err)
	}
	if _, ok := raw["context"]; ok {
		t.Error("expected 'context' to be omitted from JSON when empty")
	}
}

func TestMachine_ContextIncludedWhenSet(t *testing.T) {
	machine := xstate.Machine{
		ID:      "TestMachine",
		Initial: "Pending",
		Context: map[string]interface{}{"retryCount": 0},
		States:  map[string]xstate.State{},
	}
	data, err := json.Marshal(machine)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("Unmarshal failed: %v", err)
	}
	if _, ok := raw["context"]; !ok {
		t.Error("expected 'context' to be included in JSON when set")
	}
}

func TestState_MetaOmittedWhenNil(t *testing.T) {
	s := xstate.State{Type: "final"}
	data, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("Unmarshal failed: %v", err)
	}
	if _, ok := raw["meta"]; ok {
		t.Error("expected 'meta' to be omitted when nil")
	}
}

func TestState_MetaIncludedWhenSet(t *testing.T) {
	s := xstate.State{
		Meta: &xstate.StateMeta{Requeue: "30s", Error: true},
	}
	data, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var got xstate.State
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("Unmarshal failed: %v", err)
	}
	if got.Meta == nil {
		t.Fatal("expected Meta to be set")
	}
	if got.Meta.Requeue != "30s" {
		t.Errorf("Meta.Requeue = %q, want 30s", got.Meta.Requeue)
	}
	if !got.Meta.Error {
		t.Error("expected Meta.Error to be true")
	}
}

func TestStateMeta_AllFields(t *testing.T) {
	meta := xstate.StateMeta{
		Requeue:     "1m",
		Description: "A test state",
		Error:       true,
		Deletion:    true,
		Fields:      map[string]string{"foo": "string"},
	}
	data, err := json.Marshal(meta)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var got xstate.StateMeta
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("json.Unmarshal failed: %v", err)
	}
	if got.Requeue != meta.Requeue {
		t.Errorf("Requeue = %q, want %q", got.Requeue, meta.Requeue)
	}
	if got.Description != meta.Description {
		t.Errorf("Description = %q, want %q", got.Description, meta.Description)
	}
	if !got.Error {
		t.Error("Error should be true")
	}
	if !got.Deletion {
		t.Error("Deletion should be true")
	}
	if got.Fields["foo"] != "string" {
		t.Errorf("Fields[foo] = %q, want string", got.Fields["foo"])
	}
}

func TestState_TagsRoundTrip(t *testing.T) {
	s := xstate.State{
		Tags: []string{"ready", "terminal"},
	}
	data, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var got xstate.State
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("json.Unmarshal failed: %v", err)
	}
	if len(got.Tags) != 2 || got.Tags[0] != "ready" || got.Tags[1] != "terminal" {
		t.Errorf("Tags = %v, want [ready terminal]", got.Tags)
	}
}

func TestState_OnTransitionsRoundTrip(t *testing.T) {
	s := xstate.State{
		On: map[string]xstate.Transition{
			"MARK_READY": {Target: "Ready"},
			"FAIL":       {Target: "Failed", Cond: "hasErrors"},
		},
	}
	data, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var got xstate.State
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("json.Unmarshal failed: %v", err)
	}
	if len(got.On) != 2 {
		t.Fatalf("len(On) = %d, want 2", len(got.On))
	}
	if got.On["MARK_READY"].Target != "Ready" {
		t.Errorf("On[MARK_READY].Target = %q, want Ready", got.On["MARK_READY"].Target)
	}
	if got.On["FAIL"].Target != "Failed" {
		t.Errorf("On[FAIL].Target = %q, want Failed", got.On["FAIL"].Target)
	}
	if got.On["FAIL"].Cond != "hasErrors" {
		t.Errorf("On[FAIL].Cond = %q, want hasErrors", got.On["FAIL"].Cond)
	}
}
