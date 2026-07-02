package harness

import (
	"strings"
	"testing"
)

func TestGooseCommandWithRecipe(t *testing.T) {
	got := GooseCommand(Config{Recipe: "/etc/goose/recipes/agent.yaml", Task: "fix the flaky test", TaskFile: "/tmp/goose/task.md"})
	want := "goose run --recipe /etc/goose/recipes/agent.yaml --no-profile --with-builtin developer --params task_file=/tmp/goose/task.md"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q, want %q", strings.Join(got, " "), want)
	}
}

func TestGooseCommandBareTask(t *testing.T) {
	got := GooseCommand(Config{Task: "do the thing"})
	if len(got) != 4 || got[0] != "goose" || got[1] != "run" || got[2] != "--text" || got[3] != "do the thing" {
		t.Fatalf("unexpected bare-task command: %v", got)
	}
}

func TestGooseCommandNoTaskIsNil(t *testing.T) {
	if got := GooseCommand(Config{}); got != nil {
		t.Fatalf("expected nil command for a warm-base boot with no task, got %v", got)
	}
}

func TestGooseCommandColdBuildNamesSession(t *testing.T) {
	// A cold build with a session name still uses the recipe, but adds --name so
	// the session is persisted under that name for a later resume (ADR 026 Phase 2).
	got := GooseCommand(Config{Recipe: "artifact.yaml", Task: "make a ball", TaskFile: "/tmp/goose/task.md", SessionName: "thread-1"})
	want := "goose run --recipe artifact.yaml --name thread-1 --no-profile --with-builtin developer --params task_file=/tmp/goose/task.md"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q, want %q", strings.Join(got, " "), want)
	}
}

func TestGooseCommandResume(t *testing.T) {
	// Resume replays the named session AND re-passes the recipe so goose re-applies
	// the recipe's response schema + settings to the follow-up turn. The task goes
	// via --params task_file (-t conflicts with --recipe in goose's CLI).
	got := GooseCommand(Config{Recipe: "agent.yaml", Task: "how does it work?", TaskFile: "/tmp/goose/task.md", SessionName: "thread-1", Resume: true})
	want := "goose run --recipe agent.yaml --name thread-1 --resume --no-profile --with-builtin developer --params task_file=/tmp/goose/task.md"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q, want %q", strings.Join(got, " "), want)
	}
}

func TestGooseCommandResumeWithoutRecipeUsesText(t *testing.T) {
	// Edge case: a resume with a session but no recipe to re-pass falls back to a
	// plain --resume with the reply as -t (no schema to restore).
	got := GooseCommand(Config{Task: "keep going", SessionName: "thread-1", Resume: true})
	want := "goose run --name thread-1 --resume --no-profile --with-builtin developer -t keep going"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q, want %q", strings.Join(got, " "), want)
	}
}

func TestGooseCommandResumeWithoutSessionFallsBackToRecipe(t *testing.T) {
	// Resume needs a session name; without one it cannot --resume, so it falls back
	// to the recipe path (the cold build).
	got := GooseCommand(Config{Recipe: "artifact.yaml", Task: "x", Resume: true})
	if len(got) < 3 || got[2] != "--recipe" {
		t.Fatalf("resume without session should use the recipe, got %v", got)
	}
}

func TestTaskFileParamStaysSingleArg(t *testing.T) {
	// The task_file param is one argv element even if the path contains a space;
	// no shell splitting.
	got := GooseCommand(Config{Recipe: "r.yaml", Task: "a b c", TaskFile: "/tmp/goose dir/task.md"})
	last := got[len(got)-1]
	if last != "task_file=/tmp/goose dir/task.md" {
		t.Fatalf("task_file param should be a single arg, got %q", last)
	}
}

func TestBareTaskPassesRawTextNotFile(t *testing.T) {
	// The recipe-less path passes the raw task via --text, where goose uses it
	// verbatim as the prompt: multi-line content is safe here (no YAML templating).
	got := GooseCommand(Config{Task: "line one\nline two"})
	if len(got) != 4 || got[2] != "--text" || got[3] != "line one\nline two" {
		t.Fatalf("bare task should pass raw multi-line text via --text, got %v", got)
	}
}
