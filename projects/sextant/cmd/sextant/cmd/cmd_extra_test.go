package cmd

// cmd_extra_test.go contains additional tests that complement the main
// cmd_test.go suite, covering:
//   - stderr output content from runValidate and runGenerate
//   - edge cases not exercised by the primary test file (missing terminal
//     state, --output without --xstate, auto-created output directory)
//   - Execute() integration path for generate with too many args
//   - Complex YAML with guards exercising guard-count output in stderr

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// --- YAML fixtures ---

// missingTerminalStateYAML is a machine whose transition targets a state that does
// not exist in the states list. The validator rejects undefined destination states
// with a "does not exist" error. Note: absent terminal states are intentionally not
// enforced by the validator (treated as a warning, not an error).
const missingTerminalStateYAML = `apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: NoTerminal
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Processing
transitions:
  - from: Pending
    to: Nonexistent
    action: Start
`

// validStateMachineWithGuardsYAML is a well-formed machine that includes a
// guard reference, exercising the guards count in the validate stderr output.
// Guards must be specified as a YAML map (map[string]Guard), not a list.
const validStateMachineWithGuardsYAML = `apiVersion: sextant.jomcgi.dev/v1alpha1
kind: StateMachine
metadata:
  name: GuardedResource
  group: test.io
  version: v1alpha1
status:
  phaseField: phase
states:
  - name: Pending
    initial: true
  - name: Ready
    terminal: true
  - name: Failed
    terminal: true
transitions:
  - from: Pending
    to: Ready
    action: MarkReady
    guard: IsReady
  - from: Pending
    to: Failed
    action: MarkFailed
guards:
  IsReady:
    description: "Check if resource is ready"
`

// captureStderr redirects os.Stderr to a pipe for the duration of fn, then
// restores the original file descriptor and returns whatever was written.
func captureStderr(t *testing.T, fn func()) string {
	t.Helper()
	orig := os.Stderr
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("captureStderr: failed to create pipe: %v", err)
	}
	os.Stderr = w

	fn()

	w.Close()
	os.Stderr = orig

	var buf bytes.Buffer
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatalf("captureStderr: failed to read pipe: %v", err)
	}
	return buf.String()
}

// --- validate stderr output tests ---

// TestRunValidate_StderrContainsResourceName verifies that a successful
// validate writes the state-machine name to stderr.
func TestRunValidate_StderrContainsResourceName(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, validStateMachineYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runValidate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	if !strings.Contains(stderr, "TestResource") {
		t.Errorf("expected stderr to contain 'TestResource', got: %s", stderr)
	}
}

// TestRunValidate_StderrContainsStateCounts verifies that the validate command
// reports the number of states and transitions on stderr.
func TestRunValidate_StderrContainsStateCounts(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, validStateMachineYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runValidate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	// The YAML has 2 states and 1 transition.
	if !strings.Contains(stderr, "States: 2") {
		t.Errorf("expected stderr to contain 'States: 2', got: %s", stderr)
	}
	if !strings.Contains(stderr, "Transitions: 1") {
		t.Errorf("expected stderr to contain 'Transitions: 1', got: %s", stderr)
	}
}

// TestRunValidate_StderrContainsGuardCount verifies that when the machine has
// guards the validate command reports the correct guard count on stderr.
func TestRunValidate_StderrContainsGuardCount(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, validStateMachineWithGuardsYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runValidate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	if !strings.Contains(stderr, "Guards: 1") {
		t.Errorf("expected stderr to contain 'Guards: 1', got: %s", stderr)
	}
}

// TestRunValidate_StderrReportsXStateOutputPath verifies that when --xstate and
// --output are both set the written path is reported on stderr.
func TestRunValidate_StderrReportsXStateOutputPath(t *testing.T) {
	defer resetValidateFlags()

	validateOutputXState = true
	outFile := filepath.Join(t.TempDir(), "out.xstate.json")
	validateOutputPath = outFile

	filePath := writeYAMLFile(t, validStateMachineYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runValidate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	if !strings.Contains(stderr, outFile) {
		t.Errorf("expected stderr to contain the output path %q, got: %s", outFile, stderr)
	}
}

// --- validate edge cases ---

// TestRunValidate_MissingTerminalState verifies that a machine with a transition
// targeting an undefined state fails validation. (Note: absent terminal states are
// not enforced — the validator only requires an initial state and valid references.)
func TestRunValidate_MissingTerminalState(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, missingTerminalStateYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected validation error for undefined transition target, got nil")
	}
	if !strings.Contains(err.Error(), "does not exist") {
		t.Errorf("expected error to mention 'does not exist', got: %v", err)
	}
}

// TestRunValidate_OutputPathIgnoredWhenXStateFalse verifies that setting
// --output without --xstate is silently ignored (no file is written, no error).
func TestRunValidate_OutputPathIgnoredWhenXStateFalse(t *testing.T) {
	defer resetValidateFlags()

	// Set output path but do NOT set xstate flag.
	validateOutputXState = false
	outFile := filepath.Join(t.TempDir(), "should-not-be-created.json")
	validateOutputPath = outFile

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runValidate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	if _, statErr := os.Stat(outFile); statErr == nil {
		t.Error("expected output file NOT to be created when --xstate is false, but it was")
	}
}

// TestRunValidate_XStateOutputContainsStateName verifies that the XState JSON
// written to a file mentions the state machine's states.
func TestRunValidate_XStateOutputContainsStateName(t *testing.T) {
	defer resetValidateFlags()

	validateOutputXState = true
	outFile := filepath.Join(t.TempDir(), "out.xstate.json")
	validateOutputPath = outFile

	filePath := writeYAMLFile(t, validStateMachineYAML)
	if err := runValidate(&cobra.Command{}, []string{filePath}); err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	content, err := os.ReadFile(outFile)
	if err != nil {
		t.Fatalf("expected output file to exist: %v", err)
	}
	if !json.Valid(content) {
		t.Fatalf("expected valid JSON, got: %s", content)
	}
	// State names should appear in the XState JSON output.
	if !strings.Contains(string(content), "Pending") {
		t.Errorf("expected XState JSON to contain state name 'Pending', got: %s", content)
	}
}

// --- generate stderr output tests ---

// TestRunGenerate_StderrContainsResourceName verifies that a successful
// generate writes the state-machine name to stderr.
func TestRunGenerate_StderrContainsResourceName(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "testpkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runGenerate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	if !strings.Contains(stderr, "TestResource") {
		t.Errorf("expected stderr to contain 'TestResource', got: %s", stderr)
	}
}

// TestRunGenerate_StderrContainsOutputDir verifies that the output directory
// path is reported on stderr after successful code generation.
func TestRunGenerate_StderrContainsOutputDir(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "testpkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runGenerate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	if !strings.Contains(stderr, outDir) {
		t.Errorf("expected stderr to contain output dir %q, got: %s", outDir, stderr)
	}
}

// TestRunGenerate_StderrContainsPackageName verifies that the package name is
// reported on stderr after successful code generation.
func TestRunGenerate_StderrContainsPackageName(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "mypkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runGenerate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	if !strings.Contains(stderr, "mypkg") {
		t.Errorf("expected stderr to contain 'mypkg', got: %s", stderr)
	}
}

// TestRunGenerate_StderrContainsStateCounts verifies that the state and
// transition counts are reported on stderr after code generation.
func TestRunGenerate_StderrContainsStateCounts(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "testpkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	var runErr error
	stderr := captureStderr(t, func() {
		runErr = runGenerate(&cobra.Command{}, []string{filePath})
	})

	if runErr != nil {
		t.Fatalf("expected no error, got: %v", runErr)
	}
	// The YAML has 2 states; generate adds 1 for Unknown, so States: 3.
	if !strings.Contains(stderr, "States: 3") {
		t.Errorf("expected stderr to contain 'States: 3' (2 + Unknown), got: %s", stderr)
	}
	if !strings.Contains(stderr, "Transitions: 1") {
		t.Errorf("expected stderr to contain 'Transitions: 1', got: %s", stderr)
	}
}

// --- generate edge cases ---

// TestRunGenerate_ValidationFailsForMissingInitialState verifies that the
// generate command propagates schema validation errors (missing initial state
// is caught before code generation begins).
func TestRunGenerate_ValidationFailsForMissingInitialState(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "testpkg"

	filePath := writeYAMLFile(t, missingInitialStateYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err == nil {
		t.Fatal("expected validation error for missing initial state, got nil")
	}
	if !strings.Contains(err.Error(), "initial") {
		t.Errorf("expected error to mention 'initial', got: %v", err)
	}
}

// TestRunGenerate_CreatesOutputDirIfNotExist verifies that the generator
// creates the output directory when it does not already exist (exercises the
// os.MkdirAll path inside codegen.Generate).
func TestRunGenerate_CreatesOutputDirIfNotExist(t *testing.T) {
	defer resetGenerateFlags()

	// Point to a sub-directory that does not yet exist.
	outDir := filepath.Join(t.TempDir(), "new", "nested", "dir")
	generateOutputDir = outDir
	generatePackage = "newpkg"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error when output dir does not exist, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("expected output dir to be created, but ReadDir failed: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in the auto-created output directory, found none")
	}
}

// TestRunGenerate_ValidYAMLWithGuards verifies that a machine with guard
// references generates code without error.
func TestRunGenerate_ValidYAMLWithGuards(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "guardedpkg"

	filePath := writeYAMLFile(t, validStateMachineWithGuardsYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected no error for machine with guards, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files for machine with guards, found none")
	}
}

// --- Execute() integration tests for missing coverage ---

// TestExecute_Generate_TooManyArgs_ReturnsError verifies ExactArgs(1) rejection
// for the generate subcommand when more than one positional argument is given.
func TestExecute_Generate_TooManyArgs_ReturnsError(t *testing.T) {
	defer resetGenerateFlags()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"generate", filePath, "extra-arg"})

	err := Execute()
	if err == nil {
		t.Fatal("expected error when generate called with too many args, got nil")
	}
}

// TestExecute_Validate_WithGuardedMachine verifies that the full Execute()
// path handles a machine with guards correctly.
func TestExecute_Validate_WithGuardedMachine(t *testing.T) {
	defer resetValidateFlags()
	filePath := writeYAMLFile(t, validStateMachineWithGuardsYAML)
	setRootArgs(t, []string{"validate", filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error for guarded machine, got: %v", err)
	}
}

// TestExecute_Generate_WithGuardedMachine verifies that the full Execute()
// path generates code from a machine with guards.
func TestExecute_Generate_WithGuardedMachine(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	filePath := writeYAMLFile(t, validStateMachineWithGuardsYAML)
	setRootArgs(t, []string{"generate", "--output", outDir, "--package", "guardedpkg", filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error for guarded machine via Execute(), got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files for guarded machine, found none")
	}
}

// TestExecute_Validate_MissingTerminalState verifies that Execute() propagates
// the validation error when a transition targets an undefined state.
func TestExecute_Validate_MissingTerminalState(t *testing.T) {
	defer resetValidateFlags()
	filePath := writeYAMLFile(t, missingTerminalStateYAML)
	setRootArgs(t, []string{"validate", filePath})

	err := Execute()
	if err == nil {
		t.Fatal("expected validation error for undefined transition target, got nil")
	}
}

// TestExecute_Generate_MissingTerminalState verifies that Execute() propagates
// the validation error from generate when a transition targets an undefined state.
func TestExecute_Generate_MissingTerminalState(t *testing.T) {
	defer resetGenerateFlags()
	outDir := t.TempDir()
	filePath := writeYAMLFile(t, missingTerminalStateYAML)
	setRootArgs(t, []string{"generate", "--output", outDir, "--package", "testpkg", filePath})

	err := Execute()
	if err == nil {
		t.Fatal("expected validation error for undefined transition target, got nil")
	}
}

// TestExecute_Generate_CreatesNestedOutputDir verifies that the full command
// tree creates nested output directories that do not yet exist.
func TestExecute_Generate_CreatesNestedOutputDir(t *testing.T) {
	defer resetGenerateFlags()
	outDir := filepath.Join(t.TempDir(), "level1", "level2")
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"generate", "--output", outDir, "--package", "nestedpkg", filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error for nested output dir, got: %v", err)
	}

	if _, statErr := os.Stat(outDir); os.IsNotExist(statErr) {
		t.Error("expected output directory to be created, but it does not exist")
	}
}
