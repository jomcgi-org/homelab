package cmd

// validate_test.go provides focused unit tests for runValidate (validate.go).
//
// Coverage targets:
//   - Valid file returns nil (happy path)
//   - Invalid YAML returns a non-nil error
//   - Missing input file returns a non-nil error

import (
	"testing"

	"github.com/spf13/cobra"
)

// TestValidate_ValidFile_ReturnsNil verifies the happy path: a well-formed
// state machine YAML file is validated without error.
func TestValidate_ValidFile_ReturnsNil(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, validStateMachineYAML)
	if err := runValidate(&cobra.Command{}, []string{filePath}); err != nil {
		t.Fatalf("expected nil error for valid YAML, got: %v", err)
	}
}

// TestValidate_InvalidYAML_ReturnsError verifies that runValidate returns a
// non-nil error when the input file contains structurally invalid YAML.
func TestValidate_InvalidYAML_ReturnsError(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, invalidYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected error for malformed YAML, got nil")
	}
	if err.Error() == "" {
		t.Error("expected non-empty error message for malformed YAML")
	}
}

// TestValidate_MissingFile_ReturnsError verifies that runValidate returns a
// non-nil error when the input file path does not exist on disk.
func TestValidate_MissingFile_ReturnsError(t *testing.T) {
	defer resetValidateFlags()

	err := runValidate(&cobra.Command{}, []string{"/nonexistent/no-such.sextant.yaml"})
	if err == nil {
		t.Fatal("expected error for missing input file, got nil")
	}
	if err.Error() == "" {
		t.Error("expected non-empty error message for missing input file")
	}
}
