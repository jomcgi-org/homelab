package schema

import (
	"testing"
)

// ---------------------------------------------------------------------------
// collectStateNames tests (internal package — access to unexported helper)
// ---------------------------------------------------------------------------

func TestCollectStateNames(t *testing.T) {
	tests := []struct {
		name   string
		states []State
		want   map[string]bool
	}{
		{
			name: "NormalStates",
			states: []State{
				{Name: "Pending"},
				{Name: "Creating"},
				{Name: "Ready"},
			},
			want: map[string]bool{
				"Pending":  true,
				"Creating": true,
				"Ready":    true,
			},
		},
		{
			name:   "EmptySlice",
			states: []State{},
			want:   map[string]bool{},
		},
		{
			name:   "NilSlice",
			states: nil,
			want:   map[string]bool{},
		},
		{
			name: "DuplicateNames",
			// The function unconditionally sets names[s.Name] = true, so
			// duplicates simply overwrite — no panic, the last write wins and
			// the key is still present.
			states: []State{
				{Name: "Pending"},
				{Name: "Pending"},
				{Name: "Ready"},
			},
			want: map[string]bool{
				"Pending": true,
				"Ready":   true,
			},
		},
		{
			name: "SingleState",
			states: []State{
				{Name: "OnlyState"},
			},
			want: map[string]bool{
				"OnlyState": true,
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := collectStateNames(tt.states)

			// Check length matches.
			if len(got) != len(tt.want) {
				t.Errorf("collectStateNames() len = %d, want %d; got %v", len(got), len(tt.want), got)
			}

			// Check every expected key is present.
			for name, wantVal := range tt.want {
				gotVal, ok := got[name]
				if !ok {
					t.Errorf("collectStateNames() missing key %q", name)
					continue
				}
				if gotVal != wantVal {
					t.Errorf("collectStateNames()[%q] = %v, want %v", name, gotVal, wantVal)
				}
			}

			// Check no unexpected keys are present.
			for name := range got {
				if _, ok := tt.want[name]; !ok {
					t.Errorf("collectStateNames() unexpected key %q", name)
				}
			}
		})
	}
}
