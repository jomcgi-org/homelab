package cmd

// generate_test.go provides focused unit tests for runGenerate (generate.go).
//
// Coverage targets:
//   - Missing input file returns an error
//   - Valid YAML produces .go output files
//   - Package name defaults to the base name of the output directory
//   - API import path is auto-inferred as <module>/api/<version> when --module
//     is set but --api is omitted

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// TestGenerate_MissingFile_ReturnsError verifies that runGenerate returns a
// non-nil, non-empty error when the input file does not exist.
func TestGenerate_MissingFile_ReturnsError(t *testing.T) {
	defer resetGenerateFlags()

	generateOutputDir = t.TempDir()
	generatePackage = "testpkg"

	err := runGenerate(&cobra.Command{}, []string{"/nonexistent/no-such-file.sextant.yaml"})
	if err == nil {
		t.Fatal("expected error for missing input file, got nil")
	}
	if err.Error() == "" {
		t.Error("expected non-empty error message for missing input file")
	}
}

// TestGenerate_ValidYAML_ProducesGoFiles verifies that runGenerate writes at
// least one file with a .go extension to the output directory when given a
// well-formed YAML definition.
func TestGenerate_ValidYAML_ProducesGoFiles(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "genfilepkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	if err := runGenerate(&cobra.Command{}, []string{filePath}); err != nil {
		t.Fatalf("expected no error for valid YAML, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("expected at least one generated file, found none")
	}

	hasGoFile := false
	for _, e := range entries {
		if filepath.Ext(e.Name()) == ".go" {
			hasGoFile = true
			break
		}
	}
	if !hasGoFile {
		t.Error("expected at least one .go file in the output directory")
	}
}

// TestGenerate_PackageName_DefaultsToOutputDirBasename verifies the branch in
// runGenerate (generate.go lines 68-70) that derives the package name from
// filepath.Base(generateOutputDir) when --package is not set.
// This test specifically exercises the "statemachine" basename, matching the
// default --output value of "./pkg/statemachine".
func TestGenerate_PackageName_DefaultsToOutputDirBasename(t *testing.T) {
	defer resetGenerateFlags()

	// Use a directory whose base name is "statemachine" — matching the
	// default output path "./pkg/statemachine".
	outDir := filepath.Join(t.TempDir(), "statemachine")
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		t.Fatalf("failed to create output dir: %v", err)
	}
	generateOutputDir = outDir
	generatePackage = "" // intentionally empty to trigger basename derivation

	filePath := writeYAMLFile(t, validStateMachineYAML)
	if err := runGenerate(&cobra.Command{}, []string{filePath}); err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil || len(entries) == 0 {
		t.Fatal("expected generated files in output directory")
	}

	// Confirm that at least one generated file declares "package statemachine".
	found := false
	for _, e := range entries {
		content, err := os.ReadFile(filepath.Join(outDir, e.Name()))
		if err != nil {
			continue
		}
		if strings.Contains(string(content), "package statemachine") {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected at least one generated file to declare 'package statemachine' (derived from output dir basename)")
	}
}

// TestGenerate_ModuleWithoutAPI_InfersAPIImportPath verifies that when
// --module is provided but --api is omitted, runGenerate constructs the API
// import path as "<module>/api/<version>" using the version from the YAML
// metadata (generate.go lines 73-75).
//
// The version in validStateMachineYAML is "v1alpha1", so with module
// "github.com/example/inferop" the expected derived path is
// "github.com/example/inferop/api/v1alpha1".
func TestGenerate_ModuleWithoutAPI_InfersAPIImportPath(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "inferpkg"
	generateModule = "github.com/example/inferop"
	generateAPIImportPath = "" // must be empty to trigger auto-derivation

	filePath := writeYAMLFile(t, validStateMachineYAML)
	if err := runGenerate(&cobra.Command{}, []string{filePath}); err != nil {
		t.Fatalf("expected no error when module is set and --api is omitted, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil || len(entries) == 0 {
		t.Fatal("expected generated files in output directory")
	}

	// The derived API import path must appear in at least one generated file.
	expectedPath := "github.com/example/inferop/api/v1alpha1"
	found := false
	for _, e := range entries {
		content, err := os.ReadFile(filepath.Join(outDir, e.Name()))
		if err != nil {
			continue
		}
		if strings.Contains(string(content), expectedPath) {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("expected derived API import path %q to appear in a generated file", expectedPath)
	}
}
