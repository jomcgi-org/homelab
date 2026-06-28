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

func TestTaskWithSpacesStaysSingleArg(t *testing.T) {
	// The task is one argv element even with spaces; no shell splitting.
	got := GooseCommand(Config{Recipe: "r.yaml", Task: "a b c"})
	last := got[len(got)-1]
	if last != "task_description=a b c" {
		t.Fatalf("task param should be a single arg, got %q", last)
	}
}
