package statemachine

// Dedicated tests for model_cache_phases.go.
//
// The shared test files already verify:
//   - IsKnownPhase accepts all six named phases and the empty initial state
//     (statemachine_test.go)
//   - IsKnownPhase rejects near-miss strings (model_cache_missing_test.go)
//   - AllPhases() returns all six phases in order (model_cache_missing_test.go)
//
// This file adds:
//   - Exact string value assertions for each phase constant (typo guard)
//   - AllPhases() slice mutation safety (caller can mutate returned slice
//     without affecting future AllPhases() calls)
//   - IsKnownPhase rejection of a distinct set of unrelated invalid strings.

import (
	"testing"
)

// =============================================================================
// Phase constant exact values
// =============================================================================

// Each constant must equal its documented string value.
// This guards against silent typos in generated code (e.g. "Resoving" instead of "Resolving").
func TestPhaseConstants_ExactStringValues(t *testing.T) {
	cases := []struct {
		name      string
		got       string
		wantExact string
	}{
		{"PhasePending", PhasePending, "Pending"},
		{"PhaseResolving", PhaseResolving, "Resolving"},
		{"PhaseSyncing", PhaseSyncing, "Syncing"},
		{"PhaseReady", PhaseReady, "Ready"},
		{"PhaseFailed", PhaseFailed, "Failed"},
		{"PhaseUnknown", PhaseUnknown, "Unknown"},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			if tc.got != tc.wantExact {
				t.Errorf("%s = %q, want %q", tc.name, tc.got, tc.wantExact)
			}
		})
	}
}

// =============================================================================
// AllPhases() slice mutation safety
// =============================================================================

// AllPhases() must return a new slice each time so that a caller modifying the
// returned slice does not affect subsequent calls.
func TestAllPhases_MutationSafe(t *testing.T) {
	first := AllPhases()
	if len(first) == 0 {
		t.Fatal("AllPhases() returned an empty slice")
	}

	// Corrupt the first returned slice.
	original := first[0]
	first[0] = "corrupted"

	second := AllPhases()
	if len(second) == 0 {
		t.Fatal("AllPhases() returned an empty slice after mutation")
	}
	if second[0] == "corrupted" {
		t.Errorf("AllPhases() returned the same underlying slice; "+
			"mutating the first call's result corrupted the second call (got %q, want %q)",
			second[0], original)
	}
	if second[0] != original {
		t.Errorf("AllPhases()[0] = %q after mutation experiment, want %q", second[0], original)
	}
}

// =============================================================================
// IsKnownPhase: invalid inputs distinct from those in model_cache_missing_test.go
// =============================================================================

// These invalid strings are different from the near-misses in model_cache_missing_test.go
// to avoid duplication while still verifying the default branch is reachable.
func TestIsKnownPhase_RejectsUnrelatedStrings(t *testing.T) {
	invalids := []string{
		"active",      // different domain entirely
		"running",     // Kubernetes pod phase, not ours
		"completed",   // job phase, not a ModelCache phase
		"terminating", // Kubernetes namespace phase
		"null",        // JSON null as a string
		"0",           // number
		"true",        // boolean
		"  ",          // whitespace only
	}
	for _, phase := range invalids {
		phase := phase
		t.Run(phase, func(t *testing.T) {
			if IsKnownPhase(phase) {
				t.Errorf("IsKnownPhase(%q) = true; want false", phase)
			}
		})
	}
}
