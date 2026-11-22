package review

import (
	"strings"
	"testing"
)

// HOME-758: an invariant file with two equal IDs is invalid.
func TestParseMechanismsRejectsDuplicateIDs(t *testing.T) {
	for name, tc := range map[string]struct {
		data string
		ok   bool
	}{
		"unique":                    {"invariants:\n- id: FACT-1\n  mechanism: [a]\n- id: HOME-758-1\n  mechanism: [b]\n", true},
		"same number, other prefix": {"invariants:\n- id: FACT-14\n  mechanism: []\n- id: PLAT-14\n  mechanism: []\n", true},
		"duplicate":                 {"invariants:\n- id: FACT-14\n  mechanism: [a]\n- id: FACT-14\n  mechanism: [b]\n", false},
		"duplicate ticket ID":       {"invariants:\n- id: HOME-758-1\n  mechanism: []\n- id: HOME-758-1\n  mechanism: []\n", false},
	} {
		err := ParseMechanisms(invariantFile, []byte(tc.data), map[string]bool{})
		if (err == nil) != tc.ok {
			t.Errorf("%s: err = %v, want ok %t", name, err, tc.ok)
		}
		if err != nil && !strings.Contains(err.Error(), "duplicate invariant ID") {
			t.Errorf("%s: err = %v, want a duplicate ID error", name, err)
		}
	}
}
