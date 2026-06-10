package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

const validStateMachineYAML = `apiVersion: sextant.jomcgi.dev/v1alpha1
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

const invalidYAML = `not: valid: yaml: [[[`

const missingInitialStateYAML = `apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: TestResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
  - name: Ready
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
`

func writeYAMLFile(t *testing.T, content string) string {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "*.sextant.yaml")
	if err != nil {
		t.Fatalf("failed to create temp file: %v", err)
	}
	if _, err := f.WriteString(content); err != nil {
		t.Fatalf("failed to write temp file: %v", err)
	}
	if err := f.Close(); err != nil {
		t.Fatalf("failed to close temp file: %v", err)
	}
	return f.Name()
}

func resetValidateFlags() {
	validateOutputXState = false
	validateOutputPath = ""
}

func resetGenerateFlags() {
	generateOutputDir = "./pkg/statemachine"
	generatePackage = ""
	generateModule = ""
	generateAPIImportPath = ""
}

// ---- validate tests ----

func TestRunValidate_ValidFile(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}
}

func TestRunValidate_FileNotFound(t *testing.T) {
	defer resetValidateFlags()

	err := runValidate(&cobra.Command{}, []string{"/nonexistent/path/does-not-exist.sextant.yaml"})
	if err == nil {
		t.Fatal("expected error for non-existent file, got nil")
	}
}

func TestRunValidate_InvalidYAML(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, invalidYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected error for invalid YAML, got nil")
	}
}

func TestRunValidate_MissingInitialState(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, missingInitialStateYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected validation error for missing initial state, got nil")
	}
	if !strings.Contains(err.Error(), "initial") {
		t.Errorf("expected error to mention 'initial', got: %v", err)
	}
}

func TestRunValidate_WithXStateFlag_WritesToStdout(t *testing.T) {
	defer resetValidateFlags()

	validateOutputXState = true

	filePath := writeYAMLFile(t, validStateMachineYAML)

	// Capture stdout
	origStdout := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("failed to create pipe: %v", err)
	}
	os.Stdout = w

	runErr := runValidate(&cobra.Command{}, []string{filePath})

	w.Close()
	os.Stdout = origStdout

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}

	buf := make([]byte, 65536)
	n, _ := r.Read(buf)
	output := string(buf[:n])

	if !json.Valid([]byte(output)) {
		t.Errorf("expected valid JSON on stdout, got: %s", output)
	}
}

func TestRunValidate_WithXStateAndOutputFile(t *testing.T) {
	defer resetValidateFlags()

	validateOutputXState = true
	outFile := filepath.Join(t.TempDir(), "out.xstate.json")
	validateOutputPath = outFile

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	content, err := os.ReadFile(outFile)
	if err != nil {
		t.Fatalf("expected output file to exist: %v", err)
	}
	if !json.Valid(content) {
		t.Errorf("expected valid JSON in output file, got: %s", content)
	}
}

func TestValidateCmd_RequiresExactlyOneArg(t *testing.T) {
	cmd := validateCmd

	if err := cmd.Args(cmd, []string{}); err == nil {
		t.Error("expected error with 0 args, got nil")
	}
	if err := cmd.Args(cmd, []string{"a", "b"}); err == nil {
		t.Error("expected error with 2 args, got nil")
	}
	if err := cmd.Args(cmd, []string{"a"}); err != nil {
		t.Errorf("expected no error with 1 arg, got: %v", err)
	}
}

// ---- generate tests ----

func TestRunGenerate_ValidFile(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "testpkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in output directory, found none")
	}
}

func TestRunGenerate_FileNotFound(t *testing.T) {
	defer resetGenerateFlags()

	generateOutputDir = t.TempDir()
	generatePackage = "testpkg"

	err := runGenerate(&cobra.Command{}, []string{"/nonexistent/path/does-not-exist.sextant.yaml"})
	if err == nil {
		t.Fatal("expected error for non-existent file, got nil")
	}
}

func TestRunGenerate_InvalidYAML(t *testing.T) {
	defer resetGenerateFlags()

	generateOutputDir = t.TempDir()
	generatePackage = "testpkg"

	filePath := writeYAMLFile(t, invalidYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected error for invalid YAML, got nil")
	}
}

func TestRunGenerate_CustomPackageName(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "mysm"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("expected generated files, found none")
	}

	// Verify at least one generated file uses the custom package name
	found := false
	for _, e := range entries {
		content, err := os.ReadFile(filepath.Join(outDir, e.Name()))
		if err != nil {
			continue
		}
		if strings.Contains(string(content), "package mysm") {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected at least one generated file to contain 'package mysm'")
	}
}

func TestRunGenerate_DefaultsPackageFromDirName(t *testing.T) {
	defer resetGenerateFlags()

	outDir := filepath.Join(t.TempDir(), "mypkg")
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		t.Fatalf("failed to create output dir: %v", err)
	}
	generateOutputDir = outDir
	generatePackage = "" // let it default from dir name

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("expected generated files, found none")
	}

	found := false
	for _, e := range entries {
		content, err := os.ReadFile(filepath.Join(outDir, e.Name()))
		if err != nil {
			continue
		}
		if strings.Contains(string(content), "package mypkg") {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected at least one generated file to contain 'package mypkg' (derived from dir name)")
	}
}

func TestRunGenerate_WithModuleAndAPIPath(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "testpkg"
	generateModule = "github.com/example/operator"
	generateAPIImportPath = "github.com/example/operator/api/v1alpha1"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error with module and api flags, got: %v", err)
	}
}

func TestGenerateCmd_RequiresExactlyOneArg(t *testing.T) {
	cmd := generateCmd

	if err := cmd.Args(cmd, []string{}); err == nil {
		t.Error("expected error with 0 args, got nil")
	}
	if err := cmd.Args(cmd, []string{"a", "b"}); err == nil {
		t.Error("expected error with 2 args, got nil")
	}
	if err := cmd.Args(cmd, []string{"a"}); err != nil {
		t.Errorf("expected no error with 1 arg, got: %v", err)
	}
}

// TestRunGenerate_AutoDerivesAPIPathFromModule verifies the code path (lines
// 73-75 in generate.go) that constructs the API import path as
// "<module>/api/<version>" when --module is set but --api is omitted.
func TestRunGenerate_AutoDerivesAPIPathFromModule(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "testpkg"
	generateModule = "github.com/example/myoperator"
	generateAPIImportPath = "" // intentionally empty — should be auto-derived

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error when module is set and --api is omitted, got: %v", err)
	}

	// Generated files must exist, confirming code generation ran successfully.
	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in output directory, found none")
	}
}

// TestRunGenerate_CodeGenerationFailed verifies that runGenerate wraps the
// error with "code generation failed:" when the generator's Generate call
// fails. We trigger this by pointing --output at a path where a regular file
// already exists at the location the generator would use as a directory,
// causing os.MkdirAll to fail inside Generate().
func TestRunGenerate_CodeGenerationFailed(t *testing.T) {
	defer resetGenerateFlags()

	// Create a regular file where the output directory is expected.
	// os.MkdirAll will fail because the path is a file, not a directory.
	parent := t.TempDir()
	blockerFile := filepath.Join(parent, "output")
	if err := os.WriteFile(blockerFile, []byte("blocker"), 0o644); err != nil {
		t.Fatalf("failed to create blocker file: %v", err)
	}

	// Point the output directory at the file so MkdirAll fails.
	generateOutputDir = filepath.Join(blockerFile, "subdir")
	generatePackage = "testpkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected error when output dir is beneath a file, got nil")
	}
	if !strings.Contains(err.Error(), "code generation failed:") {
		t.Errorf("expected error to contain 'code generation failed:', got: %v", err)
	}
}

// TestRunValidate_OutputFileWriteError verifies that runValidate returns an
// error when --xstate and --output are set but the output path is not writable
// (e.g., the parent directory does not exist).
func TestRunValidate_OutputFileWriteError(t *testing.T) {
	defer resetValidateFlags()

	validateOutputXState = true
	validateOutputPath = filepath.Join(t.TempDir(), "nonexistent-dir", "out.json")

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected error when output file path is unwritable, got nil")
	}
}

// ---- Execute() integration tests (full Cobra command tree) ----
//
// The tests above call runValidate/runGenerate directly, bypassing Cobra's
// argument parsing, flag binding, and subcommand routing. The tests below call
// Execute() (which in turn calls rootCmd.Execute()) to exercise the full
// command tree end-to-end: subcommand dispatch, flag parsing, ExactArgs
// enforcement, and error propagation back to the caller.

// setRootArgs is a helper that sets rootCmd args for a test and registers a
// cleanup to reset them, ensuring subsequent tests use their own args.
func setRootArgs(t *testing.T, args []string) {
	t.Helper()
	rootCmd.SetArgs(args)
	t.Cleanup(func() { rootCmd.SetArgs(nil) })
}

// TestExecute_NoArgs exercises the root command with no subcommand. Cobra
// prints usage and returns nil when the root command has no RunE of its own.
func TestExecute_NoArgs(t *testing.T) {
	setRootArgs(t, []string{})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error from root command with no args, got: %v", err)
	}
}

// TestExecute_UnknownSubcommand verifies that an unrecognised subcommand
// propagates an error back through Execute().
func TestExecute_UnknownSubcommand(t *testing.T) {
	setRootArgs(t, []string{"unknown-subcommand"})

	err := Execute()
	if err == nil {
		t.Fatal("expected error for unknown subcommand, got nil")
	}
}

// TestExecute_Validate_ValidFile exercises the full validate path via Execute()
// with a well-formed YAML file. Verifies subcommand routing and RunE success.
func TestExecute_Validate_ValidFile(t *testing.T) {
	defer resetValidateFlags()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"validate", filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error for valid file, got: %v", err)
	}
}

// TestExecute_Validate_FileNotFound verifies error propagation when the input
// file does not exist.
func TestExecute_Validate_FileNotFound(t *testing.T) {
	defer resetValidateFlags()
	setRootArgs(t, []string{"validate", "/nonexistent/path/does-not-exist.sextant.yaml"})

	err := Execute()
	if err == nil {
		t.Fatal("expected error for nonexistent file, got nil")
	}
}

// TestExecute_Validate_InvalidYAML verifies error propagation when the input
// file contains invalid YAML.
func TestExecute_Validate_InvalidYAML(t *testing.T) {
	defer resetValidateFlags()
	filePath := writeYAMLFile(t, invalidYAML)
	setRootArgs(t, []string{"validate", filePath})

	err := Execute()
	if err == nil {
		t.Fatal("expected error for invalid YAML, got nil")
	}
}

// TestExecute_Validate_NoArgs_ReturnsError verifies that Cobra's ExactArgs(1)
// enforcement returns an error when validate is called without a file argument.
func TestExecute_Validate_NoArgs_ReturnsError(t *testing.T) {
	defer resetValidateFlags()
	setRootArgs(t, []string{"validate"})

	err := Execute()
	if err == nil {
		t.Fatal("expected error when validate called with no args, got nil")
	}
}

// TestExecute_Validate_TooManyArgs_ReturnsError verifies ExactArgs(1) rejection
// of more than one positional argument.
func TestExecute_Validate_TooManyArgs_ReturnsError(t *testing.T) {
	defer resetValidateFlags()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"validate", filePath, "extra-arg"})

	err := Execute()
	if err == nil {
		t.Fatal("expected error when validate called with too many args, got nil")
	}
}

// TestExecute_Validate_XStateFlag_WritesToStdout verifies that the --xstate flag
// is correctly parsed from the command line and results in JSON written to stdout.
func TestExecute_Validate_XStateFlag_WritesToStdout(t *testing.T) {
	defer resetValidateFlags()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"validate", "--xstate", filePath})

	// Capture stdout so we can inspect the JSON output.
	origStdout := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("failed to create pipe: %v", err)
	}
	os.Stdout = w

	runErr := Execute()

	w.Close()
	os.Stdout = origStdout

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}

	buf := make([]byte, 65536)
	n, _ := r.Read(buf)
	output := string(buf[:n])

	if !json.Valid([]byte(output)) {
		t.Errorf("expected valid JSON on stdout with --xstate flag, got: %s", output)
	}
}

// TestExecute_Validate_XStateOutputFile verifies that --xstate combined with
// --output writes valid JSON to the specified file.
func TestExecute_Validate_XStateOutputFile(t *testing.T) {
	defer resetValidateFlags()
	outFile := filepath.Join(t.TempDir(), "out.xstate.json")
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"validate", "--xstate", "--output", outFile, filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	content, err := os.ReadFile(outFile)
	if err != nil {
		t.Fatalf("expected output file to exist: %v", err)
	}
	if !json.Valid(content) {
		t.Errorf("expected valid JSON in output file, got: %s", content)
	}
}

// TestExecute_Validate_XStateShortOutputFlag verifies the short -o flag alias
// for --output is honoured when routing through the full command tree.
func TestExecute_Validate_XStateShortOutputFlag(t *testing.T) {
	defer resetValidateFlags()
	outFile := filepath.Join(t.TempDir(), "short.xstate.json")
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"validate", "--xstate", "-o", outFile, filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error with short -o flag, got: %v", err)
	}

	content, err := os.ReadFile(outFile)
	if err != nil {
		t.Fatalf("expected output file to exist: %v", err)
	}
	if !json.Valid(content) {
		t.Errorf("expected valid JSON in output file, got: %s", content)
	}
}

// TestExecute_Generate_ValidFile exercises the full generate path via Execute().
// Verifies that subcommand routing and flag parsing produce output files.
func TestExecute_Generate_ValidFile(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"generate", "--output", outDir, "--package", "testpkg", filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error for valid file, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in output directory, found none")
	}
}

// TestExecute_Generate_FileNotFound verifies error propagation for a missing
// input file through the full Cobra command tree.
func TestExecute_Generate_FileNotFound(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	setRootArgs(t, []string{"generate", "--output", outDir, "--package", "testpkg", "/nonexistent/file.sextant.yaml"})

	err := Execute()
	if err == nil {
		t.Fatal("expected error for nonexistent file, got nil")
	}
}

// TestExecute_Generate_InvalidYAML verifies error propagation for malformed
// YAML through the full Cobra command tree.
func TestExecute_Generate_InvalidYAML(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	filePath := writeYAMLFile(t, invalidYAML)
	setRootArgs(t, []string{"generate", "--output", outDir, "--package", "testpkg", filePath})

	err := Execute()
	if err == nil {
		t.Fatal("expected error for invalid YAML, got nil")
	}
}

// TestExecute_Generate_NoArgs_ReturnsError verifies ExactArgs(1) enforcement
// for the generate subcommand when no positional argument is provided.
func TestExecute_Generate_NoArgs_ReturnsError(t *testing.T) {
	defer resetGenerateFlags()
	setRootArgs(t, []string{"generate"})

	err := Execute()
	if err == nil {
		t.Fatal("expected error when generate called with no args, got nil")
	}
}

// TestExecute_Generate_ShortOutputFlag verifies that the -o short flag alias
// for --output is correctly parsed through the full command tree.
func TestExecute_Generate_ShortOutputFlag(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"generate", "-o", outDir, "-p", "shortpkg", filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error with short -o/-p flags, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in output directory, found none")
	}

	// Verify the short -p flag was correctly parsed and used as the package name.
	found := false
	for _, e := range entries {
		content, err := os.ReadFile(filepath.Join(outDir, e.Name()))
		if err != nil {
			continue
		}
		if strings.Contains(string(content), "package shortpkg") {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected at least one generated file to contain 'package shortpkg'")
	}
}

// TestExecute_Generate_WithModuleAndAPIFlags verifies that --module and --api
// flags are correctly parsed and forwarded through the full command tree.
func TestExecute_Generate_WithModuleAndAPIFlags(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{
		"generate",
		"--output", outDir,
		"--package", "testpkg",
		"--module", "github.com/example/operator",
		"--api", "github.com/example/operator/api/v1alpha1",
		filePath,
	})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error with --module and --api flags, got: %v", err)
	}
}

// TestExecute_Generate_AutoDerivesAPIPath verifies that when --module is set
// but --api is omitted the API import path is auto-derived through the full
// command tree (exercises the branch at generate.go lines 73-75).
func TestExecute_Generate_AutoDerivesAPIPath(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{
		"generate",
		"--output", outDir,
		"--package", "testpkg",
		"--module", "github.com/example/myoperator",
		filePath,
	})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error when --module is set and --api omitted, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in output directory, found none")
	}
}

// TestExecute_Generate_DefaultsPackageFromDirName verifies that omitting
// --package causes the package name to be derived from the output directory
// name when routed through the full command tree.
func TestExecute_Generate_DefaultsPackageFromDirName(t *testing.T) {
	defer resetGenerateFlags()
	outDir := filepath.Join(t.TempDir(), "mynamedpkg")
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		t.Fatalf("failed to create output dir: %v", err)
	}
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"generate", "--output", outDir, filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error when --package omitted, got: %v", err)
	}

	found := false
	entries, _ := os.ReadDir(outDir)
	for _, e := range entries {
		content, err := os.ReadFile(filepath.Join(outDir, e.Name()))
		if err != nil {
			continue
		}
		if strings.Contains(string(content), "package mynamedpkg") {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected at least one generated file to contain 'package mynamedpkg'")
	}
}

// ---- --help flag tests ----
//
// Cobra returns nil (not an error) when --help is passed, because help output
// is informational. These tests verify that the --help flag is handled
// gracefully at the root level and for each subcommand.

// TestExecute_HelpFlag verifies that Execute() with --help returns nil.
// Cobra treats --help as a special built-in flag that prints usage and exits
// successfully (exit code 0), not as an error.
func TestExecute_HelpFlag(t *testing.T) {
	setRootArgs(t, []string{"--help"})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error with --help, got: %v", err)
	}
}

// TestExecute_Validate_HelpFlag verifies that 'validate --help' returns nil.
// This exercises the subcommand help path for the validate command.
func TestExecute_Validate_HelpFlag(t *testing.T) {
	defer resetValidateFlags()
	setRootArgs(t, []string{"validate", "--help"})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error with 'validate --help', got: %v", err)
	}
}

// TestExecute_Generate_HelpFlag verifies that 'generate --help' returns nil.
// This exercises the subcommand help path for the generate command.
func TestExecute_Generate_HelpFlag(t *testing.T) {
	defer resetGenerateFlags()
	setRootArgs(t, []string{"generate", "--help"})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error with 'generate --help', got: %v", err)
	}
}
