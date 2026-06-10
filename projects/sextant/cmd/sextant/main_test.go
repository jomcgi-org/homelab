// Package main provides tests for the sextant CLI entry point.
//
// Because main() calls os.Exit(1) when cmd.Execute() returns an error, these
// tests use the helper-process (subprocess) pattern to exercise both branches
// without terminating the test runner.
//
// TestMain_HelperProcess acts as the subprocess sentinel: when invoked by the
// parent test with GO_WANT_HELPER_PROCESS=1, it reconstructs os.Args from
// the SEXTANT_TEST_SUBCMD and SEXTANT_TEST_FILE environment variables and
// calls main() directly. The parent test then checks the subprocess exit code.
package main

import (
	"os"
	"os/exec"
	"testing"
)

// validStateMachineYAML is a minimal valid sextant state machine definition
// used to exercise the success path of main().
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

// writeTestYAMLFile creates a temporary YAML file with the given content and
// returns its absolute path. The file is removed when the test completes.
func writeTestYAMLFile(t *testing.T, content string) string {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "*.sextant.yaml")
	if err != nil {
		t.Fatalf("failed to create temp YAML file: %v", err)
	}
	if _, err := f.WriteString(content); err != nil {
		t.Fatalf("failed to write temp YAML file: %v", err)
	}
	if err := f.Close(); err != nil {
		t.Fatalf("failed to close temp YAML file: %v", err)
	}
	return f.Name()
}

// TestMain_HelperProcess is the subprocess sentinel. When the environment
// variable GO_WANT_HELPER_PROCESS is "1", this test reconstructs os.Args
// from SEXTANT_TEST_SUBCMD and SEXTANT_TEST_FILE and invokes main() directly.
//
// This function MUST NOT be called as a regular test: when
// GO_WANT_HELPER_PROCESS is not set it is immediately skipped.
func TestMain_HelperProcess(t *testing.T) {
	if os.Getenv("GO_WANT_HELPER_PROCESS") != "1" {
		t.Skip("not a helper subprocess")
	}

	subCmd := os.Getenv("SEXTANT_TEST_SUBCMD")
	file := os.Getenv("SEXTANT_TEST_FILE")

	// Replace os.Args so that cobra parses the controlled sub-command and
	// file path, not the test-runner flags.
	os.Args = []string{"sextant", subCmd, file}

	main()

	// Explicit exit 0 in the success path so that the test framework's own
	// teardown cannot accidentally influence the observed exit code.
	os.Exit(0)
}

// TestMain_ExitsZeroOnSuccess verifies that main() exits with code 0 when
// cmd.Execute() succeeds. A valid state machine YAML file is provided so that
// the validate subcommand returns no error.
func TestMain_ExitsZeroOnSuccess(t *testing.T) {
	yamlFile := writeTestYAMLFile(t, validStateMachineYAML)

	cmd := exec.Command(os.Args[0], "-test.run=^TestMain_HelperProcess$")
	cmd.Env = append(os.Environ(),
		"GO_WANT_HELPER_PROCESS=1",
		"SEXTANT_TEST_SUBCMD=validate",
		"SEXTANT_TEST_FILE="+yamlFile,
	)

	if err := cmd.Run(); err != nil {
		t.Errorf("expected exit code 0 for valid YAML input, got: %v", err)
	}
}

// TestMain_ExitsOneOnError verifies that main() calls os.Exit(1) when
// cmd.Execute() returns an error. A nonexistent file path is provided to
// trigger the file-not-found error inside the validate subcommand, causing
// cmd.Execute() to return an error and main() to call os.Exit(1).
func TestMain_ExitsOneOnError(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-test.run=^TestMain_HelperProcess$")
	cmd.Env = append(os.Environ(),
		"GO_WANT_HELPER_PROCESS=1",
		"SEXTANT_TEST_SUBCMD=validate",
		"SEXTANT_TEST_FILE=/nonexistent/does-not-exist.sextant.yaml",
	)

	err := cmd.Run()
	if err == nil {
		t.Fatal("expected exit code 1 for nonexistent input file, got nil (exit code 0)")
	}

	exitErr, ok := err.(*exec.ExitError)
	if !ok {
		t.Fatalf("expected *exec.ExitError, got %T: %v", err, err)
	}
	if exitErr.ExitCode() != 1 {
		t.Errorf("expected exit code 1, got %d", exitErr.ExitCode())
	}
}
