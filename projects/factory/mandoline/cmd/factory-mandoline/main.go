package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"

	"github.com/jomcgi/homelab/projects/factory/mandoline"
)

// The Claude image sets this at link time. The installed Codex command always
// requires a diff base, for both the implementer and the reviewer.
var auditOnly = "false"

func main() {
	var req mandoline.Request
	flags := flag.NewFlagSet("factory-mandoline", flag.ContinueOnError)
	flags.StringVar(&req.Commit, "commit", "", "full commit SHA of the checkout")
	command := "audit|surface"
	if auditOnly != "true" {
		flags.StringVar(&req.Base, "base", "", "full SHA of the merge base with trusted main")
		command = "diff|callers"
	}
	flags.Usage = func() {
		fmt.Fprintf(flags.Output(), "Usage: factory-mandoline [options] <%s>\n", command)
		flags.PrintDefaults()
	}
	parseErr := flags.Parse(os.Args[1:])
	if errors.Is(parseErr, flag.ErrHelp) {
		return
	}
	var result mandoline.Envelope
	exit := 0
	if parseErr != nil || flags.NArg() != 1 {
		result = mandoline.Envelope{
			Commit: req.Commit, Status: "error", PackDigests: map[string]string{},
			Gaps: []string{"expected one analysis command with this harness's options"},
		}
		exit = 2
	} else {
		req.Command = flags.Arg(0)
		result = mandoline.Run(context.Background(), "/data/workspace", "/usr/local/bin/mandoline", auditOnly == "true", req)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		os.Exit(1)
	}
	if exit != 0 {
		os.Exit(exit)
	}
}
