package codegen

import (
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

func TestCamelToSnake(t *testing.T) {
	tests := []struct{ input, want string }{
		{"CloudflareTunnel", "cloudflare_tunnel"},
		{"ModelCache", "model_cache"},
		{"Simple", "simple"},
		{"ABC", "a_b_c"},
		{"", ""},
		{"alreadylower", "alreadylower"},
	}
	for _, tt := range tests {
		if got := camelToSnake(tt.input); got != tt.want {
			t.Errorf("camelToSnake(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestToEventName(t *testing.T) {
	tests := []struct{ input, want string }{
		{"MarkReady", "MARK_READY"},
		{"StartTunnelCreation", "START_TUNNEL_CREATION"},
		{"Delete", "DELETE"},
		{"", ""},
	}
	for _, tt := range tests {
		if got := toEventName(tt.input); got != tt.want {
			t.Errorf("toEventName(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestGoType(t *testing.T) {
	tests := []struct{ input, want string }{
		{"string", "string"},
		{"int", "int"},
		{"bool", "bool"},
		{"float64", "float64"},
		{"SomeCustomType", "SomeCustomType"},
		{"", ""},
	}
	for _, tt := range tests {
		if got := goType(tt.input); got != tt.want {
			t.Errorf("goType(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestDefaultValue(t *testing.T) {
	tests := []struct{ input, want string }{
		{"string", `""`},
		{"int", "0"},
		{"int32", "0"},
		{"int64", "0"},
		{"bool", "false"},
		{"float32", "0.0"},
		{"float64", "0.0"},
		{"SomeCustomType", "nil"},
	}
	for _, tt := range tests {
		if got := defaultValue(tt.input); got != tt.want {
			t.Errorf("defaultValue(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestDurationLiteral(t *testing.T) {
	tests := []struct {
		d    schema.Duration
		want string
	}{
		{schema.Duration{}, "0"},
		{schema.Duration{Duration: 30 * time.Second}, "time.Duration(30000000000)"},
		{schema.Duration{Duration: 5 * time.Minute}, "time.Duration(300000000000)"},
	}
	for _, tt := range tests {
		got := durationLiteral(tt.d)
		if got != tt.want {
			t.Errorf("durationLiteral(%v) = %q, want %q", tt.d, got, tt.want)
		}
	}
}

func TestHasRequeue(t *testing.T) {
	withRequeue := StateData{Requeue: schema.Duration{Duration: 30 * time.Second}}
	withoutRequeue := StateData{Requeue: schema.Duration{}}

	if !hasRequeue(withRequeue) {
		t.Error("hasRequeue should be true for state with requeue duration > 0")
	}
	if hasRequeue(withoutRequeue) {
		t.Error("hasRequeue should be false for state with zero requeue duration")
	}
}

func TestFieldGroupName(t *testing.T) {
	tests := []struct{ input, want string }{
		{"common", "Common"},
		{"networkConfig", "NetworkConfig"},
		{"", ""},
	}
	for _, tt := range tests {
		if got := fieldGroupName(tt.input); got != tt.want {
			t.Errorf("fieldGroupName(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestContains(t *testing.T) {
	slice := []string{"a", "b", "c"}
	if !contains(slice, "b") {
		t.Error("contains should return true for existing element")
	}
	if contains(slice, "d") {
		t.Error("contains should return false for missing element")
	}
	if contains([]string{}, "a") {
		t.Error("contains should return false for empty slice")
	}
}

func TestHasFieldInGroup(t *testing.T) {
	groups := []FieldGroupData{
		{Name: "common", Fields: []FieldData{{Name: "networkID", Type: "string"}}},
	}
	field := FieldData{Name: "networkID", Type: "string"}
	otherField := FieldData{Name: "other", Type: "string"}

	if !hasFieldInGroup(field, groups) {
		t.Error("hasFieldInGroup should return true for field in group")
	}
	if hasFieldInGroup(otherField, groups) {
		t.Error("hasFieldInGroup should return false for field not in group")
	}
}
