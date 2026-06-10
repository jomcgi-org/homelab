package cmd

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
	"github.com/jomcgi/homelab/projects/sextant/pkg/xstate"
)

var (
	validateOutputXState bool
	validateOutputPath   string
)

var validateCmd = &cobra.Command{
	Use:   "validate <file.sextant.yaml>",
	Short: "Validate a state machine YAML definition",
	Long: `Validate a state machine YAML definition and optionally output XState JSON.

This command parses the YAML file, validates all schema constraints, and
checks for reserved words, structural integrity, and consistency.

Examples:
  # Validate a state machine definition
  sextant validate myresource.sextant.yaml

  # Validate and output XState JSON
  sextant validate myresource.sextant.yaml --xstate

  # Validate and save XState JSON to a file
  sextant validate myresource.sextant.yaml --xstate -o myresource.xstate.json`,
	Args: cobra.ExactArgs(1),
	RunE: runValidate,
}

func init() {
	rootCmd.AddCommand(validateCmd)

	validateCmd.Flags().BoolVar(&validateOutputXState, "xstate", false, "Output XState JSON representation")
	validateCmd.Flags().StringVarP(&validateOutputPath, "output", "o", "", "Output file path (defaults to stdout)")
}

func runValidate(cmd *cobra.Command, args []string) error {
	filePath := args[0]

	// Parse and validate
	sm, err := schema.ValidateAndParse(filePath)
	if err != nil {
		return err
	}

	fmt.Fprintf(os.Stderr, "✓ Valid state machine: %s\n", sm.Metadata.Name)
	fmt.Fprintf(os.Stderr, "  States: %d\n", len(sm.States))
	fmt.Fprintf(os.Stderr, "  Transitions: %d\n", len(sm.Transitions))
	fmt.Fprintf(os.Stderr, "  Guards: %d\n", len(sm.Guards))

	// Output XState JSON if requested
	if validateOutputXState {
		xstateMachine := xstate.Convert(sm)
		jsonBytes, err := json.MarshalIndent(xstateMachine, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to marshal XState JSON: %w", err)
		}

		if validateOutputPath != "" {
			if err := os.WriteFile(validateOutputPath, jsonBytes, 0o644); err != nil {
				return fmt.Errorf("failed to write output file: %w", err)
			}
			fmt.Fprintf(os.Stderr, "  XState JSON written to: %s\n", validateOutputPath)
		} else {
			fmt.Println(string(jsonBytes))
		}
	}

	return nil
}
