// Package main provides the sextant CLI tool for generating type-safe
// state machines for Kubernetes operators.
package main

import (
	"os"

	"github.com/jomcgi/homelab/projects/sextant/cmd/sextant/cmd"
)

func main() {
	if err := cmd.Execute(); err != nil {
		os.Exit(1)
	}
}
