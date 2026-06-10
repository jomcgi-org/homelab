package cmd

// cmd_cli_test.go covers gaps in cmd_test.go and cmd_extra_test.go:
//   - Root command metadata (Use field, subcommand registration, completion disabled)
//   - Execute() stderr output when errors occur
//   - generate -m short flag for --module
//   - generate --api flag used standalone (without --module)
//   - validate --output without --xstate via full Execute() path
//   - validate/generate with missing initial state via full Execute() path
//   - Default flag values for validate and generate commands

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ---- root command metadata tests ----

// TestRootCmd_Use verifies the root command's Use field identifies the binary.
func TestRootCmd_Use(t *testing.T) {
	if rootCmd.Use != "sextant" {
		t.Errorf("expected rootCmd.Use == 'sextant', got: %q", rootCmd.Use)
	}
}

// TestRootCmd_ShortDescription verifies the root command has a non-empty Short
// description that mentions code generation.
func TestRootCmd_ShortDescription(t *testing.T) {
	if rootCmd.Short == "" {
		t.Error("expected rootCmd.Short to be non-empty")
	}
}

// TestRootCmd_SilenceUsage verifies that SilenceUsage is enabled so that Cobra
// does not print usage on every returned error (reduces noise).
func TestRootCmd_SilenceUsage(t *testing.T) {
	if !rootCmd.SilenceUsage {
		t.Error("expected rootCmd.SilenceUsage to be true")
	}
}

// TestRootCmd_SilenceErrors verifies that SilenceErrors is enabled so that
// Cobra does not duplicate error messages (Execute() prints them manually).
func TestRootCmd_SilenceErrors(t *testing.T) {
	if !rootCmd.SilenceErrors {
		t.Error("expected rootCmd.SilenceErrors to be true")
	}
}

// TestRootCmd_HasValidateSubcommand verifies that the validate subcommand is
// registered in the root command's command tree.
func TestRootCmd_HasValidateSubcommand(t *testing.T) {
	found := false
	for _, sub := range rootCmd.Commands() {
		if sub.Use == validateCmd.Use {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected 'validate' to be registered as a subcommand of rootCmd")
	}
}

// TestRootCmd_HasGenerateSubcommand verifies that the generate subcommand is
// registered in the root command's command tree.
func TestRootCmd_HasGenerateSubcommand(t *testing.T) {
	found := false
	for _, sub := range rootCmd.Commands() {
		if sub.Use == generateCmd.Use {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected 'generate' to be registered as a subcommand of rootCmd")
	}
}

// TestRootCmd_CompletionSubcmdDisabled verifies that Cobra's default completion
// subcommand is not present (disabled via CompletionOptions.DisableDefaultCmd).
func TestRootCmd_CompletionSubcmdDisabled(t *testing.T) {
	for _, sub := range rootCmd.Commands() {
		if sub.Name() == "completion" {
			t.Error("expected default 'completion' subcommand to be disabled, but found it")
		}
	}
}

// ---- Execute() stderr output tests ----

// TestExecute_ErrorWrittenToStderr verifies that Execute() prints "Error: ..."
// to stderr (root.go line 39) when the command returns an error. This ensures
// the error message surface is consistent for CLI users.
func TestExecute_ErrorWrittenToStderr(t *testing.T) {
	setRootArgs(t, []string{"validate", "/nonexistent/path/does-not-exist.sextant.yaml"})

	orig := os.Stderr
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("failed to create pipe: %v", err)
	}
	os.Stderr = w

	runErr := Execute()

	w.Close()
	os.Stderr = orig

	if runErr == nil {
		t.Fatal("expected an error from Execute(), got nil")
	}

	var buf bytes.Buffer
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatalf("failed to read stderr pipe: %v", err)
	}
	stderr := buf.String()

	if !strings.Contains(stderr, "Error:") {
		t.Errorf("expected stderr to contain 'Error:', got: %q", stderr)
	}
}

// ---- generate command flag tests ----

// TestGenerateCmd_DefaultOutputDirFlag verifies that the --output flag defaults
// to "./pkg/statemachine" as documented.
func TestGenerateCmd_DefaultOutputDirFlag(t *testing.T) {
	f := generateCmd.Flags().Lookup("output")
	if f == nil {
		t.Fatal("expected --output flag to be defined on generateCmd")
	}
	if f.DefValue != "./pkg/statemachine" {
		t.Errorf("expected --output default to be './pkg/statemachine', got: %q", f.DefValue)
	}
}

// TestGenerateCmd_DefaultPackageFlag verifies that the --package flag defaults
// to an empty string (package is derived from directory name when unset).
func TestGenerateCmd_DefaultPackageFlag(t *testing.T) {
	f := generateCmd.Flags().Lookup("package")
	if f == nil {
		t.Fatal("expected --package flag to be defined on generateCmd")
	}
	if f.DefValue != "" {
		t.Errorf("expected --package default to be empty string, got: %q", f.DefValue)
	}
}

// TestGenerateCmd_AllFlagsDefined verifies that all documented flags (-o/-p/-m/--api)
// are registered on the generate subcommand.
func TestGenerateCmd_AllFlagsDefined(t *testing.T) {
	flags := []struct {
		long  string
		short string
	}{
		{"output", "o"},
		{"package", "p"},
		{"module", "m"},
		{"api", ""},
	}

	for _, fl := range flags {
		f := generateCmd.Flags().Lookup(fl.long)
		if f == nil {
			t.Errorf("expected --%s flag to be defined on generateCmd", fl.long)
			continue
		}
		if fl.short != "" && f.Shorthand != fl.short {
			t.Errorf("expected --%s to have short flag -%s, got: %q", fl.long, fl.short, f.Shorthand)
		}
	}
}

// TestExecute_Generate_ShortModuleFlag verifies that the -m short alias for
// --module is correctly parsed through the full command tree.
func TestExecute_Generate_ShortModuleFlag(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{
		"generate",
		"-o", outDir,
		"-p", "testpkg",
		"-m", "github.com/example/myoperator",
		filePath,
	})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error with -m short flag for --module, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in output directory, found none")
	}
}

// TestExecute_Generate_APIFlagWithoutModule verifies that --api can be specified
// alone (without --module). The flag value is used directly as the API import
// path without auto-derivation.
func TestExecute_Generate_APIFlagWithoutModule(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{
		"generate",
		"--output", outDir,
		"--package", "testpkg",
		"--api", "github.com/example/operator/api/v1alpha1",
		filePath,
	})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error when --api is set without --module, got: %v", err)
	}

	entries, err := os.ReadDir(outDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}
	if len(entries) == 0 {
		t.Error("expected generated files in output directory, found none")
	}
}

// ---- validate command flag tests ----

// TestValidateCmd_AllFlagsDefined verifies that all documented flags (--xstate,
// -o/--output) are registered on the validate subcommand.
func TestValidateCmd_AllFlagsDefined(t *testing.T) {
	if f := validateCmd.Flags().Lookup("xstate"); f == nil {
		t.Error("expected --xstate flag to be defined on validateCmd")
	}
	if f := validateCmd.Flags().Lookup("output"); f == nil {
		t.Error("expected --output flag to be defined on validateCmd")
	}
}

// TestValidateCmd_XStateFlagDefault verifies that --xstate defaults to false.
func TestValidateCmd_XStateFlagDefault(t *testing.T) {
	f := validateCmd.Flags().Lookup("xstate")
	if f == nil {
		t.Fatal("expected --xstate flag to be defined on validateCmd")
	}
	if f.DefValue != "false" {
		t.Errorf("expected --xstate default to be 'false', got: %q", f.DefValue)
	}
}

// TestValidateCmd_OutputFlagDefault verifies that --output defaults to empty
// (meaning stdout is used when --xstate is set).
func TestValidateCmd_OutputFlagDefault(t *testing.T) {
	f := validateCmd.Flags().Lookup("output")
	if f == nil {
		t.Fatal("expected --output flag to be defined on validateCmd")
	}
	if f.DefValue != "" {
		t.Errorf("expected --output default to be empty string, got: %q", f.DefValue)
	}
}

// TestExecute_Validate_OutputWithoutXStateIgnored verifies that --output without
// --xstate is silently ignored through the full Cobra command tree (no file is
// created, no error is returned).
func TestExecute_Validate_OutputWithoutXStateIgnored(t *testing.T) {
	defer resetValidateFlags()

	outFile := filepath.Join(t.TempDir(), "should-not-exist.json")
	filePath := writeYAMLFile(t, validStateMachineYAML)
	setRootArgs(t, []string{"validate", "--output", outFile, filePath})

	err := Execute()
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	if _, statErr := os.Stat(outFile); statErr == nil {
		t.Error("expected output file NOT to be created when --xstate is not set, but it was")
	}
}

// ---- missing initial state via Execute() ----

// TestExecute_Validate_MissingInitialState verifies that Execute() propagates
// the validation error when the YAML has no initial state. This exercises the
// full command-tree path for schema validation errors in the validate subcommand.
func TestExecute_Validate_MissingInitialState(t *testing.T) {
	defer resetValidateFlags()

	filePath := writeYAMLFile(t, missingInitialStateYAML)
	setRootArgs(t, []string{"validate", filePath})

	err := Execute()
	if err == nil {
		t.Fatal("expected validation error for missing initial state via Execute(), got nil")
	}
	if !strings.Contains(err.Error(), "initial") {
		t.Errorf("expected error to mention 'initial', got: %v", err)
	}
}

// TestExecute_Generate_MissingInitialState verifies that Execute() propagates
// the validation error from the generate command when the YAML has no initial
// state. Schema validation runs before code generation begins.
func TestExecute_Generate_MissingInitialState(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	filePath := writeYAMLFile(t, missingInitialStateYAML)
	setRootArgs(t, []string{"generate", "--output", outDir, "--package", "testpkg", filePath})

	err := Execute()
	if err == nil {
		t.Fatal("expected validation error for missing initial state via Execute(), got nil")
	}
	if !strings.Contains(err.Error(), "initial") {
		t.Errorf("expected error to mention 'initial', got: %v", err)
	}
}
