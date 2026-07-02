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
	// Resume replays the named session (--resume) and continues with Task as the
	// new instruction (ADR 026 Phase 2, Model A). The recipe is re-passed so goose
	// re-applies its response schema and settings to the follow-up turn (see
	// GooseCommand). Requires SessionName; ignored without it.
	Resume bool
}

// GooseCommand returns the argv to run, mirroring the established invocation:
//
//	resume:         goose run --recipe <recipe> --name <session> --resume --no-profile --with-builtin developer --params task_description=<task>
//	with a recipe:  goose run --recipe <recipe> [--name <session>] --no-profile --with-builtin developer --params task_description=<task>
//	bare task only: goose run --text <task>
//
// --no-profile keeps the run deterministic (it ignores the baked config.yaml), but
// it also suppresses extension loading, and a recipe's own `extensions:` field does
// not reliably load builtins, so the agent would start with NO tools (it could talk
// to the model but not run a shell). --with-builtin developer explicitly loads the
// shell/editor extension so the agent can actually do work.
//
// On resume the recipe IS re-passed. goose applies a recipe's system prompt and
// response schema (settings.response.json_schema, which forces the structured
// final output the delivery path depends on) at RUNTIME on every invocation, not
// as replayed messages, so re-passing does not duplicate the instructions:
// --resume replays the prior conversation, and the recipe re-applies the schema
// and settings on top of it. Dropping --recipe on resume silently loses the
// schema, so a follow-up turn emits no structured output and delivery falls back
// to scraping the raw transcript. The new instruction goes via --params (goose's
// CLI makes -t/-i conflict with --recipe, so the task cannot be passed with -t
// alongside a recipe). When there is no recipe to re-pass (an edge case), fall
// back to a plain --resume with the reply as -t.
//
// It returns nil when there is nothing to run (a warm-base boot with no task),
// in which case fc-agent-init idles without a harness until a task arrives.
func GooseCommand(c Config) []string {
	if c.Resume && c.SessionName != "" {
		if c.Recipe != "" {
			return []string{
				"goose", "run",
				"--recipe", c.Recipe,
				"--name", c.SessionName,
				"--resume",
				"--no-profile",
				"--with-builtin", "developer",
				"--params", fmt.Sprintf("task_description=%s", c.Task),
			}
		}
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
