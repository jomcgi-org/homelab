package dispatch

import (
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

func TestCoderDelegateIsTaskAgent(t *testing.T) {
	for name, tc := range map[string]struct {
		in   []itsaplan.Assignee
		want string
	}{
		"task-agent": {[]itsaplan.Assignee{
			{UserID: "old", Username: "homelab-codex", Kind: "agent"},
			{UserID: "new", Username: "task-agent", Kind: "agent"},
		}, "new"},
		"the retired agent never counts": {[]itsaplan.Assignee{
			{UserID: "old", Username: "homelab-codex", Kind: "agent"},
		}, ""},
		"a human with the same name never counts": {[]itsaplan.Assignee{
			{UserID: "human", Username: "task-agent", Kind: "member"},
		}, ""},
		"none": {nil, ""},
	} {
		if got := coderDelegate(tc.in); got != tc.want {
			t.Errorf("%s: got %q, want %q", name, got, tc.want)
		}
	}
}
