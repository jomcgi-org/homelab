package schema_test

import (
	"testing"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// --- Duration tests ---

func TestDuration_UnmarshalYAML_ValidDurations(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  time.Duration
	}{
		{"seconds", "5s", 5 * time.Second},
		{"minutes", "2m", 2 * time.Minute},
		{"hours", "1h", time.Hour},
		{"milliseconds", "500ms", 500 * time.Millisecond},
		{"composite", "1h30m", 90 * time.Minute},
		{"empty string", "", 0},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			type wrapper struct {
				D schema.Duration `yaml:"d"`
			}
			data := "d: " + tc.input
			if tc.input == "" {
				data = "d: \"\""
			}
			var w wrapper
			if err := yaml.Unmarshal([]byte(data), &w); err != nil {
				t.Fatalf("UnmarshalYAML(%q) error: %v", tc.input, err)
			}
			if w.D.Duration != tc.want {
				t.Errorf("UnmarshalYAML(%q) = %v, want %v", tc.input, w.D.Duration, tc.want)
			}
		})
	}
}

func TestDuration_UnmarshalYAML_InvalidDuration(t *testing.T) {
	type wrapper struct {
		D schema.Duration `yaml:"d"`
	}
	var w wrapper
	err := yaml.Unmarshal([]byte("d: not-a-duration"), &w)
	if err == nil {
		t.Error("expected error for invalid duration string, got nil")
	}
}

func TestDuration_MarshalYAML_Zero(t *testing.T) {
	type wrapper struct {
		D schema.Duration `yaml:"d"`
	}
	w := wrapper{D: schema.Duration{}}
	data, err := yaml.Marshal(&w)
	if err != nil {
		t.Fatalf("MarshalYAML failed: %v", err)
	}
	// Zero duration should marshal to empty string
	var w2 wrapper
	if err := yaml.Unmarshal(data, &w2); err != nil {
		t.Fatalf("round-trip unmarshal failed: %v", err)
	}
	if w2.D.Duration != 0 {
		t.Errorf("expected zero duration after round-trip, got %v", w2.D.Duration)
	}
}

func TestDuration_MarshalYAML_NonZero(t *testing.T) {
	type wrapper struct {
		D schema.Duration `yaml:"d"`
	}
	w := wrapper{D: schema.Duration{Duration: 5 * time.Second}}
	data, err := yaml.Marshal(&w)
	if err != nil {
		t.Fatalf("MarshalYAML failed: %v", err)
	}
	// Should round-trip correctly
	var w2 wrapper
	if err := yaml.Unmarshal(data, &w2); err != nil {
		t.Fatalf("round-trip unmarshal failed: %v", err)
	}
	if w2.D.Duration != 5*time.Second {
		t.Errorf("round-trip: got %v, want 5s", w2.D.Duration)
	}
}

// --- TransitionSource tests ---

func TestTransitionSource_UnmarshalYAML_SingleString(t *testing.T) {
	type wrapper struct {
		From schema.TransitionSource `yaml:"from"`
	}
	var w wrapper
	if err := yaml.Unmarshal([]byte("from: Pending"), &w); err != nil {
		t.Fatalf("UnmarshalYAML failed: %v", err)
	}
	if len(w.From.States) != 1 || w.From.States[0] != "Pending" {
		t.Errorf("got states %v, want [Pending]", w.From.States)
	}
}

func TestTransitionSource_UnmarshalYAML_List(t *testing.T) {
	type wrapper struct {
		From schema.TransitionSource `yaml:"from"`
	}
	var w wrapper
	yaml := "from:\n  - Pending\n  - Running\n"
	if err := yaml_unmarshal([]byte(yaml), &w); err != nil {
		t.Fatalf("UnmarshalYAML failed: %v", err)
	}
	if len(w.From.States) != 2 {
		t.Fatalf("got %d states, want 2", len(w.From.States))
	}
	if w.From.States[0] != "Pending" || w.From.States[1] != "Running" {
		t.Errorf("got states %v, want [Pending Running]", w.From.States)
	}
}

func TestTransitionSource_MarshalYAML_Single(t *testing.T) {
	type wrapper struct {
		From schema.TransitionSource `yaml:"from"`
	}
	w := wrapper{From: schema.TransitionSource{States: []string{"Pending"}}}
	data, err := yaml_marshal(&w)
	if err != nil {
		t.Fatalf("MarshalYAML failed: %v", err)
	}
	// Should round-trip as single string
	var w2 wrapper
	if err := yaml_unmarshal(data, &w2); err != nil {
		t.Fatalf("round-trip unmarshal failed: %v", err)
	}
	if len(w2.From.States) != 1 || w2.From.States[0] != "Pending" {
		t.Errorf("round-trip: got %v, want [Pending]", w2.From.States)
	}
}

func TestTransitionSource_MarshalYAML_Multiple(t *testing.T) {
	type wrapper struct {
		From schema.TransitionSource `yaml:"from"`
	}
	w := wrapper{From: schema.TransitionSource{States: []string{"Pending", "Running"}}}
	data, err := yaml_marshal(&w)
	if err != nil {
		t.Fatalf("MarshalYAML failed: %v", err)
	}
	// Should round-trip as list
	var w2 wrapper
	if err := yaml_unmarshal(data, &w2); err != nil {
		t.Fatalf("round-trip unmarshal failed: %v", err)
	}
	if len(w2.From.States) != 2 {
		t.Fatalf("round-trip: got %d states, want 2", len(w2.From.States))
	}
}

func TestTransitionSource_MarshalYAML_Empty(t *testing.T) {
	type wrapper struct {
		From schema.TransitionSource `yaml:"from"`
	}
	w := wrapper{From: schema.TransitionSource{States: []string{}}}
	data, err := yaml_marshal(&w)
	if err != nil {
		t.Fatalf("MarshalYAML failed: %v", err)
	}
	// Empty list should marshal to list (not single string)
	var w2 wrapper
	if err := yaml_unmarshal(data, &w2); err != nil {
		t.Fatalf("round-trip unmarshal failed: %v", err)
	}
	if len(w2.From.States) != 0 {
		t.Errorf("round-trip: got %v, want empty", w2.From.States)
	}
}

// --- TransitionParam tests ---

func TestTransitionParam_UnmarshalYAML(t *testing.T) {
	tests := []struct {
		name     string
		yaml     string
		wantName string
		wantType string
	}{
		{
			name:     "string type",
			yaml:     "tunnelID: string",
			wantName: "tunnelID",
			wantType: "string",
		},
		{
			name:     "int type",
			yaml:     "count: int",
			wantName: "count",
			wantType: "int",
		},
		{
			name:     "bool type",
			yaml:     "enabled: bool",
			wantName: "enabled",
			wantType: "bool",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var p schema.TransitionParam
			if err := yaml_unmarshal([]byte(tc.yaml), &p); err != nil {
				t.Fatalf("UnmarshalYAML failed: %v", err)
			}
			if p.Name != tc.wantName {
				t.Errorf("Name = %q, want %q", p.Name, tc.wantName)
			}
			if p.Type != tc.wantType {
				t.Errorf("Type = %q, want %q", p.Type, tc.wantType)
			}
		})
	}
}

func TestTransitionParam_MarshalYAML_RoundTrip(t *testing.T) {
	original := schema.TransitionParam{Name: "tunnelID", Type: "string"}
	data, err := yaml_marshal(original)
	if err != nil {
		t.Fatalf("MarshalYAML failed: %v", err)
	}
	var got schema.TransitionParam
	if err := yaml_unmarshal(data, &got); err != nil {
		t.Fatalf("round-trip unmarshal failed: %v", err)
	}
	if got.Name != original.Name || got.Type != original.Type {
		t.Errorf("round-trip: got {%q, %q}, want {%q, %q}", got.Name, got.Type, original.Name, original.Type)
	}
}

// --- State.Resolve tests ---

func TestState_Resolve_NoFieldGroups(t *testing.T) {
	s := schema.State{
		Name:   "Pending",
		Fields: map[string]string{"tunnelID": "string"},
	}
	resolved := s.Resolve(nil)
	if resolved.AllFields["tunnelID"] != "string" {
		t.Errorf("AllFields[tunnelID] = %q, want %q", resolved.AllFields["tunnelID"], "string")
	}
	if len(resolved.AllFields) != 1 {
		t.Errorf("len(AllFields) = %d, want 1", len(resolved.AllFields))
	}
}

func TestState_Resolve_WithFieldGroups(t *testing.T) {
	groups := map[string]schema.FieldGroup{
		"common": {"id": "string", "name": "string"},
	}
	s := schema.State{
		Name:        "Pending",
		FieldGroups: []string{"common"},
	}
	resolved := s.Resolve(groups)
	if resolved.AllFields["id"] != "string" {
		t.Errorf("AllFields[id] = %q, want string", resolved.AllFields["id"])
	}
	if resolved.AllFields["name"] != "string" {
		t.Errorf("AllFields[name] = %q, want string", resolved.AllFields["name"])
	}
	if len(resolved.AllFields) != 2 {
		t.Errorf("len(AllFields) = %d, want 2", len(resolved.AllFields))
	}
}

func TestState_Resolve_DirectFieldsOverrideGroups(t *testing.T) {
	groups := map[string]schema.FieldGroup{
		"common": {"id": "string", "count": "int"},
	}
	s := schema.State{
		Name:        "Running",
		FieldGroups: []string{"common"},
		Fields:      map[string]string{"count": "int64"}, // override
	}
	resolved := s.Resolve(groups)
	if resolved.AllFields["count"] != "int64" {
		t.Errorf("AllFields[count] = %q, want int64 (direct field should override group)", resolved.AllFields["count"])
	}
	if resolved.AllFields["id"] != "string" {
		t.Errorf("AllFields[id] = %q, want string (from group)", resolved.AllFields["id"])
	}
}

func TestState_Resolve_MultipleFieldGroups(t *testing.T) {
	groups := map[string]schema.FieldGroup{
		"ids":   {"tunnelID": "string"},
		"times": {"createdAt": "string"},
	}
	s := schema.State{
		Name:        "Active",
		FieldGroups: []string{"ids", "times"},
	}
	resolved := s.Resolve(groups)
	if len(resolved.AllFields) != 2 {
		t.Errorf("len(AllFields) = %d, want 2", len(resolved.AllFields))
	}
	if resolved.AllFields["tunnelID"] != "string" {
		t.Error("expected tunnelID from ids group")
	}
	if resolved.AllFields["createdAt"] != "string" {
		t.Error("expected createdAt from times group")
	}
}

func TestState_Resolve_MissingGroupIgnored(t *testing.T) {
	groups := map[string]schema.FieldGroup{
		"existing": {"foo": "string"},
	}
	s := schema.State{
		Name:        "Pending",
		FieldGroups: []string{"existing", "missing"},
	}
	// Should not panic; missing group is silently ignored
	resolved := s.Resolve(groups)
	if resolved.AllFields["foo"] != "string" {
		t.Errorf("AllFields[foo] = %q, want string", resolved.AllFields["foo"])
	}
	if len(resolved.AllFields) != 1 {
		t.Errorf("len(AllFields) = %d, want 1 (missing group should be ignored)", len(resolved.AllFields))
	}
}

func TestState_Resolve_NilGroups(t *testing.T) {
	s := schema.State{
		Name:   "Pending",
		Fields: map[string]string{"x": "string"},
	}
	// nil groups map should not panic
	resolved := s.Resolve(nil)
	if resolved.AllFields["x"] != "string" {
		t.Errorf("AllFields[x] = %q, want string", resolved.AllFields["x"])
	}
}

func TestState_Resolve_PreservesStateFields(t *testing.T) {
	s := schema.State{
		Name:     "Pending",
		Initial:  true,
		Terminal: false,
		Error:    false,
		Deletion: false,
	}
	resolved := s.Resolve(nil)
	if resolved.Name != "Pending" {
		t.Errorf("resolved.Name = %q, want Pending", resolved.Name)
	}
	if !resolved.Initial {
		t.Error("expected Initial to be preserved")
	}
}

func TestState_Resolve_EmptyFields(t *testing.T) {
	s := schema.State{Name: "Pending"}
	resolved := s.Resolve(map[string]schema.FieldGroup{})
	if len(resolved.AllFields) != 0 {
		t.Errorf("expected empty AllFields, got %v", resolved.AllFields)
	}
}

// yaml_unmarshal and yaml_marshal are thin wrappers to allow easy YAML testing.
func yaml_unmarshal(data []byte, v interface{}) error {
	return yaml.Unmarshal(data, v)
}

func yaml_marshal(v interface{}) ([]byte, error) {
	return yaml.Marshal(v)
}
