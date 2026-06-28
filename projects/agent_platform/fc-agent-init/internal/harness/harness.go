// Package harness builds the agent-harness command fc-agent-init launches as the
// microVM's workload (ADR 022 Phase 5 / Plan B). The harness is Goose: a
// turn-capped recipe run whose between-turns boundary is the quiescent point the
// idle detector snapshots on. The command is a pure function of the environment
// so it is unit-testable with no Goose binary present.
package harness

import "fmt"

// Config is the harness invocation resolved from the environment.
type Config struct {
	// Recipe is the path to a Goose recipe YAML (FC_GOOSE_RECIPE). Recipes carry
	// their own settings.max_turns / max_tool_repetitions, which is what bounds
	// the run and gives the idle detector a quiescent boundary.
	Recipe string
	// Task is the task description fed to the recipe (FC_TASK).
	Task string
}

// GooseCommand returns the argv to run, mirroring the established invocation:
//
//	with a recipe:  goose run --recipe <recipe> --no-profile --with-builtin developer --params task_description=<task>
//	bare task only: goose run --text <task>
//
// --no-profile keeps the run deterministic (it ignores the baked config.yaml), but
// it also suppresses extension loading, and a recipe's own `extensions:` field does
// not reliably load builtins, so the agent would start with NO tools (it could talk
// to the model but not run a shell). --with-builtin developer explicitly loads the
// shell/editor extension so the agent can actually do work.
//
// It returns nil when there is nothing to run (a warm-base boot with no task),
// in which case fc-agent-init idles without a harness until a task arrives.
func GooseCommand(c Config) []string {
	if c.Recipe != "" {
		return []string{
			"goose", "run",
			"--recipe", c.Recipe,
			"--no-profile",
			"--with-builtin", "developer",
			"--params", fmt.Sprintf("task_description=%s", c.Task),
		}
	}
	if c.Task != "" {
		return []string{"goose", "run", "--text", c.Task}
	}
	return nil
}
