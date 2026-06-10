package cmd

// codegen_new_test.go documents and tests the codegen.New() code path in
// runGenerate (generate.go lines 85-88).
//
// The "failed to create generator:" error (wrapped around codegen.New() errors)
// is not reachable at runtime: codegen.New() only fails if the embedded
// template filesystem cannot be parsed, but the templates are compiled into the
// binary via //go:embed — they always exist and are always valid.
//
// What CAN be tested:
//   - codegen.New() succeeds for every valid Config shape (no panic, no error)
//   - The happy path through lines 85-88 is covered in every TestRunGenerate_*
//     test that calls runGenerate with a valid YAML file.
//
// This file adds an explicit test that exercises line 85 (the codegen.New()
// call) and verifies the generator is created successfully, ensuring the
// success branch is always covered and any future regressions in the
// constructor are immediately caught.

import (
	"testing"

	"github.com/spf13/cobra"
)

// TestRunGenerate_CodegenNewSucceeds verifies that the codegen.New() call on
// line 85 of generate.go always succeeds with a well-formed Config. This
// exercises lines 85-88 of generate.go (the codegen.New() call and the
// immediate error-check branch) along the success path.
//
// Note: the error path on line 87 ("failed to create generator:") cannot be
// triggered from outside the codegen package because templates are embedded at
// compile time via //go:embed and ParseFS never fails for embedded filesystems.
func TestRunGenerate_CodegenNewSucceeds(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "newpkg"
	generateModule = "github.com/example/op"
	generateAPIImportPath = "github.com/example/op/api/v1alpha1"

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected codegen.New() to succeed and code generation to complete, got: %v", err)
	}
}

// TestRunGenerate_CodegenNewSucceeds_NoModuleNoAPI verifies that codegen.New()
// also succeeds when neither --module nor --api is provided (zero-value Config
// fields for those options).
func TestRunGenerate_CodegenNewSucceeds_NoModuleNoAPI(t *testing.T) {
	defer resetGenerateFlags()

	outDir := t.TempDir()
	generateOutputDir = outDir
	generatePackage = "minpkg"
	// generateModule and generateAPIImportPath intentionally left as defaults (empty)

	filePath := writeYAMLFile(t, validStateMachineYAML)
	err := runGenerate(&cobra.Command{}, []string{filePath})
	if err != nil {
		t.Fatalf("expected codegen.New() to succeed with empty module/api, got: %v", err)
	}
}
