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
	// SessionName names goose's SQLite session (--name). On a cold build it is set
	// so the session is persisted under a stable name; on a resume it selects which
	// session to replay (ADR 026 Phase 2). Empty means goose auto-manages.
	SessionName string
	// Resume runs the named session with --resume instead of the recipe: goose
	// replays the full prior conversation (which already contains the recipe's
	// system prompt from turn 1) and continues with Task as the new instruction
	// (ADR 026 Phase 2, Model A). Requires SessionName; ignored without it.
	Resume bool
}

// GooseCommand returns the argv to run, mirroring the established invocation:
//
//	resume:         goose run --name <session> --resume --no-profile --with-builtin developer -t <task>
//	with a recipe:  goose run --recipe <recipe> [--name <session>] --no-profile --with-builtin developer --params task_description=<task>
//	bare task only: goose run --text <task>
//
// --no-profile keeps the run deterministic (it ignores the baked config.yaml), but
// it also suppresses extension loading, and a recipe's own `extensions:` field does
// not reliably load builtins, so the agent would start with NO tools (it could talk
// to the model but not run a shell). --with-builtin developer explicitly loads the
// shell/editor extension so the agent can actually do work.
//
// On resume the recipe is NOT re-passed: turn 1 wrote the recipe's system prompt
// into the session, and --resume replays the whole conversation, so re-passing it
// would duplicate the instructions. The task is the latest instruction, given via
// -t (the recipe's task_description param does not apply without --recipe).
//
// It returns nil when there is nothing to run (a warm-base boot with no task),
// in which case fc-agent-init idles without a harness until a task arrives.
func GooseCommand(c Config) []string {
	if c.Resume && c.SessionName != "" {
		return []string{
			"goose", "run",
			"--name", c.SessionName,
			"--resume",
			"--no-profile",
			"--with-builtin", "developer",
			"-t", c.Task,
		}
	}
	if c.Recipe != "" {
		argv := []string{"goose", "run", "--recipe", c.Recipe}
		if c.SessionName != "" {
			// Name the session on the cold build so a later reply can --resume it.
			argv = append(argv, "--name", c.SessionName)
		}
		return append(argv,
			"--no-profile",
			"--with-builtin", "developer",
			"--params", fmt.Sprintf("task_description=%s", c.Task),
		)
	}
	if c.Task != "" {
		return []string{"goose", "run", "--text", c.Task}
	}
	return nil
}
