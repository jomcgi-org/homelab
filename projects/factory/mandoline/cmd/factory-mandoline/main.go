package main

import (
	"context"
	"encoding/json"
	"flag"
	"os"

	"github.com/jomcgi/homelab/projects/factory/mandoline"
)

// The Claude image sets this at link time. The installed Codex command always
// requires a diff base, for both the implementer and the reviewer.
var auditOnly = "false"

func main() {
	var req mandoline.Request
	flags := flag.NewFlagSet("factory-mandoline", flag.ExitOnError)
	flags.StringVar(&req.Commit, "commit", "", "full commit SHA of the checkout")
	if auditOnly != "true" {
		flags.StringVar(&req.Base, "base", "", "full commit SHA for the diff base")
	}
	flags.Parse(os.Args[1:])
	if flags.NArg() != 1 {
		flags.Usage()
		os.Exit(2)
	}
	req.Command = flags.Arg(0)
	cwd, _ := os.Getwd()
	result := mandoline.Run(context.Background(), "/data/workspace", cwd, "/usr/local/bin/mandoline", auditOnly == "true", req)
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		os.Exit(1)
	}
}
