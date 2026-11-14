// Command configcheck validates rendered reviewer config with the service's
// strict parser. The chart render test supplies YAML on stdin.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/jomcgi/homelab/projects/factory/reviewer/config"
)

func main() {
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	cfg, err := config.Parse(data)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := json.NewEncoder(os.Stdout).Encode(cfg); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
