package harness

import (
	"strings"
	"testing"
)

func TestGooseCommandWithRecipe(t *testing.T) {
	got := GooseCommand(Config{Recipe: "/etc/goose/recipes/agent.yaml", Task: "fix the flaky test"})
	want := "goose run --recipe /etc/goose/recipes/agent.yaml --no-profile --with-builtin developer --params task_description=fix the flaky test"
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
	got := GooseCommand(Config{Recipe: "artifact.yaml", Task: "make a ball", SessionName: "thread-1"})
	want := "goose run --recipe artifact.yaml --name thread-1 --no-profile --with-builtin developer --params task_description=make a ball"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q, want %q", strings.Join(got, " "), want)
	}
}

func TestGooseCommandResume(t *testing.T) {
	// Resume replays the named session (--resume, no --recipe) with the reply as -t.
	got := GooseCommand(Config{Recipe: "artifact.yaml", Task: "make it bigger", SessionName: "thread-1", Resume: true})
	want := "goose run --name thread-1 --resume --no-profile --with-builtin developer -t make it bigger"
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

func TestTaskWithSpacesStaysSingleArg(t *testing.T) {
	// The task is one argv element even with spaces; no shell splitting.
	got := GooseCommand(Config{Recipe: "r.yaml", Task: "a b c"})
	last := got[len(got)-1]
	if last != "task_description=a b c" {
		t.Fatalf("task param should be a single arg, got %q", last)
	}
}
