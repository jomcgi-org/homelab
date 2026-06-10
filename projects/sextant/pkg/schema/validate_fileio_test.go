package schema_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// validYAML is a minimal well-formed StateMachine used across file I/O tests.
const validYAML = `apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`

// invalidYAML is not valid YAML syntax.
const invalidYAML = `{this: is: not: [valid yaml`

// schemaViolationYAML is valid YAML but fails schema validation (missing apiVersion).
const schemaViolationYAML = `kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`

func writeTemp(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("writeTemp: %v", err)
	}
	return path
}

// ---------------------------------------------------------------------------
// ParseFile tests
// ---------------------------------------------------------------------------

func TestParseFile(t *testing.T) {
	tests := []struct {
		name      string
		setup     func(dir string) string // returns path to pass to ParseFile
		wantErr   bool
		wantErrIs string // substring expected in error message
		check     func(t *testing.T, sm *schema.StateMachine)
	}{
		{
			name: "ValidYAMLFile",
			setup: func(dir string) string {
				return writeTemp(t, dir, "valid.yaml", validYAML)
			},
			wantErr: false,
			check: func(t *testing.T, sm *schema.StateMachine) {
				if sm == nil {
					t.Fatal("expected non-nil StateMachine")
				}
				if sm.Metadata.Name != "TestResource" {
					t.Errorf("got name %q, want %q", sm.Metadata.Name, "TestResource")
				}
				if len(sm.States) != 2 {
					t.Errorf("got %d states, want 2", len(sm.States))
				}
			},
		},
		{
			name: "NonExistentFilePath",
			setup: func(dir string) string {
				return filepath.Join(dir, "does_not_exist.yaml")
			},
			wantErr:   true,
			wantErrIs: "failed to read file",
		},
		{
			name: "FileWithInvalidYAMLContent",
			setup: func(dir string) string {
				return writeTemp(t, dir, "invalid.yaml", invalidYAML)
			},
			wantErr:   true,
			wantErrIs: "failed to parse YAML",
		},
		{
			name: "FileWithEmptyContent",
			setup: func(dir string) string {
				return writeTemp(t, dir, "empty.yaml", "")
			},
			// Empty YAML unmarshal into zero-value struct succeeds without error.
			wantErr: false,
			check: func(t *testing.T, sm *schema.StateMachine) {
				if sm == nil {
					t.Fatal("expected non-nil StateMachine for empty file")
				}
				// Zero-value: no states, no transitions, empty metadata.
				if len(sm.States) != 0 {
					t.Errorf("expected 0 states for empty file, got %d", len(sm.States))
				}
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			path := tt.setup(dir)

			sm, err := schema.ParseFile(path)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("ParseFile(%q) expected error, got nil", path)
				}
				if tt.wantErrIs != "" && !strings.Contains(err.Error(), tt.wantErrIs) {
					t.Errorf("ParseFile(%q) error = %v, want substring %q", path, err, tt.wantErrIs)
				}
				return
			}
			if err != nil {
				t.Fatalf("ParseFile(%q) unexpected error: %v", path, err)
			}
			if tt.check != nil {
				tt.check(t, sm)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// ValidateAndParse tests
// ---------------------------------------------------------------------------

func TestValidateAndParse(t *testing.T) {
	tests := []struct {
		name      string
		setup     func(dir string) string
		wantErr   bool
		wantErrIs string
		check     func(t *testing.T, sm *schema.StateMachine)
	}{
		{
			name: "ValidFileReturnsParsedStateMachine",
			setup: func(dir string) string {
				return writeTemp(t, dir, "valid.yaml", validYAML)
			},
			wantErr: false,
			check: func(t *testing.T, sm *schema.StateMachine) {
				if sm == nil {
					t.Fatal("expected non-nil StateMachine")
				}
				if sm.APIVersion != "sextant.jomcgi.dev/v1alpha1" {
					t.Errorf("got APIVersion %q, want %q", sm.APIVersion, "sextant.jomcgi.dev/v1alpha1")
				}
				if sm.Kind != "StateMachine" {
					t.Errorf("got Kind %q, want %q", sm.Kind, "StateMachine")
				}
				if len(sm.States) != 2 {
					t.Errorf("got %d states, want 2", len(sm.States))
				}
				if len(sm.Transitions) != 1 {
					t.Errorf("got %d transitions, want 1", len(sm.Transitions))
				}
			},
		},
		{
			name: "FileWithSchemaViolationsReturnsValidationError",
			setup: func(dir string) string {
				return writeTemp(t, dir, "violation.yaml", schemaViolationYAML)
			},
			wantErr:   true,
			wantErrIs: "apiVersion",
		},
		{
			name: "NonExistentFileReturnsError",
			setup: func(dir string) string {
				return filepath.Join(dir, "no_such_file.yaml")
			},
			wantErr:   true,
			wantErrIs: "failed to read file",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			path := tt.setup(dir)

			sm, err := schema.ValidateAndParse(path)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("ValidateAndParse(%q) expected error, got nil", path)
				}
				if tt.wantErrIs != "" && !strings.Contains(err.Error(), tt.wantErrIs) {
					t.Errorf("ValidateAndParse(%q) error = %v, want substring %q", path, err, tt.wantErrIs)
				}
				return
			}
			if err != nil {
				t.Fatalf("ValidateAndParse(%q) unexpected error: %v", path, err)
			}
			if tt.check != nil {
				tt.check(t, sm)
			}
		})
	}
}
