package cmd

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	"github.com/jomcgi/homelab/projects/sextant/pkg/codegen"
	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

var (
	generateOutputDir     string
	generatePackage       string
	generateModule        string
	generateAPIImportPath string
)

var generateCmd = &cobra.Command{
	Use:   "generate <file.sextant.yaml>",
	Short: "Generate Go code from a state machine definition",
	Long: `Generate type-safe Go code from a state machine YAML definition.

This command generates:
  - Phase constants for compile-time safe phase references
  - State types with sealed interface for exhaustive handling
  - State calculator for level-triggered state determination
  - Type-safe transition methods with idempotency key parameters
  - Visitor pattern for exhaustive state handling
  - Observability hooks with optional OpenTelemetry integration
  - Server-Side Apply helpers for status updates

Examples:
  # Generate code to ./pkg/statemachine
  sextant generate myresource.sextant.yaml -o ./pkg/statemachine

  # Generate with custom package name
  sextant generate myresource.sextant.yaml -o ./internal/sm --package statemachine

  # Generate with API import path
  sextant generate myresource.sextant.yaml -o ./pkg/sm --api github.com/myorg/operator/api/v1`,
	Args: cobra.ExactArgs(1),
	RunE: runGenerate,
}

func init() {
	rootCmd.AddCommand(generateCmd)

	generateCmd.Flags().StringVarP(&generateOutputDir, "output", "o", "./pkg/statemachine", "Output directory for generated code")
	generateCmd.Flags().StringVarP(&generatePackage, "package", "p", "", "Go package name (defaults to directory name)")
	generateCmd.Flags().StringVarP(&generateModule, "module", "m", "", "Go module path (e.g., github.com/joe/operator)")
	generateCmd.Flags().StringVar(&generateAPIImportPath, "api", "", "Import path for API types (e.g., github.com/joe/operator/api/v1)")
}

func runGenerate(cmd *cobra.Command, args []string) error {
	filePath := args[0]

	// Parse and validate
	sm, err := schema.ValidateAndParse(filePath)
	if err != nil {
		return err
	}

	// Determine package name
	packageName := generatePackage
	if packageName == "" {
		packageName = filepath.Base(generateOutputDir)
	}

	// Build API import path if not specified
	apiImportPath := generateAPIImportPath
	if apiImportPath == "" && generateModule != "" {
		apiImportPath = generateModule + "/api/" + sm.Metadata.Version
	}

	config := codegen.Config{
		OutputDir:     generateOutputDir,
		Package:       packageName,
		Module:        generateModule,
		APIImportPath: apiImportPath,
	}

	gen, err := codegen.New(config)
	if err != nil {
		return fmt.Errorf("failed to create generator: %w", err)
	}

	if err := gen.Generate(sm); err != nil {
		return fmt.Errorf("code generation failed: %w", err)
	}

	fmt.Fprintf(os.Stderr, "✓ Generated code for %s\n", sm.Metadata.Name)
	fmt.Fprintf(os.Stderr, "  Output: %s\n", generateOutputDir)
	fmt.Fprintf(os.Stderr, "  Package: %s\n", packageName)
	fmt.Fprintf(os.Stderr, "  States: %d\n", len(sm.States)+1) // +1 for auto-generated Unknown
	fmt.Fprintf(os.Stderr, "  Transitions: %d\n", len(sm.Transitions))

	return nil
}
