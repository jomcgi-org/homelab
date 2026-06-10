// Package cmd provides the CLI commands for the sextant tool.
package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var rootCmd = &cobra.Command{
	Use:   "sextant",
	Short: "Generate type-safe state machines for Kubernetes operators",
	Long: `sextant is a code generation tool that produces type-safe state machines
for Kubernetes operators from a YAML DSL.

Named after the nautical navigation instrument, sextant helps you chart a
reliable course through the complexity of Kubernetes operator state management.

Key features:
  - Compiler-enforced state transitions
  - Idempotency by construction
  - Level-triggered state calculation
  - Sealed interfaces for exhaustive handling
  - OpenTelemetry integration

Example workflow:
  1. Define your state machine in YAML
  2. Validate the definition: sextant validate myresource.sextant.yaml
  3. Generate Go code: sextant generate myresource.sextant.yaml -o ./pkg/statemachine
  4. Use the generated code in your controller`,
	SilenceUsage:  true,
	SilenceErrors: true,
}

// Execute runs the root command.
func Execute() error {
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		return err
	}
	return nil
}

func init() {
	rootCmd.CompletionOptions.DisableDefaultCmd = true
}
