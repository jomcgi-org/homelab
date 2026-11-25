package dispatch

import (
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

func TestCoderDelegatePrefersTaskAgent(t *testing.T) {
	for name, tc := range map[string]struct {
		in   []itsaplan.Assignee
		want string
	}{
		"task-agent wins in either order": {[]itsaplan.Assignee{
			{UserID: "old", Username: "homelab-codex", Kind: "agent"},
			{UserID: "new", Username: "task-agent", Kind: "agent"},
		}, "new"},
		"homelab-codex until task-agent exists": {[]itsaplan.Assignee{
			{UserID: "old", Username: "homelab-codex", Kind: "agent"},
		}, "old"},
		"a human with the same name never counts": {[]itsaplan.Assignee{
			{UserID: "human", Username: "task-agent", Kind: "member"},
			{UserID: "old", Username: "homelab-codex", Kind: "agent"},
		}, "old"},
		"none": {nil, ""},
	} {
		if got := coderDelegate(tc.in); got != tc.want {
			t.Errorf("%s: got %q, want %q", name, got, tc.want)
		}
	}
	rev := []itsaplan.Assignee{
		{UserID: "new", Username: "task-agent", Kind: "agent"},
		{UserID: "old", Username: "homelab-codex", Kind: "agent"},
	}
	if got := coderDelegate(rev); got != "new" {
		t.Errorf("reversed order: got %q, want new", got)
	}
}
